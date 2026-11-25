"""Claude Code: stream-json turns, including the host tool-permission protocol."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from ..platform.base import ExecutableHints
from .base import Model, Provider, TurnContext, TurnRequest, TurnResult
from .rpc import RpcProcess, raise_if_session_lost


def control(request, ask):
    kind = request["subtype"]
    if kind != "can_use_tool":
        raise ValueError(f"Unsupported Claude request: {kind}")
    name, inputs = request["tool_name"], request["input"]
    if name == "AskUserQuestion":
        answers = {}
        for q in inputs.get("questions", []):
            options = [{"id": x["label"], "label": x["label"], "multi": bool(q.get("multiSelect"))} for x in q.get("options", [])]
            options.append({"id": "_write", "label": "Write an answer", "input": True})
            answers[q["question"]] = ask(q["question"], json.dumps(q, ensure_ascii=False, indent=2), options)
        return {"behavior": "allow", "updatedInput": {**inputs, "answers": answers}}
    options = [{"id": "allow", "label": "Allow once"}, {"id": "deny", "label": "Decline"}]
    detail = json.dumps(inputs, ensure_ascii=False, indent=2)
    suggestions = request.get("permission_suggestions") or []
    if suggestions:
        detail += "\n\nClaude suggested this permission change:\n" + json.dumps(suggestions, ensure_ascii=False, indent=2)
        options.append({"id": "suggestion", "label": "Allow and apply the shown permission change"})
    choice = ask(f"Allow Claude to use {name}?", detail, options)
    if choice == "suggestion" and suggestions:
        return {"behavior": "allow", "updatedInput": inputs, "updatedPermissions": suggestions}
    if choice == "allow":
        return {"behavior": "allow", "updatedInput": inputs}
    return {"behavior": "deny", "message": "The user declined this tool request."}


class ClaudeProvider(Provider):
    id = "claude"
    label = "Claude Code"
    install_url = "https://docs.claude.com/en/docs/claude-code/setup"
    # Native installer: ~/.local/bin. Older npm "local" installs: ~/.claude/local. npm, Homebrew, winget, Scoop
    # and version managers are found through the platform's search path. The Claude desktop app's private
    # copy is deliberately not used: its path changes with every app update.
    hints = ExecutableHints(
        commands=("claude",),
        paths={"windows": ("{home}/.local/bin/claude.exe", "{home}/.claude/local/claude.cmd"),
               "macos": ("{home}/.local/bin/claude", "{home}/.claude/local/claude")},
        # Older Claude desktop apps register a "Claude.exe" app alias here that opens the app, not the CLI.
        exclude={"windows": ("{localappdata}/Microsoft/WindowsApps",)})

    def list_models(self, workspace: Path, cancel: threading.Event) -> list[Model]:
        args = [self.executable(), "-p", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json"]
        request_id = "commonroom-models"
        with RpcProcess(self.platform, args, workspace, cancel) as rpc:
            rpc.send({"type": "control_request", "request_id": request_id, "request": {"subtype": "initialize"}})
            while True:
                event = rpc.read()
                response = event.get("response") or {}
                if event.get("type") != "control_response" or response.get("request_id") != request_id:
                    continue
                if response.get("subtype") != "success":
                    raise RuntimeError(response.get("error") or "Claude Code could not initialize model discovery.")
                return [Model(record["value"], record["displayName"], list(record.get("supportedEffortLevels") or ()),
                              default_effort=record.get("defaultEffort"), description=record.get("description"),
                              resolved_model=record.get("resolvedModel"))
                        for record in response["response"]["models"]]

    def run_turn(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        try:
            return self._run(request, context)
        except RuntimeError as exc:
            raise_if_session_lost(exc, request.session, context.cancel)
            raise

    def _run(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        # Full autonomy is a deliberate product choice: bypassPermissions, with host prompts for what still asks.
        args = [self.executable(), "-p", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json",
                "--permission-mode", "bypassPermissions", "--permission-prompts", "host",
                "--permission-prompt-tool", "stdio", "--model", request.model]
        if request.effort:
            args += ["--effort", request.effort]
        if request.session:
            args += ["--resume", request.session]
        activity = context.activity
        with RpcProcess(self.platform, args, request.workspace, context.cancel) as proc:
            proc.on_cancel = lambda: proc.send({"type": "control_request", "request_id": uuid.uuid4().hex,
                                                "request": {"subtype": "interrupt"}})
            pending = {}

            def answer_request(event, withdrawn):
                rid = event["request_id"]

                def ask(title, detail, choices):
                    return context.ask(title, detail, choices, withdrawn)
                try:
                    answer = control(event["request"], ask)
                    reply = {"subtype": "success", "response": answer}
                except Exception as exc:
                    reply = {"subtype": "error", "error": str(exc)}
                if not withdrawn.is_set():
                    try:
                        proc.send({"type": "control_response", "response": {"request_id": rid, **reply}})
                    except (OSError, ValueError):
                        pass
                pending.pop(rid, None)

            proc.send({"type": "user", "message": {"role": "user", "content": request.prompt}})
            try:
                while True:
                    event = proc.read()
                    kind = event.get("type")
                    if kind == "control_request":
                        withdrawn = threading.Event()
                        pending[event["request_id"]] = withdrawn
                        threading.Thread(target=answer_request, args=(event, withdrawn), daemon=True).start()
                    elif kind == "control_cancel_request":
                        withdrawn = pending.get(event.get("request_id"))
                        if withdrawn:
                            withdrawn.set()
                    elif kind == "result":
                        if event.get("is_error") or event.get("subtype") != "success":
                            raise RuntimeError(event.get("result") or str(event.get("errors")) or "Claude turn failed")
                        return TurnResult(event.get("result", ""), event["session_id"])
                    elif kind == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") in ("tool_use", "server_tool_use"):
                                activity(block["name"])
                    elif kind == "system":
                        subtype = event.get("subtype", "")
                        if subtype == "init" and event.get("session_id"):
                            context.remember_session(event["session_id"])
                        if subtype in ("init", "compact_boundary", "task_started", "task_notification"):
                            activity({"init": "Reading the room", "compact_boundary": "Compacting context"}.get(
                                subtype, subtype.replace("_", " ")))
            finally:
                for withdrawn in list(pending.values()):
                    withdrawn.set()
