"""Offline tests for Magentic routing instructions and agent wiring."""

import json
import logging
from collections import deque

import httpx
import pytest
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
    tool,
)
from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from src.orchestrator import magentic_workflow


def progress(satisfied, next_speaker):
    def item(answer):
        return {"reason": "fixture", "answer": answer}

    return json.dumps(
        {
            "is_request_satisfied": item(satisfied),
            "is_in_loop": item(False),
            "is_progress_being_made": item(True),
            "next_speaker": item(next_speaker),
            "instruction_or_question": item("Complete the requested task."),
        }
    )


def specialist_plan(selected_agents, expected_tools):
    allowed = {
        "CustomerQueryAgent": {"analyze_customer_query"},
        "DestinationAgent": set(),
        "ItineraryAgent": {"suggest_hotels", "suggest_flights"},
    }
    return json.dumps(
        {
            "direct_response": not selected_agents,
            "requirements": [
                {
                    "agent": name,
                    "instruction": "Complete the requested task.",
                    "required_tools": [tool_name for tool_name in expected_tools if tool_name in allowed[name]],
                }
                for name in selected_agents
            ],
            "plan": "Complete the requested work.",
        }
    )


class ScriptedClient(BaseChatClient):
    model = "fixture"

    def __init__(self, selected_agents, expected_tools=None):
        super().__init__()
        steps = [("manager", "Facts"), ("manager", specialist_plan(selected_agents, expected_tools or []))]
        for name in selected_agents:
            steps.extend([("manager", progress(False, name)), (name, "Task completed.")])
        steps.extend(
            [
                ("manager", progress(True, selected_agents[-1] if selected_agents else "CustomerQueryAgent")),
                ("manager", "Final answer."),
            ]
        )
        self.steps = deque(steps)
        self.calls = []
        self.manager_instructions = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        role, response = self.steps.popleft()
        instructions = options.get("instructions", "")
        self.calls.append(role)
        if role == "manager":
            self.manager_instructions.append(instructions)
        else:
            expected = {
                "CustomerQueryAgent": "You are a Customer Query Agent",
                "DestinationAgent": "You are a Destination Recommendation Agent",
                "ItineraryAgent": "You are an Itinerary Planning Agent",
            }
            assert expected[role] in instructions

        if stream:
            async def updates():
                yield ChatResponseUpdate(contents=[Content.from_text(response)], role="assistant", finish_reason="stop")

            return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

        async def answer():
            return ChatResponse(messages=[Message(role="assistant", contents=[response])])

        return answer()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_message", "selected_agents"),
    [("Hello.", [])],
    ids=["greeting"],
)
async def test_scripted_magentic_routing_preserves_instructions_and_tools(monkeypatch, user_message, selected_agents):
    customer_tool = tool(lambda: "customer fixture", name="customer_fixture")
    itinerary_tool = tool(lambda: "itinerary fixture", name="itinerary_fixture")
    metadata = {
        "customer-query": {"name": "Customer Query", "url": "http://customer.invalid/mcp"},
        "itinerary-planning": {"name": "Itinerary Planning", "url": "http://itinerary.invalid/mcp/"},
    }
    monkeypatch.setattr(magentic_workflow.tool_registry, "get_server_metadata", metadata.get)
    mcp_calls = []

    def create_tool(**kwargs):
        mcp_calls.append(kwargs)
        return customer_tool if kwargs["name"] == "Customer Query" else itinerary_tool

    monkeypatch.setattr(magentic_workflow, "MCPStreamableHTTPTool", create_tool)
    created_agents = {}

    def create_agent(**kwargs):
        created_agents[kwargs["name"]] = kwargs
        return Agent(**kwargs)

    monkeypatch.setattr(magentic_workflow, "Agent", create_agent)
    builder_options = []
    original_builder = magentic_workflow.MagenticBuilder

    def create_builder(**kwargs):
        builder_options.append(kwargs)
        return original_builder(**kwargs)

    monkeypatch.setattr(magentic_workflow, "MagenticBuilder", create_builder)
    client = ScriptedClient(selected_agents)
    orchestrator = magentic_workflow.MagenticTravelOrchestrator()
    orchestrator.chat_client = client
    events = [event async for event in orchestrator.process_request_stream(user_message)]

    instructions = created_agents["MagenticManager"]["instructions"]
    assert "customer inquiry analysis to CustomerQueryAgent" in instructions
    assert "destination recommendations to DestinationAgent" in instructions
    assert "itinerary, hotel, or flight planning to ItineraryAgent" in instructions
    assert "only the agents needed" in instructions
    assert "combining them for multi-part travel planning" in instructions
    assert "CustomerQueryAgent to use its MCP tool" in instructions
    assert "Hotel lookups require ItineraryAgent to use suggest_hotels" in instructions
    assert "Activities-only itineraries do not require those tools" in instructions
    assert "DestinationAgent is LLM-only" in instructions
    assert "Your own facts, plan, or knowledge do not complete requested travel work" in instructions
    assert "Wait for each relevant specialist to respond and finish the required tool work" in instructions
    assert "Do not select unrelated agents or repeat completed work" in instructions
    assert "For a greeting with no travel work, respond directly" in instructions
    assert "Finish as soon as the user's requested tasks are satisfied" in instructions
    assert all(value == instructions for value in client.manager_instructions)
    assert [role for role in client.calls if role != "manager"] == selected_agents
    assert not client.steps
    assert created_agents["CustomerQueryAgent"]["tools"] is customer_tool
    assert created_agents["ItineraryAgent"]["tools"] is itinerary_tool
    assert "tools" not in created_agents["DestinationAgent"]
    assert {call["name"] for call in mcp_calls} == {"Customer Query", "Itinerary Planning"}
    assert all(call["request_timeout"] == 30 for call in mcp_calls)
    assert builder_options[0]["manager"].max_round_count == 8
    assert builder_options[0]["manager"].max_stall_count == 2
    assert builder_options[0]["manager"].max_reset_count == 1
    assert any(event.get("event") == "FinalResult" and event["data"]["message"] for event in events)
    assert not any(event.get("type") == "error" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_message", "selected_agents", "expected_tools", "outcome"),
    [
        ("Hello.", [], [], "success"),
        ("Analyze a customer inquiry.", ["CustomerQueryAgent"], ["analyze_customer_query"], "success"),
        ("Recommend a destination.", ["DestinationAgent"], [], "success"),
        ("Plan an activities-only itinerary.", ["ItineraryAgent"], [], "success"),
        ("Plan an activities-only itinerary.", ["ItineraryAgent"], [], "optional-tool-unavailable"),
        ("Plan a hotel itinerary.", ["ItineraryAgent"], ["suggest_hotels"], "success"),
        ("Plan a flight.", ["ItineraryAgent"], ["suggest_flights"], "success"),
        ("Find hotels and flights.", ["ItineraryAgent"], ["suggest_hotels", "suggest_flights"], "success"),
        (
            "Analyze a customer inquiry, recommend a destination, and plan a hotel itinerary.",
            ["CustomerQueryAgent", "DestinationAgent", "ItineraryAgent"],
            ["analyze_customer_query", "suggest_hotels"],
            "success",
        ),
        ("Analyze a customer inquiry.", ["CustomerQueryAgent"], ["analyze_customer_query"], "missing"),
        ("Analyze a customer inquiry.", ["CustomerQueryAgent"], ["analyze_customer_query"], "failed"),
        ("Plan a hotel itinerary.", ["ItineraryAgent"], ["suggest_hotels"], "empty"),
        ("Plan a flight.", ["ItineraryAgent"], ["suggest_flights"], "flight-error"),
        ("Find hotels in Paris and Tokyo.", ["ItineraryAgent"], ["suggest_hotels"], "batch-failure"),
        ("Find hotels in Paris and Tokyo.", ["ItineraryAgent"], ["suggest_hotels"], "batch-retry"),
        ("Find hotels in Paris and Tokyo.", ["ItineraryAgent"], ["suggest_hotels"], "batch-reused-id-retry"),
        ("Find a hotel in Paris.", ["ItineraryAgent"], ["suggest_hotels"], "optional-tool-reused-id"),
        ("Analyze a customer inquiry.", ["CustomerQueryAgent"], ["analyze_customer_query"], "customer-unavailable"),
        ("Plan a hotel itinerary.", ["ItineraryAgent"], ["suggest_hotels"], "itinerary-unavailable"),
    ],
    ids=[
        "greeting",
        "customer-only",
        "destination-only",
        "activities-only",
        "activities-only-without-tool",
        "hotel",
        "flight",
        "hotel-and-flight",
        "multi-intent",
        "missing-tool",
        "failed-tool",
        "empty-tool-result",
        "flight-tool-error",
        "same-tool-batch-failure",
        "same-input-batch-retry",
        "same-input-reused-id-retry",
        "optional-tool-reused-id",
        "customer-tool-unavailable",
        "itinerary-tool-unavailable",
    ],
)
async def test_scripted_specialist_tool_execution(
    monkeypatch, caplog, user_message, selected_agents, expected_tools, outcome
):
    caplog.set_level(logging.INFO)
    invoked = []
    hotel_queries = []

    def customer(query: str) -> str:
        invoked.append("analyze_customer_query")
        if outcome == "failed":
            raise ValueError("synthetic tool failure")
        return "Customer analysis."

    def hotels(query: str) -> str:
        invoked.append("suggest_hotels")
        hotel_queries.append(query)
        if (
            outcome in {"batch-failure", "batch-retry", "batch-reused-id-retry"}
            and query == "Paris"
            and hotel_queries.count(query) == 1
        ):
            raise ValueError("synthetic tool failure")
        return "null" if outcome == "empty" else "Hotel suggestions."

    def flights(query: str) -> str:
        invoked.append("suggest_flights")
        return "Error: return_date must be after departure_date" if outcome == "flight-error" else "Flight suggestions."

    tools = {
        "Customer Query": tool(customer, name="analyze_customer_query"),
        "Itinerary Planning": [tool(hotels, name="suggest_hotels"), tool(flights, name="suggest_flights")],
    }
    metadata = {
        "customer-query": {"name": "Customer Query", "url": "http://customer.invalid/mcp"},
        "itinerary-planning": {"name": "Itinerary Planning", "url": "http://itinerary.invalid/mcp/"},
    }
    if outcome == "customer-unavailable":
        metadata.pop("customer-query")
    elif outcome in {"itinerary-unavailable", "optional-tool-unavailable"}:
        metadata.pop("itinerary-planning")
    monkeypatch.setattr(magentic_workflow.tool_registry, "get_server_metadata", metadata.get)
    monkeypatch.setattr(magentic_workflow, "MCPStreamableHTTPTool", lambda **kwargs: tools[kwargs["name"]])
    created_agents = {}

    def create_agent(**kwargs):
        created_agents[kwargs["name"]] = kwargs
        return Agent(**kwargs)

    monkeypatch.setattr(magentic_workflow, "Agent", create_agent)
    if outcome in {"missing", "failed", "empty", "flight-error", "batch-failure"}:
        original_manager = magentic_workflow.GuardedMagenticManager

        def limited_manager(**kwargs):
            kwargs["max_round_count"] = 2 if outcome == "batch-failure" else 1
            return original_manager(**kwargs)

        monkeypatch.setattr(magentic_workflow, "GuardedMagenticManager", limited_manager)
    plan = json.loads(specialist_plan(selected_agents, expected_tools))
    if outcome in {"batch-failure", "batch-retry", "batch-reused-id-retry"}:
        plan["requirements"][0]["instruction"] = "Find hotel options in Paris and Tokyo."
    steps = deque([("manager", "Facts"), ("manager", json.dumps(plan))])
    tool_names = {
        "CustomerQueryAgent": [name for name in expected_tools if name == "analyze_customer_query"],
        "ItineraryAgent": [name for name in expected_tools if name in {"suggest_hotels", "suggest_flights"}],
    }
    for name in selected_agents:
        steps.append(("manager", progress(True, "DestinationAgent")))
        if outcome in {"batch-failure", "batch-retry", "batch-reused-id-retry"} and name == "ItineraryAgent":
            steps.append((name, {"batch": [{"tool": "suggest_hotels", "query": city} for city in ("Paris", "Tokyo")]}))
            if outcome in {"batch-retry", "batch-reused-id-retry"}:
                steps.append((name, {"batch": [{"tool": "suggest_hotels", "query": "Paris"}]}))
        elif outcome == "optional-tool-reused-id" and name == "ItineraryAgent":
            steps.append((name, {"batch": [{"tool": "suggest_hotels", "query": "Paris"}]}))
            steps.append((name, {"batch": [{"tool": "suggest_flights", "query": "Paris"}]}))
        elif outcome != "missing":
            steps.extend((name, {"tool": tool_name}) for tool_name in tool_names.get(name, []))
        steps.append((name, "Specialist response."))
    if outcome == "batch-failure":
        steps.extend([("manager", progress(True, "DestinationAgent")), ("ItineraryAgent", "Paris lookup failed.")])
    else:
        steps.extend(
            [
                ("manager", progress(True, selected_agents[-1] if selected_agents else "CustomerQueryAgent")),
                ("manager", "Final answer."),
            ]
        )
    observed = []
    participant_instructions = {
        "CustomerQueryAgent": "You are a Customer Query Agent",
        "DestinationAgent": "You are a Destination Recommendation Agent",
        "ItineraryAgent": "You are an Itinerary Planning Agent",
    }

    def handler(request):
        role, response = steps.popleft()
        body = json.loads(request.content)
        messages = json.dumps(body["messages"])
        if role == "manager":
            assert "Your own facts, plan, or knowledge do not complete requested travel work" in messages
            assert "For a greeting with no travel work, respond directly" in messages
            if isinstance(response, str) and response.startswith('{"direct_response"'):
                assert "Choose specialists by the meaning" in messages
                assert "List requirements in the order they should run" in messages
                assert "Require suggest_hotels for a requested hotel lookup" in messages
                assert "suggest_flights for a requested flight lookup" in messages
                assert "needs no hotel or flight tool" in messages
        else:
            assert participant_instructions[role] in messages
        observed.append(role)

        is_tool_call = isinstance(response, dict)
        call_specs = response.get("batch", [response]) if is_tool_call else []
        tool_calls = [
            {
                "id": (
                    f"call-{spec['query']}"
                    if outcome in {"batch-reused-id-retry", "optional-tool-reused-id"}
                    else f"call-{len(observed)}-{index}"
                ),
                "type": "function",
                "function": {
                    "name": spec["tool"],
                    "arguments": json.dumps({"query": spec.get("query", "fixture")}),
                },
            }
            for index, spec in enumerate(call_specs)
        ]
        finish_reason = "tool_calls" if is_tool_call else "stop"
        if body.get("stream"):
            delta = {"role": "assistant"}
            if is_tool_call:
                delta["tool_calls"] = [dict(call, index=index) for index, call in enumerate(tool_calls)]
            else:
                delta["content"] = response
            base = {"id": "chat-fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture"}
            chunks = [
                dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish_reason}]),
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
            )

        message = {"role": "assistant", "content": None if is_tool_call else response}
        if is_tool_call:
            message["tool_calls"] = tool_calls
        return httpx.Response(
            200,
            json={
                "id": "chat-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    raw_client = AsyncOpenAI(api_key="fixture", base_url="https://example.invalid/v1", http_client=http)
    orchestrator = magentic_workflow.MagenticTravelOrchestrator()
    orchestrator.chat_client = OpenAIChatCompletionClient(model="fixture", async_client=raw_client)
    try:
        events = [event async for event in orchestrator.process_request_stream(user_message)]
    finally:
        await raw_client.close()

    unavailable_required = outcome in {"customer-unavailable", "itinerary-unavailable"}
    if outcome in {"batch-failure", "batch-retry", "batch-reused-id-retry"}:
        assert invoked == ["suggest_hotels"] * (2 if outcome == "batch-failure" else 3)
    elif outcome == "optional-tool-reused-id":
        assert invoked == ["suggest_hotels", "suggest_flights"]
    else:
        assert invoked == ([] if outcome == "missing" or unavailable_required else expected_tools)
    assert any(role == "manager" for role in observed)
    assert [role for role in observed if role != "manager"] == [
        role
        for role in selected_agents
        if not unavailable_required
        for _ in range(
            3
            if outcome in {"batch-failure", "batch-retry", "batch-reused-id-retry", "optional-tool-reused-id"}
            and role == "ItineraryAgent"
            else 1 + (len(tool_names.get(role, [])) if outcome != "missing" else 0)
        )
    ]
    assert "tools" not in created_agents["DestinationAgent"]
    assert user_message not in caplog.text
    assert "Customer analysis." not in caplog.text
    assert "Hotel suggestions." not in caplog.text
    assert "Flight suggestions." not in caplog.text
    assert "Error: return_date must be after departure_date" not in caplog.text
    if outcome in {"batch-failure", "batch-retry", "batch-reused-id-retry", "optional-tool-reused-id"}:
        assert "Paris" not in caplog.text
        assert "Tokyo" not in caplog.text
    if outcome in {
        "success",
        "optional-tool-unavailable",
        "batch-retry",
        "batch-reused-id-retry",
        "optional-tool-reused-id",
    }:
        assert not steps
        assert any(
            event.get("event") == "OrchestratorInstruction"
            and event["data"]["message"].startswith("Complete the requested work.")
            and '"direct_response"' not in event["data"]["message"]
            for event in events
        )
        assert any(event.get("event") == "FinalResult" and event["data"]["message"] for event in events)
        assert not any(event.get("type") == "error" for event in events)
    else:
        errors = [event for event in events if event.get("type") == "error"]
        assert errors
        if outcome == "batch-failure":
            assert (
                errors[0]["message"] == "Workflow error: The request could not be verified as complete. Please retry."
            )
        assert not any(event.get("event") == "FinalResult" for event in events)
