# CRITICAL: Initialize telemetry BEFORE importing AI frameworks
from sap_cloud_sdk.aicore import set_aicore_config
from sap_cloud_sdk.core.telemetry import auto_instrument

set_aicore_config()
auto_instrument()

import logging
import os

import click
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from starlette.middleware.base import BaseHTTPMiddleware

from agent_executor import AgentExecutor
from mcp_providers.agw import set_user_token, reset_user_token
from opentelemetry.instrumentation.starlette import StarletteInstrumentor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))

_DESCRIPTION = (
    "An AI agent that triages blocked supplier invoices. It gathers invoice, "
    "PO/goods-receipt, block-reason, and recent-vendor evidence. If the agent "
    "proposes release_payment_block, a Python wrapper re-reads the relevant "
    "evidence and asks JEV (TypeSafe System One) for a typed release decision. "
    "Only an explicit allow invokes the original release tool; other or unavailable "
    "decisions hold or deny the release. Clearing the block makes the invoice "
    "eligible for a later payment run; it does not itself execute payment."
)


@click.command()
@click.option("--host", default=HOST)
@click.option("--port", default=PORT)
def main(host: str, port: int):
    skill = AgentSkill(
        id="invoice-triage-jev",
        name="invoice-triage-jev",
        description=_DESCRIPTION,
        tags=["invoice", "accounts-payable", "s4hana", "procure-to-pay", "jev", "decision-layer"],
        examples=[
            "Triage blocked invoice INV-5500011 for fiscal year 2026.",
            "Should the payment block on INV-5500011 be released?",
            "Review the blocked supplier invoice INV-5500011 and recommend release, clerk review, or escalation.",
        ],
    )
    agent_card = AgentCard(
        name="invoice-triage-jev",
        description=_DESCRIPTION,
        url=os.environ.get("AGENT_PUBLIC_URL", f"http://{host}:{port}/"),
        version="1.0.0",
        default_input_modes=["text", "text/plain"],
        default_output_modes=["text", "text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[skill],
    )
    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=DefaultRequestHandler(
            agent_executor=AgentExecutor(),
            task_store=InMemoryTaskStore(),
        ),
    )
    app = server.build()

    class JWTContextMiddleware(BaseHTTPMiddleware):
        """Extracts JWT token from Authorization header and sets it in context."""

        async def dispatch(self, request, call_next):
            auth_header = request.headers.get("authorization", "")
            token = auth_header[7:] if auth_header.lower().startswith("bearer ") else None
            token_ctx = set_user_token(token)
            try:
                return await call_next(request)
            finally:
                reset_user_token(token_ctx)

    app.add_middleware(JWTContextMiddleware)

    StarletteInstrumentor().instrument_app(app)

    logger.info(f"Starting A2A server at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
