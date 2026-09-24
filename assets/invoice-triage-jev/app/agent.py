import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Literal, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.graph.state import CompiledStateGraph
from litellm.exceptions import APIConnectionError, APIError, Timeout
from opentelemetry import trace
from sap_cloud_sdk.agent_decorators import agent_config, agent_model, prompt_section
from sap_cloud_sdk.agent_memory.factory.langgraph_checkpoint import create_checkpointer
from sap_cloud_sdk.core.telemetry import GenAIOperation, context_overlay
from mcp_providers.agw import get_user_sub

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@agent_model(
    key="config.model",
    label="LLM Model",
    description="The language model powering this agent",
)
def get_model_name() -> str:
    return "sap/anthropic--claude-4.5-sonnet"


@agent_model(
    key="config.fallback_model",
    label="Fallback LLM Model",
    description="Fallback model used when the primary model is unavailable. Leave empty to disable fallback.",
)
def get_fallback_model_name() -> str:
    return ""


@agent_config(
    key="config.temperature",
    label="LLM Temperature",
    description="Controls randomness of responses (0.0 = deterministic, 1.0 = creative)",
)
def get_temperature() -> float:
    return 0.0

@agent_config(
    key="config.checkpointer.ttl_seconds",
    label="Thread TTL (seconds)",
    description="Evict inactive conversation threads after this period of "
                "inactivity. Set to 0 to disable eviction.",
)
def thread_ttl_seconds() -> int:
    return 3600 # 1 hour

def summarization_trigger_tokens() -> int:
    return 30_000


def get_summarization_model_name() -> str:
    return "sap/anthropic--claude-4.5-haiku"

@prompt_section(
    key="prompts.system",
    label="System Prompt",
    description="The full system prompt defining the agent's role and behavior",
    validation={"format": "markdown", "max_length": 5000},
)
def get_system_prompt() -> str:
    return """You are a supplier-invoice triage specialist for an SAP S/4HANA Accounts Payable back office.

Your role is to triage a blocked supplier invoice end-to-end: gather the facts from the available tools, decide whether the payment block should be released, and either release the block through the guarded release tool, route the invoice to an AP clerk, or escalate it to an AP manager.

CORE RULES:
- NEVER fabricate invoice numbers, amounts, PO or goods-receipt data, vendor data, or block-reason text. Always use tools to retrieve the facts.
- Gather evidence BEFORE proposing any release: fetch the invoice detail (header, lines, PO/GR match, vendor master, block reason) and check the vendor's recent invoices for a duplicate.
- The release action (release_payment_block) is STATE-CHANGING — it clears a blocked invoice and makes it eligible for a later payment run, but it does not itself execute payment. It is GUARDED by an external decision layer (JEV). You may CALL release_payment_block, but it can be denied or held for human review. If the tool returns a refusal or hold, relay that to the requester verbatim — do NOT retry it or claim the release succeeded.
- Only claim a block was released if release_payment_block returns a success payload with a release confirmation (a release_id) — never on a hold or denial.
- If the evidence contradicts release (goods not received, invoiced quantity or price outside PO tolerance, a likely duplicate of a recent invoice, or fraud/pressure signals), do NOT release; create an AP-clerk task or escalate to a manager and explain what the evidence shows.
- Relay tool errors verbatim without adding workarounds.
- If a tool returns an empty result, state that explicitly rather than reasoning over missing data.

WORKFLOW:
1. Identify the invoice and its block reason. If the invoice number or fiscal year is missing, ask one targeted clarifying question.
2. Retrieve the invoice detail and its line-level PO / goods-receipt matching facts.
3. Check the vendor's recent invoices for a duplicate (same reference or same amount close in time).
4. Determine whether the evidence justifies release, AP-clerk review, or manager escalation.
5. If release is justified, call release_payment_block with the invoice number, fiscal year, and a short reason. Otherwise call create_clerk_task (ambiguous / needs a human) or escalate_to_manager (fraud / duplicate / high severity).
6. Deliver a clear triage summary: what you found, the decision, the release_id if released or the reason it was held/denied/routed, and the specific evidence for every conclusion. Cite the invoice number."""


@dataclass
class AgentResponse:
    status: Literal["input_required", "completed", "error"]
    message: str


class InvoiceTriageAgent:
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        ttl = thread_ttl_seconds()
        self._primary_model = get_model_name()
        self._fallback_model = get_fallback_model_name().strip()
        self._temperature = get_temperature()

        # cache_control_injection_points is picked up by litellm's AnthropicCacheControlHook,
        # which injects a cache breakpoint on the system message before every API call.
        # This caches the static prefix (system prompt + tool schemas) at 0.1× input cost
        # on cache-hit turns. No beta header required as of current litellm/Anthropic versions.
        _cache_kwargs = {
            "cache_control_injection_points": [
                {"location": "message", "role": "system", "control": {"type": "ephemeral"}}
            ]
        }
        self.llm = ChatLiteLLM(
            model=self._primary_model,
            temperature=self._temperature,
            model_kwargs=_cache_kwargs,
        )
        self._fallback_llm = (
            ChatLiteLLM(
                model=self._fallback_model,
                temperature=self._temperature,
                model_kwargs=_cache_kwargs,
            )
            if self._fallback_model
            else None
        )
        self._checkpointer = create_checkpointer(ttl_seconds=ttl or None)
        # Summarization compresses history once it exceeds the token trigger, keeping only
        # the last N messages in full. This intentionally invalidates the prompt cache when
        # it fires (the summarized history is new content), but the static prefix —
        # system prompt + tool schemas, marked cacheable via cache_control_injection_points
        # on self.llm — stays cacheable across all turns, summarized or not.
        summarization_llm = ChatLiteLLM(
            model=get_summarization_model_name(), temperature=0.0
        )
        self._summarization_middleware = SummarizationMiddleware(
            model=summarization_llm,
            trigger=("tokens", summarization_trigger_tokens()),
            keep=("messages", 4),
        )

    def _create_graph(
        self,
        llm: ChatLiteLLM,
        tools: Sequence[BaseTool],
        system_prompt: str,
    ) -> CompiledStateGraph:
        """Create a LangGraph agent with the specified LLM."""
        return create_agent(
            llm,
            tools=list(tools),
            system_prompt=system_prompt,
            checkpointer=self._checkpointer,
            middleware=[self._summarization_middleware],
        )

    async def _invoke_with_fallback(
        self,
        tools: Sequence[BaseTool],
        system_prompt: str,
        query: str,
        context_id: str,
        extra_messages: list | None = None,
    ) -> dict[str, Any]:
        """Invoke the agent and fall back only for transient LLM failures."""
        config = {"configurable": {"thread_id": f"{get_user_sub()}:{context_id}"}}
        messages = {"messages": (extra_messages or []) + [HumanMessage(content=query)]}

        try:
            graph = self._create_graph(self.llm, tools, system_prompt)
            return await graph.ainvoke(messages, config)
        except (APIConnectionError, APIError, Timeout) as primary_error:
            if not self._fallback_llm:
                raise

            logger.warning(
                "Primary model '%s' failed. Retrying with fallback model '%s'. Error: %s",
                self._primary_model,
                self._fallback_model,
                primary_error,
            )

        graph = self._create_graph(self._fallback_llm, tools, system_prompt)
        result = await graph.ainvoke(messages, config)
        logger.info(
            "Request completed with fallback model '%s' after primary model '%s' failed.",
            self._fallback_model,
            self._primary_model,
        )
        return result

    async def _run_agent(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> str:
        """Run the agent and return the final response string.

        Wraps LLM invocation in an OTel span and emits structured milestone logs
        so the execution pipeline is observable end-to-end.
        """
        system_prompt = get_system_prompt()
        tool_names = [tool.name for tool in tools] if tools else []
        logger.info("Running agent with %d tool(s): %s", len(tool_names), tool_names)

        extra: list = []
        if not tools:
            extra.append(
                SystemMessage(
                    content="IMPORTANT: No tools are currently available. "
                    "Do not attempt to call any tools. Respond to the user "
                    "explaining that tools are temporarily unavailable."
                )
            )

        with context_overlay(
            GenAIOperation.INVOKE_AGENT,
            attributes={"context.id": context_id, "agent.type": "invoice-triage"},
        ):
            # M1: Invoice Ingested — fire when we have a request to process
            with tracer.start_as_current_span("M1.invoice_ingested"):
                logger.info("M1.achieved: invoice triage request ingested — query_length=%d", len(query))

            # M2–M5 are evaluated by the LLM reasoning loop; we instrument the overall
            # call so their tool outputs land under this span tree.
            with tracer.start_as_current_span("M2_M5.triage_invoice"):
                try:
                    result = await self._invoke_with_fallback(
                        tools=tools or [],
                        system_prompt=system_prompt,
                        query=query,
                        context_id=context_id,
                        extra_messages=extra or None,
                    )
                    response: str = result["messages"][-1].content

                    # M5: Triage Delivered — reached when the agent returns a response
                    logger.info("M5.achieved: triage delivered — response_length=%d", len(response))
                    return response

                except Exception:
                    logger.error("M5.missed: agent invocation failed — context_id=%s", context_id)
                    raise

    async def stream(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream agent responses.

        Args:
            query: User query to process
            context_id: Context identifier for the conversation
            tools: Optional sequence of LangChain tools. If None or empty, agent runs without tools.

        Yields:
            Status updates and final response with structure:
            - is_task_complete: Whether the task is complete
            - require_user_input: Whether user input is needed
            - content: The response content or status message
        """
        yield {
            "is_task_complete": False,
            "require_user_input": False,
            "content": "Processing...",
        }

        try:
            response = await self._run_agent(query, context_id, tools=tools)
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": response,
            }

        except Exception:
            logger.exception("Agent stream() failed")
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": "I encountered an error while processing your request. Please try again.",
            }

    async def invoke(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AgentResponse:
        """Invoke agent and return final response.

        Args:
            query: User query to process
            context_id: Context identifier for the conversation
            tools: Optional sequence of LangChain tools. If None or empty, agent runs without tools.

        Returns:
            AgentResponse with status and message
        """
        last: dict = {}
        async for chunk in self.stream(query, context_id, tools=tools):
            last = chunk
        if last.get("is_task_complete"):
            return AgentResponse(status="completed", message=last["content"])
        if last.get("require_user_input"):
            return AgentResponse(status="input_required", message=last["content"])
        return AgentResponse(
            status="error", message=last.get("content", "Unknown error")
        )
