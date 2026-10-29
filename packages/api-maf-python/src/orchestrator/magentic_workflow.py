"""Magentic Orchestration for Travel Planning using Microsoft Agent Framework.

This module implements the Magentic orchestration pattern from Microsoft Agent Framework
for coordinating multiple specialized travel planning agents. Agents work with MCP tools
following the exact patterns from Microsoft Agent Framework samples.

Reference: https://github.com/microsoft/agent-framework/blob/main/python/samples/03-workflows/orchestrations/magentic.py
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, AsyncGenerator, Dict, List, Optional

from agent_framework import (
    Agent,
    MCPStreamableHTTPTool,
)
from agent_framework.orchestrations import MagenticBuilder

from src.config import settings
from src.orchestrator.delegation_guard import GuardedMagenticManager
from src.orchestrator.providers import get_llm_client
from src.orchestrator.tools.tool_registry import tool_registry

logger = logging.getLogger(__name__)


class MagenticTravelOrchestrator:
    """Magentic-based travel planning orchestrator using Microsoft Agent Framework.

    Completely simplified implementation strictly following MAF best practices.
    Each workflow run creates fresh agents with their own MCP tool instances,
    properly managed using async context managers exactly as shown in MAF samples.

    Architecture:
    - CustomerQueryAgent: Handles customer inquiries with customer-query MCP tools
    - ItineraryAgent: Plans itineraries with itinerary-planning MCP tools
    - DestinationAgent: Recommends destinations (no MCP tools, uses LLM knowledge)

    Reference:
    - Magentic: https://github.com/microsoft/agent-framework/blob/main/python/samples/03-workflows/orchestrations/magentic.py
    - MCP tools: https://github.com/microsoft/agent-framework/blob/main/python/samples/getting_started/agents/openai/openai_responses_client_with_hosted_mcp.py
    """

    def __init__(self):
        """Initialize the Magentic travel orchestrator."""
        self.chat_client: Optional[Any] = None
        self.resources = AsyncExitStack()
        logger.info("Magentic Travel Orchestrator initialized")

    async def initialize(self) -> None:
        """Initialize the chat client for the workflow."""
        logger.info("Initializing Magentic travel planning workflow...")

        # Get the chat client from Microsoft Agent Framework
        try:
            self.chat_client = await get_llm_client(self.resources)
        except Exception:
            await self.resources.aclose()
            raise
        logger.info(f"✓ Chat client initialized for provider: {settings.llm_provider}")
        logger.info("✓ Magentic workflow ready")

    async def process_request_stream(
        self,
        user_message: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Process a user request using the Magentic workflow with true streaming.

        Creates a fresh workflow for each request following MAF best practices.
        MCP tools are created once and passed to agents at creation time - MAF handles
        the connection lifecycle automatically through the workflow's async context manager.

        Args:
            user_message: The user's message/request
            conversation_history: Optional conversation history (not used in current implementation)

        Yields:
            Event dictionaries with type, agent, event, and data for UI consumption
        """
        if not self.chat_client:
            raise RuntimeError("Chat client not initialized. Call initialize() first.")

        logger.info("Processing request with Magentic workflow")

        # Get MCP server metadata
        customer_query_metadata = tool_registry.get_server_metadata("customer-query")
        itinerary_metadata = tool_registry.get_server_metadata("itinerary-planning")

        # Helper function to safely create MCP tool
        def create_mcp_tool(metadata: Optional[Dict[str, Any]]) -> Optional[MCPStreamableHTTPTool]:
            """Create MCP tool from metadata with error handling."""
            if not metadata:
                return None

            try:
                headers = {}
                if metadata.get("access_token"):
                    headers["Authorization"] = f"Bearer {metadata['access_token']}"

                # Create tool exactly as shown in MAF samples
                return MCPStreamableHTTPTool(
                    name=metadata["name"],
                    url=metadata["url"],
                    static_headers=headers if headers else None,
                    load_tools=True,
                    load_prompts=False,
                    request_timeout=30,
                    approval_mode="never_require",  # Auto-approve for seamless experience
                )
            except Exception as e:
                logger.warning(f"⚠ Could not create MCP tool for {metadata.get('name')}: {e}")
                return None

        # Create MCP tool instances - will be passed to agents at creation
        customer_query_tool = create_mcp_tool(customer_query_metadata)
        itinerary_tool = create_mcp_tool(itinerary_metadata)

        # Log MCP tool availability
        if customer_query_tool:
            logger.info("✓ Customer Query MCP tool configured")
        else:
            logger.warning("⚠ Customer Query MCP tool is not configured")

        if itinerary_tool:
            logger.info("✓ Itinerary MCP tool configured")
        else:
            logger.warning("⚠ Itinerary MCP tool is not configured")

        try:
            # Create event queue for streaming
            event_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
            workflow_done = False
            workflow_error: Optional[Exception] = None

            agents = [
                Agent(
                    name="CustomerQueryAgent",
                    description="Handles customer questions and travel information",
                    instructions=(
                        "You are a Customer Query Agent for a travel planning system. "
                        "Answer customer questions about destinations, hotels, and travel logistics. "
                        + (
                            "Use the MCP tools to retrieve accurate information. "
                            if customer_query_tool
                            else "Use your knowledge. "
                        )
                        + "Be helpful and customer-focused."
                    ),
                    client=self.chat_client,
                    tools=customer_query_tool,
                ),
                Agent(
                    name="ItineraryAgent",
                    description="Creates detailed travel itineraries and schedules",
                    instructions=(
                        "You are an Itinerary Planning Agent for a travel planning system. "
                        "Create detailed day-by-day travel itineraries. "
                        + (
                            "Use the MCP tools for requested hotel or flight lookups. "
                            if itinerary_tool
                            else "Use your knowledge. "
                        )
                        + "Be thorough and organized."
                    ),
                    client=self.chat_client,
                    tools=itinerary_tool,
                ),
                Agent(
                    name="DestinationAgent",
                    description="Recommends travel destinations based on preferences",
                    instructions=(
                        "You are a Destination Recommendation Agent. "
                        "Recommend destinations based on customer preferences. "
                        "Be creative and match recommendations to customer needs."
                    ),
                    client=self.chat_client,
                ),
            ]
            manager = GuardedMagenticManager(
                agent=Agent(
                    name="MagenticManager",
                    description="Coordinates travel planning agents",
                    instructions=(
                        "Coordinate the agents to fulfill the user's request. "
                        "Route customer inquiry analysis to CustomerQueryAgent, destination recommendations to "
                        "DestinationAgent, and itinerary, hotel, or flight planning to ItineraryAgent. "
                        "Use only the agents needed for the request, combining them for multi-part travel planning. "
                        "Customer inquiry analysis requires CustomerQueryAgent to use its MCP tool. "
                        "Hotel lookups require ItineraryAgent to use suggest_hotels, and flight lookups require "
                        "suggest_flights. Activities-only itineraries do not require those tools. "
                        "DestinationAgent is LLM-only. "
                        "Your own facts, plan, or knowledge do not complete requested travel work. "
                        "Wait for each relevant specialist to respond and finish the required tool work. "
                        "Do not select unrelated agents or repeat completed work. "
                        "For a greeting with no travel work, respond directly. "
                        "Finish as soon as the user's requested tasks are satisfied."
                    ),
                    client=self.chat_client,
                ),
                available_tool_agents={
                    name
                    for name, tool in (
                        ("CustomerQueryAgent", customer_query_tool),
                        ("ItineraryAgent", itinerary_tool),
                    )
                    if tool
                },
                max_round_count=8,
                max_stall_count=2,
                max_reset_count=1,
            )
            workflow = MagenticBuilder(
                participants=agents,
                intermediate_output_from=agents,
                manager=manager,
            ).build()

            # Run workflow in background task
            async def run_workflow():
                """Execute workflow and mark completion."""
                nonlocal workflow_done, workflow_error
                try:
                    final_chunks: list[str] = []
                    async for event in workflow.run(user_message, stream=True):
                        if event.type == "output" and not manager.finalization_authorized:
                            raise RuntimeError("The request could not be verified as complete. Please retry.")
                        event_data = self._convert_workflow_event(event)
                        if event_data:
                            if event.type == "output" and event_data["event"] == "AgentDelta":
                                final_chunks.append(event_data["data"]["delta"])
                            await event_queue.put(event_data)
                    if not final_chunks:
                        raise RuntimeError("Workflow completed without a final output")
                    await event_queue.put({
                        "type": "metadata",
                        "agent": None,
                        "event": "FinalResult",
                        "data": {"agent": None, "message": "".join(final_chunks), "completed": True},
                    })
                except TimeoutError as e:
                    # Handle timeout and service errors specially
                    logger.error(f"Service error in workflow: {e}", exc_info=True)
                    workflow_error = e
                    error_event = {
                        "type": "error",  # This is the ChatEvent.type
                        "event": "ServiceError",
                        "message": "Request timed out or service unavailable. Please try again.",
                        "statusCode": 504,
                        "data": {"agent": None, "error": str(e)},
                    }
                    await event_queue.put(error_event)
                except Exception as e:
                    logger.error(f"Error in workflow execution: {e}", exc_info=True)
                    workflow_error = e
                    error_event = {
                        "type": "error",  # This is the ChatEvent.type
                        "agent": None,
                        "event": "Error",
                        "message": f"Workflow error: {str(e)}",
                        "statusCode": 500,
                        "data": {
                            "agent": None,
                            "error": str(e),
                        },
                    }
                    await event_queue.put(error_event)
                finally:
                    workflow_done = True
                    await event_queue.put(None)  # Signal completion

            # Start workflow
            workflow_task = asyncio.create_task(run_workflow())

            # Stream events as they arrive
            try:
                while True:
                    try:
                        event_data = await asyncio.wait_for(event_queue.get(), timeout=0.1)

                        if event_data is None:
                            # Completion signal
                            break

                        yield event_data

                    except TimeoutError:
                        if workflow_done:
                            # Drain remaining events
                            while not event_queue.empty():
                                event_data = await event_queue.get()
                                if event_data:
                                    yield event_data
                            break
                        continue

                # Wait for workflow task
                await workflow_task

                # If there was an error, it's already been sent
                if workflow_error:
                    logger.error(f"✗ Workflow completed with error: {workflow_error}")
                else:
                    logger.info("✓ Workflow completed successfully")

            except Exception as e:
                logger.error(f"Error streaming workflow events: {e}", exc_info=True)
                workflow_task.cancel()
                try:
                    await workflow_task
                except asyncio.CancelledError:
                    pass
                raise
            finally:
                if not workflow_task.done():
                    workflow_task.cancel()
                    try:
                        await workflow_task
                    except asyncio.CancelledError:
                        pass

        except TimeoutError as e:
            # Handle timeout and service errors specially
            logger.error(f"Service error in Magentic workflow: {e}", exc_info=True)
            yield {
                "type": "error",  # ChatEvent.type
                "agent": None,
                "event": "ServiceError",
                "message": "Request timed out or service unavailable. Please try again.",
                "statusCode": 504,
                "data": {
                    "agent": None,
                    "error": str(e),
                },
            }
        except Exception as e:
            logger.error(f"Error in Magentic workflow: {e}", exc_info=True)
            yield {
                "type": "error",  # ChatEvent.type
                "agent": None,
                "event": "Error",
                "message": f"Workflow error: {str(e)}",
                "statusCode": 500,
                "data": {
                    "agent": None,
                    "error": str(e),
                },
            }

    def _convert_workflow_event(self, event: Any) -> Optional[Dict[str, Any]]:
        """Convert a Magentic workflow event to our API event format.

        Expected UI format:
        {
          type: "metadata",
          agent: agent || null,
          event: event display name,
          data: { agent, ...event data }
        }

        Args:
            event: Workflow event from Magentic

        Returns:
            Event dictionary in our API format, or None if not relevant for UI
        """
        if event.type in ("intermediate", "output"):
            if isinstance(event.data, str):
                text = event.data
            elif isinstance(event.data, list):
                text = "".join(getattr(message, "text", "") or "" for message in event.data)
            else:
                text = getattr(event.data, "text", "") or ""
            if text:
                agent_id = event.executor_id or "Orchestrator"
                return {
                    "type": "metadata",
                    "agent": agent_id,
                    "event": "AgentDelta",
                    "data": {"agent": agent_id, "delta": text},
                }

        if event.type == "magentic_orchestrator":
            content = getattr(event.data, "content", None)
            message = getattr(content, "text", "") or ""
            if message:
                return {
                    "type": "metadata",
                    "agent": "Orchestrator",
                    "event": "OrchestratorInstruction",
                    "data": {"agent": "Orchestrator", "message": message},
                }

        return None


# Global orchestrator instance
magentic_orchestrator = MagenticTravelOrchestrator()
