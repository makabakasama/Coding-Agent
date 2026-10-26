"""Program-owned tool schemas, validated decisions, and protocol feedback."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any
import uuid


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


TOOL_DEFINITIONS = [
    _tool("list_files", "List text files inside the repository.", {
        "subdir": {"type": "string", "minLength": 1, "default": "."},
    }, []),
    
    _tool("search_code", "Search repository text for a literal substring.", {
        "query": {"type": "string", "minLength": 1, "maxLength": 100},
    }, ["query"]),
    _tool("read_file", "Read at most 201 inclusive lines. Returns snapshot_id, whole-file sha256, and actual displayed start/end.", {
        "path": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 1, "default": 1},
        "end": {"type": "integer", "minimum": 1, "default": 160},
    }, ["path"]),
    _tool("replace_lines", "Replace complete displayed lines using a read_file snapshot. Rejects any file version change. Reread after each edit.", {
        "snapshot_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "start": {"type": "integer", "minimum": 1},
        "end": {"type": "integer", "minimum": 1},
        "new_text": {"type": "string", "maxLength": 60000},
    }, ["snapshot_id", "start", "end", "new_text"]),
    _tool("run_tests", "Execute focused pytest tests and return their actual results.", {
        "target": {"type": "string", "minLength": 1, "maxLength": 220, "default": "."},
    }, []),
    _tool("finish", "End the run with a summary, actual test results, and unverified items.", {
        "answer": {"type": "string"},
    }, ["answer"]),
]
# Existing local/offline callers can still use these helpers. They are not
# advertised or accepted as native model calls, which must use snapshots.
_LEGACY_TOOL_DEFINITIONS = [
    _tool("replace_text", "Legacy exact text replacement.", {
        "path": {"type": "string", "minLength": 1},
        "old_text": {"type": "string", "minLength": 1, "maxLength": 60000},
        "new_text": {"type": "string", "maxLength": 60000},
    }, ["path", "old_text", "new_text"]),
    _tool("apply_patch", "Legacy unified diff application.", {
        "patch": {"type": "string", "minLength": 1, "maxLength": 60000},
    }, ["patch"]),
]
_NATIVE_TOOL_NAMES = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
_PARAMETERS = {tool["function"]["name"]: tool["function"]["parameters"]
               for tool in TOOL_DEFINITIONS + _LEGACY_TOOL_DEFINITIONS}


def _validate(action: str, args: dict) -> None:
    """Validate the flat string/integer schemas used by this MVP."""
    if not isinstance(action, str) or action not in _PARAMETERS:
        raise ValueError("Unknown tool name")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    schema = _PARAMETERS[action]
    if set(schema["required"]) - args.keys():
        raise ValueError(f"Missing required arguments for {action}")
    if args.keys() - schema["properties"].keys():
        raise ValueError(f"Unexpected arguments for {action}")
    for name, value in args.items():
        spec = schema["properties"][name]
        expected = str if spec["type"] == "string" else int
        if type(value) is not expected:
            raise ValueError(f"{action}.{name} must be {spec['type']}")
        if isinstance(value, str):
            if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", float("inf")):
                raise ValueError(f"{action}.{name} has invalid length")
        elif value < spec.get("minimum", float("-inf")):
            raise ValueError(f"{action}.{name} is below its minimum")
    if action in ("read_file", "replace_lines"):
        start, end = args.get("start", 1), args.get("end", 160)
        if end < start or end - start > 200:
            raise ValueError(f"{action} requires start <= end and at most 201 inclusive lines")


@dataclass
class Decision:
    """The model selects values; Python constructs the internal decision/message."""
    action: str
    args: dict
    tool_call_id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex)
    assistant_message: dict[str, Any] | None = None

    def __post_init__(self):
        _validate(self.action, self.args)
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
            raise ValueError("A nonempty tool call id is required")
        if self.assistant_message is None:
            # Used by offline models; all JSON quoting is handled by Python.
            self.assistant_message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": self.tool_call_id, "type": "function", "function": {
                    "name": self.action, "arguments": json.dumps(self.args, ensure_ascii=False),
                },
            }]}

    def as_dict(self) -> dict:
        return {"action": self.action, "args": self.args}

    def feedback_message(self, observation: dict) -> dict:
        return {"role": "tool", "tool_call_id": self.tool_call_id,
                "content": json.dumps(observation, ensure_ascii=False)}


class DecisionError(ValueError):
    """Recoverable model-protocol error; no tool has executed."""
    def __init__(self, reason: str, message: Any):
        super().__init__(reason)
        self.message = deepcopy(message)

    def feedback_messages(self) -> list[dict]:
        observation = {"ok": False, "error": str(self),
                       "hint": "Call exactly one declared function; use finish to end the run."}
        message = self.message if isinstance(self.message, dict) else {}
        calls = message.get("tool_calls")
        if message.get("role") == "assistant" and isinstance(calls, list) and calls:
            ids = [call.get("id") if isinstance(call, dict) else None for call in calls]
            valid_shape = all(
                isinstance(call, dict) and call.get("type") == "function"
                and isinstance(call.get("function"), dict)
                and isinstance(call["function"].get("name"), str)
                and isinstance(call["function"].get("arguments"), str)
                for call in calls
            )
            if (valid_shape and all(isinstance(call_id, str) and call_id for call_id in ids)
                    and len(set(ids)) == len(ids)):
                # Reject the whole batch, with one result for every pending id.
                return [deepcopy(self.message)] + [
                    {"role": "tool", "tool_call_id": call_id,
                     "content": json.dumps(observation, ensure_ascii=False)} for call_id in ids
                ]
        # Missing/invalid ids cannot form a valid tool-result pair.
        return [{"role": "user", "content": json.dumps(observation, ensure_ascii=False)}]


def parse_tool_decision(message: Any) -> Decision:
    """Read native tool fields, never action/args JSON from ordinary content."""
    def reject(reason):
        raise DecisionError(reason, message)

    if not isinstance(message, dict) or message.get("role") != "assistant":
        reject("Expected an assistant tool-call message")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        reject("Expected exactly one native tool call")
    call = calls[0]
    if not isinstance(call, dict) or call.get("type") != "function":
        reject("Expected a function tool call")
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
        reject("Expected a function name and encoded argument object")
    if not isinstance(function.get("name"), str) or function["name"] not in _NATIVE_TOOL_NAMES:
        reject("Tool is not offered to the model; edit with read_file then replace_lines")
    try:
        args = json.loads(function["arguments"])
    except (ValueError, TypeError):
        reject("Tool arguments are not valid JSON; return them through the function interface again")
    try:
        # Retain the complete API message, including reasoning metadata needed
        # by some providers when tool-call history is sent in the next request.
        return Decision(function.get("name"), args, call.get("id"), deepcopy(message))
    except ValueError as exc:
        reject(str(exc))
