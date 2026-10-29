#!/usr/bin/env python3
"""Exercise the app and inspect deployed CloudHealth models without writes.

Examples:
  python3 infra/healthmodel-evaluate.py readiness --environment anbotrai
  python3 infra/healthmodel-evaluate.py usage --environment anbotrai
  python3 infra/healthmodel-evaluate.py snapshot --environment anbotrai --label baseline

Alternatively, --config accepts a JSON object with ui, langchain, llamaindex,
python, subscription_id and resource_group values. It can supplement an
explicitly selected azd environment or supply values for offline fixtures.
Output is JSON; no response bodies, prompts, tool URLs, or azd values are
included in the output. Usage checks only the selected backends and the UI;
partial scope does not establish full-application readiness. HTTP-200 stream
failures count explicit workflow or tool errors, not incomplete responses.
An explicit error seen before a size cap is counted as a stream failure.
Python destination requests do not require a destination MCP tool; their
agent routing is unverified unless confirmed independently.
Stream statistics count observed HTTP body bytes; event bytes include frame
delimiters. Unknown event types are grouped as "other"; incomplete frames are
not attributed, and totals are lower bounds when reading stops early. Tool
events use only recognized domain IDs, "task", or "other". Output nonempty
flags use the same content check as tool verification; error flags indicate
only an explicitly reported error. A null flag means its value could not be
inspected safely.
Snapshots compare entity names, relationship edges and signal placement with
the intended model; coverage counts only expected signals.
Readback compares configured health properties and expected role assignments
against a hashed contract. It does not enumerate additional role assignments;
signal health is inspected separately by snapshot.
"""

from __future__ import annotations

import argparse
import codecs
import concurrent.futures
import datetime as dt
import hashlib
import html.parser
import http.client
import json
import math
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal


BACKENDS = ("langchain", "llamaindex", "python")
COMPONENTS = ("interface", "assistant", "preferences", "destinations", "itinerary", "generation")
FLOW = "plan-trip"
RESOURCE_BINDINGS = (
    ("interface", "interface-resource", None, False),
    ("assistant", "assistant-langchain-resource", "langchain", False),
    ("assistant", "assistant-llamaindex-resource", "llamaindex", False),
    ("assistant", "assistant-python-resource", "python", False),
    ("preferences", "preferences-resource", None, False),
    ("destinations", "destinations-mcp-resource", None, False),
    ("destinations", "destinations-ai-resource", None, True),
    ("itinerary", "itinerary-resource", None, False),
    ("generation", "generation-resource", None, True),
)
TOOLS = ("customer-query", "destination-recommendation", "itinerary-planning")
MODELS = {
    "anbotrai-platform": "platform",
    "anbotrai-stability": "stability",
    "anbotrai-capacity": "capacity",
    "anbotrai-experience": "experience",
}
PROMPTS = (
    (
        "preferences",
        "customer-query",
        "Use the customer inquiry tool to analyze a fictional traveler who likes museums, "
        "walkable cities, and a moderate budget.",
    ),
    (
        "destinations",
        "destination-recommendation",
        "Use the destination recommendation tool to suggest a cultural destination "
        "for a fictional traveler with a moderate budget.",
    ),
    (
        "itinerary",
        "itinerary-planning",
        "Use the itinerary planning tool to suggest hotels in Paris for check-in "
        "2030-05-10 and check-out 2030-05-13, then outline a three-day visit.",
    ),
)
ENV_KEYS = {
    "AZURE_ENV_NAME",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_RESOURCE_GROUP",
    "AZURE_RESOURCE_UI_ANGULAR_ID",
    "SERVICE_UI_ANGULAR_ENDPOINT_URL",
    "NG_API_URL_LANGCHAIN_JS",
    "NG_API_URL_LLAMAINDEX_TS",
    "NG_API_URL_MAF_PYTHON",
}
API_VERSION = "2026-11-01"
ROLE_API_VERSION = "2022-04-01"
RESOURCE_HEALTH_SIGNAL = "resourcehealth-availabilitystate"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_COMMAND_BYTES = 8 * 1024 * 1024
MAX_REQUEST_TIMEOUT = 115.0
DEFAULT_REQUEST_TIMEOUT = 110.0
KNOWN_STATES = {"Healthy", "Degraded", "Unhealthy"}
SAFE_EVENT_TYPES = frozenset({
    "llm_token", "tool_start", "tool_end", "agent_complete", "workflow_error", "error",
    "on_chain_start", "on_chain_stream", "on_chain_end",
    "on_chat_model_start", "on_chat_model_stream", "on_chat_model_end",
    "on_llm_start", "on_llm_stream", "on_llm_end", "on_tool_start", "on_tool_end",
    "on_retriever_start", "on_retriever_end", "on_prompt_start", "on_prompt_end",
    "on_parser_start", "on_parser_stream", "on_parser_end", "on_custom_event",
    "llamaindex-start", "llamaindex-stop", "AgentToolCall", "AgentToolCallResult",
    "AgentOutput", "AgentInput", "AgentSetup", "AgentStepEvent", "ToolCallsEvent", "ToolResultsEvent",
    "WorkflowStarted", "AgentDelta", "AgentMessage", "FinalResult", "WorkflowComplete", "Complete",
    "OrchestratorUserTask", "OrchestratorInstruction", "ServiceError", "Error",
    *(str(number) for number in range(10)),
})
FRAME_DELIMITER = re.compile(rb"\r?\n\r?\n")


class EvaluationError(Exception):
    """A safe, concise failure suitable for an evaluation result."""

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class AngularDocument(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.started: set[str] = set()
        self.ended: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.started.add(tag)

    def handle_endtag(self, tag: str) -> None:
        self.ended.add(tag)

    def is_application(self) -> bool:
        return {"html", "body", "app-root"} <= self.started and {"html", "body", "app-root"} <= self.ended


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def milliseconds(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def safe_error(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://[^\s\"']+", "[redacted-url]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+", "authorization=[redacted]", text)
    text = re.sub(
        r"(?i)(access[_-]?token|password|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    return text if len(text) <= 500 else text[:500] + "..."


def command(args: list[str], timeout: float = 120.0) -> str:
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise EvaluationError(f"{args[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise EvaluationError(f"{args[0]} command timed out") from exc
    if len(result.stdout) > MAX_COMMAND_BYTES or len(result.stderr) > MAX_COMMAND_BYTES:
        raise EvaluationError(f"{args[0]} output exceeded the limit")
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").lower()
        if "resourcenotfound" in error or "resourcegroupnotfound" in error or "(notfound)" in error:
            reason = "resource not found"
        elif "authorizationfailed" in error or "forbidden" in error:
            reason = "access denied"
        elif "az login" in error or "authentication" in error:
            reason = "authentication required"
        else:
            reason = f"{args[0]} command failed"
        raise EvaluationError(reason)
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvaluationError(f"{args[0]} returned invalid text") from exc


def environment_values(environment: str) -> dict[str, str]:
    output = command(["azd", "env", "get-values", "--environment", environment, "--no-prompt"])
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, encoded = line.partition("=")
        if not separator or key not in ENV_KEYS:
            continue
        try:
            parts = shlex.split(encoded)
        except ValueError as exc:
            raise EvaluationError(f"invalid azd value for {key}") from exc
        if len(parts) != 1:
            raise EvaluationError(f"invalid azd value for {key}")
        values[key] = parts[0]
    if values.get("AZURE_ENV_NAME") and values["AZURE_ENV_NAME"] != environment:
        raise EvaluationError("azd returned a different environment")
    return values


def load_config(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        raw = Path(path).read_bytes()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise EvaluationError("configuration exceeds the size limit")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("could not read JSON configuration") from exc
    if not isinstance(value, dict):
        raise EvaluationError("configuration must be a JSON object")
    allowed = {"ui", *BACKENDS, "subscription_id", "resource_group"}
    if any(key not in allowed or not isinstance(item, str) for key, item in value.items()):
        raise EvaluationError("configuration contains an unsupported key or non-string value")
    return value


def resource_scope(resource_id: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.App/containerApps/ui-angular",
        resource_id,
        flags=re.IGNORECASE,
    )
    return (match.group(1), match.group(2)) if match else None


def scope(values: dict[str, str], config: dict[str, str]) -> tuple[str, str]:
    ui_scope = resource_scope(values.get("AZURE_RESOURCE_UI_ANGULAR_ID", ""))
    subscription = config.get("subscription_id") or values.get("AZURE_SUBSCRIPTION_ID") or (
        ui_scope[0] if ui_scope else ""
    )
    resource_group = config.get("resource_group") or values.get("AZURE_RESOURCE_GROUP") or (
        ui_scope[1] if ui_scope else ""
    )
    try:
        subscription = str(uuid.UUID(subscription))
    except ValueError as exc:
        raise EvaluationError("a valid subscription_id is required") from exc
    if not re.fullmatch(r"[a-zA-Z0-9._()\-]{1,90}", resource_group) or resource_group.endswith("."):
        raise EvaluationError("a valid resource_group is required")
    if ui_scope and (
        ui_scope[0].lower() != subscription.lower() or ui_scope[1].lower() != resource_group.lower()
    ):
        raise EvaluationError("configured scope does not match the deployed UI resource")
    return subscription, resource_group


def base_url(value: str, name: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
        )
        _ = parsed.port
    except ValueError as exc:
        raise EvaluationError(f"invalid {name} endpoint") from exc
    if not valid:
        raise EvaluationError(f"invalid {name} endpoint")
    return value.rstrip("/")


def endpoints(
    values: dict[str, str], config: dict[str, str], backends: Sequence[str] = BACKENDS
) -> dict[str, str]:
    candidates = {
        "ui": config.get("ui") or values.get("SERVICE_UI_ANGULAR_ENDPOINT_URL", ""),
        "langchain": config.get("langchain") or values.get("NG_API_URL_LANGCHAIN_JS", ""),
        "llamaindex": config.get("llamaindex") or values.get("NG_API_URL_LLAMAINDEX_TS", ""),
        "python": config.get("python") or values.get("NG_API_URL_MAF_PYTHON", ""),
    }
    found = {"ui": candidates["ui"], **{backend: candidates[backend] for backend in backends}}
    if not found["ui"] and values.get("AZURE_RESOURCE_UI_ANGULAR_ID"):
        resource_id = values["AZURE_RESOURCE_UI_ANGULAR_ID"]
        if not resource_scope(resource_id):
            raise EvaluationError("invalid deployed UI resource ID")
        subscription, _ = scope(values, config)
        fqdn = command(
            [
                "az", "containerapp", "show", "--ids", resource_id,
                "--subscription", subscription, "--query", "properties.configuration.ingress.fqdn",
                "--output", "tsv", "--only-show-errors",
            ]
        ).strip()
        if fqdn and fqdn != "None":
            found["ui"] = "https://" + fqdn
    missing = [key for key, value in found.items() if not value]
    if missing:
        raise EvaluationError("missing endpoints: " + ", ".join(missing))
    return {name: base_url(value, name) for name, value in found.items()}


def read_response(response: Any, started: float, stream: ChatStream | None = None) -> tuple[bytes, float | None]:
    first = response.read(1)
    ttfb = milliseconds(started) if first else None
    parts = [] if stream is not None else [first]
    if stream is not None:
        stream.ttfb_ms = ttfb
        stream.feed(first)
    size = len(first)
    while True:
        part = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
        if not part:
            break
        size += len(part)
        if stream is not None:
            stream.feed(part)
        if size > MAX_RESPONSE_BYTES:
            raise EvaluationError("response exceeded the size limit", category="incomplete_response")
        if stream is None:
            parts.append(part)
    if getattr(response, "length", None) not in (None, 0):
        raise EvaluationError("truncated HTTP response", category="incomplete_response")
    return b"".join(parts), ttfb


def selected_tools(data: Any) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("tools"), list):
        raise EvaluationError("tool discovery returned an invalid response")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in data["tools"]:
        if not isinstance(entry, dict) or entry.get("id") not in TOOLS:
            continue
        if entry["id"] in by_id:
            raise EvaluationError("tool discovery returned duplicate required servers")
        by_id[entry["id"]] = entry
    selected: list[dict[str, Any]] = []
    aliases: dict[str, list[str]] = {}
    for tool_id in TOOLS:
        entry = by_id.get(tool_id)
        if not entry or entry.get("reachable") is not True:
            raise EvaluationError(f"required tool server is unreachable: {tool_id}")
        definitions = entry.get("tools")
        if not isinstance(definitions, list):
            raise EvaluationError(f"required tool definitions are missing: {tool_id}")
        names = [
            item["name"] for item in definitions
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].strip()
            and not item["name"].lower().startswith("echo")
        ]
        if not names:
            raise EvaluationError(f"required tool definitions are empty: {tool_id}")
        selected.append({"id": tool_id})
        aliases[tool_id] = names
    return selected, aliases


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(text_content(item) for item in value).strip()
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"].strip()
        if "content" in value:
            return text_content(value["content"])
    return ""


def useful_text(value: Any) -> bool:
    text = text_content(value)
    placeholders = {"task completed", "workflow completed", "request processed successfully"}
    return len(text) >= 15 and text.lower().rstrip(".! ") not in placeholders


def langchain_final(data: Any) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return False
    for message in reversed(data["messages"]):
        if not isinstance(message, dict):
            continue
        identity = message.get("id")
        kind = str(message.get("type", "")).lower()
        if isinstance(identity, list):
            identity = identity[-1] if identity else ""
        if "aimessage" in str(identity).lower() or kind in {"ai", "assistant"}:
            kwargs = message.get("kwargs")
            return useful_text(kwargs.get("content") if isinstance(kwargs, dict) else message.get("content"))
    return False


def error_in_tool_result(value: Any) -> bool:
    if isinstance(value, dict):
        if (
            value.get("isError") is True or value.get("is_error") is True
            or value.get("status") == "error" or value.get("error")
        ):
            return True
        return any(error_in_tool_result(item) for item in value.values())
    if isinstance(value, list):
        return any(error_in_tool_result(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^\s*(error|exception|invalid input)\s*:", value, flags=re.IGNORECASE))
    return False


def has_tool_output(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(has_tool_output(item) for item in value)
    if isinstance(value, dict):
        for key in ("kwargs", "result", "output", "content", "text"):
            if key in value:
                return has_tool_output(value[key])
        return bool(set(value) - {"id", "name", "type", "status", "isError", "is_error", "error"})
    return value is not None


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def match_tool(name: Any, aliases: dict[str, list[str]]) -> str | None:
    if not isinstance(name, str):
        return None
    normalized = normalize_name(name)
    matches = {
        tool_id for tool_id, names in aliases.items()
        for alias in names
        if normalize_name(alias) and normalized.endswith(normalize_name(alias))
    }
    return next(iter(matches)) if len(matches) == 1 else None


def event_names(event: dict[str, Any]) -> set[str]:
    nested = event.get("event")
    names = [event.get("type"), nested]
    if isinstance(nested, dict):
        names.extend((nested.get("type"), nested.get("eventName"), nested.get("event")))
    return {name.lower() for name in names if isinstance(name, str)}


def tool_output_kind(output: Any, present: bool) -> str:
    if not present:
        return "missing"
    if output is None:
        return "null"
    if isinstance(output, str):
        return "string"
    if isinstance(output, list):
        return "array"
    if isinstance(output, dict):
        identity = output.get("id")
        if isinstance(identity, list) and identity and identity[-1] in ("ToolMessage", "ToolMessageChunk"):
            return "tool_message"
        return "object"
    if isinstance(output, bool):
        return "boolean"
    if isinstance(output, (int, float)):
        return "number"
    return "other"


def parse_event_frame(frame: str) -> dict[str, Any] | None:
    if not frame.strip():
        return None
    lines = frame.split("\n")
    if any(line.startswith("data:") for line in lines):
        payload = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
    elif all(line.startswith(":") or line.startswith(("event:", "id:", "retry:")) for line in lines):
        return None
    else:
        payload = frame
    try:
        event = json.loads(payload)
    except (ValueError, RecursionError) as exc:
        raise EvaluationError("stream contains malformed JSON", category="invalid_response") from exc
    if not isinstance(event, dict):
        raise EvaluationError("stream contains a non-object event", category="invalid_response")
    return event


def stream_events(body: bytes) -> list[dict[str, Any]]:
    try:
        text = body.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise EvaluationError("stream contains invalid UTF-8", category="invalid_response") from exc
    if not text.endswith("\n\n"):
        raise EvaluationError("stream is empty or truncated", category="incomplete_response")
    events: list[dict[str, Any]] = []
    for frame in text.split("\n\n"):
        event = parse_event_frame(frame)
        if event is not None:
            events.append(event)
    if not events:
        raise EvaluationError("stream contains no events", category="incomplete_response")
    return events


class ChatStream:
    def __init__(self, backend: str, target: str | None, aliases: dict[str, list[str]]) -> None:
        self.backend = backend
        self.target = target
        self.aliases = aliases
        self.response_bytes = 0
        self.accepted_bytes = 0
        self.event_bytes = 0
        self.control_bytes = 0
        self.event_types: dict[str, dict[str, int]] = {}
        self.tool_events: dict[str, dict[str, Any]] = {}
        self.ttfb_ms: float | None = None
        self._pending = bytearray()
        self._scan_from = 0
        self._tail = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._utf8_error = False
        self._parse_error: EvaluationError | None = None
        self._event_error: EvaluationError | None = None
        self._event_count = 0
        self._complete = False
        self._useful = False
        self._python_end = False
        self._observed: set[str] = set()

    def feed(self, chunk: bytes) -> None:
        self.response_bytes += len(chunk)
        accepted = chunk[:max(0, MAX_RESPONSE_BYTES - self.accepted_bytes)]
        self.accepted_bytes += len(accepted)
        if not accepted:
            return
        if not self._utf8_error:
            try:
                self._decoder.decode(accepted)
            except UnicodeDecodeError:
                self._utf8_error = True
        self._tail = (self._tail + accepted)[-4:]
        self._pending.extend(accepted)
        cursor = 0
        search_from = self._scan_from
        while match := FRAME_DELIMITER.search(self._pending, search_from):
            self._consume_frame(bytes(self._pending[cursor:match.start()]), match.end() - cursor)
            cursor = match.end()
            search_from = cursor
        if cursor:
            del self._pending[:cursor]
        self._scan_from = max(0, len(self._pending) - 3)

    def _consume_frame(self, frame: bytes, size: int) -> None:
        try:
            text = frame.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            self._record_type("invalid", size)
            if self._parse_error is None:
                self._parse_error = EvaluationError("stream contains invalid UTF-8", category="invalid_response")
            return
        try:
            event = parse_event_frame(text)
        except EvaluationError as exc:
            self._record_type("invalid", size)
            if self._parse_error is None:
                self._parse_error = EvaluationError(str(exc), category=exc.category)
            return
        if event is None:
            self.control_bytes += size
            return
        self._event_count += 1
        nested_event = event.get("event")
        if event.get("type") == "error":
            event_type = "error"
        elif isinstance(nested_event, str):
            event_type = nested_event
        elif isinstance(nested_event, dict):
            event_type = nested_event.get("type") or nested_event.get("eventName") or nested_event.get("event")
        else:
            event_type = event.get("type")
        safe_type = event_type if isinstance(event_type, str) and event_type in SAFE_EVENT_TYPES else "other"
        self._record_type(safe_type, size)
        try:
            self._record_tool_event(event)
            if self._event_error is None:
                self._inspect_event(event)
        except RecursionError:
            if self._parse_error is None:
                self._parse_error = EvaluationError(
                    "stream contains excessively nested data", category="invalid_response"
                )

    def _record_type(self, event_type: str, size: int) -> None:
        counts = self.event_types.setdefault(event_type, {"count": 0, "bytes": 0})
        counts["count"] += 1
        counts["bytes"] += size
        self.event_bytes += size

    def _record_tool_event(self, event: dict[str, Any]) -> None:
        names = event_names(event)
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}
        if self.backend == "langchain":
            is_start = "tool_start" in names
            is_end = "tool_end" in names
            name = data.get("tool")
            output_key = "output"
        elif self.backend == "llamaindex":
            is_start = "agenttoolcall" in names or "0" in names
            is_end = "agenttoolcallresult" in names or "1" in names or (
                "toolName" in data and "toolOutput" in data
            )
            name = data.get("toolName")
            output_key = "toolOutput" if "toolOutput" in data else "raw"
        else:
            return
        if not is_start and not is_end:
            return
        matched = match_tool(name, self.aliases)
        category = "task" if name == "task" else matched if matched in TOOLS else "other"
        if is_end:
            output = data.get(output_key)
            try:
                nonempty = has_tool_output(output)
            except RecursionError:
                nonempty = None
            try:
                reported_error = error_in_tool_result(output)
            except RecursionError:
                reported_error = None
            observation = {
                "kind": tool_output_kind(output, output_key in data),
                "nonempty": nonempty,
                "reported_error": reported_error,
            }
        counts = self.tool_events.setdefault(category, {"starts": 0, "ends": 0, "outputs": []})
        if is_start:
            counts["starts"] += 1
        if is_end:
            counts["ends"] += 1
            for existing in counts["outputs"]:
                if all(existing[key] == value for key, value in observation.items()):
                    existing["count"] += 1
                    break
            else:
                counts["outputs"].append({**observation, "count": 1})

    def _inspect_event(self, event: dict[str, Any]) -> None:
        lower_names = event_names(event)
        data = event.get("data")
        if (
            "error" in lower_names
            or "workflow_error" in lower_names
            or event.get("error")
            or (isinstance(data, dict) and data.get("error"))
        ):
            self._event_error = EvaluationError("stream reported a workflow error", category="workflow_error")
            return

        if self.backend == "langchain" and "agent_complete" in lower_names:
            self._complete = True
            self._useful = langchain_final(data)
        elif self.backend == "llamaindex" and "llamaindex-stop" in lower_names:
            self._complete = True
            message = data.get("message") if isinstance(data, dict) else None
            self._useful = useful_text(message)
        elif self.backend == "python":
            if "complete" in lower_names:
                self._python_end = True
            if "finalresult" in lower_names or "workflowcomplete" in lower_names:
                self._complete = True
                value = data.get("message") or data.get("output") if isinstance(data, dict) else None
                self._useful = self._useful or useful_text(value)

        is_langchain_tool = self.backend == "langchain" and "tool_end" in lower_names
        is_llama_tool = (
            self.backend == "llamaindex"
            and isinstance(data, dict)
            and ("agenttoolcallresult" in lower_names or ("toolName" in data and "toolOutput" in data))
        )
        if (is_langchain_tool or is_llama_tool) and isinstance(data, dict):
            name = data.get("tool") if is_langchain_tool else data.get("toolName")
            output = data.get("output") if is_langchain_tool else data.get("toolOutput", data.get("raw"))
            if error_in_tool_result(output):
                self._event_error = EvaluationError("stream reported a tool error", category="tool_error")
                return
            tool_id = match_tool(name, self.aliases)
            if tool_id and has_tool_output(output):
                self._observed.add(tool_id)

    def finish(self) -> dict[str, Any]:
        if self.response_bytes > MAX_RESPONSE_BYTES:
            raise EvaluationError("response exceeded the size limit", category="incomplete_response")
        if not self._utf8_error:
            try:
                self._decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                self._utf8_error = True
        if self._utf8_error:
            raise EvaluationError("stream contains invalid UTF-8", category="invalid_response")
        if not self._tail.replace(b"\r\n", b"\n").endswith(b"\n\n"):
            raise EvaluationError("stream is empty or truncated", category="incomplete_response")
        if self._pending and not self._pending.strip():
            self.control_bytes += len(self._pending)
            self._pending.clear()
        if self._parse_error:
            raise self._parse_error
        if not self._event_count:
            raise EvaluationError("stream contains no events", category="incomplete_response")
        if self._event_error:
            raise self._event_error
        if not self._complete or (self.backend == "python" and not self._python_end):
            raise EvaluationError("stream has no completion event", category="incomplete_response")
        if not self._useful:
            raise EvaluationError("stream has no useful final output", category="no_useful_output")
        verified = self.target is not None and self.target in self._observed
        return {
            "status": "ok" if verified else "unverified",
            "reason": None if verified else (
                "Python destination-agent routing is not exposed by this stream"
                if self.backend == "python" and self.target is None
                else "tool calls are not exposed by this backend" if self.backend == "python"
                else "no successful result observed for the requested tool"
            ),
            "completion_observed": True,
            "useful_final_output": True,
            "requested_tool": self.target,
            "observed_tools": sorted(self._observed),
        }

    def summary(self, response_complete: bool) -> dict[str, Any]:
        result = {
            "response_bytes": self.response_bytes,
            "response_bytes_lower_bound": not response_complete,
            "event_types": self.event_types,
            "tool_events": {
                category: {
                    **counts,
                    "outputs": sorted(counts["outputs"], key=lambda item: (
                        item["kind"], str(item["nonempty"]), str(item["reported_error"])
                    )),
                }
                for category, counts in self.tool_events.items()
            },
            "control_bytes": self.control_bytes,
            "unframed_bytes": self.response_bytes - self.event_bytes - self.control_bytes,
            "observed_failure_category": self._event_error.category if self._event_error else None,
        }
        self._pending.clear()
        self._tail = b""
        self._decoder.reset()
        return result


def inspect_stream(body: bytes, backend: str, target: str | None, aliases: dict[str, list[str]]) -> dict[str, Any]:
    stream = ChatStream(backend, target, aliases)
    stream.feed(body)
    return stream.finish()


def http_worker(request: dict[str, Any]) -> dict[str, Any]:
    started_at = timestamp()
    started = time.monotonic()
    result: dict[str, Any] = {
        "started_at": started_at, "status": "failed", "http_status": None, "ttfb_ms": None,
        "response_complete": False,
    }
    kind = request["kind"]
    stream = ChatStream(request["backend"], request["target"], request["aliases"]) if kind == "chat" else None
    try:
        payload = request.get("payload")
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "text/event-stream" if kind == "chat" else "*/*"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        http_request = urllib.request.Request(request["url"], data=data, headers=headers)
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(http_request, timeout=request["timeout"]) as response:
            result["http_status"] = response.status
            if response.status != 200:
                raise EvaluationError(f"HTTP {response.status}")
            content_type = response.headers.get_content_type()
            body, result["ttfb_ms"] = read_response(response, started, stream)
            result["response_complete"] = True
        if kind == "ui":
            if content_type != "text/html":
                raise EvaluationError("UI response is not HTML")
            document = AngularDocument()
            document.feed(body.decode("utf-8"))
            if not document.is_application():
                raise EvaluationError("UI response is not the Angular application")
            result["status"] = "ok"
        elif kind == "health":
            health = json.loads(body)
            if not isinstance(health, dict) or health.get("status") != "OK":
                raise EvaluationError("health response is not JSON OK")
            expected = {"langchain": "langchain-js", "llamaindex": "llamaindex-ts"}
            backend = request["backend"]
            if backend in expected and health.get("orchestrator") != expected[backend]:
                raise EvaluationError("health response identifies a different assistant")
            if backend == "python" and not isinstance(health.get("mcp"), dict):
                raise EvaluationError("health response does not identify the Python assistant")
            result["status"] = "ok"
        elif kind == "tools":
            selected, aliases = selected_tools(json.loads(body))
            result.update(status="ok", selected=selected, aliases=aliases)
        elif stream is not None:
            if content_type != "text/event-stream":
                raise EvaluationError("chat response is not an event stream", category="invalid_response")
            result.update(stream.finish())
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["reason"] = f"HTTP {exc.code}"
        result["failure_category"] = "http_error"
    except (TimeoutError, socket.timeout):
        result.update(status="timeout", reason="request timed out", failure_category="timeout")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            result.update(status="timeout", reason="request timed out", failure_category="timeout")
        else:
            result.update(reason="network request failed", failure_category="network_error")
    except (http.client.IncompleteRead, ConnectionError):
        result.update(reason="truncated HTTP response", failure_category="incomplete_response")
    except (UnicodeDecodeError, json.JSONDecodeError):
        result.update(reason="response contains invalid text or JSON", failure_category="invalid_response")
    except EvaluationError as exc:
        result["reason"] = str(exc)
        if exc.category:
            result["failure_category"] = exc.category
    if stream is not None:
        result["stream_stats"] = stream.summary(result["response_complete"])
        if result["ttfb_ms"] is None:
            result["ttfb_ms"] = stream.ttfb_ms
    result["latency_ms"] = milliseconds(started)
    return result


def request_with_deadline(request: dict[str, Any]) -> dict[str, Any]:
    started_at = timestamp()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--http-worker"],
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=request["timeout"],
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "started_at": started_at, "status": "timeout", "reason": "request deadline exceeded",
            "http_status": None, "ttfb_ms": None, "latency_ms": milliseconds(started),
        }
    except OSError:
        return {
            "started_at": started_at, "status": "failed", "reason": "could not start request worker",
            "http_status": None, "ttfb_ms": None, "latency_ms": milliseconds(started),
        }
    if completed.returncode != 0 or len(completed.stdout) > MAX_RESPONSE_BYTES * 2:
        return {
            "started_at": started_at, "status": "failed", "reason": "request worker failed",
            "http_status": None, "ttfb_ms": None, "latency_ms": milliseconds(started),
        }
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "started_at": started_at, "status": "failed", "reason": "request worker returned invalid output",
            "http_status": None, "ttfb_ms": None, "latency_ms": milliseconds(started),
        }
    return result


def backend_scope(backends: Sequence[str]) -> dict[str, Any]:
    omitted = [backend for backend in BACKENDS if backend not in backends]
    return {"selected_backends": list(backends), "omitted_backends": omitted, "partial": bool(omitted)}


def readiness(
    urls: dict[str, str], timeout: float, backends: Sequence[str] = BACKENDS
) -> tuple[dict[str, Any], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    discovered: dict[str, Any] = {}
    requests = [("ui", "ui", urls["ui"] + "/")]
    for backend in backends:
        requests.extend(
            [(backend, "health", urls[backend] + "/api/health"), (backend, "tools", urls[backend] + "/api/tools")]
        )
    for backend, kind, url in requests:
        result = request_with_deadline({"kind": kind, "backend": backend, "url": url, "timeout": timeout})
        if kind == "tools" and result["status"] == "ok":
            discovered[backend] = {"selected": result.pop("selected"), "aliases": result.pop("aliases")}
            result["definition_counts"] = {
                tool_id: len(discovered[backend]["aliases"][tool_id]) for tool_id in TOOLS
            }
        checks.append({"backend": backend, "check": kind, **result})
    ready = all(check["status"] == "ok" for check in checks)
    scope_info = backend_scope(backends)
    return {
        "ready": ready, "full_application_ready": None if scope_info["partial"] else ready,
        "scope": scope_info, "checks": checks,
    }, discovered


def run_usage(urls: dict[str, str], ready: dict[str, Any], tools: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {
        "phase": "usage", "started_at": timestamp(), "scope": backend_scope(args.backends), "readiness": ready,
        "request_limit": len(args.backends) * len(PROMPTS), "concurrency_limit": 2, "requests": [],
        "model_interpretation": {
            "detects_every_chat_stream_failure": False,
            "expected_behavior": (
                "The logged-assistant-failures signal can detect selected emitted errors, "
                "but an unlogged HTTP-200 chat-stream failure can leave a model Healthy."
            ),
            "live_model_observation": "not captured by usage; compare separately captured snapshots",
        },
    }
    if not ready["ready"]:
        result.update(status="blocked", reason="readiness checks did not all pass", completed_at=timestamp())
        return result
    jobs = []
    for backend in args.backends:
        for prompt_id, target, prompt in PROMPTS:
            requested_tool: str | None = target
            if backend == "python" and prompt_id == "destinations":
                requested_tool = None
                prompt = "Suggest a cultural destination for a fictional traveler with a moderate budget."
            payload = {"message": prompt, "context": {}} if backend == "python" else {
                "message": prompt, "tools": tools[backend]["selected"],
            }
            request = {
                "kind": "chat", "backend": backend, "url": urls[backend] + "/api/chat",
                "payload": payload, "target": requested_tool, "aliases": tools[backend]["aliases"],
                "timeout": args.timeout,
            }
            jobs.append((backend, prompt_id, requested_tool, request))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        for offset in range(0, len(jobs), 2):
            batch = jobs[offset:offset + 2]
            futures = [
                (backend, prompt_id, target, executor.submit(request_with_deadline, request))
                for backend, prompt_id, target, request in batch
            ]
            outcomes = []
            for backend, prompt_id, target, future in futures:
                outcome = {"backend": backend, "prompt": prompt_id, "requested_tool": target, **future.result()}
                outcomes.append(outcome)
                result["requests"].append(outcome)
            if any(
                outcome["status"] == "timeout"
                or (
                    outcome["status"] == "failed"
                    and not (
                        outcome.get("response_complete")
                        and outcome.get("reason") in {
                            "stream reported a workflow error",
                            "stream reported a tool error",
                            "stream has no useful final output",
                        }
                    )
                )
                for outcome in outcomes
            ):
                result["requests"].extend({
                    "backend": backend, "prompt": prompt_id, "requested_tool": target, "status": "not_sent",
                    "reason": "stopped after an incomplete or timed-out request",
                } for backend, prompt_id, target, _ in jobs[offset + len(batch):])
                break
    statuses = {item["status"] for item in result["requests"]}
    result["requests_sent"] = sum(1 for item in result["requests"] if item["status"] != "not_sent")
    failed_200 = [
        item for item in result["requests"]
        if item.get("http_status") == 200 and item["status"] in {"failed", "timeout"}
    ]
    explicit_failures = []
    other_failures = []
    for item in failed_200:
        stats = item.get("stream_stats")
        observed_failure = stats.get("observed_failure_category") if isinstance(stats, dict) else None
        if item.get("failure_category") in {"workflow_error", "tool_error"} or observed_failure in {
            "workflow_error", "tool_error"
        }:
            explicit_failures.append(item)
        else:
            other_failures.append(item)
    result["http_200_stream_failures"] = len(explicit_failures)
    result["http_200_incomplete_responses"] = sum(
        1 for item in other_failures
        if (
            item.get("failure_category") == "incomplete_response"
            or item["status"] == "timeout"
            or item.get("response_complete") is False
        )
    )
    result["http_200_other_failed_responses"] = (
        len(other_failures) - result["http_200_incomplete_responses"]
    )
    result["status"] = "failed" if statuses & {"failed", "timeout"} else (
        "unverified" if "unverified" in statuses else "ok"
    )
    result["completed_at"] = timestamp()
    return result


def azure_get(url: str, subscription: str) -> dict[str, Any]:
    output = command(
        ["az", "rest", "--method", "get", "--url", url, "--subscription", subscription, "--output", "json",
         "--only-show-errors"]
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise EvaluationError("Azure returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise EvaluationError("Azure returned an invalid object")
    return value


def list_model_items(
    model_url: str, subscription: str,
    collection: Literal["entities", "relationships", "signaldefinitions", "authenticationsettings"],
) -> list[dict[str, Any]]:
    url = f"{model_url}/{collection}?api-version={API_VERSION}"
    model_path = urllib.parse.urlsplit(model_url).path + f"/{collection}"
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for _ in range(20):
        if url in seen:
            raise EvaluationError(f"{collection} pagination repeated a page")
        seen.add(url)
        page = azure_get(url, subscription)
        if not isinstance(page.get("value"), list) or not all(isinstance(item, dict) for item in page["value"]):
            raise EvaluationError(f"Azure returned an invalid {collection} list")
        items.extend(page["value"])
        next_link = page.get("nextLink")
        if next_link is None:
            return items
        if not isinstance(next_link, str) or not next_link:
            raise EvaluationError("Azure returned an invalid continuation link")
        parsed = urllib.parse.urlsplit(next_link)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "management.azure.com"
            or urllib.parse.unquote(parsed.path).lower() != urllib.parse.unquote(model_path).lower()
            or urllib.parse.parse_qs(parsed.query).get("api-version") != [API_VERSION]
        ):
            raise EvaluationError("Azure returned an unsafe continuation link")
        url = next_link
    raise EvaluationError(f"{collection} pagination exceeded the limit")


def expected_signals(profile: str) -> set[tuple[str, str]]:
    expected: set[tuple[str, str]] = set()
    for component, entity, backend, is_ai in RESOURCE_BINDINGS:
        if backend is not None:
            expected.add((entity, "logged-assistant-failures"))
        if backend == "python":
            expected.add((entity, "appinsights-required-tool-failures"))
        expected.add((entity, "ai-availability" if is_ai else "replicas"))
        if is_ai:
            expected.add((entity, RESOURCE_HEALTH_SIGNAL))
        if not is_ai and profile == "stability":
            expected.add((entity, "container-terminations"))
        if not is_ai and profile == "capacity":
            expected.update(((entity, "cpu"), (entity, "memory")))
        if profile == "experience":
            name = "ai-latency" if is_ai else "response-chat" if component == "assistant" else "response-fast"
            expected.add((entity, name))
    return expected


def signal_record(entity: str, group: str, signal: dict[str, Any], resource_health: bool = False) -> dict[str, Any]:
    status = signal.get("status")
    status = status if isinstance(status, dict) else {}
    state = status.get("healthState")
    value = status.get("value")
    reported_at = status.get("reportedAt")
    error = safe_error(status.get("error"))
    availability_state = status.get("availabilityState")
    try:
        reported_time = dt.datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
        valid_time = reported_time.tzinfo is not None
    except (AttributeError, ValueError):
        valid_time = False
    numeric = (
        not isinstance(value, bool)
        and (isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)))
    )
    if isinstance(value, float) and not math.isfinite(value):
        value = str(value)
    valid_resource_health = (
        resource_health
        and (value is None or numeric)
        and availability_state in (None, "Available", "Unavailable", "Degraded")
    )
    known = (
        isinstance(state, str) and state in KNOWN_STATES and valid_time and not error
        and (valid_resource_health or (not resource_health and numeric))
    )
    record = {
        "entity": entity,
        "group": group,
        "signal": RESOURCE_HEALTH_SIGNAL if resource_health else signal.get("name"),
        "healthState": state if isinstance(state, str) else "Unknown",
        "value": value,
        "reportedAt": reported_at,
        "error": error,
        "present": True,
        "known": known,
    }
    if resource_health:
        record["availabilityState"] = availability_state
        record["sourceSignalName"] = signal.get("signalName")
    return record


def coverage(signals: list[dict[str, Any]]) -> dict[str, int]:
    known = sum(1 for signal in signals if signal["known"])
    return {
        "total": len(signals), "known": known, "unknown": len(signals) - known,
        "missing": sum(1 for signal in signals if not signal["present"]),
    }


def snapshot_model(name: str, profile: str, subscription: str, resource_group: str) -> dict[str, Any]:
    path = (
        f"/subscriptions/{subscription}/resourceGroups/{urllib.parse.quote(resource_group, safe='')}"
        f"/providers/Microsoft.CloudHealth/healthmodels/{name}"
    )
    model_url = "https://management.azure.com" + path
    expected = expected_signals(profile)
    result: dict[str, Any] = {
        "model": name, "captured_at": timestamp(), "provisioningState": None,
        "status": "unavailable", "reason": None, "root": "Unknown",
        "flows": {FLOW: {"healthState": "Unknown"}},
        "components": {component: {"healthState": "Unknown"} for component in COMPONENTS},
        "assistants": {backend: {"healthState": "Unknown"} for backend in BACKENDS},
    }
    if profile == "stability":
        result["limitations"] = [
            "Container terminations can include normal deployments and older revisions, not just failures."
        ]
    entities: list[dict[str, Any]] = []
    try:
        model = azure_get(f"{model_url}?api-version={API_VERSION}", subscription)
        properties = model.get("properties")
        if not isinstance(properties, dict):
            raise EvaluationError("model has no provisioning state")
        result["provisioningState"] = properties.get("provisioningState")
        if result["provisioningState"] != "Succeeded":
            raise EvaluationError("model deployment is not succeeded")
        entities = list_model_items(model_url, subscription, "entities")
        result["status"] = "available"
    except EvaluationError as exc:
        result["reason"] = str(exc)

    relationships: list[dict[str, Any]] | None = None
    if result["status"] == "available":
        try:
            relationships = list_model_items(model_url, subscription, "relationships")
        except EvaluationError as exc:
            result.update(status="incomplete", reason=f"relationship read failed: {exc}")

    found: dict[tuple[str, str], dict[str, Any]] = {}
    unexpected_signal_names: set[tuple[str, str]] = set()
    entity_names: set[str] = set()
    for entity in entities:
        entity_name = entity.get("name")
        properties = entity.get("properties")
        if not isinstance(entity_name, str) or not isinstance(properties, dict):
            result.update(status="incomplete", reason="entity list contains an invalid entry")
            continue
        if entity_name in entity_names:
            result.update(status="incomplete", reason="entity list contains a duplicate")
            continue
        entity_names.add(entity_name)
        state = properties.get("healthState") or "Unknown"
        if entity_name == name:
            result["root"] = state
        if entity_name == FLOW:
            result["flows"][entity_name]["healthState"] = state
        if entity_name in COMPONENTS:
            result["components"][entity_name]["healthState"] = state
        for _, resource_name, backend, _ in RESOURCE_BINDINGS:
            if backend is not None and entity_name == resource_name:
                result["assistants"][backend]["healthState"] = state
        groups = properties.get("signalGroups")
        if not isinstance(groups, dict):
            continue
        for group_name, group in groups.items():
            if not isinstance(group, dict):
                continue
            signals = group.get("signals", [])
            if not isinstance(signals, list):
                result.update(status="incomplete", reason="entity contains an invalid signal group")
                continue
            for signal in signals:
                if not isinstance(signal, dict) or not isinstance(signal.get("name"), str):
                    result.update(status="incomplete", reason="entity contains an invalid signal")
                    continue
                record = signal_record(entity_name, group_name, signal)
                if record["signal"] == RESOURCE_HEALTH_SIGNAL:
                    unexpected_signal_names.add((entity_name, record["signal"]))
                    continue
                if (entity_name, record["signal"]) in found:
                    result.update(status="incomplete", reason="entity contains a duplicate signal")
                found[(entity_name, record["signal"])] = record
            resource_health = group.get("resourceHealth")
            if isinstance(resource_health, dict) and resource_health.get("enabled") != "Disabled":
                signal_name = resource_health.get("signalName")
                if signal_name is not None and (not isinstance(signal_name, str) or not signal_name.strip()):
                    result.update(status="incomplete", reason="entity contains an invalid resource health signal")
                    continue
                record = signal_record(entity_name, group_name, resource_health, resource_health=True)
                if (entity_name, record["signal"]) in found:
                    result.update(status="incomplete", reason="entity contains a duplicate signal")
                found[(entity_name, record["signal"])] = record

    unexpected_signals = (found.keys() - expected) | unexpected_signal_names
    for entity, signal in expected - found.keys():
        found[(entity, signal)] = {
            "entity": entity,
            "group": "azureLogAnalytics" if signal in {
                "logged-assistant-failures", "appinsights-required-tool-failures", "container-terminations"
            }
            else "azureResource",
            "signal": signal,
            "healthState": "Unknown", "value": None, "reportedAt": None, "error": None,
            "present": False, "known": False,
        }
        if signal == RESOURCE_HEALTH_SIGNAL:
            found[(entity, signal)]["availabilityState"] = None
    signals = sorted(found.values(), key=lambda item: (item["entity"], item["signal"]))
    expected_records = [found[key] for key in sorted(expected)]
    result["signals"] = signals
    result["coverage"] = coverage(expected_records)
    result["expected_signals"] = len(expected)
    result["unexpected_signals"] = [
        {"entity": entity, "signal": signal} for entity, signal in sorted(unexpected_signals)
    ]
    required_entities = {name, FLOW, *COMPONENTS}
    expected_relationships = {(f"{name}-{FLOW}", name, FLOW)}
    for component in COMPONENTS:
        expected_relationships.add((f"{FLOW}-{component}", FLOW, component))
    for component, resource_name, _, _ in RESOURCE_BINDINGS:
        required_entities.add(resource_name)
        expected_relationships.add((f"{component}-{resource_name}", component, resource_name))
    result["entity_count"] = len(entity_names)
    result["missing_entities"] = sorted(required_entities - entity_names)
    result["unexpected_entities"] = sorted(entity_names - required_entities)
    result["relationship_count"] = None
    result["missing_relationships"] = None
    result["unexpected_relationships"] = None
    if relationships is not None:
        found_relationships: set[tuple[str, str, str]] = set()
        for relationship in relationships:
            properties = relationship.get("properties")
            rel_name = relationship.get("name")
            if not isinstance(properties, dict) or not isinstance(rel_name, str):
                result.update(status="incomplete", reason="relationship list contains an invalid entry")
                continue
            parent = properties.get("parentEntityName")
            child = properties.get("childEntityName")
            if not isinstance(parent, str) or not isinstance(child, str):
                result.update(status="incomplete", reason="relationship list contains an invalid entry")
                continue
            edge = (rel_name, parent, child)
            if edge in found_relationships:
                result.update(status="incomplete", reason="relationship list contains a duplicate")
            found_relationships.add(edge)
        result["relationship_count"] = len(relationships)
        result["missing_relationships"] = sorted(expected_relationships - found_relationships)
        result["unexpected_relationships"] = sorted(found_relationships - expected_relationships)
    result["evaluation"] = (
        "unknown" if not result["coverage"]["known"] else
        "partial" if result["coverage"]["unknown"] else "complete"
    )
    result["flows"][FLOW]["coverage"] = coverage(expected_records)
    for _, resource_name, backend, _ in RESOURCE_BINDINGS:
        if backend is not None:
            result["assistants"][backend]["coverage"] = coverage(
                [signal for signal in expected_records if signal["entity"] == resource_name]
            )
    if result["status"] == "available" and result["coverage"]["missing"]:
        result.update(status="incomplete", reason="expected signals are missing")
    if result["status"] == "available" and result["unexpected_signals"]:
        result.update(status="incomplete", reason="unexpected signals are present")
    if result["status"] == "available" and (result["missing_entities"] or result["unexpected_entities"]):
        result.update(status="incomplete", reason="entity list does not match the model")
    if result["status"] == "available" and (result["missing_relationships"] or result["unexpected_relationships"]):
        result.update(status="incomplete", reason="relationship list does not match the model")
    result["completed_at"] = timestamp()
    return result


def named_items(value: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise EvaluationError(f"{label} is not a list")
    items: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise EvaluationError(f"{label} contains an invalid item")
        name = item["name"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{1,258}[a-zA-Z0-9]", name):
            raise EvaluationError(f"{label} contains an invalid name")
        if name in items or not isinstance(item.get("properties"), dict):
            raise EvaluationError(f"{label} contains a duplicate or invalid item")
        items[name] = item
    return items


def load_readback_contract(
    path: str, expected_hash: str, subscription: str, resource_group: str,
) -> tuple[dict[str, Any], str]:
    try:
        with Path(path).open("rb") as contract_file:
            raw = contract_file.read(MAX_COMMAND_BYTES + 1)
        if len(raw) > MAX_COMMAND_BYTES:
            raise EvaluationError("readback contract exceeds the size limit")
        contract = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("could not read JSON readback contract") from exc
    contract_hash = hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash) or contract_hash != expected_hash.lower():
        raise EvaluationError("readback contract hash does not match")
    if not isinstance(contract, dict) or contract.get("apiVersion") != API_VERSION:
        raise EvaluationError("readback contract has an unsupported API version")
    for key, filename in (
        ("sharedTemplateSha256", "healthmodel.bicep"),
        ("entryTemplateSha256", "healthmodels.bicep"),
    ):
        try:
            actual_hash = hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()
        except OSError as exc:
            raise EvaluationError("could not read a local health-model template") from exc
        if contract.get(key) != actual_hash:
            raise EvaluationError("readback contract does not match local templates")

    models = contract.get("models")
    if not isinstance(models, list) or len(models) != len(MODELS):
        raise EvaluationError("readback contract has an invalid model list")
    group = f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
    workspace_ids: set[str] = set()
    names: set[str] = set()
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("name"), str):
            raise EvaluationError("readback contract has an invalid model")
        name = model["name"]
        if name not in MODELS or name in names:
            raise EvaluationError("readback contract has an invalid model")
        names.add(name)
        if (
            not isinstance(model.get("model"), dict)
            or model["model"].get("name") != name
            or not isinstance(model["model"].get("location"), str)
            or model["model"].get("identity") != {"type": "SystemAssigned"}
        ):
            raise EvaluationError("readback contract has invalid model settings")
        entities = named_items(model.get("entities"), f"{name} entities")
        expected_entities = {FLOW, *COMPONENTS, *(resource for _, resource, _, _ in RESOURCE_BINDINGS)}
        if set(entities) != expected_entities:
            raise EvaluationError("readback contract has unexpected entities")
        resource_kinds = {resource: is_ai for _, resource, _, is_ai in RESOURCE_BINDINGS}
        pairs: set[tuple[str, str]] = set()
        for entity_name, entity in entities.items():
            properties = entity["properties"]
            alerts = properties.get("alerts") or {}
            if not isinstance(alerts, dict):
                raise EvaluationError("readback contract has invalid alerts")
            if entity_name == FLOW:
                if (
                    properties.get("healthObjective") != 99.9
                    or set(alerts) != {"degraded", "unhealthy"}
                    or any(
                        not isinstance(alerts[state], dict)
                        or alerts[state].get("severity") != severity
                        or alerts[state].get("actionGroupIds")
                        for state, severity in (("degraded", "Sev2"), ("unhealthy", "Sev1"))
                    )
                ):
                    raise EvaluationError("readback contract has unexpected flow alerts or objective")
            elif alerts:
                raise EvaluationError("readback contract has unexpected alerts")
            groups = entity["properties"].get("signalGroups") or {}
            if not isinstance(groups, dict):
                raise EvaluationError("readback contract has invalid signal groups")
            if entity_name in resource_kinds and not isinstance(groups.get("azureResource"), dict):
                raise EvaluationError("readback contract is missing a resource binding")
            for group_name, value in groups.items():
                if group_name not in {"dependencies", "azureResource", "azureLogAnalytics"} or not isinstance(value, dict):
                    raise EvaluationError("readback contract has an invalid signal group")
                signals = value.get("signals", [])
                if not isinstance(signals, list):
                    raise EvaluationError("readback contract has an invalid signal list")
                for signal in signals:
                    if not isinstance(signal, dict) or not isinstance(signal.get("name"), str):
                        raise EvaluationError("readback contract has an invalid signal")
                    expected_kind = (
                        "AzureResourceMetric" if group_name == "azureResource" else
                        "LogAnalyticsQuery" if group_name == "azureLogAnalytics" else None
                    )
                    if signal.get("signalKind") != expected_kind:
                        raise EvaluationError("readback contract has an invalid signal kind")
                    pair = (entity_name, signal["name"])
                    if pair in pairs:
                        raise EvaluationError("readback contract has duplicate signals")
                    pairs.add(pair)
                if group_name == "azureResource":
                    resource_id = value.get("azureResourceId")
                    if not isinstance(resource_id, str) or not resource_id.lower().startswith(
                        (group + "/providers/").lower()
                    ):
                        raise EvaluationError("readback contract has an invalid resource scope")
                    health = value.get("resourceHealth")
                    if not isinstance(health, dict) or health.get("enabled") not in ("Enabled", "Disabled"):
                        raise EvaluationError("readback contract has invalid resource health settings")
                    if entity_name not in resource_kinds or health["enabled"] != (
                        "Enabled" if resource_kinds[entity_name] else "Disabled"
                    ):
                        raise EvaluationError("readback contract has unexpected resource health settings")
                    if health["enabled"] == "Enabled":
                        pair = (entity_name, "resourcehealth-availabilitystate")
                        if pair in pairs:
                            raise EvaluationError("readback contract has duplicate signals")
                        pairs.add(pair)
                if group_name == "azureLogAnalytics":
                    workspace = value.get("logAnalyticsWorkspaceResourceId")
                    if not isinstance(workspace, str):
                        raise EvaluationError("readback contract has an invalid workspace")
                    workspace_ids.add(workspace.lower())
        if pairs != expected_signals(MODELS[name]):
            raise EvaluationError("readback contract has unexpected signals")
        relationships = named_items(model.get("relationships"), f"{name} relationships")
        expected_edges = {(name, FLOW)} | {(FLOW, component) for component in COMPONENTS} | {
            (component, resource) for component, resource, _, _ in RESOURCE_BINDINGS
        }
        edges = [
            (item["properties"].get("parentEntityName"), item["properties"].get("childEntityName"))
            for item in relationships.values()
        ]
        if (
            len(edges) != len(expected_edges)
            or any(not isinstance(parent, str) or not isinstance(child, str) for parent, child in edges)
            or set(edges) != expected_edges
        ):
            raise EvaluationError("readback contract has unexpected relationships")
        definitions = named_items(model.get("signalDefinitions"), f"{name} signalDefinitions")
        expected_definitions = {"replicas", "ai-availability"}
        if MODELS[name] == "capacity":
            expected_definitions.update(("cpu", "memory"))
        elif MODELS[name] == "experience":
            expected_definitions.update(("response-fast", "response-chat", "ai-latency"))
        if set(definitions) != expected_definitions:
            raise EvaluationError("readback contract has unexpected signal definitions")
        auth = named_items(model.get("authenticationSettings"), f"{name} authenticationSettings")
        if set(auth) != {"system-assigned"} or auth["system-assigned"]["properties"] != {
            "authenticationKind": "ManagedIdentity", "managedIdentityName": "SystemAssigned",
        }:
            raise EvaluationError("readback contract has unexpected authentication settings")
    if names != set(MODELS):
        raise EvaluationError("readback contract is missing models")
    if len(workspace_ids) != 1:
        raise EvaluationError("readback contract has unexpected workspaces")

    roles = contract.get("roles")
    if not isinstance(roles, list) or len(roles) != len(MODELS) * 2:
        raise EvaluationError("readback contract has an invalid role list")
    definitions = {
        "acdd72a7-3385-48ef-bd42-f606fba81ae7": "reader",
        "43d0d8ad-25c7-4714-9337-8ba259a9fe05": "monitoring",
    }
    assignments: dict[str, set[str]] = {name: set() for name in MODELS}
    role_ids: set[str] = set()
    for role in roles:
        if not isinstance(role, dict) or any(
            not isinstance(role.get(key), str) for key in ("id", "scope", "roleDefinitionId", "principalModel")
        ):
            raise EvaluationError("readback contract contains an invalid role")
        role_id, role_scope, definition, model_name = (
            role["id"], role["scope"], role["roleDefinitionId"], role["principalModel"]
        )
        if model_name not in assignments or role_id.lower() in role_ids:
            raise EvaluationError("readback contract contains an invalid role identity")
        prefix = role_scope + "/providers/Microsoft.Authorization/roleAssignments/"
        if not role_id.lower().startswith(prefix.lower()):
            raise EvaluationError("readback contract contains an invalid role ID")
        try:
            role_guid = str(uuid.UUID(role_id[len(prefix):]))
        except ValueError as exc:
            raise EvaluationError("readback contract contains an invalid role ID") from exc
        if role_id[len(prefix):].lower() != role_guid:
            raise EvaluationError("readback contract contains an invalid role ID")
        definition_prefix = f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/"
        if not definition.lower().startswith(definition_prefix.lower()):
            raise EvaluationError("readback contract contains an invalid role definition")
        kind = definitions.get(definition[len(definition_prefix):].lower())
        workspace_pattern = (
            re.escape(group) + r"/providers/Microsoft\.OperationalInsights/workspaces/"
            r"[a-zA-Z0-9][a-zA-Z0-9-]{1,61}[a-zA-Z0-9]"
        )
        if (
            kind is None
            or (kind == "reader" and role_scope.lower() != group.lower())
            or (kind == "monitoring" and not re.fullmatch(workspace_pattern, role_scope, re.IGNORECASE))
            or (kind == "monitoring" and role_scope.lower() not in workspace_ids)
            or kind in assignments[model_name]
        ):
            raise EvaluationError("readback contract contains an invalid role scope")
        assignments[model_name].add(kind)
        role_ids.add(role_id.lower())
    if any(kinds != {"reader", "monitoring"} for kinds in assignments.values()):
        raise EvaluationError("readback contract is missing required roles")
    return contract, contract_hash


def compare_readback_fields(expected: Any, actual: Any, path: str, mismatches: list[str]) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            mismatches.append(path)
            return
        for key, value in expected.items():
            if key not in actual:
                mismatches.append(f"{path}.{key}")
            else:
                compare_readback_fields(value, actual[key], f"{path}.{key}", mismatches)
        return
    if isinstance(expected, list):
        if not isinstance(actual, list):
            mismatches.append(path)
            return
        if expected and all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in expected):
            try:
                expected_by_name = {item["name"]: item for item in expected}
                actual_by_name = {item["name"]: item for item in actual if isinstance(item, dict)}
            except (KeyError, TypeError):
                mismatches.append(path)
                return
            if len(expected_by_name) != len(expected) or len(actual_by_name) != len(actual):
                mismatches.append(path)
                return
            if set(expected_by_name) != set(actual_by_name):
                mismatches.append(path)
            for name in expected_by_name.keys() & actual_by_name.keys():
                compare_readback_fields(expected_by_name[name], actual_by_name[name], f"{path}.{name}", mismatches)
        elif expected != actual:
            mismatches.append(path)
        return
    if isinstance(expected, str) and isinstance(actual, str) and (
        expected.lower().startswith("/subscriptions/") or path.endswith((".location", ".principalId"))
    ):
        if expected.lower() != actual.lower():
            mismatches.append(path)
    elif type(expected) is not type(actual) and not (
        isinstance(expected, (int, float))
        and not isinstance(expected, bool)
        and isinstance(actual, (int, float))
        and not isinstance(actual, bool)
    ):
        mismatches.append(path)
    elif expected != actual:
        mismatches.append(path)


def compare_readback_collection(
    expected: Any, actual: Any, label: str, mismatches: list[str], *, root: str | None = None,
) -> None:
    expected_items = named_items(expected, f"expected {label}")
    actual_items = named_items(actual, f"actual {label}")
    if root is not None:
        root_item = actual_items.pop(root, None)
        if root_item is None:
            mismatches.append(f"{label}.{root}")
        else:
            root_props = root_item["properties"]
            root_groups = root_props.get("signalGroups") or {}
            dependencies = (root_groups.get("dependencies") or {}) if isinstance(root_groups, dict) else None
            if not isinstance(root_groups, dict) or not isinstance(dependencies, dict) or (
                root_props.get("alerts")
                or root_props.get("signalAggregationGroups")
                or root_props.get("impact") not in (None, "Standard")
                or ("provisioningState" in root_props and root_props["provisioningState"] != "Succeeded")
                or dependencies.get("aggregationType") not in (None, "WorstOf")
                or bool(set(dependencies) - {"aggregationType", "ignoreUnknown"})
                or ("ignoreUnknown" in dependencies and dependencies["ignoreUnknown"] is not True)
                or any(group != "dependencies" and value for group, value in root_groups.items())
            ):
                mismatches.append(f"{label}.{root}.properties")
    if set(expected_items) != set(actual_items):
        mismatches.append(f"{label}.names")
    for name in expected_items.keys() & actual_items.keys():
        expected_props = expected_items[name]["properties"]
        actual_props = actual_items[name]["properties"]
        path = f"{label}.{name}.properties"
        compare_readback_fields(expected_props, actual_props, path, mismatches)
        if "provisioningState" in actual_props and actual_props["provisioningState"] != "Succeeded":
            mismatches.append(f"{path}.provisioningState")
        if (
            label == "signalDefinitions"
            and actual_props.get("dimensionFilter")
            and not expected_props.get("dimensionFilter")
        ):
            mismatches.append(f"{path}.dimensionFilter")
        if label == "entities":
            expected_alerts = expected_props.get("alerts") or {}
            actual_alerts = actual_props.get("alerts") or {}
            if not isinstance(expected_alerts, dict) or not isinstance(actual_alerts, dict):
                mismatches.append(f"{path}.alerts")
            elif set(actual_alerts) != set(expected_alerts):
                mismatches.append(f"{path}.alerts")
            elif any(
                isinstance(alert, dict) and alert.get("actionGroupIds")
                and not (expected_alerts.get(state) or {}).get("actionGroupIds")
                for state, alert in actual_alerts.items()
            ):
                mismatches.append(f"{path}.alerts.actionGroupIds")
            if (
                actual_props.get("signalAggregationGroups")
                or ("impact" not in expected_props and actual_props.get("impact") not in (None, "Standard"))
                or ("healthObjective" not in expected_props and actual_props.get("healthObjective") is not None)
            ):
                mismatches.append(path)
            expected_groups = expected_props.get("signalGroups") or {}
            actual_groups = actual_props.get("signalGroups") or {}
            if isinstance(actual_groups, dict):
                expected_dependencies = expected_groups.get("dependencies") if isinstance(expected_groups, dict) else None
                actual_dependencies = actual_groups.get("dependencies")
                if (
                    isinstance(expected_dependencies, dict)
                    and expected_dependencies.get("aggregationType") == "WorstOf"
                    and isinstance(actual_dependencies, dict)
                    and (
                        set(actual_dependencies) - {"aggregationType", "ignoreUnknown"}
                        or (
                            "ignoreUnknown" not in expected_dependencies
                            and "ignoreUnknown" in actual_dependencies
                            and actual_dependencies["ignoreUnknown"] is not True
                        )
                    )
                ):
                    mismatches.append(f"{path}.signalGroups.dependencies")
                for group, value in actual_groups.items():
                    if group not in expected_groups and value:
                        default_dependencies = (
                            group == "dependencies"
                            and isinstance(value, dict)
                            and value.get("aggregationType") in (None, "WorstOf")
                            and set(value) <= {"aggregationType", "ignoreUnknown"}
                            and ("ignoreUnknown" not in value or value["ignoreUnknown"] is True)
                        )
                        if not default_dependencies:
                            mismatches.append(f"{path}.signalGroups.{group}")


def compare_readback_signal_overrides(
    expected: Any, actual: Any, definitions: Any, mismatches: list[str],
) -> None:
    expected_items = named_items(expected, "expected entities")
    actual_items = named_items(actual, "actual entities")
    definition_items = named_items(definitions, "expected signal definitions")
    for name in expected_items.keys() & actual_items.keys():
        expected_groups = expected_items[name]["properties"].get("signalGroups") or {}
        actual_groups = actual_items[name]["properties"].get("signalGroups") or {}
        if not isinstance(expected_groups, dict) or not isinstance(actual_groups, dict):
            continue
        for group_name in ("azureResource", "azureLogAnalytics"):
            expected_group = expected_groups.get(group_name) or {}
            actual_group = actual_groups.get(group_name) or {}
            if not isinstance(expected_group, dict) or not isinstance(actual_group, dict):
                continue
            expected_list = expected_group.get("signals", [])
            actual_list = actual_group.get("signals", [])
            if not isinstance(expected_list, list) or not isinstance(actual_list, list):
                raise EvaluationError("readback contains an invalid signal list")
            expected_signals = {
                signal["name"]: signal for signal in expected_list
                if isinstance(signal, dict) and isinstance(signal.get("name"), str)
            }
            actual_signals = {
                signal["name"]: signal for signal in actual_list
                if isinstance(signal, dict) and isinstance(signal.get("name"), str)
            }
            for signal_name in expected_signals.keys() & actual_signals.keys():
                expected_signal = expected_signals[signal_name]
                actual_signal = actual_signals[signal_name]
                path = f"entities.{name}.properties.signalGroups.{group_name}.signals.{signal_name}"
                definition_name = expected_signal.get("signalDefinitionName")
                if definition_name:
                    if not isinstance(definition_name, str):
                        raise EvaluationError("readback contains an invalid signal definition reference")
                    definition = definition_items.get(definition_name)
                    if definition is None:
                        mismatches.append(f"{path}.signalDefinitionName")
                        continue
                    for field in (
                        "metricNamespace", "metricName", "aggregationType", "dataUnit", "timeGrain",
                        "refreshInterval", "evaluationRules", "displayName",
                    ):
                        if field in actual_signal and field in definition["properties"]:
                            compare_readback_fields(
                                definition["properties"][field], actual_signal[field], f"{path}.{field}", mismatches,
                            )
                elif actual_signal.get("signalDefinitionName"):
                    mismatches.append(f"{path}.signalDefinitionName")
                if actual_signal.get("dimensionFilter") and not expected_signal.get("dimensionFilter"):
                    mismatches.append(f"{path}.dimensionFilter")


def verify_readback_model(
    expected: dict[str, Any], roles: list[dict[str, Any]], subscription: str, resource_group: str,
) -> dict[str, Any]:
    name = expected["name"]
    resource_id = (
        f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CloudHealth/healthmodels/{name}"
    )
    model_url = (
        f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/"
        f"{urllib.parse.quote(resource_group, safe='')}/providers/Microsoft.CloudHealth/healthmodels/{name}"
    )
    result: dict[str, Any] = {
        "model": name, "captured_at": timestamp(), "status": "incomplete", "mismatches": [], "reason": None,
    }
    mismatches: list[str] = result["mismatches"]
    stage = "model"
    try:
        model = azure_get(f"{model_url}?api-version={API_VERSION}", subscription)
        if not isinstance(model.get("properties"), dict) or model["properties"].get("provisioningState") != "Succeeded":
            raise EvaluationError("model deployment is not succeeded")
        if not isinstance(model.get("id"), str) or model["id"].lower() != resource_id.lower():
            mismatches.append("model.id")
        compare_readback_fields(expected["model"], model, "model", mismatches)
        identity = model.get("identity")
        if not isinstance(identity, dict) or not isinstance(identity.get("principalId"), str):
            raise EvaluationError("model identity principal is unavailable")
        try:
            principal = str(uuid.UUID(identity["principalId"]))
        except ValueError as exc:
            raise EvaluationError("model identity principal is invalid") from exc
        for field, collection in (
            ("entities", "entities"),
            ("relationships", "relationships"),
            ("signalDefinitions", "signaldefinitions"),
            ("authenticationSettings", "authenticationsettings"),
        ):
            stage = field
            actual = list_model_items(model_url, subscription, collection)
            compare_readback_collection(expected[field], actual, field, mismatches, root=name if field == "entities" else None)
            for item in actual:
                item_id = item.get("id")
                if not isinstance(item_id, str) or item_id.lower() != (
                    f"{resource_id}/{collection}/{item['name']}".lower()
                ):
                    mismatches.append(f"{field}.{item['name']}.id")
            if field == "entities":
                compare_readback_signal_overrides(expected[field], actual, expected["signalDefinitions"], mismatches)
        for role in roles:
            if role["principalModel"] != name:
                continue
            stage = "role assignment"
            actual = azure_get(f"https://management.azure.com{role['id']}?api-version={ROLE_API_VERSION}", subscription)
            expected_role = {
                "id": role["id"],
                "properties": {
                    "scope": role["scope"], "roleDefinitionId": role["roleDefinitionId"],
                    "principalId": principal, "principalType": "ServicePrincipal",
                },
            }
            compare_readback_fields(expected_role, actual, f"roles.{role['id'].rsplit('/', 1)[-1]}", mismatches)
    except EvaluationError as exc:
        result["reason"] = f"{stage}: {exc}"
        result["status"] = "unavailable" if stage == "model" and str(exc) == "resource not found" else "incomplete"
        result["completed_at"] = timestamp()
        return result
    result["mismatches"] = sorted(set(mismatches))
    result["status"] = "mismatch" if result["mismatches"] else "verified"
    result["completed_at"] = timestamp()
    return result


def timeout_argument(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not 0 < number <= MAX_REQUEST_TIMEOUT:
        raise argparse.ArgumentTypeError(f"timeout must be greater than 0 and at most {MAX_REQUEST_TIMEOUT:g} seconds")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = root.add_subparsers(dest="mode", required=True)
    for name in ("readiness", "usage", "snapshot", "readback"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--environment", help="explicit azd environment name")
        sub.add_argument("--config", help="JSON configuration file, optionally overriding azd values")
        if name in ("readiness", "usage"):
            sub.add_argument("--timeout", type=timeout_argument, default=DEFAULT_REQUEST_TIMEOUT,
                             help="hard per-request deadline in seconds (maximum 115)")
        if name == "usage":
            sub.add_argument(
                "--backends", nargs="+", choices=BACKENDS, default=list(BACKENDS),
                help="assistants to exercise (default: all; a subset is partial readiness)",
            )
        if name == "snapshot":
            sub.add_argument("--label", default="observation", help="capture phase label")
        if name == "readback":
            sub.add_argument("--contract", required=True, help="validated expected readback contract JSON")
            sub.add_argument("--contract-sha256", required=True, help="SHA256 recorded for the validated contract")
    return root


def main() -> int:
    args = parser().parse_args()
    started_at = timestamp()
    if args.mode == "usage":
        args.backends = list(dict.fromkeys(args.backends))
    backends = args.backends if args.mode == "usage" else BACKENDS
    try:
        if not args.environment and not args.config:
            raise EvaluationError("specify --environment or --config")
        config = load_config(args.config)
        values = environment_values(args.environment) if args.environment else {}
        if args.mode == "readback":
            subscription, resource_group = scope(values, config)
            contract, contract_hash = load_readback_contract(
                args.contract, args.contract_sha256, subscription, resource_group,
            )
            models = [
                verify_readback_model(model, contract["roles"], subscription, resource_group)
                for model in contract["models"]
            ]
            verified = all(model["status"] == "verified" for model in models)
            result = {
                "phase": "readback", "started_at": started_at, "completed_at": timestamp(),
                "status": "verified" if verified else "incomplete", "contract_sha256": contract_hash, "models": models,
                "limitations": ["Additional role assignments are not enumerated; signal health is checked separately."],
            }
            exit_code = 0 if verified else 1
        elif args.mode == "snapshot":
            subscription, resource_group = scope(values, config)
            models = [snapshot_model(name, profile, subscription, resource_group) for name, profile in MODELS.items()]
            result = {
                "phase": "snapshot", "label": args.label, "started_at": started_at,
                "completed_at": timestamp(), "api_version": API_VERSION, "models": models,
            }
            exit_code = 0 if all(model["status"] == "available" for model in models) else 1
        else:
            urls = endpoints(values, config, backends)
            ready, tools = readiness(urls, args.timeout, backends)
            if args.mode == "readiness":
                result = {"phase": "readiness", "started_at": started_at, **ready, "completed_at": timestamp()}
                exit_code = 0 if ready["ready"] else 1
            else:
                result = run_usage(urls, ready, tools, args)
                exit_code = 0 if result["status"] == "ok" else 1
    except EvaluationError as exc:
        result = {"phase": args.mode, "started_at": started_at, "status": "blocked", "reason": str(exc),
                  "completed_at": timestamp()}
        if args.mode == "usage":
            result["scope"] = backend_scope(args.backends)
        exit_code = 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    if sys.argv[1:] == ["--http-worker"]:
        try:
            print(json.dumps(http_worker(json.load(sys.stdin)), ensure_ascii=True))
        except Exception as exc:
            print(json.dumps({"status": "failed", "reason": f"request worker error ({type(exc).__name__})"}))
            sys.exit(1)
    else:
        sys.exit(main())
