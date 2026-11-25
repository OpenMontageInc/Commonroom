"""Codex: drives a turn through the installed ``codex app-server`` native protocol."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ..platform.base import ExecutableHints
from .base import Model, Provider, TurnContext, TurnRequest, TurnResult
from .rpc import RpcProcess, ignore_event, raise_if_session_lost, unsupported_request


_DECISION_LABELS = {
    "accept": "Allow once",
    "acceptForSession": "Allow for this session",
    "decline": "Deny and continue",
    "cancel": "Deny and stop this turn",
}
_STANDARD_DECISIONS = ("accept", "acceptForSession", "decline", "cancel")


def _detail(**fields):
    sections = []
    for label, value in fields.items():
        if value is None or value == "":
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        sections.append(label.replace("_", " ") + "\n" + text)
    return "\n\n".join(sections)


def _select(ask, title, detail, options, default):
    if ask and options:
        choices = [{"id": key, "label": label} for key, (label, _) in options.items()]
        selected = ask(title, detail, choices)
        if selected in options:
            return options[selected][1]
    return default


def _command_decisions(params):
    decisions = params.get("availableDecisions")
    if decisions is None:
        decisions = ("accept", "decline", "cancel") if params.get("kind") == "humanCommand" else _STANDARD_DECISIONS
    options = {}
    for decision in decisions:
        if isinstance(decision, str) and decision in _DECISION_LABELS:
            options[decision] = (_DECISION_LABELS[decision], decision)
        elif isinstance(decision, dict):
            if "acceptWithExecpolicyAmendment" in decision:
                options["exec-policy"] = ("Allow and save this command rule", decision)
            elif "applyNetworkPolicyAmendment" in decision:
                amendment = decision["applyNetworkPolicyAmendment"]["network_policy_amendment"]
                action, host = amendment["action"], amendment["host"]
                options[f"network-policy-{len(options)}"] = (f"Always {action} requests to {host}", decision)
    return options


def _decision_result(ask, title, detail, options):
    decision = _select(ask, title, detail, options, None)
    if decision is None:
        for key in ("decline", "cancel"):
            if key in options:
                return {"decision": options[key][1]}
        raise RuntimeError("Codex's approval request has no available denial choice.")
    return {"decision": decision}


def _answer_questions(ask, params):
    answers = {}
    if not ask:
        return {"answers": answers}
    for question in params["questions"]:
        title = question["header"]
        options = {str(i): option["label"] for i, option in enumerate(question.get("options") or [])}
        answer = None
        if options:
            detail = "\n\n".join([question["question"]] + [
                f"{option['label']}\n{option['description']}" for option in question["options"]
            ])
            if question.get("isOther"):
                options["other"] = "Give another answer"
            selected = ask(title, detail, [{"id": key, "label": label} for key, label in options.items()])
            if selected not in options:
                continue
            if selected != "other":
                answer = options[selected]
        if answer is None:
            answer = ask(title, question["question"], [{
                "id": "answer", "label": "Send answer", "input": True, "secret": bool(question.get("isSecret")),
            }])
        if answer is not None:
            answers[question["id"]] = {"answers": [answer]}
    return {"answers": answers}


def _handle_elicitation(ask, params):
    title = params.get("title") or "Codex tool request"
    mode = params.get("mode")
    detail = _detail(
        Tool=params["serverName"], Request=params.get("message") or params.get("description"),
        Address=params.get("url"), Requested_fields=params.get("requestedSchema"),
    )
    decline = {"decline": ("Decline", "decline"), "cancel": ("Cancel", "cancel")}
    if mode == "url":
        options = {"accept": ("I completed the request", "accept"), **decline}
        return {"action": _select(ask, title, detail, options, "decline")}
    if ask and mode in {"form", "openai/form", "openaiForm"}:
        choices = [
            {"id": "content", "label": "Submit JSON", "input": True},
            *[{"id": key, "label": label} for key, (label, _) in decline.items()],
        ]
        prompt = detail
        while True:
            answer = ask(title, prompt, choices)
            if answer in decline or answer is None:
                return {"action": answer or "decline"}
            try:
                content = json.loads(answer)
                if not isinstance(content, dict):
                    raise ValueError("Expected a JSON object")
                json.dumps(content, allow_nan=False)
            except ValueError:
                prompt = "Enter a JSON object with the requested fields.\n\n" + detail
            else:
                return {"action": "accept", "content": content}
    if mode == "openai/userVerification":
        detail += "\n\nThis request requires device verification in a Codex client."
    return {"action": _select(ask, title, detail, decline, "decline")}


def _handle_request(method, params, ask, file_changes):
    reason = params.get("reason")
    if method == "item/commandExecution/requestApproval":
        titles = {"writeStdin": "Allow input to a running command?", "humanCommand": "Codex needs your approval"}
        options = _command_decisions(params)
        command_rule = params.get("proposedExecpolicyAmendment")
        network_rules = params.get("proposedNetworkPolicyAmendments")
        if not command_rule and "exec-policy" in options:
            command_rule = options["exec-policy"][1]["acceptWithExecpolicyAmendment"]["execpolicy_amendment"]
        if not network_rules:
            network_rules = [
                decision["applyNetworkPolicyAmendment"]["network_policy_amendment"]
                for key, (_, decision) in options.items() if key.startswith("network-policy-")
            ]
        return _decision_result(ask, titles.get(params.get("kind"), "Allow this command?"), _detail(
            Reason=reason, Command=params.get("command"), Working_directory=params.get("cwd"),
            Network_request=params.get("networkApprovalContext"), Extra_permissions=params.get("additionalPermissions"),
            Proposed_command_rule=command_rule, Proposed_network_rules=network_rules or None,
        ), options)
    if method == "item/fileChange/requestApproval":
        return _decision_result(ask, "Allow these file changes?", _detail(
            Reason=reason, Write_access=params.get("grantRoot"), Changes=file_changes.get(params["itemId"]),
        ), {key: (_DECISION_LABELS[key], key) for key in _STANDARD_DECISIONS})
    if method == "item/permissions/requestApproval":
        scope = _select(ask, "Grant additional permissions?", _detail(
            Reason=reason, Working_directory=params["cwd"], Requested_permissions=params["permissions"],
        ), {"turn": ("Grant for this turn", "turn"), "session": ("Grant for this session", "session"),
            "decline": ("Deny", None)}, None)
        return {"permissions": params["permissions"] if scope else {}, "scope": scope or "turn"}
    if method == "item/tool/requestUserInput":
        return _answer_questions(ask, params)
    if method in {"execCommandApproval", "applyPatchApproval"}:
        options = {
            "accept": (_DECISION_LABELS["accept"], "approved"),
            "acceptForSession": (_DECISION_LABELS["acceptForSession"], "approved_for_session"),
            "decline": (_DECISION_LABELS["decline"], {"denied": {"rejection": "Declined in Commonroom."}}),
            "cancel": (_DECISION_LABELS["cancel"], "abort"),
        }
        return _decision_result(ask, "Allow this command?" if method == "execCommandApproval" else "Allow these file changes?", _detail(
            Reason=reason, Command=params.get("command"), Working_directory=params.get("cwd"),
            Write_access=params.get("grantRoot"), Changes=params.get("fileChanges"),
        ), options)
    if method == "mcpServer/elicitation/request":
        return _handle_elicitation(ask, params)
    raise RuntimeError(f"Commonroom does not support the Codex request {method!r}.")


class CodexProvider(Provider):
    id = "codex"
    label = "Codex"
    install_url = "https://developers.openai.com/codex/cli"
    hints = ExecutableHints(commands=("codex",),
                            store_packages={"windows": (("OpenAI.Codex", "app/resources/codex.exe"),)})

    def list_models(self, workspace: Path, cancel: threading.Event) -> list[Model]:
        models = []
        with RpcProcess(self.platform, [self.executable(), "app-server", "--stdio"], workspace, cancel) as rpc:
            rpc.call("initialize", {"clientInfo": {"name": "commonroom", "version": "0.1.0"}},
                     ignore_event, unsupported_request)
            rpc.notify("initialized", {})
            cursor = None
            while True:
                response = rpc.call("model/list", {"includeHidden": False, "cursor": cursor},
                                    ignore_event, unsupported_request)
                for record in response["data"]:
                    if not record.get("hidden"):
                        models.append(Model(
                            record["model"], record["displayName"],
                            [option["reasoningEffort"] for option in record["supportedReasoningEfforts"]],
                            default_effort=record.get("defaultReasoningEffort"), description=record.get("description"),
                        ))
                cursor = response.get("nextCursor")
                if not cursor:
                    return models

    def run_turn(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        try:
            return self._run(request, context)
        except RuntimeError as exc:
            raise_if_session_lost(exc, request.session, context.cancel)
            raise

    def _run(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        codex = self.executable()
        workspace = request.workspace
        session = (request.session or "").strip()
        model = (request.model or "").strip()
        effort = (request.effort or "").strip()
        activity = context.activity

        def ask(title, detail, choices):
            return context.ask(title, detail, choices, None)

        turn_id = None
        announced_turn_id = None
        finished = None
        starting = False
        user_interrupted = False
        pending_events = []
        replies = {}
        files = {}
        parents = {}
        child_names = {}
        error = None

        def remember_child(child, parent, nickname=None):
            if child and parent and child != parent:
                parents.setdefault(child, parent)
                if nickname:
                    child_names[child] = nickname

        def belongs_to_session(thread_id):
            visited = set()
            while thread_id and thread_id not in visited:
                if thread_id == session:
                    return True
                visited.add(thread_id)
                thread_id = parents.get(thread_id)
            return False

        def on_event(method, params):
            nonlocal announced_turn_id, finished, error
            if method == "thread/started":
                thread = params["thread"]
                parent = thread.get("parentThreadId")
                source = thread.get("source")
                subagent = source.get("subAgent") if isinstance(source, dict) else None
                if not parent and isinstance(subagent, dict):
                    parent = (subagent.get("thread_spawn") or {}).get("parent_thread_id")
                remember_child(thread.get("id"), parent, thread.get("agentNickname"))
                return
            thread_id = params.get("threadId")
            item = params.get("item") or {}
            if method in {"item/started", "item/completed"}:
                if item.get("type") == "collabAgentToolCall" and item.get("tool") == "spawnAgent" and item.get("senderThreadId") == thread_id:
                    for child in item.get("receiverThreadIds", []):
                        remember_child(child, thread_id)
                elif item.get("type") == "subAgentActivity" and item.get("kind") == "started":
                    remember_child(item.get("agentThreadId"), thread_id)
            if not belongs_to_session(thread_id):
                return
            observed_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
            child = thread_id != session
            if observed_turn and (child or starting or observed_turn == turn_id):
                if method == "item/fileChange/patchUpdated":
                    files.setdefault(thread_id, {})[params["itemId"]] = params["changes"]
                elif method == "item/started" and item.get("type") == "fileChange":
                    files.setdefault(thread_id, {})[item["id"]] = item["changes"]
            if child:
                return
            if method == "turn/started" and (starting or observed_turn == turn_id):
                announced_turn_id = observed_turn
            if starting:
                pending_events.append((method, params))
                return
            if not turn_id or observed_turn != turn_id:
                return
            if method == "turn/completed":
                finished = params["turn"]
            elif method == "error":
                message = (params.get("error") or {}).get("message")
                if params.get("willRetry"):
                    if message:
                        activity(message[:200])
                elif message:
                    error = message
            elif method == "item/completed" and item.get("type") == "agentMessage":
                replies[item["id"]] = item
            elif method == "item/started":
                label = {
                    "commandExecution": "Running a command",
                    "fileChange": "Editing files",
                    "mcpToolCall": f"Using {item.get('server', 'a tool')}",
                    "webSearch": "Searching the web",
                    "contextCompaction": "Compacting context",
                    "plan": "Updating plan",
                }.get(item.get("type"))
                if label:
                    activity(label[:200])

        def on_request(method, params):
            nonlocal user_interrupted
            request_session = params.get("threadId") or params.get("conversationId")
            if request_session and not belongs_to_session(request_session):
                raise RuntimeError("Codex sent a request for a different session.")
            if request_session == session and turn_id and params.get("turnId") and params["turnId"] != turn_id:
                raise RuntimeError("Codex sent a request for a different turn.")
            request_ask = ask
            if request_session and request_session != session:
                nickname = child_names.get(request_session, "Codex helper")

                def request_ask(title, detail, choices):
                    return ask(f"{nickname} · {title}", detail, choices)

            result = _handle_request(method, params, request_ask, files.get(request_session, {}))
            if request_session == session and result.get("decision") in ("cancel", "abort"):
                user_interrupted = True
            return result

        with RpcProcess(self.platform, [codex, "app-server", "--stdio"], workspace, context.cancel) as rpc:
            def interrupt_turn():
                if not session or finished is not None or not (starting or turn_id):
                    return None
                current_turn = turn_id or announced_turn_id
                if not current_turn or current_turn != announced_turn_id:
                    # turn/start can acknowledge before the native turn is actually active.
                    def matching_start(event):
                        params = event.get("params") or {}
                        return (event.get("method") == "turn/started" and params.get("threadId") == session
                                and (not turn_id or (params.get("turn") or {}).get("id") == turn_id))

                    started = rpc.wait_for(matching_start, timeout=2)
                    if started:
                        current_turn = started["params"]["turn"]["id"]
                if not current_turn:
                    return None
                request_id = "commonroom-turn-interrupt"
                rpc.send({"jsonrpc": "2.0", "id": request_id, "method": "turn/interrupt",
                          "params": {"threadId": session, "turnId": current_turn}})
                return request_id

            rpc.on_cancel = interrupt_turn
            rpc.call("initialize", {
                "clientInfo": {"name": "commonroom", "title": "Commonroom", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True, "requiredHumanApproval": True},
            }, on_event, on_request)
            rpc.notify("initialized", {})
            # Full autonomy is a deliberate product choice: agents act without sandbox prompts.
            thread_params = {"cwd": str(workspace), "sandbox": "danger-full-access", "approvalPolicy": "never"}
            if model:
                thread_params["model"] = model
            if session:
                thread_params.update(threadId=session, excludeTurns=True)
            opened = rpc.call("thread/resume" if session else "thread/start", thread_params, on_event, on_request)
            session = opened["thread"]["id"]
            context.remember_session(session)
            turn_params = {"threadId": session, "input": [{"type": "text", "text": request.prompt}]}
            if effort:
                turn_params["effort"] = effort
            activity("Thinking")
            starting = True
            turn = rpc.call("turn/start", turn_params, on_event, on_request)
            turn_id = turn["turn"]["id"]
            starting = False
            for method, params in pending_events:
                on_event(method, params)
            rpc.pump_until(lambda: finished is not None, on_event, on_request)

        if finished["status"] == "interrupted" and user_interrupted:
            return TurnResult("", session)
        if finished["status"] != "completed":
            message = (finished.get("error") or {}).get("message") or error
            raise RuntimeError(message or f"The Codex turn was {finished['status']}.")
        for item in finished.get("items", []):
            if item.get("type") == "agentMessage":
                replies[item["id"]] = item
        for phase in ("final_answer", "partial_answer", None):
            for item in reversed(replies.values()):
                if item.get("phase") == phase and item.get("text"):
                    return TurnResult(item["text"], session)
        if error:
            raise RuntimeError(error)
        return TurnResult("", session)
