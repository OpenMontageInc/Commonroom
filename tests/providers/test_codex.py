from pathlib import Path
from threading import Event
import unittest
from unittest.mock import MagicMock, Mock, patch

from commonroom.providers.base import SessionLost, TurnResult
from commonroom.providers.codex import CodexProvider, _handle_request
from commonroom.providers.rpc import RpcError

from .fakes import FakePlatform, context, turn


def fake_rpc():
    rpc = Mock()
    rpc.__enter__ = Mock(return_value=rpc)
    rpc.__exit__ = Mock(return_value=False)
    return rpc


class CodexTurnTests(unittest.TestCase):
    def setUp(self):
        self.platform = FakePlatform({"codex": "native-codex"})
        self.provider = CodexProvider(self.platform)

    def run_turn(self, rpc, request, ctx):
        with patch("commonroom.providers.codex.RpcProcess", return_value=rpc) as transport:
            return self.provider.run_turn(request, ctx), transport

    def test_native_turn_relays_approvals_and_returns_only_final_answer(self):
        calls = []
        decisions = []
        workspace = Path.cwd()
        rpc = fake_rpc()
        notification = {"threadId": "native-thread", "turnId": "current-turn"}
        command = {**notification, "command": "Get-Location", "cwd": str(workspace),
                   "availableDecisions": ["accept", "decline"]}

        def call(method, params, on_event, on_request):
            calls.append((method, params))
            if method == "thread/resume":
                return {"thread": {"id": "native-thread"}}
            if method == "turn/start":
                on_event("item/completed", {**notification, "item": {
                    "id": "thought", "type": "reasoning", "content": ["Private reasoning"],
                }})
                on_event("item/completed", {**notification, "item": {
                    "id": "first", "type": "agentMessage", "phase": "commentary", "text": "Private progress",
                }})
                decisions.append(on_request("item/commandExecution/requestApproval", command))
                return {"turn": {"id": "current-turn"}}
            return {}

        def pump_until(predicate, on_event, on_request):
            self.assertFalse(predicate())
            decisions.append(on_request("item/commandExecution/requestApproval", command))
            on_event("item/completed", {**notification, "turnId": "other-turn", "item": {
                "id": "other", "type": "agentMessage", "phase": "final_answer", "text": "Wrong turn",
            }})
            on_event("item/completed", {**notification, "item": {
                "id": "second", "type": "agentMessage", "phase": "final_answer", "text": "Public reply",
            }})
            on_event("turn/completed", {"threadId": "native-thread", "turn": {
                "id": "current-turn", "status": "completed", "items": [],
            }})
            self.assertTrue(predicate())

        rpc.call.side_effect = call
        rpc.pump_until.side_effect = pump_until
        ask = Mock(side_effect=["accept", "decline"])
        ctx = context(ask)
        request = turn(session="native-thread", model="gpt-test", effort="medium", workspace=workspace)
        result, transport = self.run_turn(rpc, request, ctx)
        self.assertEqual(result, TurnResult("Public reply", "native-thread"))
        transport.assert_called_once_with(self.platform, ["native-codex", "app-server", "--stdio"], workspace, ctx.cancel)
        rpc.notify.assert_called_once_with("initialized", {})
        self.assertEqual([method for method, _ in calls], ["initialize", "thread/resume", "turn/start"])
        self.assertEqual(calls[1][1], {
            "cwd": str(workspace), "sandbox": "danger-full-access", "approvalPolicy": "never",
            "model": "gpt-test", "threadId": "native-thread", "excludeTurns": True,
        })
        self.assertEqual(calls[2][1], {
            "threadId": "native-thread", "input": [{"type": "text", "text": "Start"}], "effort": "medium",
        })
        self.assertEqual(decisions, [{"decision": "accept"}, {"decision": "decline"}])
        self.assertIn("Get-Location", ask.call_args.args[1])
        self.assertEqual([choice["id"] for choice in ask.call_args.args[2]], ["accept", "decline"])
        ctx.remember_session.assert_called_once_with("native-thread")

    def test_user_cancel_keeps_native_session_available(self):
        rpc = fake_rpc()
        rpc.call.side_effect = [{}, {"thread": {"id": "native-thread"}}, {"turn": {"id": "turn"}}]

        def pump_until(predicate, on_event, on_request):
            result = on_request("item/commandExecution/requestApproval", {
                "threadId": "native-thread", "turnId": "turn", "availableDecisions": ["accept", "cancel"],
            })
            self.assertEqual(result, {"decision": "cancel"})
            on_event("turn/completed", {"threadId": "native-thread", "turn": {
                "id": "turn", "status": "interrupted", "items": [],
            }})

        rpc.pump_until.side_effect = pump_until
        result, _ = self.run_turn(rpc, turn(), context(Mock(return_value="cancel")))
        self.assertEqual(result, TurnResult("", "native-thread"))

    def test_stop_sends_native_interrupt_and_session_is_already_saved(self):
        rpc = fake_rpc()
        ctx = context()

        def call(method, params, on_event, on_request):
            if method == "thread/start":
                return {"thread": {"id": "root-session"}}
            if method == "turn/start":
                ctx.remember_session.assert_called_once_with("root-session")
                on_event("turn/started", {"threadId": "root-session", "turn": {"id": "active-turn"}})
                rpc.on_cancel()
                raise RuntimeError("Turn stopped.")
            return {}

        rpc.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, "Turn stopped"):
            self.run_turn(rpc, turn(), ctx)
        rpc.send.assert_called_once_with({
            "jsonrpc": "2.0", "id": "commonroom-turn-interrupt", "method": "turn/interrupt",
            "params": {"threadId": "root-session", "turnId": "active-turn"},
        })

    def test_stop_waits_until_the_acknowledged_turn_is_active(self):
        rpc = fake_rpc()
        rpc.call.side_effect = [{}, {"thread": {"id": "root"}}, {"turn": {"id": "turn"}}]

        def wait_for(predicate, timeout):
            active = {"method": "turn/started", "params": {"threadId": "root", "turn": {"id": "turn"}}}
            self.assertFalse(predicate({"method": "turn/started", "params": {"threadId": "child", "turn": {"id": "turn"}}}))
            self.assertTrue(predicate(active))
            return active

        def pump_until(predicate, on_event, on_request):
            self.assertEqual(rpc.on_cancel(), "commonroom-turn-interrupt")
            raise RuntimeError("Turn stopped.")

        rpc.wait_for.side_effect = wait_for
        rpc.pump_until.side_effect = pump_until
        with self.assertRaisesRegex(RuntimeError, "Turn stopped"):
            self.run_turn(rpc, turn(), context())
        self.assertEqual(rpc.send.call_args.args[0]["params"], {"threadId": "root", "turnId": "turn"})

    def test_helper_request_requires_native_parentage(self):
        rpc = fake_rpc()
        rpc.call.side_effect = [{}, {"thread": {"id": "root"}}, {"turn": {"id": "root-turn"}}]
        ask = Mock(return_value="accept")

        def pump_until(predicate, on_event, on_request):
            method = "item/commandExecution/requestApproval"
            approval = {"threadId": "child", "turnId": "child-turn", "availableDecisions": ["accept", "cancel"]}
            with self.assertRaisesRegex(RuntimeError, "different session"):
                on_request(method, approval)
            on_event("thread/started", {"thread": {"id": "child", "parentThreadId": "root", "agentNickname": "Helper"}})
            self.assertEqual(on_request(method, approval), {"decision": "accept"})
            on_event("item/completed", {"threadId": "child", "turnId": "child-turn", "item": {
                "type": "collabAgentToolCall", "tool": "spawnAgent", "senderThreadId": "child",
                "receiverThreadIds": ["grandchild"],
            }})
            self.assertEqual(on_request(method, {**approval, "threadId": "grandchild"}), {"decision": "accept"})
            on_event("turn/completed", {"threadId": "child", "turn": {"id": "child-turn", "status": "completed"}})
            self.assertFalse(predicate())
            on_event("turn/completed", {"threadId": "root", "turn": {"id": "root-turn", "status": "completed", "items": []}})

        rpc.pump_until.side_effect = pump_until
        result, _ = self.run_turn(rpc, turn(), context(ask))
        self.assertEqual(result, TurnResult("", "root"))
        self.assertEqual(ask.call_args_list[0].args[0], "Helper · Allow this command?")
        self.assertEqual(ask.call_args_list[1].args[0], "Codex helper · Allow this command?")

    def test_missing_saved_thread_raises_session_lost(self):
        rpc = fake_rpc()
        rpc.call.side_effect = [{}, RpcError("thread/resume", {"code": -32600, "message": "thread not found: old"})]
        with self.assertRaises(SessionLost):
            self.run_turn(rpc, turn(session="old"), context())

    def test_same_error_without_a_saved_session_is_an_ordinary_failure(self):
        rpc = fake_rpc()
        rpc.call.side_effect = [{}, RpcError("thread/start", {"message": "thread not found"})]
        with self.assertRaises(RpcError) as raised:
            self.run_turn(rpc, turn(), context())
        self.assertNotIsInstance(raised.exception, SessionLost)

    def test_missing_cli_names_install_page(self):
        provider = CodexProvider(FakePlatform())
        with self.assertRaisesRegex(RuntimeError, "developers.openai.com/codex/cli"):
            provider.run_turn(turn(), context())


class CodexRequestTests(unittest.TestCase):
    def test_command_amendment_is_returned_unchanged(self):
        amendment = {"applyNetworkPolicyAmendment": {"network_policy_amendment": {
            "host": "example.test", "action": "deny",
        }}}
        params = {"availableDecisions": [amendment, "cancel"], "reason": "Check status"}
        ask = Mock(return_value="network-policy-0")
        self.assertEqual(_handle_request("item/commandExecution/requestApproval", params, ask, {}),
                         {"decision": amendment})
        self.assertEqual(ask.call_args.args[2][0]["label"], "Always deny requests to example.test")
        self.assertEqual(_handle_request("item/commandExecution/requestApproval", params, None, {}),
                         {"decision": "cancel"})

    def test_permissions_grant_exact_scope_and_profile(self):
        permissions = {"network": {"enabled": True}}
        params = {"permissions": permissions, "cwd": "workspace"}
        method = "item/permissions/requestApproval"
        self.assertEqual(_handle_request(method, params, Mock(return_value="session"), {}),
                         {"scope": "session", "permissions": permissions})
        self.assertEqual(_handle_request(method, params, Mock(return_value="decline"), {}),
                         {"scope": "turn", "permissions": {}})

    def test_file_approval_relays_patch(self):
        ask = Mock(return_value="acceptForSession")
        changes = [{"path": "welcome.txt", "diff": "+Hello"}]
        result = _handle_request("item/fileChange/requestApproval", {"itemId": "id"}, ask, {"id": changes})
        self.assertEqual(result, {"decision": "acceptForSession"})
        self.assertIn("welcome.txt", ask.call_args.args[1])
        self.assertIn("+Hello", ask.call_args.args[1])

    def test_native_questions_keep_choice_and_custom_answer_distinct(self):
        ask = Mock(side_effect=["0", "other", "0", "secret value"])
        option = {"label": "Default", "description": "The default setting"}
        params = {"questions": [
            {"id": "mode", "header": "Mode", "question": "Choose one", "options": [option]},
            {"id": "count", "header": "Count", "question": "How many?", "options": [option], "isOther": True},
            {"id": "token", "header": "Token", "question": "Enter a token", "isSecret": True},
        ]}
        self.assertEqual(_handle_request("item/tool/requestUserInput", params, ask, {}), {"answers": {
            "mode": {"answers": ["Default"]}, "count": {"answers": ["0"]}, "token": {"answers": ["secret value"]},
        }})
        self.assertTrue(ask.call_args.args[2][0]["secret"])

    def test_mcp_relays_form_and_url_responses(self):
        method = "mcpServer/elicitation/request"
        ask = Mock(side_effect=["invalid", '{"color":"cyan"}'])
        schema = {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]}
        form = {"serverName": "sample", "mode": "form", "message": "Pick a color", "requestedSchema": schema}
        self.assertEqual(_handle_request(method, form, ask, {}), {"action": "accept", "content": {"color": "cyan"}})
        self.assertIn('"color"', ask.call_args.args[1])
        url = {"serverName": "sample", "mode": "url", "message": "Visit this page", "url": "https://example.test"}
        ask = Mock(return_value="accept")
        self.assertEqual(_handle_request(method, url, ask, {}), {"action": "accept"})
        self.assertIn(url["url"], ask.call_args.args[1])


class CodexModelTests(unittest.TestCase):
    def test_codex_preserves_native_effort_ids_and_reads_every_page(self):
        rpc = MagicMock()
        rpc.__enter__.return_value = rpc
        effort = [{"reasoningEffort": "medium"}, {"reasoningEffort": "ultra"}]
        first = {"model": "big", "displayName": "Big", "description": "Complex work",
                 "supportedReasoningEfforts": effort, "defaultReasoningEffort": "medium"}
        second = {"model": "sol", "displayName": "Sol", "supportedReasoningEfforts": effort}
        rpc.call.side_effect = [{}, {"data": [first], "nextCursor": "next"},
                                {"data": [second, {"hidden": True}], "nextCursor": None}]
        with patch("commonroom.providers.codex.RpcProcess", return_value=rpc):
            models = CodexProvider(FakePlatform({"codex": "codex"})).list_models(Path.cwd(), Event())
        self.assertEqual([model.to_dict() for model in models], [
            {"id": "big", "label": "Big", "description": "Complex work", "efforts": ["medium", "ultra"], "defaultEffort": "medium"},
            {"id": "sol", "label": "Sol", "efforts": ["medium", "ultra"]},
        ])
        self.assertEqual([call.args[0] for call in rpc.call.call_args_list], ["initialize", "model/list", "model/list"])
        self.assertEqual(rpc.call.call_args.args[1], {"cursor": "next", "includeHidden": False})


if __name__ == "__main__":
    unittest.main()
