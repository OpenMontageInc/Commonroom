"""Antigravity: turns through its native Agent Client Protocol (ACP) server."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ..platform.base import ExecutableHints
from .base import Model, Provider, SessionLost, TurnContext, TurnRequest, TurnResult
from .rpc import LOST_SESSION_MARKERS, RpcProcess, ignore_event, raise_if_session_lost, unsupported_request

#: ACP's "resource not found" error code, returned when a saved session no longer exists.
_ACP_NOT_FOUND = -32002


def configure(call, session, options, model, effort):
    config = {option["id"]: option for option in options}
    if model:
        values = {option["value"] for option in config.get("model", {}).get("options", [])}
        if model not in values:
            base, _, suffix = model.rpartition("-")
            if base in values and suffix in {"low", "medium", "high", "xhigh", "max"}:
                model, effort = base, effort or suffix
        if config.get("model", {}).get("currentValue") != model:
            changed = call("session/set_config_option", {"sessionId": session, "configId": "model", "value": model})
            config = {option["id"]: option for option in changed["configOptions"]}
    thinking = config.get("thought_level")
    if effort and thinking and thinking.get("currentValue") != effort:
        call("session/set_config_option", {"sessionId": session, "configId": "thought_level", "value": effort})


def _acp_choices(option):
    for choice in option.get("options", []):
        if "options" in choice:
            yield from choice["options"]
        else:
            yield choice


class AntigravityProvider(Provider):
    id = "antigravity"
    label = "Antigravity"
    install_url = "https://antigravity.google"
    # The standalone Antigravity CLI. Its official installers use these folders; Homebrew
    # (antigravity-cli cask), winget (Google.AntigravityCLI) and Chocolatey are found through the search path.
    # The Antigravity IDE's own "antigravity" launcher only opens the IDE and has no ACP server.
    hints = ExecutableHints(commands=("agy",), paths={"windows": ("{localappdata}/agy/bin/agy.exe",
                                                                  "{localappdata}/Programs/agy/bin/agy.exe"),
                                                      "macos": ("{home}/.local/bin/agy",)})

    def list_models(self, workspace: Path, cancel: threading.Event) -> list[Model]:
        with RpcProcess(self.platform, [self.executable(), "acp", "--stdio"], workspace, cancel) as rpc:
            def call(method, params):
                return rpc.call(method, params, ignore_event, unsupported_request)

            response = call("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "commonroom", "version": "0.1.0"},
            })
            if response.get("protocolVersion") != 1:
                raise RuntimeError("Antigravity did not negotiate Agent Client Protocol 1.")
            opened = call("session/new", {"cwd": str(workspace), "mcpServers": []})
            configs = opened["configOptions"]
            available = next(option for option in configs if option["id"] == "model")
            current = available.get("currentValue")
            models = []
            for choice in _acp_choices(available):
                model_id = choice["value"]
                if model_id != current:
                    changed = call("session/set_config_option", {
                        "sessionId": opened["sessionId"], "configId": "model", "value": model_id,
                    })
                    configs = changed["configOptions"]
                    current = model_id
                thinking = next((option for option in configs if option["id"] == "thought_level"), {})
                labels = {option["value"]: option.get("name", option["value"]) for option in _acp_choices(thinking)}
                models.append(Model(model_id, choice["name"], list(labels), default_effort=thinking.get("currentValue"),
                                    description=choice.get("description"), effort_labels=labels or None))
            return models

    def run_turn(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        workspace = Path(request.workspace).resolve()
        cancel = context.cancel
        activity = context.activity
        session = request.session or ""
        if session and request.session_workspace and not self.platform.same_folder(Path(request.session_workspace), workspace):
            # Antigravity resumes against the session's original working directory.
            # Let the room start a session in the selected directory with its own transcript.
            raise SessionLost("Antigravity session not found in the selected project folder.")
        tool_calls = {}
        answer = []
        message_id = None
        collecting = False
        permission_cancelled = False

        def stopped():
            return cancel is not None and cancel.is_set()

        def on_event(method, params):
            nonlocal message_id
            if method != "session/update" or params.get("sessionId") != session or not collecting:
                return
            update = params.get("update", {})
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text" and (text := content.get("text")):
                    current_id = update.get("messageId")
                    if current_id and current_id != message_id:
                        message_id = current_id
                        answer.clear()
                    answer.append(text)
            elif kind in {"tool_call", "tool_call_update"}:
                identification = update.get("toolCallId")
                tool = tool_calls.setdefault(identification, {})
                tool.update(update)
                if kind == "tool_call":
                    answer.clear()
                    message_id = None
                if tool.get("status") in {"pending", "in_progress", "failed"} and tool.get("title"):
                    activity(tool["title"])
            elif kind in {"plan", "plan_update"}:
                activity("Updating plan")

        def on_request(method, params):
            nonlocal permission_cancelled
            if method != "session/request_permission":
                raise RuntimeError(f"Antigravity requested an unsupported client method: {method}")
            update = params.get("toolCall", {})
            tool = {**tool_calls.get(update.get("toolCallId"), {}), **update}
            title = tool.get("title") or "Antigravity requests permission"
            raw = tool.get("rawInput")
            if isinstance(raw, str):
                detail = raw
            elif raw is not None:
                detail = json.dumps(raw, ensure_ascii=False, indent=2)
            else:
                detail = "\n".join(location["path"] for location in tool.get("locations", []) if location.get("path"))
            choices = [{"id": option["optionId"], "label": option["name"]} for option in params.get("options", [])]
            if not stopped():
                try:
                    choice = context.ask(title, detail, choices, None)
                except RuntimeError:
                    if not stopped():
                        raise
                    choice = None
                if any(option["id"] == choice for option in choices) and not stopped():
                    return {"outcome": {"outcome": "selected", "optionId": choice}}
            permission_cancelled = True
            return {"outcome": {"outcome": "cancelled"}}

        # Full autonomy is a deliberate product choice.
        argv = [self.executable(), "acp", "--stdio", "--dangerously-skip-permissions"]
        try:
            with RpcProcess(self.platform, argv, workspace, cancel) as rpc:
                def call(method, params):
                    return rpc.call(method, params, on_event, on_request)

                def cancel_prompt():
                    if collecting:
                        try:
                            rpc.notify("session/cancel", {"sessionId": session})
                        except OSError:
                            pass

                rpc.on_cancel = cancel_prompt
                initialized = call("initialize", {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "commonroom", "title": "Commonroom", "version": "1.0.0"},
                })
                if initialized.get("protocolVersion") != 1:
                    raise RuntimeError("Antigravity did not negotiate Agent Client Protocol 1.")
                capabilities = initialized.get("agentCapabilities", {})
                lifecycle = capabilities.get("sessionCapabilities", {})
                setup = {"cwd": str(workspace), "mcpServers": []}
                if session:
                    if "resume" in lifecycle:
                        method = "session/resume"
                    elif capabilities.get("loadSession"):
                        method = "session/load"
                    else:
                        raise RuntimeError("Antigravity does not support reopening native sessions on this installation.")
                    reopen = {**setup, "sessionId": session}
                    try:
                        try:
                            started = call(method, reopen)
                        except RuntimeError as exc:
                            hint = str(exc).lower()
                            if method != "session/resume" or not capabilities.get("loadSession") or "load it with session/load" not in hint:
                                raise
                            started = call("session/load", reopen)
                    except RuntimeError as exc:
                        detail = str(exc).lower()
                        if getattr(exc, "code", None) == _ACP_NOT_FOUND or any(marker in detail for marker in LOST_SESSION_MARKERS):
                            raise SessionLost(f"Antigravity session not found: {exc}") from exc
                        raise
                else:
                    started = call("session/new", setup)
                    session = started.get("sessionId")
                    if not session:
                        raise RuntimeError("Antigravity did not report a session ID.")

                context.remember_session(session)
                configure(call, session, started.get("configOptions", []), request.model, request.effort)
                activity("Thinking")
                collecting = True
                result = call("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": request.prompt}]})
                collecting = False
                stop_reason = result.get("stopReason")
                silent = permission_cancelled or stop_reason == "cancelled" or stopped()
                if not silent and stop_reason not in {"end_turn", "refusal"}:
                    raise RuntimeError(f"Antigravity stopped before finishing the reply ({stop_reason or 'no stop reason'}).")
                if "close" in lifecycle:
                    try:
                        call("session/close", {"sessionId": session})
                    except RuntimeError:
                        pass  # The completed turn is durable even if freeing the active session fails.
                return TurnResult("" if silent else "".join(answer).strip(), session)
        except (RuntimeError, OSError) as exc:
            if stopped() or permission_cancelled:
                return TurnResult("", session)
            # A saved session can also vanish mid-turn; the room then retries in a fresh session.
            raise_if_session_lost(exc, request.session or "", cancel)
            raise
