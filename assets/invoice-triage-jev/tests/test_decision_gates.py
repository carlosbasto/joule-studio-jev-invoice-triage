"""Offline regression tests for the live JEV release wrapper.

No network and no runtime LLM are required. JEV calls and HTTP transport are
patched so the tests exercise the fail-safe control flow deterministically.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel


@pytest.fixture
def dg(add_agent_to_path):
    import decision_gates
    return decision_gates


def _patch_guard(dg, monkeypatch, *, enabled=True, guard=None):
    monkeypatch.setattr(dg.jev, "is_enabled", lambda: enabled)

    async def _tool_guard(**kwargs):
        return guard

    monkeypatch.setattr(dg.jev, "tool_guard", _tool_guard)


class _ReleaseArgs(BaseModel):
    invoice_number: str
    fiscal_year: str
    reason: str


def _release_tool(recorder):
    async def _run(**kwargs):
        recorder.append(kwargs)
        return json.dumps({"status": "block_released", "release_id": "REL-1", **kwargs})

    return StructuredTool(
        name="release_payment_block",
        description="Release a payment block (clears the invoice for payment).",
        args_schema=_ReleaseArgs,
        coroutine=_run,
    )


class TestReleaseGuard:
    async def test_only_release_tool_is_wrapped(self, dg, monkeypatch):
        _patch_guard(dg, monkeypatch, guard={"decision": "allow"})
        other = StructuredTool(
            name="get_invoice_detail",
            description="x",
            args_schema=_ReleaseArgs,
            coroutine=_release_tool([]).coroutine,
        )
        wrapped = dg.install_release_guard([other])
        assert wrapped[0] is other  # untouched

    async def test_allow_runs_original(self, dg, monkeypatch):
        _patch_guard(dg, monkeypatch, guard={"decision": "allow"})
        calls: list = []
        wrapped = dg.install_release_guard([_release_tool(calls)])
        out = await wrapped[0].coroutine(
            invoice_number="INV-5500011", fiscal_year="2026", reason="within tolerance"
        )
        assert json.loads(out)["release_id"] == "REL-1"
        assert calls  # original executed

    async def test_deny_short_circuits(self, dg, monkeypatch):
        _patch_guard(dg, monkeypatch, guard={"decision": "deny", "guidance": "duplicate"})
        calls: list = []
        wrapped = dg.install_release_guard([_release_tool(calls)])
        out = await wrapped[0].coroutine(
            invoice_number="INV-5500011", fiscal_year="2026", reason="x"
        )
        assert json.loads(out)["status"] == "release_denied"
        assert not calls  # original never ran — the block was not released

    @pytest.mark.parametrize("verdict", ["review", "confirm"])
    async def test_review_holds(self, dg, monkeypatch, verdict):
        _patch_guard(dg, monkeypatch, guard={"decision": verdict})
        calls: list = []
        wrapped = dg.install_release_guard([_release_tool(calls)])
        out = await wrapped[0].coroutine(
            invoice_number="INV-5500011", fiscal_year="2026", reason="x"
        )
        assert json.loads(out)["status"] == "release_pending_review"
        assert not calls

    async def test_disabled_holds_without_requesting_a_decision(self, dg, monkeypatch):
        _patch_guard(dg, monkeypatch, enabled=False)
        decision_request = AsyncMock()
        monkeypatch.setattr(dg.jev, "tool_guard", decision_request)
        calls: list = []
        wrapped = dg.install_release_guard([_release_tool(calls)])
        out = await wrapped[0].coroutine(
            invoice_number="INV-5500011", fiscal_year="2026", reason="x"
        )
        assert json.loads(out)["status"] == "release_pending_review"
        assert not calls
        decision_request.assert_not_awaited()

    @pytest.mark.parametrize(
        "guard",
        [None, {}, {"confidence": 0.99}, {"decision": None},
         {"decision": ""}, {"decision": "unexpected"},
         {"decision": 1}, {"decision": ["allow"]}, "allow", ["allow"]],
    )
    async def test_missing_or_invalid_decision_holds(self, dg, monkeypatch, guard):
        _patch_guard(dg, monkeypatch, guard=guard)
        calls: list = []
        tool = dg.guard_release_tool(_release_tool(calls))
        out = json.loads(await tool.coroutine(
            invoice_number="INV-5500011", fiscal_year="2026", reason="x"
        ))
        assert out["status"] == "release_pending_review"
        assert out["invoice_number"] == "INV-5500011"
        assert "release_id" not in out
        assert not calls

    @pytest.mark.parametrize("error", [TimeoutError("timeout"), RuntimeError("request failed")])
    async def test_unexpected_guard_error_holds(self, dg, monkeypatch, error):
        _patch_guard(dg, monkeypatch)
        monkeypatch.setattr(dg.jev, "tool_guard", AsyncMock(side_effect=error))
        calls: list = []
        tool = dg.guard_release_tool(_release_tool(calls))
        out = await tool.coroutine(
            invoice_number="INV-5500011", fiscal_year="2026", reason="x"
        )
        assert json.loads(out)["status"] == "release_pending_review"
        assert not calls

    @pytest.mark.parametrize("failure", ["timeout", "http_error", "invalid_json", "missing_choice"])
    async def test_client_failure_reaches_hold(self, dg, monkeypatch, failure):
        # Exercise the actual JEV client and response parser with mocked HTTP I/O.
        monkeypatch.setattr(dg.jev, "is_enabled", lambda: True)
        monkeypatch.setattr(dg.jev, "_api_key", lambda: "offline-test-key")
        response = MagicMock()
        response.json.return_value = {"answers": {"decision": {"confidence": 0.99}}}
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        if failure == "timeout":
            client.post.side_effect = TimeoutError("timeout")
        elif failure == "http_error":
            response.raise_for_status.side_effect = RuntimeError("HTTP error")
        elif failure == "invalid_json":
            response.json.side_effect = ValueError("invalid JSON")
        monkeypatch.setattr(dg.jev.httpx, "AsyncClient", lambda **kwargs: client)
        calls: list = []
        tool = dg.guard_release_tool(_release_tool(calls))
        out = await tool.coroutine(
            invoice_number="INV-5500011", fiscal_year="2026", reason="x"
        )
        assert json.loads(out)["status"] == "release_pending_review"
        assert not calls
        client.post.assert_awaited_once()


class _DetailArgs(BaseModel):
    invoice_number: str
    fiscal_year: str


class _HistoryArgs(BaseModel):
    vendor_id: str
    lookback_days: int | None = None


async def test_guard_rebuilds_duplicate_risk_context(dg, monkeypatch):
    monkeypatch.setattr(dg.jev, "is_enabled", lambda: True)
    captured = {}

    async def _capture_guard(**kwargs):
        captured.update(kwargs)
        return {"decision": "deny"}

    monkeypatch.setattr(dg.jev, "tool_guard", _capture_guard)

    async def _detail(**kwargs):
        return json.dumps({
            "invoice_number": "INV-5500011",
            "vendor_id": "VEND-30012",
            "gross_amount": 8420.0,
            "goods_receipt_status": "posted",
            "match_status": "within_tolerance",
            "price_variance_pct": 3.0,
        })

    async def _history(**kwargs):
        return json.dumps({
            "invoices": [
                {"invoice_number": "INV-5500011", "gross_amount": 8420.0},
                {"invoice_number": "INV-5500007", "gross_amount": 8420.0},
            ]
        })

    detail_tool = StructuredTool(
        name="get_invoice_detail", description="detail", args_schema=_DetailArgs, coroutine=_detail
    )
    history_tool = StructuredTool(
        name="get_recent_invoices_for_vendor", description="history", args_schema=_HistoryArgs, coroutine=_history
    )
    calls = []
    tools = dg.install_release_guard([detail_tool, history_tool, _release_tool(calls)])
    release = next(t for t in tools if t.name == "release_payment_block")
    out = json.loads(await release.coroutine(
        invoice_number="INV-5500011", fiscal_year="2026", reason="manager reviewed"
    ))

    assert out["status"] == "release_denied"
    assert calls == []
    assert captured["reversibility"] == "hard_to_recover_after_payment"
    assert any("Duplicate risk" in item for item in captured["safeguards"])
