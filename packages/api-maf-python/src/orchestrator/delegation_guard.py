"""Magentic manager that verifies planned specialist and tool work."""

import json
from typing import Literal

from agent_framework import Message, SupportsAgentRun
from agent_framework.orchestrations import (
    MagenticContext,
    MagenticProgressLedger,
    MagenticProgressLedgerItem,
    StandardMagenticManager,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentName = Literal["CustomerQueryAgent", "DestinationAgent", "ItineraryAgent"]

ALLOWED_TOOLS = {
    "CustomerQueryAgent": {"analyze_customer_query"},
    "DestinationAgent": set(),
    "ItineraryAgent": {"suggest_hotels", "suggest_flights"},
}

PLAN_PROMPT = """
Plan the original request using the team below:
{team}

Return only a JSON object with this shape:
{{"direct_response": false, "requirements": [
  {{"agent": "CustomerQueryAgent", "instruction": "task for this specialist",
    "required_tools": ["analyze_customer_query"]}}
], "plan": "brief plan"}}

Choose specialists by the meaning of the user's request, not by isolated words. Use CustomerQueryAgent and
analyze_customer_query for customer inquiry analysis. Use DestinationAgent without tools for destination
recommendations. Use ItineraryAgent for itinerary planning. Require suggest_hotels for a requested hotel lookup and
suggest_flights for a requested flight lookup, including both when both are requested. An activities-only itinerary
needs no hotel or flight tool. Include every specialist and tool needed for multi-part requests, combining tasks for
the same specialist into one requirement. List requirements in the order they should run.
Do not select unrelated specialists.
For a greeting or other request that needs no specialist, set direct_response to true and requirements to an empty list.
Otherwise set direct_response to false. Each instruction must describe the requested task for that specialist.
The plan must be readable and name each required specialist.
"""

FULL_PLAN_PROMPT = "{plan}\n\nFacts:\n{facts}\n\nRequest:\n{task}\n\nTeam:\n{team}"


class SpecialistRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent: AgentName
    instruction: str = Field(min_length=1)
    required_tools: list[str]

    @model_validator(mode="after")
    def validate_requirement(self) -> "SpecialistRequirement":
        if not self.instruction.strip():
            raise ValueError("A specialist instruction is required")
        allowed = ALLOWED_TOOLS[self.agent]
        if len(set(self.required_tools)) != len(self.required_tools):
            raise ValueError("Duplicate tools are not allowed")
        if not set(self.required_tools).issubset(allowed):
            raise ValueError("A required tool is not available to the specialist")
        if self.agent == "CustomerQueryAgent" and not self.required_tools:
            raise ValueError("This specialist requires an MCP tool")
        return self


class SpecialistPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    direct_response: bool
    requirements: list[SpecialistRequirement]
    plan: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_plan(self) -> "SpecialistPlan":
        if not self.plan.strip() or self.direct_response == bool(self.requirements):
            raise ValueError("The direct-response choice and requirements disagree")
        names = [requirement.agent for requirement in self.requirements]
        if len(set(names)) != len(names):
            raise ValueError("Combine tasks for the same specialist")
        return self


class GuardedMagenticManager(StandardMagenticManager):
    def __init__(
        self,
        agent: SupportsAgentRun,
        *,
        available_tool_agents: set[str],
        max_round_count: int,
        max_stall_count: int,
        max_reset_count: int,
    ):
        super().__init__(
            agent,
            task_ledger_plan_prompt=PLAN_PROMPT,
            task_ledger_plan_update_prompt=PLAN_PROMPT,
            task_ledger_full_prompt=FULL_PLAN_PROMPT,
            max_round_count=max_round_count,
            max_stall_count=max_stall_count,
            max_reset_count=max_reset_count,
        )
        self._available_tool_agents = frozenset(available_tool_agents)
        self._plan: SpecialistPlan | None = None
        self._last_progress_satisfied = False
        self.finalization_authorized = False
        self._evidence_reset_count = 0
        self._previous_unresolved: set[tuple[str, str, str]] = set()
        self._unresolved: set[tuple[str, str, str]] = set()
        self._invalid_evidence = False

    def _read_plan(self, message: Message) -> tuple[SpecialistPlan, str]:
        try:
            text = message.text.lstrip()
            value, end = json.JSONDecoder().raw_decode(text)
            if not text[end:].lstrip().startswith("Facts:\n"):
                raise ValueError("Unexpected content after specialist plan")
            return SpecialistPlan.model_validate(value), text[end:]
        except (ValueError, TypeError):
            raise RuntimeError("Magentic specialist plan is invalid") from None

    def _check_availability(self, plan: SpecialistPlan, context: MagenticContext) -> None:
        if any(item.agent not in context.participant_descriptions for item in plan.requirements):
            raise RuntimeError("Magentic specialist plan names an unavailable agent")
        if any(item.required_tools and item.agent not in self._available_tool_agents for item in plan.requirements):
            raise RuntimeError("A required specialist MCP tool is unavailable")

    async def plan(self, magentic_context: MagenticContext) -> Message:
        message = await super().plan(magentic_context)
        self._plan, details = self._read_plan(message)
        self._check_availability(self._plan, magentic_context)
        self._last_progress_satisfied = False
        self.finalization_authorized = False
        return Message(role=message.role, contents=[self._plan.plan + details], author_name=message.author_name)

    async def replan(self, magentic_context: MagenticContext) -> Message:
        if self._invalid_evidence:
            raise RuntimeError("Magentic tool evidence is invalid. Please retry.")
        message = await super().replan(magentic_context)
        new_plan, details = self._read_plan(message)
        if self._plan is None:
            raise RuntimeError("Magentic specialist plan is missing")
        new_requirements = {item.agent: set(item.required_tools) for item in new_plan.requirements}
        if any(
            item.agent not in new_requirements or not set(item.required_tools).issubset(new_requirements[item.agent])
            for item in self._plan.requirements
        ):
            raise RuntimeError("Magentic replan omitted required specialist work")
        self._check_availability(new_plan, magentic_context)
        self._plan = new_plan
        self._last_progress_satisfied = False
        self.finalization_authorized = False
        return Message(role=message.role, contents=[new_plan.plan + details], author_name=message.author_name)

    def _pending(self, context: MagenticContext) -> list[SpecialistRequirement]:
        if self._plan is None:
            raise RuntimeError("Magentic specialist plan is missing")

        if context.reset_count > self._evidence_reset_count:
            self._previous_unresolved = self._unresolved.copy()
            self._evidence_reset_count = context.reset_count

        required_tools = {(item.agent, tool) for item in self._plan.requirements for tool in item.required_tools}
        required_agents = {agent for agent, _ in required_tools}
        calls: dict[tuple[str, str], tuple[tuple[str, str, str] | None, int, str | None]] = {}
        tool_attempts: dict[tuple[str, str, str], tuple[int, dict[str, int]]] = {}
        seen_results: dict[tuple[str, str], str | None] = {}
        occurrence_ids: dict[tuple[str, str], set[str]] = {}
        reused_calls: set[tuple[str, str]] = set()
        responses: dict[str, int] = {}
        for index, message in enumerate(context.chat_history):
            author = message.author_name
            if author is None or author not in ALLOWED_TOOLS:
                continue
            for content in message.contents:
                if message.role == "assistant" and content.type == "text" and content.text and content.text.strip():
                    responses[author] = index
                elif message.role == "assistant" and content.type == "function_call":
                    if not content.name:
                        if author in required_agents:
                            self._invalid_evidence = True
                            raise RuntimeError("Magentic tool call evidence is invalid")
                        continue
                    if author not in required_agents:
                        continue
                    if not content.call_id:
                        self._invalid_evidence = True
                        raise RuntimeError("Magentic tool call evidence is invalid")
                    call_id = (author, content.call_id)
                    if call_id in calls:
                        previous_occurrence = calls[call_id][2]
                        if (
                            call_id not in seen_results
                            or not previous_occurrence
                            or seen_results[call_id] != previous_occurrence
                            or not content.id
                            or content.id in occurrence_ids[call_id]
                        ):
                            self._invalid_evidence = True
                            raise RuntimeError("Magentic tool call evidence is invalid")
                        del seen_results[call_id]
                        reused_calls.add(call_id)
                    else:
                        occurrence_ids[call_id] = set()
                    if content.id:
                        occurrence_ids[call_id].add(content.id)
                    if (author, content.name) not in required_tools:
                        calls[call_id] = (None, index, content.id)
                        continue
                    try:
                        arguments = content.arguments
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        if arguments is None:
                            arguments = {}
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object")
                        arguments_key = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    except (TypeError, ValueError):
                        self._invalid_evidence = True
                        raise RuntimeError("Magentic tool call arguments are invalid") from None
                    key = (author, content.name, arguments_key)
                    if key not in tool_attempts or tool_attempts[key][0] != index:
                        tool_attempts[key] = (index, {})
                    tool_attempts[key][1][content.call_id] = -1
                    calls[call_id] = (key, index, content.id)
                elif message.role == "tool" and content.type == "function_result" and content.call_id:
                    call = calls.get((author, content.call_id))
                    if call is None:
                        continue
                    if (author, content.call_id) in seen_results:
                        self._invalid_evidence = True
                        raise RuntimeError("Magentic tool result evidence is invalid")
                    key, call_index, occurrence_id = call
                    if (author, content.call_id) in reused_calls and (
                        not occurrence_id or not content.id or occurrence_id != content.id
                    ):
                        self._invalid_evidence = True
                        raise RuntimeError("Magentic tool result evidence is invalid")
                    seen_results[(author, content.call_id)] = content.id
                    if key is None or tool_attempts[key][0] != call_index:
                        continue
                    if (
                        self._usable_tool_result(key[1], content.result)
                        and content.exception is None
                        and content.status not in {"error", "failed"}
                    ):
                        tool_attempts[key][1][content.call_id] = index
                    else:
                        tool_attempts[key][1][content.call_id] = -1

        self._unresolved = self._previous_unresolved.copy()
        for key, (_, attempts) in tool_attempts.items():
            if all(result_index >= 0 for result_index in attempts.values()):
                self._unresolved.discard(key)
            else:
                self._unresolved.add(key)

        pending = []
        for requirement in self._plan.requirements:
            response_index = responses.get(requirement.agent, -1)
            incomplete = response_index < 0
            for tool in requirement.required_tools:
                tool_key = (requirement.agent, tool)
                attempts = [results for key, (_, results) in tool_attempts.items() if key[:2] == tool_key]
                if (
                    not attempts
                    or any(key[:2] == tool_key for key in self._unresolved)
                    or any(
                        result_index < 0 or result_index >= response_index
                        for group in attempts
                        for result_index in group.values()
                    )
                ):
                    incomplete = True
            if incomplete:
                pending.append(requirement)
        return pending

    @staticmethod
    def _usable_tool_result(name: str, result: object) -> bool:
        while isinstance(result, str):
            text = result.strip()
            if not text:
                return False
            if name == "suggest_flights" and text.casefold() == "error: return_date must be after departure_date":
                return False
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                return True
        if result is None or isinstance(result, dict) and not result:
            return False
        return True

    async def create_progress_ledger(self, magentic_context: MagenticContext) -> MagenticProgressLedger:
        pending = self._pending(magentic_context)
        ledger = await super().create_progress_ledger(magentic_context)
        if pending:
            requirement = pending[0]
            instruction = f"User request:\n{magentic_context.task}\n\nAssigned task:\n{requirement.instruction}"
            if requirement.required_tools:
                instruction += f" Use the required MCP tool(s): {', '.join(requirement.required_tools)}."
            ledger.is_request_satisfied = MagenticProgressLedgerItem(reason="Required work remains.", answer=False)
            ledger.next_speaker = MagenticProgressLedgerItem(reason="Required specialist.", answer=requirement.agent)
            ledger.instruction_or_question = MagenticProgressLedgerItem(
                reason="Complete required work.", answer=instruction
            )
        self._last_progress_satisfied = ledger.is_request_satisfied.answer is True
        return ledger

    async def prepare_final_answer(self, magentic_context: MagenticContext) -> Message:
        if self._invalid_evidence or not self._last_progress_satisfied or self._pending(magentic_context):
            raise RuntimeError("Magentic workflow ended before required specialist work completed")
        message = await super().prepare_final_answer(magentic_context)
        self.finalization_authorized = True
        return message
