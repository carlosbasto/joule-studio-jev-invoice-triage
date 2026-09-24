"""Minimal TypeSafe JEV client used by the deployed invoice-triage agent.

The live runtime uses two typed System One choice checks:
- ``tool_guard`` gates ``release_payment_block`` before the original tool runs.
- ``completion`` performs an advisory post-response completeness assessment.

Secrets are never stored in source. Configure ``TYPESAFE_API_KEY`` (or the
compatibility alias ``JEV_API_KEY``) through the runtime environment. Missing,
failed, or malformed decisions resolve to ``None``; the release wrapper treats
that as a hold rather than authorization.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.typesafe.ai"
_SYSTEMONE_PATH = "/v1/systemone"
_DEFAULT_MODEL = "jev-latest"
_DECISION_KEY = "decision"

def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _api_key() -> str:
    """Return the TypeSafe credential from environment configuration only."""
    return os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_API_KEY") or ""

def is_enabled() -> bool:
    """True when a key is available and the decision layer is not explicitly disabled.

    A key auto-enables the layer unless TYPESAFE_ENABLED / JEV_ENABLED is set to
    a falsy value to force it off.
    """
    if not _api_key():
        return False
    flag = os.getenv("TYPESAFE_ENABLED")
    if flag is None:
        flag = os.getenv("JEV_ENABLED")
    if flag is None:
        return True  # key present and no explicit flag → on
    return _truthy(flag)


def _base_url() -> str:
    return (
        os.getenv("TYPESAFE_BASE_URL")
        or os.getenv("JEV_BASE_URL")
        or _DEFAULT_BASE_URL
    ).rstrip("/")


def _default_model() -> str:
    return os.getenv("TYPESAFE_DEFAULT_MODEL") or _DEFAULT_MODEL


def _timeout_seconds() -> float:
    raw = os.getenv("TYPESAFE_TIMEOUT_SECONDS") or os.getenv("JEV_TIMEOUT_SECONDS") or "15"
    try:
        return float(raw)
    except ValueError:
        return 15.0


async def _system_one(
    state: Any,
    questions: dict[str, dict[str, Any]],
    model: str | None = None,
) -> dict[str, Any] | None:
    """POST to the System One endpoint; return the parsed ``answers`` map or None.

    Never raises: a missing key, transport errors, non-200 status, and a
    malformed body all resolve to None after logging, so the decision layer can
    never break the agent flow.
    """
    api_key = _api_key()
    if not api_key:
        logger.warning("JEV: no API key (TYPESAFE_API_KEY / JEV_API_KEY); skipping call")
        return None

    url = f"{_base_url()}{_SYSTEMONE_PATH}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "state": state,
        "model": model or _default_model(),
        "questions": questions,
    }
    try:
        async with httpx.AsyncClient(timeout=_timeout_seconds()) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:  # noqa: BLE001 — decision layer must never break the flow
        logger.warning("JEV: call to %s failed: %s", _SYSTEMONE_PATH, exc)
        return None

    if not isinstance(body, dict):
        logger.warning("JEV: %s returned a non-object body (%s)", _SYSTEMONE_PATH, type(body).__name__)
        return None

    answers = body.get("answers")
    # Callers index `answers[key]` as a mapping. A missing/malformed answers
    # block degrades safely to None instead of raising downstream.
    if not isinstance(answers, dict):
        logger.warning(
            "JEV: %s response missing 'answers' map (got %s)",
            _SYSTEMONE_PATH,
            type(answers).__name__,
        )
        return None
    return answers


def _read_choice(answers: dict[str, Any] | None, key: str = _DECISION_KEY) -> dict[str, Any] | None:
    """Normalise a System One ``choice`` answer into ``{decision, confidence, probabilities}``.

    Returns None if the answer is missing or has no valid ``choice`` — which the
    gates treat as "no decision" and degrade safely (never to auto-approve).
    """
    if not isinstance(answers, dict):
        return None
    ans = answers.get(key)
    if not isinstance(ans, dict):
        return None
    choice = ans.get("choice")
    if not isinstance(choice, str) or not choice:
        return None
    out: dict[str, Any] = {"decision": choice}
    if "confidence" in ans:
        out["confidence"] = ans["confidence"]
    if "probabilities" in ans:
        out["probabilities"] = ans["probabilities"]
    return out


async def _ask_choice(
    state: Any,
    instructions: str,
    criteria: dict[str, str],
    *,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Ask System One a single ``choice`` question and return the normalised decision."""
    questions = {
        _DECISION_KEY: {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }
    }
    answers = await _system_one(state, questions, model=model)
    return _read_choice(answers)


async def completion(
    objective: str,
    completed_work: list[str],
    verification: list[str] | None = None,
    known_gaps: list[str] | None = None,
) -> dict[str, Any] | None:
    """Ask whether `objective` is genuinely complete.

    Returns ``{decision, confidence, probabilities}`` where ``decision`` is one
    of ``complete | verify_more | incomplete`` — or None if unreachable/errored.
    """
    state: dict[str, Any] = {
        "objective": objective,
        "completed_work": completed_work,
    }
    if verification:
        state["verification"] = verification
    if known_gaps:
        state["known_gaps"] = known_gaps
    return await _ask_choice(
        state,
        instructions=(
            "Given the objective and the work done so far, is the objective "
            "genuinely complete?"
        ),
        criteria={
            "complete": "The objective is genuinely satisfied; nothing material is missing.",
            "verify_more": "Looks done but needs verification before declaring complete.",
            "incomplete": "The objective is not yet met — work remains.",
        },
    )


async def tool_guard(
    tool: str,
    action: str,
    arguments_summary: str | None = None,
    side_effects: list[str] | None = None,
    safeguards: list[str] | None = None,
    policy: str | None = None,
    reversibility: str | None = None,
) -> dict[str, Any] | None:
    """Evaluate a consequential tool call before it runs.

    Returns ``{decision, ...}`` where ``decision`` is one of
    ``allow | confirm | review | deny`` — or None on failure.
    """
    state: dict[str, Any] = {"tool": tool, "action": action}
    if arguments_summary:
        state["arguments_summary"] = arguments_summary
    if side_effects:
        state["side_effects"] = side_effects
    if safeguards:
        state["safeguards"] = safeguards
    if policy:
        state["policy"] = policy
    if reversibility:
        state["reversibility"] = reversibility
    return await _ask_choice(
        state,
        instructions="Should this consequential tool call be allowed to run?",
        criteria={
            "allow": "Safe and justified — run as-is.",
            "confirm": "Run only after explicit human confirmation.",
            "review": "Hold for human review before running.",
            "deny": "Do not run — unsafe or against policy.",
        },
    )
