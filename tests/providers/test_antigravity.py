import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import MagicMock, Mock, patch

from commonroom.providers.antigravity import AntigravityProvider
from commonroom.providers.base import SessionLost, TurnResult

from .fakes import FakePlatform, context, turn

SESSION = "native-session"


def options(effort):
    return [
        {"id": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon"}]},
        {"id": "thought_level", "currentValue": effort, "options": [{"value": "medium"}, {"value": "high"}]},
    ]


class FakeAcp:
    def __init__(self, case, steps):
        self.case = case
        self.steps = iter(steps)
        self.notifications = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.case.assertEqual(list(self.steps), [])

    def call(self, method, params, on_event, on_request):
        expected_method, result = next(self.steps)
        self.case.assertEqual(method, expected_method)
        if callable(result):
            return result(params, on_event, on_request)
        return result

    def notify(self, method, params):
        self.notifications.append((method, params))


class AntigravityTurnTests(unittest.TestCase):
    def setUp(self):
        self.platform = FakePlatform({"agy": "agy"})
        self.provider = AntigravityProvider(self.platform)

    def invoke(self, request, steps, ctx=None, process=None):
        ctx = ctx or context()
        process = process or FakeAcp(self, steps)
        with patch("commonroom.providers.antigravity.RpcProcess", return_value=process) as transport:
            result = self.provider.run_turn(request, ctx)
            transport.assert_called_once_with(self.platform, ["agy", "acp", "--stdio", "--dangerously-skip-permissions"],
                                              Path.cwd().resolve(), ctx.cancel)
        return result, [call.args[0] for call in ctx.activity.call_args_list], ctx

    def test_fresh_session_selects_effort_and_relays_permission_and_final_answer(self):
        asked = []

        def ask(title, detail, choices, withdrawn):
            asked.append((title, detail, choices))
            return "reject-once"

        def configure(params, _on_event, _on_request):
            self.assertEqual(params, {"sessionId": SESSION, "configId": "thought_level", "value": "medium"})
            return {"configOptions": options("medium")}

        def prompt(params, on_event, on_request):
            self.assertEqual(params, {"sessionId": SESSION, "prompt": [{"type": "text", "text": "Room prompt"}]})

            def update(kind, **fields):
                on_event("session/update", {"sessionId": SESSION, "update": {"sessionUpdate": kind, **fields}})

            update("agent_thought_chunk", content={"type": "text", "text": "Private thoughts"})
            update("agent_message_chunk", content={"type": "text", "text": "I'll update it."})
            update("tool_call", toolCallId="edit", title="Edit note.txt", status="pending", rawInput={"path": "note.txt"})
            outcome = on_request("session/request_permission", {
                "sessionId": SESSION,
                "toolCall": {"toolCallId": "edit"},
                "options": [
                    {"optionId": "allow-once", "name": "Allow once"},
                    {"optionId": "reject-once", "name": "Reject"},
                ],
            })
            self.assertEqual(outcome, {"outcome": {"outcome": "selected", "optionId": "reject-once"}})
            update("agent_message_chunk", content={"type": "text", "text": "Edit "}, messageId="final")
            update("agent_message_chunk", content={"type": "text", "text": "declined."}, messageId="final")
            return {"stopReason": "end_turn"}

        reply, activity, _ = self.invoke(turn("Room prompt", model="gemini-4-neon-medium"), [
            ("initialize", {"protocolVersion": 1, "agentCapabilities": {"sessionCapabilities": {"close": {}}}}),
            ("session/new", {"sessionId": SESSION, "configOptions": options("high")}),
            ("session/set_config_option", configure),
            ("session/prompt", prompt),
            ("session/close", {}),
        ], context(ask))
        self.assertEqual(reply, TurnResult("Edit declined.", SESSION))
        self.assertEqual(activity, ["Thinking", "Edit note.txt"])
        self.assertEqual(asked, [("Edit note.txt", '{\n  "path": "note.txt"\n}', [
            {"id": "allow-once", "label": "Allow once"}, {"id": "reject-once", "label": "Reject"}
        ])])

    def test_load_fallback_discards_replayed_text(self):
        def load(params, on_event, _on_request):
            self.assertEqual(params, {"sessionId": SESSION, "cwd": str(Path.cwd().resolve()), "mcpServers": []})
            on_event("session/update", {"sessionId": SESSION, "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Prior reply."}
            }})
            return {"configOptions": options("medium")}

        def prompt(params, on_event, _on_request):
            self.assertEqual(params["sessionId"], SESSION)
            on_event("session/update", {"sessionId": SESSION, "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "New reply."}
            }})
            return {"stopReason": "end_turn"}

        reply, activity, _ = self.invoke(
            turn("Room prompt", model="gemini-4-neon", effort="medium", session=SESSION, session_workspace=str(Path.cwd())), [
                ("initialize", {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}),
                ("session/load", load),
                ("session/prompt", prompt),
            ])
        self.assertEqual(reply, TurnResult("New reply.", SESSION))
        self.assertEqual(activity, ["Thinking"])

    def test_model_without_thinking_selector_uses_native_default_and_persists_session(self):
        model = "gemini-pro-orchestrator-neon"
        ctx = context()

        def configure(params, _on_event, _on_request):
            ctx.remember_session.assert_called_once_with(SESSION)
            self.assertEqual(params, {"sessionId": SESSION, "configId": "model", "value": model})
            return {"configOptions": [{"id": "model", "currentValue": model, "options": [{"value": model}]}]}

        reply, _, _ = self.invoke(turn(model=model, effort="medium"), [
            ("initialize", {"protocolVersion": 1, "agentCapabilities": {}}),
            ("session/new", {"sessionId": SESSION, "configOptions": options("high")}),
            ("session/set_config_option", configure),
            ("session/prompt", {"stopReason": "end_turn"}),
        ], ctx)
        self.assertEqual(reply, TurnResult("", SESSION))

    def test_cancelled_permission_is_silent_and_keeps_session(self):
        def prompt(params, on_event, on_request):
            self.assertEqual(params["sessionId"], SESSION)
            outcome = on_request("session/request_permission", {
                "sessionId": SESSION, "toolCall": {"title": "Edit note.txt"},
                "options": [{"optionId": "allow-once", "name": "Allow once"}],
            })
            self.assertEqual(outcome, {"outcome": {"outcome": "cancelled"}})
            on_event("session/update", {"sessionId": SESSION, "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Partial reply."}
            }})
            return {"stopReason": "cancelled"}

        reply, _, _ = self.invoke(turn(), [
            ("initialize", {"protocolVersion": 1, "agentCapabilities": {}}),
            ("session/new", {"sessionId": SESSION}),
            ("session/prompt", prompt),
        ], context(lambda *_: None))
        self.assertEqual(reply, TurnResult("", SESSION))

    def test_missing_session_follows_native_load_hint_and_raises_session_lost(self):
        def resume(params, _on_event, _on_request):
            self.assertEqual(params["sessionId"], SESSION)
            raise RuntimeError(f"Failed to read session {SESSION}; retry, or load it with session/load")

        def load(params, _on_event, _on_request):
            self.assertEqual(params["sessionId"], SESSION)
            raise RuntimeError(f"Session not found: {SESSION}")

        with self.assertRaisesRegex(SessionLost, "Antigravity session not found"):
            self.invoke(turn(session=SESSION), [
                ("initialize", {"protocolVersion": 1, "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"resume": {}}}}),
                ("session/resume", resume),
                ("session/load", load),
            ])

    def test_session_lost_during_the_prompt_raises_session_lost_for_a_fresh_retry(self):
        def prompt(_params, _on_event, _on_request):
            raise RuntimeError("session/prompt: Unknown session " + SESSION)

        with self.assertRaises(SessionLost):
            self.invoke(turn(session=SESSION), [
                ("initialize", {"protocolVersion": 1, "agentCapabilities": {"sessionCapabilities": {"resume": {}}}}),
                ("session/resume", {}),
                ("session/prompt", prompt),
            ])

    def test_lost_session_text_from_a_brand_new_session_is_an_ordinary_error(self):
        def prompt(_params, _on_event, _on_request):
            raise RuntimeError("session/prompt: session not found")

        with self.assertRaises(RuntimeError) as raised:
            self.invoke(turn(), [
                ("initialize", {"protocolVersion": 1, "agentCapabilities": {}}),
                ("session/new", {"sessionId": SESSION}),
                ("session/prompt", prompt),
            ])
        self.assertNotIsInstance(raised.exception, SessionLost)

    def test_session_from_another_folder_is_lost_before_starting_the_tool(self):
        with TemporaryDirectory() as other, patch("commonroom.providers.antigravity.RpcProcess") as transport:
            with self.assertRaises(SessionLost):
                self.provider.run_turn(turn(session=SESSION, session_workspace=other), context())
        transport.assert_not_called()

    def test_stop_while_waiting_for_permission_relays_native_cancel_and_keeps_session(self):
        cancel = Event()

        def ask(_title, _detail, _choices, _withdrawn):
            cancel.set()
            raise RuntimeError("Turn stopped.")

        def prompt(_params, _on_event, on_request):
            outcome = on_request("session/request_permission", {
                "sessionId": SESSION, "toolCall": {"title": "Edit note.txt"},
                "options": [{"optionId": "allow-once", "name": "Allow once"}],
            })
            self.assertEqual(outcome, {"outcome": {"outcome": "cancelled"}})
            process.on_cancel()
            raise RuntimeError("Turn stopped.")

        process = FakeAcp(self, [
            ("initialize", {"protocolVersion": 1, "agentCapabilities": {}}),
            ("session/new", {"sessionId": SESSION}),
            ("session/prompt", prompt),
        ])
        reply, _, _ = self.invoke(turn(), None, context(ask, cancel), process)
        self.assertEqual(reply, TurnResult("", SESSION))
        self.assertEqual(process.notifications, [("session/cancel", {"sessionId": SESSION})])


class AntigravityModelTests(unittest.TestCase):
    def test_antigravity_keeps_fixed_model_suffixes_and_model_specific_efforts(self):
        rpc = MagicMock()
        rpc.__enter__.return_value = rpc
        available = {"id": "model", "currentValue": "gemini-4-neon", "options": [
            {"group": "main", "name": "Gemini", "options": [
                {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
                {"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"},
            ]},
            {"value": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
        ]}
        neon = {"id": "thought_level", "currentValue": "high", "options": [
            {"value": "low", "name": "Low"}, {"value": "high", "name": "High"},
        ]}
        carbon = {"id": "thought_level", "currentValue": "medium", "options": [
            {"value": "medium", "name": "Medium"}, {"value": "high", "name": "High"},
        ]}
        rpc.call.side_effect = [{"protocolVersion": 1}, {"sessionId": "discovery", "configOptions": [available, neon]},
                                {"configOptions": [available, carbon]}, {"configOptions": [available]}]
        with patch("commonroom.providers.antigravity.RpcProcess", return_value=rpc):
            models = AntigravityProvider(FakePlatform({"agy": "agy"})).list_models(Path.cwd(), Event())
        self.assertEqual([model.to_dict() for model in models], [
            {"id": "gemini-4-neon", "label": "Gemini 4 Neon", "efforts": ["low", "high"],
             "defaultEffort": "high", "effortLabels": {"low": "Low", "high": "High"}},
            {"id": "gemini-4-carbon", "label": "Gemini 4 Carbon", "efforts": ["medium", "high"],
             "defaultEffort": "medium", "effortLabels": {"medium": "Medium", "high": "High"}},
            {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)", "efforts": []},
        ])
        self.assertEqual([call.args[0] for call in rpc.call.call_args_list], [
            "initialize", "session/new", "session/set_config_option", "session/set_config_option",
        ])
        self.assertEqual(rpc.call.call_args.args[1], {
            "sessionId": "discovery", "configId": "model", "value": "gpt-oss-120b-medium",
        })


if __name__ == "__main__":
    unittest.main()
