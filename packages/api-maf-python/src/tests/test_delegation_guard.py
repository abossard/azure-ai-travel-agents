"""Offline tests for Magentic completion and delegation safeguards."""

import asyncio
import importlib
import json
import traceback
from collections import deque
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agent_framework import (
    Agent,
    Content,
    MCPStreamableHTTPTool,
    Message,
    tool,
)
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework.orchestrations import (
    MagenticContext,
    MagenticProgressLedger,
    StandardMagenticManager,
)
from mcp import types as mcp_types
from openai import AsyncOpenAI

from src.orchestrator import magentic_workflow
from src.orchestrator.delegation_guard import GuardedMagenticManager, SpecialistPlan
from src.tests.test_magentic_routing import ScriptedClient, progress, specialist_plan


def participant_history(
    agent, tools=(), *, outcome="success", include_response=True, response_first=False, call_prefix="call"
):
    history = []
    if response_first:
        history.append(Message(role="assistant", author_name=agent, contents=["Earlier response."]))
    for index, name in enumerate(tools):
        call_id = f"{call_prefix}-{index}"
        history.append(
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call(call_id, name, arguments={})],
            )
        )
        if outcome != "missing":
            history.append(
                Message(
                    role="tool",
                    author_name=agent,
                    contents=[
                        Content.from_function_result(
                            call_id,
                            result="fixture" if outcome != "empty" else None,
                            exception="synthetic failure" if outcome == "failed" else None,
                        )
                    ],
                )
            )
    if include_response:
        history.append(Message(role="assistant", author_name=agent, contents=["Completed response."]))
    return history


@pytest.mark.parametrize(
    ("tool_name", "raw_result", "usable"),
    [
        ("analyze_customer_query", mcp_types.CallToolResult(content=[]), False),
        ("analyze_customer_query", mcp_types.CallToolResult(content=[mcp_types.TextContent(text="null")]), False),
        ("analyze_customer_query", mcp_types.CallToolResult(content=[mcp_types.TextContent(text="{}")]), False),
        ("analyze_customer_query", mcp_types.CallToolResult(content=[], structuredContent={}), False),
        ("analyze_customer_query", mcp_types.CallToolResult(content=[mcp_types.TextContent(text='""')]), False),
        (
            "suggest_flights",
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(text="Error: return_date must be after departure_date")]
            ),
            False,
        ),
        (
            "suggest_flights",
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(text='"Error: return_date must be after departure_date"')]
            ),
            False,
        ),
        ("suggest_hotels", mcp_types.CallToolResult(content=[mcp_types.TextContent(text='{"hotels":[]}')]), True),
        (
            "suggest_flights",
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(text='{"departure_flights":[],"return_flights":[]}')]
            ),
            True,
        ),
        ("suggest_hotels", mcp_types.CallToolResult(content=[mcp_types.TextContent(text="[]")]), True),
        ("suggest_flights", mcp_types.CallToolResult(content=[mcp_types.TextContent(text="No matching flights.")]), True),
        (
            "analyze_customer_query",
            mcp_types.CallToolResult(content=[mcp_types.TextContent(text='{"intent":"private-sentinel"}')]),
            True,
        ),
    ],
    ids=[
        "empty-content",
        "null-text",
        "empty-object",
        "empty-structured-object",
        "empty-string",
        "flight-error",
        "quoted-flight-error",
        "no-hotels",
        "no-flights",
        "empty-result-list",
        "no-results-text",
        "customer-result",
    ],
)
def test_real_sdk_mcp_result_shapes_are_checked_without_logging(caplog, tool_name, raw_result, usable):
    agent = "CustomerQueryAgent" if tool_name == "analyze_customer_query" else "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={"CustomerQueryAgent", "ItineraryAgent"},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], [tool_name])))
    mcp = MCPStreamableHTTPTool(name="fixture", url="http://example.invalid/mcp", load_prompts=False)
    parsed = mcp._parse_tool_result_from_mcp(raw_result)
    history = [
        Message(role="assistant", author_name=agent, contents=[Content.from_function_call("call", tool_name)]),
        Message(role="tool", author_name=agent, contents=[Content.from_function_result("call", result=parsed)]),
        Message(role="assistant", author_name=agent, contents=["Completed response."]),
    ]
    context = MagenticContext(task="fixture", chat_history=history, participant_descriptions={agent: agent})
    assert (not manager._pending(context)) is usable
    assert "private-sentinel" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agents", "tools", "history", "pending"),
    [
        ([], [], [], None),
        (["CustomerQueryAgent"], ["analyze_customer_query"], [], "CustomerQueryAgent"),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent"),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"], outcome="failed"),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"], outcome="missing"),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"], outcome="empty"),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            [
                Message(
                    role="user",
                    author_name="CustomerQueryAgent",
                    contents=[Content.from_function_call("call", "analyze_customer_query", arguments={})],
                ),
                Message(
                    role="tool",
                    author_name="CustomerQueryAgent",
                    contents=[Content.from_function_result("call", result="fixture")],
                ),
                Message(role="assistant", author_name="CustomerQueryAgent", contents=["Completed response."]),
            ],
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            [
                Message(
                    role="assistant",
                    author_name="CustomerQueryAgent",
                    contents=[Content.from_function_call("call", "analyze_customer_query", arguments={})],
                ),
                Message(
                    role="assistant",
                    author_name="CustomerQueryAgent",
                    contents=[Content.from_function_result("call", result="fixture")],
                ),
                Message(role="assistant", author_name="CustomerQueryAgent", contents=["Completed response."]),
            ],
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"], include_response=False),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history(
                "CustomerQueryAgent", ["analyze_customer_query"], include_response=False, response_first=True
            ),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"]),
            None,
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"], call_prefix="first")
            + participant_history("CustomerQueryAgent", ["analyze_customer_query"], outcome="failed", call_prefix="next"),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"], call_prefix="first")
            + participant_history("CustomerQueryAgent", ["analyze_customer_query"], outcome="missing", call_prefix="next"),
            "CustomerQueryAgent",
        ),
        (
            ["CustomerQueryAgent"],
            ["analyze_customer_query"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"], outcome="failed", call_prefix="first")
            + participant_history("CustomerQueryAgent", ["analyze_customer_query"], call_prefix="next"),
            None,
        ),
        (["DestinationAgent"], [], participant_history("DestinationAgent"), None),
        (["ItineraryAgent"], [], participant_history("ItineraryAgent"), None),
        (["ItineraryAgent"], ["suggest_hotels"], participant_history("ItineraryAgent", ["suggest_hotels"]), None),
        (["ItineraryAgent"], ["suggest_flights"], participant_history("ItineraryAgent", ["suggest_flights"]), None),
        (
            ["ItineraryAgent"],
            ["suggest_hotels", "suggest_flights"],
            participant_history("ItineraryAgent", ["suggest_hotels"]),
            "ItineraryAgent",
        ),
        (
            ["ItineraryAgent"],
            ["suggest_hotels", "suggest_flights"],
            participant_history("ItineraryAgent", ["suggest_hotels", "suggest_flights"]),
            None,
        ),
        (
            ["CustomerQueryAgent", "DestinationAgent", "ItineraryAgent"],
            ["analyze_customer_query", "suggest_hotels"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"])
            + participant_history("DestinationAgent"),
            "ItineraryAgent",
        ),
        (
            ["CustomerQueryAgent", "DestinationAgent", "ItineraryAgent"],
            ["analyze_customer_query", "suggest_hotels"],
            participant_history("CustomerQueryAgent", ["analyze_customer_query"])
            + participant_history("DestinationAgent")
            + participant_history("ItineraryAgent", ["suggest_hotels"]),
            None,
        ),
    ],
    ids=[
        "greeting",
        "missing-specialist",
        "missing-tool-call",
        "failed-tool",
        "missing-tool-result",
        "empty-tool-result",
        "invalid-call-role",
        "invalid-result-role",
        "missing-specialist-response",
        "response-before-tool",
        "customer-complete",
        "failed-later-attempt",
        "missing-later-result",
        "successful-later-attempt",
        "destination-complete",
        "activities-only-complete",
        "hotel-complete",
        "flight-complete",
        "missing-second-itinerary-tool",
        "both-itinerary-tools-complete",
        "multi-intent-pending",
        "multi-intent-complete",
    ],
)
async def test_progress_and_finalization_require_evidence(agents, tools, history, pending):
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={"CustomerQueryAgent", "ItineraryAgent"},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan(agents, tools)))
    context = MagenticContext(
        task="fixture", chat_history=history, participant_descriptions={name: name for name in agents}
    )
    ledger = MagenticProgressLedger.from_dict(json.loads(progress(True, "DestinationAgent")))
    with patch.object(StandardMagenticManager, "create_progress_ledger", AsyncMock(return_value=ledger)):
        decision = await manager.create_progress_ledger(context)

    if pending:
        assert decision.is_request_satisfied.answer is False
        assert decision.next_speaker.answer == pending
        with pytest.raises(RuntimeError, match="required specialist work"):
            await manager.prepare_final_answer(context)
        assert manager.finalization_authorized is False
    else:
        assert decision.is_request_satisfied.answer is True
        with patch.object(
            StandardMagenticManager,
            "prepare_final_answer",
            AsyncMock(return_value=Message(role="assistant", contents=["Complete."])),
        ):
            await manager.prepare_final_answer(context)
        assert manager.finalization_authorized is True


@pytest.mark.parametrize(
    ("turns", "pending"),
    [
        (
            [
                (
                    [("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})],
                    [("paris", "ok"), ("tokyo", "ok")],
                )
            ],
            False,
        ),
        (
            [
                (
                    [("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})],
                    [("paris", "failed"), ("tokyo", "ok")],
                )
            ],
            True,
        ),
        (
            [
                (
                    [("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})],
                    [("paris", "ok"), ("tokyo", "failed")],
                )
            ],
            True,
        ),
        (
            [([("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})], [("tokyo", "ok")])],
            True,
        ),
        (
            [([("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})], [("paris", "ok")])],
            True,
        ),
        (
            [
                (
                    [("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})],
                    [("tokyo", "failed"), ("paris", "ok")],
                )
            ],
            True,
        ),
        (
            [
                (
                    [("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})],
                    [("tokyo", "ok"), ("paris", "ok")],
                )
            ],
            False,
        ),
        (
            [
                (
                    [("first", {"location": "Paris", "check_in": "2027-06-10"})],
                    [("first", "failed")],
                ),
                (
                    [("retry", '{"check_in":"2027-06-10","location":"Paris"}')],
                    [("retry", "ok")],
                ),
            ],
            False,
        ),
        (
            [
                (
                    [("first", {"location": "Paris"}), ("next", {"location": "Paris"})],
                    [("first", "failed"), ("next", "ok")],
                )
            ],
            True,
        ),
        (
            [
                (
                    [("paris", {"location": "Paris"}), ("tokyo", {"location": "Tokyo"})],
                    [("paris", "failed"), ("tokyo", "ok")],
                ),
                ([("retry", '{"location":"Paris"}')], [("retry", "ok")]),
            ],
            False,
        ),
        (
            [
                ([("paris", {"location": "Paris"})], [("paris", "failed")]),
                ([("tokyo", {"location": "Tokyo"})], [("tokyo", "ok")]),
            ],
            True,
        ),
        (
            [
                ([("same", {"location": "Paris"})], [("same", "failed")]),
                ([("same", {"location": "Paris"})], [("same", "ok")]),
            ],
            False,
        ),
        (
            [
                ([("same", {"location": "Paris"})], [("same", "failed")]),
                ([("same", {"location": "Tokyo"})], [("same", "ok")]),
            ],
            True,
        ),
        (
            [
                ([("first", {"location": "Paris", "date": "invalid"})], [("first", "failed")]),
                ([("corrected", {"location": "Paris", "date": "valid"})], [("corrected", "ok")]),
            ],
            True,
        ),
        (
            [
                ([("first", {"location": "Paris"})], [("first", "ok")]),
                ([("next", {"location": "Paris"})], []),
            ],
            True,
        ),
    ],
    ids=[
        "both-success",
        "first-failed",
        "second-failed",
        "first-missing",
        "second-missing",
        "reverse-failure-order",
        "reverse-success-order",
        "same-input-reordered-keys",
        "duplicate-input-batch-failed",
        "same-input-retry",
        "different-input-not-retry",
        "reused-id-same-input-retry",
        "reused-id-different-input",
        "corrected-input-not-retry",
        "later-retry-missing",
    ],
)
def test_same_tool_attempts_require_each_input(turns, pending):
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    history = []
    for turn_index, (calls, results) in enumerate(turns):
        occurrence_ids = {call_id: f"occurrence-{turn_index}-{call_id}" for call_id, _ in calls}
        history.append(
            Message(
                role="assistant",
                author_name=agent,
                contents=[
                    Content.from_function_call(call_id, "suggest_hotels", arguments=args, id=occurrence_ids[call_id])
                    for call_id, args in calls
                ],
            )
        )
        if results:
            history.append(
                Message(
                    role="tool",
                    author_name=agent,
                    contents=[
                        Content.from_function_result(
                            call_id,
                            id=occurrence_ids[call_id],
                            result="fixture" if outcome == "ok" else None,
                            exception="synthetic failure" if outcome == "failed" else None,
                        )
                        for call_id, outcome in results
                    ],
                )
            )
    history.append(Message(role="assistant", author_name=agent, contents=["Completed response."]))
    context = MagenticContext(task="fixture", chat_history=history, participant_descriptions={agent: agent})
    assert bool(manager._pending(context)) is pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ledger_failure", "duplicate_input"),
    [(False, False), (True, False), (False, True)],
    ids=["normal-reset", "ledger-error-reset", "duplicate-input-reset"],
)
async def test_failed_input_remains_pending_across_sdk_reset(ledger_failure, duplicate_input):
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    context = MagenticContext(
        task="fixture",
        participant_descriptions={agent: agent},
        chat_history=[
            Message(
                role="assistant",
                author_name=agent,
                contents=[
                    Content.from_function_call("paris", "suggest_hotels", arguments={"location": "Paris"}),
                    Content.from_function_call(
                        "tokyo", "suggest_hotels", arguments={"location": "Paris" if duplicate_input else "Tokyo"}
                    ),
                ],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[
                    Content.from_function_result("paris", exception="synthetic failure"),
                    Content.from_function_result("tokyo", result="fixture"),
                ],
            ),
            Message(role="assistant", author_name=agent, contents=["Completed response."]),
        ],
    )
    if ledger_failure:
        with patch.object(StandardMagenticManager, "create_progress_ledger", AsyncMock(side_effect=RuntimeError)):
            with pytest.raises(RuntimeError):
                await manager.create_progress_ledger(context)
    else:
        assert manager._pending(context)
    context.reset()
    context.chat_history.extend(
        [
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("tokyo-retry", "suggest_hotels", arguments={"location": "Tokyo"})],
            ),
            Message(
                role="tool", author_name=agent, contents=[Content.from_function_result("tokyo-retry", result="fixture")]
            ),
            Message(role="assistant", author_name=agent, contents=["Completed response."]),
        ]
    )
    assert manager._pending(context)
    context.chat_history.extend(
        [
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("paris-retry", "suggest_hotels", arguments={"location": "Paris"})],
            ),
            Message(
                role="tool", author_name=agent, contents=[Content.from_function_result("paris-retry", result="fixture")]
            ),
            Message(role="assistant", author_name=agent, contents=["Completed response."]),
        ]
    )
    assert not manager._pending(context)


@pytest.mark.parametrize(
    "arguments",
    ["not-json private-sentinel", '["not-an-object"]', '{"date": NaN}'],
    ids=["invalid-json", "non-object", "non-finite-number"],
)
def test_invalid_tool_arguments_fail_without_exposing_contents(arguments):
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    context = MagenticContext(
        task="fixture",
        participant_descriptions={agent: agent},
        chat_history=[
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("call", "suggest_hotels", arguments=arguments)],
            )
        ],
    )
    with pytest.raises(RuntimeError, match="tool call arguments are invalid") as error:
        manager._pending(context)
    assert "private-sentinel" not in "".join(traceback.format_exception(error.value))


@pytest.mark.asyncio
async def test_invalid_tool_evidence_cannot_be_cleared_by_replan():
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    context = MagenticContext(
        task="fixture",
        participant_descriptions={agent: agent},
        chat_history=[
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("call", "suggest_hotels", arguments="invalid")],
            )
        ],
    )
    with pytest.raises(RuntimeError, match="tool call arguments are invalid"):
        manager._pending(context)
    context.reset()
    with pytest.raises(RuntimeError, match=r"tool evidence is invalid\. Please retry\."):
        await manager.replan(context)
    with pytest.raises(RuntimeError, match="required specialist work"):
        await manager.prepare_final_answer(context)


@pytest.mark.parametrize("separate_turn", [False, True], ids=["same-batch", "earlier-result-missing"])
def test_ambiguous_tool_call_id_is_invalid(separate_turn):
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    calls = [
        Content.from_function_call("same", "suggest_hotels", arguments={"location": "Paris"}, id="first"),
        Content.from_function_call("same", "suggest_hotels", arguments={"location": "Tokyo"}, id="second"),
    ]
    history = (
        [Message(role="assistant", author_name=agent, contents=[call]) for call in calls]
        if separate_turn
        else [Message(role="assistant", author_name=agent, contents=calls)]
    )
    context = MagenticContext(task="fixture", participant_descriptions={agent: agent}, chat_history=history)
    with pytest.raises(RuntimeError, match="tool call evidence is invalid"):
        manager._pending(context)


def test_reused_tool_id_rejects_stale_occurrence_result():
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    context = MagenticContext(
        task="fixture",
        participant_descriptions={agent: agent},
        chat_history=[
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("same", "suggest_hotels", arguments={}, id="first")],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[Content.from_function_result("same", id="first", exception="synthetic failure")],
            ),
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("same", "suggest_hotels", arguments={}, id="second")],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[Content.from_function_result("same", id="first", result="fixture")],
            ),
        ],
    )
    with pytest.raises(RuntimeError, match="tool result evidence is invalid"):
        manager._pending(context)


@pytest.mark.parametrize(
    ("first_call", "first_result", "second_call", "second_result"),
    [
        (None, None, "second", "second"),
        ("first", None, "second", "second"),
        ("first", "wrong", "second", "second"),
        ("first", "first", None, "second"),
        ("first", "first", "second", None),
        ("first", "first", "first", "first"),
        ("", "", "second", "second"),
        ("first", "first", "", ""),
    ],
    ids=[
        "first-call-missing",
        "first-result-missing",
        "first-result-mismatch",
        "second-call-missing",
        "second-result-missing",
        "reused-occurrence",
        "first-occurrence-empty",
        "second-occurrence-empty",
    ],
)
def test_reused_tool_id_requires_distinct_matching_occurrences(first_call, first_result, second_call, second_result):
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    context = MagenticContext(
        task="fixture",
        participant_descriptions={agent: agent},
        chat_history=[
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("same", "suggest_hotels", arguments={}, id=first_call)],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[Content.from_function_result("same", id=first_result, exception="synthetic failure")],
            ),
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("same", "suggest_hotels", arguments={}, id=second_call)],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[Content.from_function_result("same", id=second_result, result="fixture")],
            ),
            Message(role="assistant", author_name=agent, contents=["Completed response."]),
        ],
    )
    with pytest.raises(RuntimeError, match="tool (call|result) evidence is invalid"):
        manager._pending(context)


@pytest.mark.parametrize(("call_occurrence", "result_occurrence"), [(None, None), ("call", None), (None, "result")])
def test_nonreused_tool_id_does_not_require_occurrence_ids(call_occurrence, result_occurrence):
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    context = MagenticContext(
        task="fixture",
        participant_descriptions={agent: agent},
        chat_history=[
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("call", "suggest_hotels", arguments={}, id=call_occurrence)],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[Content.from_function_result("call", id=result_occurrence, result="fixture")],
            ),
            Message(role="assistant", author_name=agent, contents=["Completed response."]),
        ],
    )
    try:
        pending = manager._pending(context)
    except RuntimeError as error:
        pytest.fail(f"Non-reused call unexpectedly failed: {error}")
    assert not pending


def test_reused_tool_id_cannot_repeat_an_earlier_occurrence():
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    history = []
    for occurrence in ("first", "second"):
        history.extend(
            [
                Message(
                    role="assistant",
                    author_name=agent,
                    contents=[Content.from_function_call("same", "suggest_hotels", arguments={}, id=occurrence)],
                ),
                Message(
                    role="tool",
                    author_name=agent,
                    contents=[Content.from_function_result("same", id=occurrence, result="fixture")],
                ),
            ]
        )
    history.append(
        Message(
            role="assistant",
            author_name=agent,
            contents=[Content.from_function_call("same", "suggest_hotels", arguments={}, id="first")],
        )
    )
    context = MagenticContext(task="fixture", participant_descriptions={agent: agent}, chat_history=history)
    with pytest.raises(RuntimeError, match="tool call evidence is invalid"):
        manager._pending(context)


@pytest.mark.parametrize(
    ("first_tool", "first_result", "invalid"),
    [
        ("suggest_hotels", "ok", False),
        ("suggest_flights", "ok", False),
        ("suggest_flights", "failed", False),
        ("suggest_hotels", "missing", True),
        ("suggest_flights", "missing", True),
    ],
    ids=["required-first", "optional-first", "optional-failed", "required-pending", "optional-pending"],
)
def test_required_and_optional_tools_can_reuse_completed_id(first_tool, first_result, invalid):
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    second_tool = "suggest_flights" if first_tool == "suggest_hotels" else "suggest_hotels"
    history = [
        Message(
            role="assistant",
            author_name=agent,
            contents=[Content.from_function_call("same", first_tool, arguments={}, id="first")],
        )
    ]
    if first_result != "missing":
        history.append(
            Message(
                role="tool",
                author_name=agent,
                contents=[
                    Content.from_function_result(
                        "same",
                        id="first",
                        result="fixture" if first_result == "ok" else None,
                        exception="synthetic failure" if first_result == "failed" else None,
                    )
                ],
            )
        )
    history.extend(
        [
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("same", second_tool, arguments={}, id="second")],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[Content.from_function_result("same", id="second", result="fixture")],
            ),
            Message(role="assistant", author_name=agent, contents=["Completed response."]),
        ]
    )
    context = MagenticContext(task="fixture", participant_descriptions={agent: agent}, chat_history=history)
    if invalid:
        with pytest.raises(RuntimeError, match="tool call evidence is invalid"):
            manager._pending(context)
    else:
        assert not manager._pending(context)


def test_repeated_tool_result_id_is_invalid():
    agent = "ItineraryAgent"
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={agent},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([agent], ["suggest_hotels"])))
    context = MagenticContext(
        task="fixture",
        participant_descriptions={agent: agent},
        chat_history=[
            Message(
                role="assistant",
                author_name=agent,
                contents=[Content.from_function_call("call", "suggest_hotels", arguments={"location": "Paris"})],
            ),
            Message(
                role="tool",
                author_name=agent,
                contents=[
                    Content.from_function_result("call", exception="synthetic failure"),
                    Content.from_function_result("call", result="fixture"),
                ],
            ),
        ],
    )
    with pytest.raises(RuntimeError, match="tool result evidence is invalid"):
        manager._pending(context)


@pytest.mark.parametrize(
    "plan",
    [
        "not-json synthetic-sensitive-sentinel",
        '{"direct_response":true,"plan":"fixture"}',
        '{"direct_response":false,"requirements":[],"plan":"fixture"}',
        '{"direct_response":true,"requirements":[{"agent":"DestinationAgent","instruction":"fixture",'
        '"required_tools":[]}],"plan":"fixture"}',
        '{"direct_response":false,"requirements":[{"agent":"UnknownAgent","instruction":"fixture",'
        '"required_tools":[]}],"plan":"fixture"}',
        '{"direct_response":false,"requirements":[{"agent":"CustomerQueryAgent","instruction":"fixture",'
        '"required_tools":[]}],"plan":"fixture"}',
        '{"direct_response":false,"requirements":[{"agent":"DestinationAgent","instruction":"fixture",'
        '"required_tools":["suggest_hotels"]}],"plan":"fixture"}',
        '{"direct_response":false,"requirements":[{"agent":"ItineraryAgent","instruction":"fixture",'
        '"required_tools":["unknown"]}],"plan":"fixture"}',
        '{"direct_response":false,"requirements":[{"agent":"DestinationAgent","instruction":"fixture",'
        '"required_tools":[]},{"agent":"DestinationAgent","instruction":"fixture","required_tools":[]}],'
        '"plan":"fixture"}',
        specialist_plan([], []) + " trailing text",
    ],
    ids=[
        "invalid-json",
        "missing-field",
        "missing-requirement",
        "direct-conflict",
        "unknown-agent",
        "missing-tool",
        "destination-tool",
        "unknown-tool",
        "duplicate-agent",
        "trailing-text",
    ],
)
def test_invalid_plan_fails_without_exposing_content(plan):
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={"CustomerQueryAgent", "ItineraryAgent"},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    with pytest.raises(RuntimeError, match="specialist plan is invalid") as error:
        manager._read_plan(Message(role="assistant", contents=[plan + "\n\nFacts:\nfixture"]))
    assert "synthetic-sensitive-sentinel" not in "".join(traceback.format_exception(error.value))


@pytest.mark.asyncio
async def test_invalid_speaker_cannot_authorize_finalization():
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={"CustomerQueryAgent", "ItineraryAgent"},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(json.loads(specialist_plan([], [])))
    context = MagenticContext(task="fixture")
    ledger = MagenticProgressLedger.from_dict(json.loads(progress(False, "UnknownAgent")))
    with patch.object(StandardMagenticManager, "create_progress_ledger", AsyncMock(return_value=ledger)):
        await manager.create_progress_ledger(context)
    with pytest.raises(RuntimeError, match="required specialist work"):
        await manager.prepare_final_answer(context)
    assert manager.finalization_authorized is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_agents", "new_tools", "omitted"),
    [
        (["CustomerQueryAgent"], ["analyze_customer_query"], False),
        (["CustomerQueryAgent", "DestinationAgent"], ["analyze_customer_query"], False),
        ([], [], True),
    ],
    ids=["preserved", "additional-specialist", "omitted-specialist"],
)
async def test_replan_cannot_drop_required_work(new_agents, new_tools, omitted):
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={"CustomerQueryAgent", "ItineraryAgent"},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    manager._plan = SpecialistPlan.model_validate(
        json.loads(specialist_plan(["CustomerQueryAgent"], ["analyze_customer_query"]))
    )
    context = MagenticContext(
        task="fixture",
        participant_descriptions={"CustomerQueryAgent": "customer", "DestinationAgent": "destination"},
    )
    message = Message(role="assistant", contents=[specialist_plan(new_agents, new_tools) + "\n\nFacts:\nfixture"])
    with patch.object(StandardMagenticManager, "replan", AsyncMock(return_value=message)):
        if omitted:
            with pytest.raises(RuntimeError, match="omitted required specialist work"):
                await manager.replan(context)
        else:
            result = await manager.replan(context)
            assert result.text.startswith("Complete the requested work.")
            assert '"direct_response"' not in result.text


@pytest.mark.asyncio
async def test_plan_rejects_unavailable_specialist():
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents={"CustomerQueryAgent", "ItineraryAgent"},
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    context = MagenticContext(task="fixture", participant_descriptions={"CustomerQueryAgent": "customer"})
    message = Message(
        role="assistant",
        contents=[specialist_plan(["DestinationAgent"], []) + "\n\nFacts:\nfixture"],
    )
    with patch.object(StandardMagenticManager, "plan", AsyncMock(return_value=message)):
        with pytest.raises(RuntimeError, match="unavailable agent"):
            await manager.plan(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agents", "tools", "unavailable"),
    [
        ([], [], False),
        (["DestinationAgent"], [], False),
        (["ItineraryAgent"], [], False),
        (["CustomerQueryAgent"], ["analyze_customer_query"], True),
        (["ItineraryAgent"], ["suggest_hotels"], True),
    ],
    ids=["greeting", "destination", "activities-only", "customer-tool", "itinerary-tool"],
)
async def test_required_tool_must_be_available(agents, tools, unavailable):
    manager = GuardedMagenticManager(
        Agent(client=ScriptedClient([]), name="MagenticManager"),
        available_tool_agents=set(),
        max_round_count=8,
        max_stall_count=2,
        max_reset_count=1,
    )
    context = MagenticContext(task="fixture", participant_descriptions={name: name for name in agents})
    message = Message(role="assistant", contents=[specialist_plan(agents, tools) + "\n\nFacts:\nfixture"])
    with patch.object(StandardMagenticManager, "plan", AsyncMock(return_value=message)):
        if unavailable:
            with pytest.raises(RuntimeError, match="required specialist MCP tool is unavailable"):
                await manager.plan(context)
        else:
            result = await manager.plan(context)
            assert result.text.startswith("Complete the requested work.")
            assert '"direct_response"' not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", ["round", "reset", "invalid-speaker", "replan-omission"])
async def test_sdk_completion_bypasses_are_streamed_as_errors(monkeypatch, exit_path):
    fixture_tool = tool(lambda: "fixture", name="analyze_customer_query")
    metadata = {
        "customer-query": {"name": "Customer Query", "url": "http://customer.invalid/mcp"},
        "itinerary-planning": {"name": "Itinerary Planning", "url": "http://itinerary.invalid/mcp/"},
    }
    monkeypatch.setattr(magentic_workflow.tool_registry, "get_server_metadata", metadata.get)
    monkeypatch.setattr(magentic_workflow, "MCPStreamableHTTPTool", lambda **kwargs: fixture_tool)
    original_manager = magentic_workflow.GuardedMagenticManager

    def limited_manager(**kwargs):
        if exit_path == "round":
            kwargs["max_round_count"] = 1
        elif exit_path in {"reset", "replan-omission"}:
            kwargs["max_stall_count"] = 0
        return original_manager(**kwargs)

    monkeypatch.setattr(magentic_workflow, "GuardedMagenticManager", limited_manager)
    if exit_path == "round":
        client = ScriptedClient(["CustomerQueryAgent"], ["analyze_customer_query"])
    elif exit_path in {"reset", "replan-omission"}:
        client = ScriptedClient([])
        plan = specialist_plan(["CustomerQueryAgent"], ["analyze_customer_query"])
        ledger = json.loads(progress(False, "CustomerQueryAgent"))
        ledger["is_in_loop"]["answer"] = True
        ledger["is_progress_being_made"]["answer"] = False
        updated_plan = specialist_plan([], []) if exit_path == "replan-omission" else plan
        client.steps = deque(
            [
                ("manager", "Facts"),
                ("manager", plan),
                ("manager", json.dumps(ledger)),
                ("manager", "Updated facts"),
                ("manager", updated_plan),
            ]
        )
    else:
        client = ScriptedClient([])
        client.steps = deque(
            [("manager", "Facts"), ("manager", specialist_plan([], []))]
            + [("manager", progress(False, "UnknownAgent"))] * 3
        )

    orchestrator = magentic_workflow.MagenticTravelOrchestrator()
    orchestrator.chat_client = client
    with patch("agent_framework.observability.configure_otel_providers"):
        main = importlib.import_module("src.main")
    monkeypatch.setattr(main, "magentic_orchestrator", orchestrator)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as http:
        response = await http.post("/api/chat", json={"message": "fixture"})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]

    assert response.status_code == 200
    assert any(event.get("error") for event in events)
    assert not any(event.get("event") in ("FinalResult", "Complete") for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_plan", ["not-json", '{"direct_response":false,"requirements":[],"plan":"fixture"}'])
async def test_invalid_sdk_plan_is_streamed_as_error(monkeypatch, invalid_plan):
    fixture_tool = tool(lambda: "fixture", name="analyze_customer_query")
    metadata = {
        "customer-query": {"name": "Customer Query", "url": "http://customer.invalid/mcp"},
        "itinerary-planning": {"name": "Itinerary Planning", "url": "http://itinerary.invalid/mcp/"},
    }
    monkeypatch.setattr(magentic_workflow.tool_registry, "get_server_metadata", metadata.get)
    monkeypatch.setattr(magentic_workflow, "MCPStreamableHTTPTool", lambda **kwargs: fixture_tool)
    client = ScriptedClient([])
    client.steps = deque([("manager", "Facts"), ("manager", invalid_plan)])
    orchestrator = magentic_workflow.MagenticTravelOrchestrator()
    orchestrator.chat_client = client
    events = [event async for event in orchestrator.process_request_stream("fixture")]

    assert any(event.get("type") == "error" for event in events)
    assert not any(event.get("event") == "FinalResult" for event in events)


@pytest.mark.asyncio
async def test_concurrent_mixed_intent_tool_evidence_is_request_local(monkeypatch):
    invoked = []

    def customer(query: str) -> str:
        invoked.append(query)
        if query == "case-A":
            raise ValueError("synthetic tool failure")
        return "Customer analysis."

    customer_tool = tool(customer, name="analyze_customer_query")
    itinerary_tool = tool(lambda: "fixture", name="suggest_hotels")
    metadata = {
        "customer-query": {"name": "Customer Query", "url": "http://customer.invalid/mcp"},
        "itinerary-planning": {"name": "Itinerary Planning", "url": "http://itinerary.invalid/mcp/"},
    }
    monkeypatch.setattr(magentic_workflow.tool_registry, "get_server_metadata", metadata.get)
    monkeypatch.setattr(
        magentic_workflow,
        "MCPStreamableHTTPTool",
        lambda **kwargs: customer_tool if kwargs["name"] == "Customer Query" else itinerary_tool,
    )
    original_manager = magentic_workflow.GuardedMagenticManager

    def limited_manager(**kwargs):
        kwargs["max_round_count"] = 3
        return original_manager(**kwargs)

    monkeypatch.setattr(magentic_workflow, "GuardedMagenticManager", limited_manager)
    customer_plan = specialist_plan(["CustomerQueryAgent"], ["analyze_customer_query"])
    mixed_plan = specialist_plan(["CustomerQueryAgent", "DestinationAgent"], ["analyze_customer_query"])
    steps = {
        "A": deque([
            ("manager", "Facts"),
            ("manager", customer_plan),
            ("manager", progress(True, "DestinationAgent")),
            ("CustomerQueryAgent", {"tool": "analyze_customer_query"}),
            ("CustomerQueryAgent", "Customer response."),
            ("manager", progress(True, "DestinationAgent")),
            ("CustomerQueryAgent", "Customer response."),
            ("manager", progress(True, "DestinationAgent")),
            ("CustomerQueryAgent", "Customer response."),
        ]),
        "B": deque([
            ("manager", "Facts"),
            ("manager", mixed_plan),
            ("manager", progress(True, "DestinationAgent")),
            ("CustomerQueryAgent", {"tool": "analyze_customer_query"}),
            ("CustomerQueryAgent", "Customer response."),
            ("manager", progress(True, "CustomerQueryAgent")),
            ("DestinationAgent", "Destination response."),
            ("manager", progress(True, "DestinationAgent")),
            ("manager", "Final answer."),
        ]),
    }
    started = set()
    both_started = asyncio.Event()

    async def handler(request):
        body = json.loads(request.content)
        messages = json.dumps(body["messages"])
        case = "A" if "case-A" in messages else "B" if "case-B" in messages else None
        assert case is not None
        if case not in started:
            started.add(case)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 3)
        role, response = steps[case].popleft()
        if role == "CustomerQueryAgent":
            assert "You are a Customer Query Agent" in messages
        elif role == "DestinationAgent":
            assert "You are a Destination Recommendation Agent" in messages
        is_tool_call = isinstance(response, dict)
        function_call = {
            "name": response["tool"] if is_tool_call else "",
            "arguments": json.dumps({"query": f"case-{case}"}),
        }
        finish_reason = "tool_calls" if is_tool_call else "stop"
        if body.get("stream"):
            delta = {"role": "assistant"}
            if is_tool_call:
                delta["tool_calls"] = [
                    {"index": 0, "id": "shared-call", "type": "function", "function": function_call}
                ]
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
        return httpx.Response(
            200,
            json={
                "id": "chat-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": response}, "finish_reason": "stop"}
                ],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    raw_client = AsyncOpenAI(api_key="fixture", base_url="https://example.invalid/v1", http_client=http)
    orchestrator = magentic_workflow.MagenticTravelOrchestrator()
    orchestrator.chat_client = OpenAIChatCompletionClient(model="fixture", async_client=raw_client)

    async def execute(case):
        return [event async for event in orchestrator.process_request_stream(f"case-{case}")]

    try:
        first, second = await asyncio.wait_for(asyncio.gather(execute("A"), execute("B")), 15)
    finally:
        await raw_client.close()

    assert started == {"A", "B"}
    assert sorted(invoked) == ["case-A", "case-B"]
    assert all(not remaining for remaining in steps.values())
    assert any(event.get("type") == "error" for event in first)
    assert not any(event.get("event") == "FinalResult" for event in first)
    assert any(event.get("event") == "FinalResult" for event in second)
    assert not any(event.get("type") == "error" for event in second)
