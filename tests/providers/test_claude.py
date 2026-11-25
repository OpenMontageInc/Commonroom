import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, Mock, patch

from commonroom.providers.base import SessionLost, TurnResult
from commonroom.providers.claude import ClaudeProvider, control

from .fakes import FakePlatform, context, turn


class ClaudeControls(unittest.TestCase):
    def test_explicit_allow_and_decline(self):
        request = {"subtype": "can_use_tool", "tool_name": "Write", "input": {"file_path": "example.txt"}}
        denied = control(request, Mock(return_value="deny"))
        self.assertEqual(denied, {"behavior": "deny", "message": "The user declined this tool request."})
        self.assertEqual(control(request, Mock(return_value="allow")), {"behavior": "allow", "updatedInput": request["input"]})

    def test_provider_permission_change_requires_its_own_choice(self):
        request = {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "pwd"},
                   "permission_suggestions": [{"type": "addRules", "destination": "session"}]}
        self.assertNotIn("updatedPermissions", control(request, Mock(return_value="allow")))
        self.assertEqual(control(request, Mock(return_value="suggestion"))["updatedPermissions"], request["permission_suggestions"])

    def test_multiple_question_answers_are_relayed_to_native_tool(self):
        ask = Mock(return_value="First, Third")
        request = {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {"questions": [
            {"question": "Which sections?", "multiSelect": True, "options": [{"label": x} for x in ("First", "Second", "Third")]}]}}
        data = control(request, ask)
        self.assertEqual(data["updatedInput"]["answers"]["Which sections?"], "First, Third")
        self.assertTrue(ask.call_args.args[2][0]["multi"])


class ClaudeTurns(unittest.TestCase):
    def setUp(self):
        self.platform = FakePlatform({"claude": "claude"})
        self.provider = ClaudeProvider(self.platform)

    def run_with(self, events, request, ctx=None):
        native = MagicMock()
        native.return_value.__enter__.return_value.read.side_effect = events
        ctx = ctx or context()
        with patch("commonroom.providers.claude.RpcProcess", native):
            return self.provider.run_turn(request, ctx), native, ctx

    def test_resumed_session_starts_in_the_requested_project_directory(self):
        project = Path.cwd() / "Selected project"
        session = "existing-claude-session"
        result, native, ctx = self.run_with([
            {"type": "system", "subtype": "init", "session_id": session},
            {"type": "result", "subtype": "success", "result": "project marker", "session_id": session},
        ], turn("Open the selected project", project, model="claude-opus-5-5", session=session))
        self.assertEqual(result, TurnResult("project marker", session))
        platform, argv, cwd, _ = native.call_args.args
        self.assertIs(platform, self.platform)
        self.assertEqual(argv[-2:], ["--resume", session])
        self.assertIn("bypassPermissions", argv)
        self.assertEqual(cwd, project)
        ctx.remember_session.assert_called_once_with(session)

    def test_omits_effort_when_native_default_is_requested(self):
        result, native, _ = self.run_with(
            [{"type": "result", "subtype": "success", "session_id": "native", "result": "hello"}],
            turn("hello", model="claude-haiku-4-5-20251001"))
        self.assertEqual(result, TurnResult("hello", "native"))
        self.assertNotIn("--effort", native.call_args.args[1])

    def test_missing_conversation_raises_session_lost(self):
        events = [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "No conversation found with session ID: gone"}]
        with self.assertRaises(SessionLost):
            self.run_with(events, turn(model="opus", session="gone"))

    def test_permission_requests_reach_the_room_with_withdrawal_event(self):
        ask = Mock(return_value="allow")
        sent = []
        native = MagicMock()
        process = native.return_value.__enter__.return_value
        process.send.side_effect = sent.append
        request = {"type": "control_request", "request_id": "r1", "request": {
            "subtype": "can_use_tool", "tool_name": "Write", "input": {"file_path": "a.txt"}}}

        def events():
            yield request
            for _ in range(200):
                if any(item.get("type") == "control_response" for item in sent):
                    break
                Event().wait(0.01)
            yield {"type": "result", "subtype": "success", "session_id": "s", "result": "done"}

        process.read.side_effect = events()
        with patch("commonroom.providers.claude.RpcProcess", native):
            result = self.provider.run_turn(turn(model="opus"), context(ask))
        self.assertEqual(result.text, "done")
        self.assertIsInstance(ask.call_args.args[3], Event)
        self.assertIn({"type": "control_response", "response": {"request_id": "r1", "subtype": "success",
                                                                "response": {"behavior": "allow", "updatedInput": {"file_path": "a.txt"}}}}, sent)


class ClaudeModels(unittest.TestCase):
    def test_claude_catalog_keeps_alias_resolution_and_advertised_efforts(self):
        rpc = MagicMock()
        rpc.__enter__.return_value = rpc
        rpc.read.return_value = {"type": "control_response", "response": {
            "subtype": "success", "request_id": "commonroom-models", "response": {"models": [
                {"value": "sonnet", "resolvedModel": "claude-sonnet-5-5", "displayName": "Sonnet 5.5",
                 "supportedEffortLevels": ["low", "high"], "description": "Routine tasks"},
                {"value": "claude-haiku-4-5-20251001", "displayName": "Haiku 4.5"},
            ]},
        }}
        with patch("commonroom.providers.claude.RpcProcess", return_value=rpc):
            models = ClaudeProvider(FakePlatform({"claude": "claude"})).list_models(Path.cwd(), Event())
        self.assertEqual([model.to_dict() for model in models], [
            {"id": "sonnet", "label": "Sonnet 5.5", "resolvedModel": "claude-sonnet-5-5",
             "description": "Routine tasks", "efforts": ["low", "high"]},
            {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5", "efforts": []},
        ])
        rpc.send.assert_called_once_with({
            "type": "control_request", "request_id": "commonroom-models", "request": {"subtype": "initialize"},
        })


if __name__ == "__main__":
    unittest.main()
