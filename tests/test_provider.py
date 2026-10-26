from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.live_llm_smoke import run_smoke
from repo_agent.agent import Agent
from repo_agent.decisions import Decision, DecisionError, parse_tool_decision
from repo_agent.provider import OpenAICompatibleProvider, ProviderError
from repo_agent.tools import RepoTools


def response(message, finish_reason="tool_calls"):
    return io.BytesIO(json.dumps({"choices": [{
        "message": message, "finish_reason": finish_reason,
    }]}, ensure_ascii=False).encode("utf-8"))


class ProviderTest(unittest.TestCase):
    def setUp(self):
        config = patch("repo_agent.provider.read_dotenv", return_value={})
        config.start()
        self.addCleanup(config.stop)
        self.provider = OpenAICompatibleProvider(
            model="test-model", base_url="https://model.invalid/v1", api_key="")

    def test_request_declares_tools_and_code_round_trips(self):
        args = {"snapshot_id": "snapshot_test", "start": 1, "end": 1,
                "new_text": 'print("a\\b")\n\t# 中文\n'}
        native_message = Decision("replace_lines", args, "call_edit").assistant_message
        native_message["reasoning_content"] = "opaque-provider-metadata"
        with patch("repo_agent.provider.urllib.request.urlopen",
                   return_value=response(native_message)) as request:
            decision = self.provider.decide([{"role": "user", "content": "Repair code"}])

        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual("coding-agent/0.1", request.call_args.args[0].get_header("User-agent"))
        self.assertEqual("test-model", sent["model"])
        self.assertEqual("auto", sent["tool_choice"])
        self.assertFalse(sent["parallel_tool_calls"])
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in sent["tools"]}
        self.assertIn("replace_lines", schemas)
        self.assertNotIn("replace_text", schemas)
        self.assertNotIn("apply_patch", schemas)
        self.assertFalse(schemas["replace_lines"]["additionalProperties"])
        self.assertEqual({"snapshot_id", "start", "end", "new_text"}, set(schemas["replace_lines"]["required"]))
        self.assertEqual(args, decision.args)
        self.assertEqual(native_message, decision.assistant_message)
        self.assertEqual("call_edit", decision.tool_call_id)
        self.assertEqual({"action": "replace_lines", "args": args},
                         json.loads(json.dumps(decision.as_dict(), ensure_ascii=False)))

    def test_native_two_turn_smoke_preserves_tool_pair_and_metadata(self):
        requests = []
        first = Decision("list_files", {"subdir": "."}, "call_list").assistant_message
        first["reasoning_content"] = "opaque-provider-metadata"

        def send(request, **kwargs):
            payload = json.loads(request.data)
            requests.append(payload)
            if len(requests) == 1:
                return response(first)
            history = payload["messages"]
            self.assertEqual(first, history[-2])
            self.assertEqual("tool", history[-1]["role"])
            self.assertEqual("call_list", history[-1]["tool_call_id"])
            observation = json.loads(history[-1]["content"])
            marker = observation["data"]["files"][0]
            return response(Decision("finish", {"answer": marker}, "call_finish").assistant_message)

        with tempfile.TemporaryDirectory() as directory, patch(
                "repo_agent.provider.urllib.request.urlopen", side_effect=send):
            self.assertTrue(run_smoke(self.provider, Path(directory))["smoke_passed"])
        self.assertEqual(2, len(requests))

    def test_invalid_arguments_are_rejected_and_can_be_corrected(self):
        bad = Decision("read_file", {"path": "sample.py"}, "call_bad").assistant_message
        bad["tool_calls"][0]["function"]["arguments"] = '{"path": "sample.py"'
        requests = []

        def send(request, **kwargs):
            payload = json.loads(request.data)
            requests.append(payload)
            if len(requests) == 1:
                return response(bad)
            feedback = payload["messages"][-1]
            self.assertEqual("tool", feedback["role"])
            self.assertEqual("call_bad", feedback["tool_call_id"])
            self.assertFalse(json.loads(feedback["content"])["ok"])
            self.assertEqual(bad, payload["messages"][-2])
            return response(Decision("finish", {"answer": "Cannot proceed."}).assistant_message)

        with tempfile.TemporaryDirectory() as directory, patch(
                "repo_agent.provider.urllib.request.urlopen", side_effect=send):
            tools = RepoTools(Path(directory))
            with patch.object(tools, "execute", wraps=tools.execute) as execute:
                agent = Agent(self.provider, tools, trace_dir=Path(directory) / "traces", max_steps=2)
                result = agent.run("Inspect code")
                execute.assert_not_called()
            events = [json.loads(line) for line in agent.trace_path.read_text(encoding="utf-8").splitlines()]
            error = next(event for event in events if event["type"] == "decision_error")
            self.assertTrue(error["recoverable"])
            self.assertEqual(bad, error["response"])
        self.assertEqual("agent_reported_finished", result["status"])
        self.assertEqual(2, result["steps"])
        self.assertFalse(result["verified_local_change"])

    def test_multiple_calls_reject_entire_batch_without_writes(self):
        call1 = Decision("replace_lines", {"snapshot_id": "s", "start": 1, "end": 1, "new_text": "changed"}, "first")
        call2 = Decision("replace_lines", {"snapshot_id": "s", "start": 1, "end": 1, "new_text": "again"}, "second")
        batch = deepcopy(call1.assistant_message)
        batch["tool_calls"].extend(call2.assistant_message["tool_calls"])
        requests = []

        def send(request, **kwargs):
            payload = json.loads(request.data)
            requests.append(payload)
            if len(requests) == 1:
                return response(batch)
            self.assertEqual(batch, payload["messages"][-3])
            feedback = payload["messages"][-2:]
            self.assertEqual(["first", "second"], [item["tool_call_id"] for item in feedback])
            self.assertTrue(all(item["role"] == "tool" and not json.loads(item["content"])["ok"]
                                for item in feedback))
            return response(Decision("finish", {"answer": "No edits made."}).assistant_message)

        with tempfile.TemporaryDirectory() as directory, patch(
                "repo_agent.provider.urllib.request.urlopen", side_effect=send):
            root = Path(directory)
            (root / "sample.py").write_bytes(b"original")
            tools = RepoTools(root, allow_writes=True)
            with patch.object(tools, "execute", wraps=tools.execute) as execute:
                result = Agent(self.provider, tools, trace_dir=root / "traces", max_steps=2).run("Edit sample")
                execute.assert_not_called()
            self.assertEqual(b"original", (root / "sample.py").read_bytes())
        self.assertEqual("agent_reported_finished", result["status"])

    def test_repeated_protocol_errors_stop_at_existing_step_budget(self):
        ordinary_text = {"role": "assistant", "content": '{"action":"finish","args":{"answer":"done"}}'}
        with tempfile.TemporaryDirectory() as directory, patch(
                "repo_agent.provider.urllib.request.urlopen", side_effect=lambda *a, **k: response(ordinary_text)) as send:
            root = Path(directory)
            tools = RepoTools(root)
            with patch.object(tools, "execute", wraps=tools.execute) as execute:
                result = Agent(self.provider, tools, trace_dir=root / "traces", max_steps=3).run("Inspect")
                execute.assert_not_called()
        self.assertEqual("step_limit", result["status"])
        self.assertEqual(3, send.call_count)

    def test_schema_rejects_wrong_names_shapes_types_and_ranges(self):
        valid = Decision("read_file", {"path": "sample.py"}).assistant_message
        bad_arguments = ["[]", "null", '"text"', '{"path": "x", "extra": 1}',
                         '{"start": 1}', '{"path": "x", "start": true}',
                         '{"path": "x", "start": "1"}',
                         '{"path": "x", "start": 1, "end": 202}']
        for arguments in bad_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(DecisionError):
                message = deepcopy(valid)
                message["tool_calls"][0]["function"]["arguments"] = arguments
                parse_tool_decision(message)
        for field, value in (("id", ""), ("type", "other")):
            with self.subTest(field=field), self.assertRaises(DecisionError):
                message = deepcopy(valid)
                message["tool_calls"][0][field] = value
                parse_tool_decision(message)
        message = deepcopy(valid)
        message["tool_calls"][0]["function"]["name"] = "shell"
        with self.assertRaises(DecisionError):
            parse_tool_decision(message)

    def test_truncated_response_never_becomes_a_decision(self):
        message = Decision("run_tests", {"target": "tests"}).assistant_message
        with patch("repo_agent.provider.urllib.request.urlopen", return_value=response(message, "length")):
            with self.assertRaisesRegex(ProviderError, "truncated"):
                self.provider.decide([])

    def test_missing_call_id_does_not_create_orphaned_tool_feedback(self):
        message = Decision("read_file", {"path": "x"}).assistant_message
        message["tool_calls"][0].pop("id")
        with self.assertRaises(DecisionError) as error:
            parse_tool_decision(message)
        feedback = error.exception.feedback_messages()
        self.assertEqual(["user"], [item["role"] for item in feedback])

    def test_invalid_call_shape_is_not_replayed_as_valid_history(self):
        message = Decision("read_file", {"path": "x"}).assistant_message
        message["tool_calls"][0]["type"] = "invalid"
        with self.assertRaises(DecisionError) as error:
            parse_tool_decision(message)
        self.assertEqual(["user"], [item["role"] for item in error.exception.feedback_messages()])

    def test_native_calls_cannot_bypass_snapshot_checks_with_legacy_editors(self):
        for action, args in (("replace_text", {"path": "x", "old_text": "a", "new_text": "b"}),
                             ("apply_patch", {"patch": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"})):
            with self.subTest(action=action), self.assertRaises(DecisionError):
                parse_tool_decision(Decision(action, args).assistant_message)
