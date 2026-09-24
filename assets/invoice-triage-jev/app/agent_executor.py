import logging

from a2a.server.agent_execution import AgentExecutor as A2AAgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

import jev
import decision_gates
from agent import InvoiceTriageAgent
from load_skill_resources import get_load_skill_resource_tool
from mcp_providers.agw import get_mcp_tools

logger = logging.getLogger(__name__)


class AgentExecutor(A2AAgentExecutor):
    def __init__(self):
        self.agent = InvoiceTriageAgent()
        self.skill_tools = get_load_skill_resource_tool()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Execute the agent and stream results back via A2A protocol.

        Discovers and loads MCP tools from Agent Gateway before each execution.
        The user token is read from the context var set by JWTContextMiddleware.

        Args:
            context: Request context containing user input and task info
            event_queue: Queue for publishing task status updates

        Raises:
            ServerError: On unrecoverable agent execution errors
        """
        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        # Load MCP tools — user token is read from context var set by JWTContextMiddleware
        tools = []
        try:
            tools = await get_mcp_tools()
            if not tools:
                logger.warning("No tools returned from Agent Gateway")
            else:
                tool_names = [t.name for t in tools]
                logger.info("Loaded %d MCP tool(s) for agent execution: %s", len(tools), tool_names)
        except Exception as e:
            logger.error(f"Failed to load tools from Agent Gateway: {e}")

        tools = [*tools, *self.skill_tools]

        # Require JEV approval before clearing an invoice for payment.
        # Disabled, failed, or missing decisions hold the release for review.
        tools = decision_gates.install_release_guard(tools)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        try:
            async for item in self.agent.stream(query, task.context_id, tools=tools):
                is_task_complete = item["is_task_complete"]
                require_user_input = item["require_user_input"]
                content = item["content"]

                if require_user_input:
                    # Agent requests more input
                    await updater.update_status(
                        TaskState.input_required,
                        new_agent_text_message(content, task.context_id, task.id),
                        final=True,
                    )
                    break
                elif is_task_complete:
                    # Optional JEV completion check (advisory: logs the verdict,
                    # never blocks delivery). No-op unless the decision layer is
                    # enabled (a TypeSafe key is available and the layer is not explicitly disabled).
                    await self._maybe_check_completion(query, content)
                    # Completed: add artifact and complete task
                    await updater.add_artifact(
                        [Part(root=TextPart(text=content))], name="agent_result"
                    )
                    await updater.complete()
                    break
                else:
                    # Working status update
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(content, task.context_id, task.id),
                    )
        except Exception as e:
            logger.exception("Agent execution error")
            raise ServerError(error=InternalError()) from e

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())

    async def _maybe_check_completion(self, query: str, plan: str) -> None:
        """Ask JEV whether the triage response genuinely completes the request.

        Advisory only — the verdict is logged, delivery is never blocked. No-op
        unless JEV is enabled (a TypeSafe key is available and the layer is not explicitly disabled). Any error is swallowed.
        """
        if not jev.is_enabled():
            return
        try:
            data = await jev.completion(
                objective=query,
                # JEV body cap is 32 KiB; the reply is well under, but trim defensively.
                completed_work=[plan[:8000]],
                verification=[
                    "Triage generated from mock invoice/PO/goods-receipt/vendor "
                    "tools (IBD_TESTING); any payment-block release passed the JEV "
                    "tool-guard."
                ],
            )
        except Exception:  # noqa: BLE001 — advisory check must never fail the request
            logger.exception("JEV completion check errored (non-fatal)")
            return
        if not data:
            return
        logger.info(
            "JEV.completion: decision=%s confidence=%.2f guidance=%s",
            data.get("decision"),
            float(data.get("confidence") or 0.0),
            data.get("guidance"),
        )
