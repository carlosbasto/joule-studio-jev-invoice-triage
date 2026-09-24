"""Execution-time JEV guard for the payment-block release tool.

The running agent installs this wrapper around ``release_payment_block`` before
the tool set is handed to the LLM. The wrapper independently re-reads invoice
and vendor-history evidence, asks JEV for a typed decision, and invokes the
original release coroutine only after an explicit ``allow``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

import jev

logger = logging.getLogger(__name__)

RELEASE_TOOL_NAME = "release_payment_block"


def _decision(data: dict[str, Any] | None) -> str | None:
    value = (data or {}).get("decision")
    return value if isinstance(value, str) and value else None


# Evidence tools the guard consults to build a risk picture before asking JEV.
_INVOICE_DETAIL_TOOLS = ("get_invoice_detail",)
_VENDOR_INVOICE_TOOLS = ("get_recent_invoices_for_vendor",)

_RELEASE_POLICY = (
    "Release the payment block only when the evidence supports it. Price/quantity "
    "block: release only when the invoiced quantity and price are within PO "
    "tolerance. Goods-receipt block: do not release unless a goods receipt is "
    "posted for the invoiced quantity. Treat an invoice whose reference or amount "
    "matches a recent invoice from the same vendor as a likely duplicate — deny or "
    "hold for review. Never release a suspected duplicate or an over-tolerance "
    "invoice automatically."
)


def _as_dict(result: Any) -> dict[str, Any]:
    """Best-effort parse of a tool result (JSON string or dict) into a dict."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def _call_sibling(
    siblings: dict[str, BaseTool], names: tuple[str, ...], **kwargs: Any
) -> dict[str, Any]:
    """Invoke the first available sibling tool by name; return its parsed dict.

    Never raises — a failed lookup just yields {} so the guard degrades to a
    thinner risk picture rather than breaking the release flow.
    """
    for name in names:
        tool = siblings.get(name)
        coro = getattr(tool, "coroutine", None)
        if coro is not None:
            try:
                return _as_dict(await coro(**kwargs))
            except Exception as exc:  # noqa: BLE001 — evidence gathering must never break the flow
                # Try the next candidate name rather than giving up on this fact.
                logger.warning("JEV guard: sibling %s failed: %s", name, exc)
                continue
    return {}


async def _gather_risk_context(
    siblings: dict[str, BaseTool], invoice_number: str, fiscal_year: str
) -> tuple[list[str], str]:
    """Re-read the invoice's evidence to build (safeguards, reversibility) for JEV.

    The guard independently pulls the same invoice detail (PO/GR match, block
    reason, vendor) and the vendor's recent invoices that the model saw, so JEV
    judges the release against the actual risk picture instead of an empty
    context. Best-effort: any gap yields a shorter list — it never raises.
    """
    safeguards: list[str] = []
    reversibility = "reversible"

    detail = await _call_sibling(
        siblings,
        _INVOICE_DETAIL_TOOLS,
        invoice_number=invoice_number,
        fiscal_year=fiscal_year,
    )
    vendor_id = detail.get("vendor_id")

    # Goods-receipt / PO matching facts.
    gr_status = detail.get("goods_receipt_status") or detail.get("gr_status")
    if gr_status:
        note = f"Goods receipt status: {gr_status}"
        if str(gr_status).lower() in ("none", "not_posted", "missing", "not received"):
            note += " — no goods receipt on file, so a 3-way match cannot be confirmed."
            reversibility = "hard_to_recover_after_payment"
        note += "."
        safeguards.append(note)

    match_status = detail.get("match_status") or detail.get("po_match_status")
    if match_status:
        variance = detail.get("price_variance_pct")
        note = f"PO match status: {match_status}"
        if variance is not None:
            note += f", price variance {variance}%"
        if str(match_status).lower() in ("over_tolerance", "mismatch", "failed"):
            note += " — outside PO tolerance; releasing pays an unverified amount."
            reversibility = "hard_to_recover_after_payment"
        note += "."
        safeguards.append(note)

    amount = detail.get("gross_amount") or detail.get("amount")

    # Duplicate detection over the vendor's recent invoices.
    if vendor_id:
        recent = await _call_sibling(
            siblings, _VENDOR_INVOICE_TOOLS, vendor_id=vendor_id, lookback_days=90
        )
        invoices = recent.get("invoices")
        if isinstance(invoices, list) and invoices:
            same_amount = [
                inv
                for inv in invoices
                if isinstance(inv, dict)
                and str(inv.get("invoice_number")) != str(invoice_number)
                and amount is not None
                and str(inv.get("gross_amount") or inv.get("amount")) == str(amount)
            ]
            if same_amount:
                dupes = ", ".join(
                    str(inv.get("invoice_number")) for inv in same_amount[:3]
                )
                safeguards.append(
                    f"Duplicate risk: {len(same_amount)} recent invoice(s) from the "
                    f"same vendor with the same amount ({dupes}) — likely duplicate."
                )
                reversibility = "hard_to_recover_after_payment"
            else:
                safeguards.append(
                    f"No duplicate: {len(invoices)} recent invoice(s) from the vendor, "
                    "none matching this amount."
                )

    return safeguards, reversibility


def guard_release_tool(
    tool: BaseTool, siblings: dict[str, BaseTool] | None = None
) -> BaseTool:
    """Return `tool` wrapped with a JEV tool-guard gate, or unchanged if it is
    not the release tool.

    Only an explicit JEV `allow` runs the original tool. A `deny` returns a
    refusal; review/confirm, disabled JEV, failed calls, and missing or invalid
    decisions return release_pending_review without clearing the payment block.
    This status does not itself create a clerk task or a human approval workflow.

    `siblings` is the full tool set (keyed by name) so the guard can re-read the
    invoice's evidence and hand JEV a real risk picture; if omitted, the guard
    still runs but with an empty risk context.
    """
    if tool.name != RELEASE_TOOL_NAME:
        return tool

    siblings = siblings or {}
    original_coroutine = tool.coroutine

    async def _guarded(**kwargs: Any) -> str:
        invoice_number = kwargs.get("invoice_number", "?")
        fiscal_year = kwargs.get("fiscal_year", "")
        reason = kwargs.get("reason", "")
        if not jev.is_enabled() or original_coroutine is None:
            logger.warning(
                "JEV.tool_guard: HOLD (guard or release tool unavailable) "
                "release_payment_block invoice=%s", invoice_number
            )
            return json.dumps(
                {
                    "status": "release_pending_review",
                    "invoice_number": invoice_number,
                    "reason": "Release held for human review: the JEV guard or release tool is unavailable.",
                }
            )

        # Feed JEV the actual risk picture instead of an empty context — this
        # is what lets the guard deny a duplicate/over-tolerance release
        # rather than rubber-stamping every reversible-looking call.
        try:
            safeguards, reversibility = await _gather_risk_context(
                siblings, str(invoice_number), str(fiscal_year)
            )
        except Exception:  # noqa: BLE001 — never break the flow on evidence gathering
            logger.exception("JEV guard: risk-context gathering errored (non-fatal)")
            safeguards, reversibility = [], "reversible"

        logger.info(
            "JEV.tool_guard: evaluating release_payment_block invoice=%s with "
            "%d risk signal(s), reversibility=%s",
            invoice_number,
            len(safeguards),
            reversibility,
        )

        try:
            guard = await jev.tool_guard(
                tool=RELEASE_TOOL_NAME,
                action=f"Release the payment block on invoice {invoice_number} (reason: {reason}).",
                arguments_summary=(
                    f"invoice_number={invoice_number}, fiscal_year={fiscal_year}, reason={reason}"
                ),
                side_effects=[
                    f"Clears invoice {invoice_number} for payment to the vendor; "
                    "hard to recover if the invoice is later found to be a duplicate "
                    "or over-tolerance."
                ],
                safeguards=safeguards or None,
                policy=_RELEASE_POLICY,
                reversibility=reversibility,
            )
        except Exception:  # noqa: BLE001 — a failed guard must hold the release
            logger.exception("JEV.tool_guard: decision request failed; holding release")
            guard = None
        if not isinstance(guard, dict):
            guard = None
        decision = _decision(guard)
        guidance = (guard or {}).get("guidance")
        if decision == "deny":
            logger.info(
                "JEV.tool_guard: DENY release_payment_block invoice=%s", invoice_number
            )
            return json.dumps(
                {
                    "status": "release_denied",
                    "invoice_number": invoice_number,
                    "reason": guidance or "Release denied by the decision layer.",
                }
            )
        if decision in ("review", "confirm"):
            logger.info(
                "JEV.tool_guard: HOLD release_payment_block invoice=%s", invoice_number
            )
            return json.dumps(
                {
                    "status": "release_pending_review",
                    "invoice_number": invoice_number,
                    "reason": guidance
                    or "Release held for human review by the decision layer.",
                }
            )
        # A failed call, absent answer, or unexpected verdict must never
        # authorize release. Only an explicit `allow` reaches the tool.
        if decision != "allow":
            logger.info(
                "JEV.tool_guard: HOLD (missing or invalid verdict %r) release_payment_block invoice=%s",
                decision,
                invoice_number,
            )
            return json.dumps(
                {
                    "status": "release_pending_review",
                    "invoice_number": invoice_number,
                    "reason": guidance
                    or "Release held for human review (decision layer returned no clear approval).",
                }
            )
        logger.info(
            "JEV.tool_guard: %s release_payment_block invoice=%s",
            decision,
            invoice_number,
        )
        return await original_coroutine(**kwargs)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=_guarded,
        handle_tool_error=True,
    )


def install_release_guard(tools: list[BaseTool]) -> list[BaseTool]:
    """Wrap the release tool in a JEV guard; pass every other tool through.

    The full tool set is handed to the guard (keyed by name) so it can re-read
    the invoice's evidence and give JEV a real risk picture at gate time.
    """
    by_name = {t.name: t for t in tools}
    return [guard_release_tool(t, by_name) for t in tools]
