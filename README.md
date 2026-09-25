# Supplier Invoice Triage Agent for Joule Studio with JEV

Public companion sample for the blocked supplier-invoice scenario described in the SAP Community article **“Joule Studio with JEV at the Decision Boundary of the Autonomous Enterprise.”**

The agent gathers synthetic invoice and vendor evidence, reasons about whether a payment block should be released, and wraps the state-changing `release_payment_block` tool with a TypeSafe JEV decision gate. The original release coroutine runs **only after an explicit JEV `allow`**. `deny`, `review`, `confirm`, missing/invalid decisions, or JEV failures do not execute the release.

> **Demo only.** The repository uses static synthetic business data and is not production-ready. `release_payment_block` is a mock tool; no SAP S/4HANA document or payment is changed by this sample.

## What is in the live path

- `app/agent.py` — runtime model configuration, system prompt, conversation memory, and agent loop.
- `app/agent_executor.py` — loads tools, installs the release guard, streams the result, and runs an advisory JEV completion check.
- `app/decision_gates.py` — reconstructs invoice/vendor risk context and gates `release_payment_block`.
- `app/jev.py` — minimal HTTP client for TypeSafe System One using typed `choice` questions.
- `app/mcp_providers/agw.py` — mock tools by default; Agent Gateway path when `IBD_TESTING=0`.
- `mcp-mock.json` — static synthetic AP data used by the demo.

The public version intentionally omits experimental/offline decision helpers that are not called by the deployed `AgentExecutor` path.

## Decision boundary

The agent can gather evidence and decide to escalate without reaching the release tool. If it does call `release_payment_block`, the wrapper re-reads the relevant evidence and asks JEV for one of four decisions:

| JEV decision | Runtime behavior |
| --- | --- |
| `allow` | Invoke the original mock release tool |
| `deny` | Return `release_denied`; do not invoke the release |
| `review` | Return `release_pending_review`; do not invoke the release |
| `confirm` | Return `release_pending_review`; do not invoke the release |

No decision is treated as approval.

## Configuration

The repository contains **no credentials**. Supply the TypeSafe key only through environment/secret configuration:

```bash
export TYPESAFE_API_KEY="..."
```

Compatibility alias: `JEV_API_KEY`.

Useful optional settings:

```bash
export TYPESAFE_ENABLED=true
export TYPESAFE_DEFAULT_MODEL=jev-latest
export TYPESAFE_TIMEOUT_SECONDS=15
```

Mock mode is the safe default in this public sample. To state it explicitly:

```bash
export IBD_TESTING=1
```

Set `IBD_TESTING=0` only after configuring the appropriate Agent Gateway/MCP dependencies and authorizations for your environment.

`.env.example` is provided as a reference only; this application does not automatically load it.

## Local tests

From the agent asset directory:

```bash
cd assets/invoice-triage-jev
python -m pip install -r requirements.txt -r requirements-test.txt
IBD_TESTING=1 python -m pytest
```

The tests use mock tools and patched JEV transport for the release-control regression cases. They do not require a real TypeSafe key.

## Build for Joule Studio

From the repository root, with the `jl` CLI authenticated against your Joule Studio environment:

```bash
jl solution validate
jl solution build
```

The resulting ZIP under `build/` can be imported into Joule Studio. Deployment details can vary by Joule Studio/EAC version.

## Demonstration prompt

```text
Triage blocked invoice INV-5500011 for fiscal year 2026 and release the payment block if it is justified.
```

The mock fixture deliberately combines clean invoice-level matching evidence with a same-vendor, same-amount recent invoice so the agent has a cross-document risk signal to evaluate.

## Security and data handling

- Never commit `TYPESAFE_API_KEY`, `JEV_API_KEY`, SAP credentials, bearer tokens, destinations, tenant URLs, or deployment IDs.
- The bundled invoice/vendor data is synthetic test data.
- Real business data requires an approved integration, authorization, and data-handling design before being sent to any external decision service.
- `release_pending_review` is a typed hold result, not a complete human-approval workflow.

See [SECURITY.md](SECURITY.md) for publication and credential-handling guidance.

## Repository layout

```text
supplier-invoice-triage-agent/
├── solution.yaml
├── README.md
├── SECURITY.md
├── .gitignore
├── .env.example
└── assets/
    └── invoice-triage-jev/
        ├── asset.yaml
        ├── mcp-mock.json
        ├── requirements.txt
        ├── requirements-test.txt
        ├── pytest.ini
        ├── app/
        ├── tests/
        └── prebuilt_tests/
```

## License

This project is licensed under the Apache License 2.0. See the `LICENSE` file for details.
