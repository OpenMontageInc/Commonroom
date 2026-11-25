# Adding a provider

A *provider* is one agent tool (a "harness") installed and signed in on the
user's machine: Codex, Claude Code and Antigravity today; GitHub Copilot CLI,
Cursor CLI, Gemini CLI or OpenCode next. Each provider is one module in
`commonroom/providers/` plus one entry in `BUILTIN_PROVIDERS` (and, optionally,
a logo file in `commonroom/providers/logos/`). The room, the
setup screen, the model picker and the HTTP API pick it up automatically; no
other file should mention your provider.

Read [ARCHITECTURE.md](ARCHITECTURE.md) first, then the three existing
providers. `claude.py` (stream-json over stdin/stdout) and `antigravity.py`
(Agent Client Protocol, JSON-RPC) are the best templates.

## Before you start

The tool needs a **headless mode**: a way to run a turn without its terminal
UI, with machine-readable output on stdout. Check that it can:

- stream progress events (tool calls, plan updates) and a final answer;
- report a **session id**, and resume that session on the next turn;
- be **interrupted** mid-turn (or at least killed safely);
- relay **permission questions** to the caller, or run without asking any;
- **list its models** (and effort / reasoning levels), ideally over the same
  channel. If it cannot, discuss in an issue what to show instead.

Commonroom starts every agent in its tool's most autonomous mode. Which flags
you pass for that must be discussed in the issue / PR (see
[CONTRIBUTING.md](../CONTRIBUTING.md#agents-run-with-full-autonomy)).

## The contract (`providers/base.py`)

```python
class Provider(ABC):
    id: ClassVar[str]                    # stable id stored in rooms and settings, e.g. "copilot"
    label: ClassVar[str]                 # shown in the UI, e.g. "GitHub Copilot"
    hints: ClassVar[ExecutableHints]     # where to find the executable
    install_url: ClassVar[str]           # shown when the tool is missing
    version_args: ClassVar[Optional[tuple[str, ...]]] = ("--version",)  # None if it has no version flag
    logo: ClassVar[Optional[str]] = None  # SVG file name in providers/logos/, e.g. "copilot.svg"

    def run_turn(self, request: TurnRequest, context: TurnContext) -> TurnResult: ...
    def list_models(self, workspace: Path, cancel: threading.Event) -> list[Model]: ...
```

`self.platform` (the current `Platform`) is set by the base constructor. Call
`self.executable()` to get the tool's path; it raises `ProviderNotInstalled`
with the install URL when the tool is missing. `detect()` (used by the setup
screen) is inherited: it finds the executable and runs it with `version_args`,
taking the first output line as the version.

### `ExecutableHints`

Declared as data, resolved by the platform:

```python
hints = ExecutableHints(
    commands=("copilot",),                                        # looked up on the search path (".exe" first on Windows)
    env=("COPILOT_CLI_PATH",),                                    # a full path the user set; checked first
    paths={"windows": ("{localappdata}/Programs/copilot/copilot.exe",),  # fallbacks after the search path
           "macos": ("{home}/.local/bin/copilot",)},
    exclude={"windows": ("{localappdata}/Microsoft/WindowsApps",)},  # folders whose match is not the tool
)
```

The search path is more than the service's own PATH, because an app opened from Finder,
the Dock or the Start menu gets a minimal one. On macOS it adds the user's login-shell PATH
(nvm, fnm, asdf, mise, Homebrew) and the usual install folders; on Windows the PATH saved in
the registry (so tools installed after Commonroom started are found) and the npm, winget,
Scoop, Chocolatey, Volta, nvm-windows, pnpm and bun folders. So a tool installed through a
package manager usually needs only `commands`; use `paths` for installers that do not touch
PATH or for apps that bundle the tool.

`paths` placeholders: `{home}`, `{localappdata}`, `{appdata}`, `{programfiles}`,
`{programdata}` (a hint using a folder the OS lacks is skipped). A `*` matches one folder
level and picks the newest match, for tools that keep versions in hashed folders.
`store_packages` covers tools installed from an OS app store (see `codex.py`).

### `TurnRequest` (what to run, as data)

| Field | Meaning |
|---|---|
| `member_id` | The room member this turn is for. |
| `model`, `effort` | Chosen in the UI. `effort` may be `""`: then omit the flag and let the tool use its default. |
| `prompt` | The full text to send. The room builds it; pass it through unchanged. |
| `workspace` | The project folder. Start the tool there (`cwd`). |
| `session` | Session id to resume, or `""` for a new session. |
| `session_workspace` | Folder the saved session was started in. If your tool can only resume in its original folder, compare with `self.platform.same_folder(...)` and raise `SessionLost` on a mismatch (see `antigravity.py`). |

### `TurnContext` (how to talk back to the room)

| Callback | Use it for |
|---|---|
| `remember_session(session_id)` | Call **as soon as the tool reports the session id**, before the turn finishes. If the turn is stopped or crashes, the next turn can still resume the same session. |
| `activity(label)` | Short status shown under the member while it works: the tool name (`"Bash"`, `"Edit"`), `"Thinking"`, `"Updating plan"`. Call it often; it is cheap. |
| `ask(title, detail, choices, withdrawn)` | Relay a permission question or a question for the human into the room. Blocks until answered. See below. |
| `cancel` | A `threading.Event` set when the user presses Stop. |

### `TurnResult`

`TurnResult(text, session)`: the agent's final reply (only the final answer,
not intermediate messages) and the session id to save. The room treats the
text `[[NO_REPLY]]` (`core.identity.SILENCE`) or an empty string as "chose not
to reply"; the prompt already tells agents how to stay silent.

### Errors

- **`SessionLost`**: the saved session no longer exists. The room then retries
  the same turn once in a fresh session with the room history. Use
  `rpc.raise_if_session_lost(exc, request.session, context.cancel)` in an
  `except RuntimeError` around the turn: it converts errors containing the
  shared `LOST_SESSION_MARKERS` ("session not found", "no conversation found",
  ...) into `SessionLost`, but only when a session was being resumed and the
  user did not press Stop. Add your tool's wording to `LOST_SESSION_MARKERS`
  if it differs.
- **`ProviderNotInstalled`**: raised for you by `self.executable()`.
- **Any other exception** marks the turn failed and shows its message, with a
  Retry button. Make messages readable for a user.

### Cancellation

When `context.cancel` is set, stop promptly and raise
`RuntimeError("Turn stopped.")`. If you read events with `RpcProcess.read()`
this happens for you: it checks `cancel` every 0.2 s, first calls
`rpc.on_cancel` (set it to a function that sends the tool's own interrupt
message, so the tool saves its session cleanly) and then raises. If
`on_cancel` returns a JSON-RPC request id, `read()` waits up to two seconds
for that response. When the `with RpcProcess(...)` block exits, the process
gets two seconds to exit after stdin closes and is then killed with its whole
process tree. Whatever you return after a stop is discarded by the room.

### Relaying questions with `context.ask`

```python
choice = context.ask(
    "Allow Copilot to run a shell command?",           # title
    json.dumps(tool_input, ensure_ascii=False, indent=2),  # detail, shown as preformatted text
    [{"id": "allow", "label": "Allow once"},
     {"id": "deny", "label": "Decline"},
     {"id": "_write", "label": "Write an answer", "input": True}],  # "input": free text
    withdrawn,                                          # threading.Event or None
)
```

- It returns the chosen `id`, or the typed text for an `input` choice. With
  `"multi": True` choices the user can pick several; you get them joined with
  `", "`.
- Pass a `threading.Event` as `withdrawn` and set it if the tool cancels its
  own question; the room then removes it.
- `ask` raises `RuntimeError` when the turn is stopped or the question is
  withdrawn. Answer the tool with "denied/cancelled" in that case.
- Read events on one thread and answer questions on another if the tool can
  keep streaming while a question is open (see `answer_request` in
  `claude.py`). `RpcProcess.send()` is thread-safe.

## The transport: `providers/rpc.py`

`RpcProcess` starts the tool with the platform's environment and options (so
no console window flashes on Windows), reads stdout as JSON lines on a
background thread, and keeps the last stderr lines for error messages.

```python
with RpcProcess(self.platform, argv, request.workspace, context.cancel) as rpc:
    rpc.send({...})        # write one JSON line (thread-safe)
    event = rpc.read()     # next JSON object; raises "Turn stopped." on cancel,
                           # or "Harness closed the connection." + stderr when the tool exits
```

For **JSON-RPC** tools (Codex app-server, ACP) use the helpers:

- `rpc.call(method, params, on_event, on_request)` sends a request and pumps
  notifications (`on_event(method, params)`) and tool-to-client requests
  (`on_request(method, params) -> result`, exceptions become JSON-RPC errors)
  until the matching response. Error responses raise `RpcError` (with `.code`
  and `.data`).
- `rpc.notify(method, params)`, `rpc.pump_until(predicate, on_event, on_request)`,
  `rpc.wait_for(predicate, timeout)`.
- `ignore_event` and `unsupported_request` are ready-made handlers for model
  discovery.

For **stream-json / JSON-lines** tools, loop on `rpc.read()` and switch on
the event type, like `claude.py`.

## Model discovery

`list_models(workspace, cancel)` returns `Model(id, label, efforts=[...],
default_effort=..., description=..., resolved_model=..., effort_labels=...)`.
It runs in the background when the app starts and on Retry; `workspace` is a
neutral folder, and `cancel` is set after 20 seconds. Pass `cancel` to
`RpcProcess` so a hung tool cannot block discovery. An empty list, a model
without an id, or a malformed response is reported to the user as a discovery
error with a Retry button; `model.id` is what `--model` (or equivalent)
receives later.

### Logo (optional)

The UI shows each provider's logo next to its name: on the setup screen, in the
provider picker, beside each agent in the left rail, on the agent preview card
and before the model name on agent messages. To add one:

1. Put the tool's official mark, unmodified, as an SVG in
   `commonroom/providers/logos/` (for example `copilot.svg`).
2. Declare `logo = "copilot.svg"` on the provider class. Only a plain file name
   ending in `.svg` inside that folder is ever served.
3. Add a line to `NOTICE` naming the mark, where you got it and its licence.

Rules:

- **Licence.** Only add a file you are allowed to redistribute under the
  project's licence: the vendor's published brand asset, or a copy under CC0,
  MIT or a similar permissive licence (the current marks come from the theSVG
  project). Trademarks stay with their owners; we use them only to identify the
  compatible tool. If unsure, leave the logo out.
- **Square-ish, no padding, no text.** It is drawn at 14–20px.
- **One-colour marks** should use `fill="currentColor"`. The UI then paints them
  in the text colour, so they stay legible in light and dark themes. Coloured
  marks are shown as they are. No scripts, external references or fonts.

Without a logo the UI draws the provider's initial in a neutral rounded square,
so a provider without one still looks finished.

The API lists the logo as `logo` on each entry of `GET /api/providers` and of
the setup scan (`GET /api/setup`), as `/api/providers/<id>/logo.svg`, or `null`.

## Step by step

1. **Create `commonroom/providers/<id>.py`** with your provider class.
2. **Register it**: add the class to `BUILTIN_PROVIDERS` in
   `commonroom/providers/registry.py`. The tuple order is the display order.
3. **Optionally add a logo** (see "Logo" above).
4. **Write tests** in `tests/providers/test_<id>.py` (checklist below).
5. **Update the README** install list with a link to the tool.
6. **Check it by hand** (checklist below), then open a PR.

## Minimal skeleton

A stream-json style tool. Event names and flags are placeholders: map them to
what your tool really emits.

```python
"""GitHub Copilot CLI: <one line on the protocol it speaks>."""
from __future__ import annotations

import threading
from pathlib import Path

from ..platform.base import ExecutableHints
from .base import Model, Provider, TurnContext, TurnRequest, TurnResult
from .rpc import RpcProcess, raise_if_session_lost


class CopilotProvider(Provider):
    id = "copilot"
    label = "GitHub Copilot"
    install_url = "https://github.com/features/copilot/cli"
    hints = ExecutableHints(commands=("copilot",))

    def list_models(self, workspace: Path, cancel: threading.Event) -> list[Model]:
        with RpcProcess(self.platform, [self.executable(), "<list-models-args>"], workspace, cancel) as rpc:
            while True:
                event = rpc.read()
                if event.get("type") == "models":
                    return [Model(m["id"], m.get("name") or m["id"], list(m.get("efforts") or ()))
                            for m in event["models"]]

    def run_turn(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        try:
            return self._run(request, context)
        except RuntimeError as exc:
            raise_if_session_lost(exc, request.session, context.cancel)
            raise

    def _run(self, request: TurnRequest, context: TurnContext) -> TurnResult:
        # Full autonomy is a deliberate product choice: <name the flag and why>.
        args = [self.executable(), "<headless-args>", "--model", request.model]
        if request.effort:
            args += ["--effort", request.effort]
        if request.session:
            args += ["--resume", request.session]
        with RpcProcess(self.platform, args, request.workspace, context.cancel) as rpc:
            rpc.on_cancel = lambda: rpc.send({"type": "interrupt"})
            rpc.send({"type": "user", "text": request.prompt})
            while True:
                event = rpc.read()
                kind = event.get("type")
                if kind == "session":
                    context.remember_session(event["session_id"])
                    context.activity("Thinking")
                elif kind == "tool_use":
                    context.activity(event["name"])
                elif kind == "permission":
                    choice = context.ask(f"Allow Copilot to use {event['tool']}?", str(event.get("input", "")),
                                         [{"id": "allow", "label": "Allow once"}, {"id": "deny", "label": "Decline"}],
                                         None)
                    rpc.send({"type": "permission_response", "id": event["id"], "allow": choice == "allow"})
                elif kind == "result":
                    if event.get("is_error"):
                        raise RuntimeError(event.get("message") or "Copilot turn failed")
                    return TurnResult(event.get("text", ""), event["session_id"])
```

## Test checklist

Port the patterns in `tests/providers/` (`test_claude.py` for stream-json,
`test_antigravity.py` / `test_codex.py` for JSON-RPC). Never start the real
tool.

- [ ] **Fake platform**: build the provider with
      `FakePlatform({"copilot": "copilot"})` from `tests/providers/fakes.py`,
      and use its `turn(...)` / `context(...)` helpers.
- [ ] **Scripted fake process**: patch `commonroom.providers.<id>.RpcProcess`
      with a `MagicMock` whose `read.side_effect` is your scripted event list
      (or a small fake class with `call`/`notify` for JSON-RPC). Assert the
      argv, the `cwd`, and the messages you `send`.
- [ ] **New session**: `remember_session` is called with the id as soon as the
      tool reports it, and the `TurnResult` carries the final answer only.
- [ ] **Session resume**: with `turn(session="s1")` the resume flag/message is
      sent, and no new session is created.
- [ ] **`SessionLost`**: the tool's "session not found" error during a resume
      raises `SessionLost`; the same error without a saved session is an
      ordinary failure.
- [ ] **Cancel**: with the cancel event set, `on_cancel` sends the tool's
      interrupt and the turn ends with `RuntimeError("Turn stopped.")` (or a
      silent result), keeping the session.
- [ ] **Ask relay**: a permission request reaches `context.ask` with readable
      title, detail and choices, and each answer is sent back in the tool's
      format, including a decline and a withdrawn/stopped question.
- [ ] **Effort**: an empty effort omits the flag.
- [ ] **Model discovery**: the tool's model list maps to `Model`s (ids,
      labels, efforts, default effort); a malformed or failing response raises
      rather than returning garbage.
- [ ] **Missing tool**: `run_turn` with an empty `FakePlatform()` raises an
      error naming `install_url`.
- [ ] Update `tests/providers/test_registry_catalog_scan.py` if it asserts the
      list of built-in ids.

Run `py -3 -m pytest tests -q`.

## Manual verification checklist

Use a throwaway service so your real rooms are untouched:

```powershell
py -3 -m commonroom --port 8741 --data "$env:TEMP\commonroom-provider-check"
```

Open `http://127.0.0.1:8741` (or point the desktop app at it with
`COMMONROOM_PORT` / `COMMONROOM_DATA`), with the real tool installed and
signed in:

- [ ] The setup screen lists the provider as installed, with its version.
      Uninstalled (or renamed off PATH), it shows the install link instead.
- [ ] Its logo (or initial) shows on the setup screen, in the provider picker,
      in the left rail and on its messages, in light and dark theme.
- [ ] The model picker lists its models and effort levels; Retry works after a
      discovery failure.
- [ ] Add an agent with it to a room; it replies to a message, and staying
      silent works when it has nothing to add.
- [ ] Activity labels appear while it works.
- [ ] A permission question (if the tool still asks any) shows in the room and
      both answers work.
- [ ] Stop mid-turn: the turn ends promptly and no stray process is left
      running (check Task Manager / Activity Monitor).
- [ ] Retry a failed or stopped turn.
- [ ] Restart the service: the agent resumes its session (it remembers the
      earlier conversation).
- [ ] Rename the agent and change its model or effort; it keeps working in the
      same session.
- [ ] Note the OS and tool version you checked in the PR.

## Adding an operating system

Same idea, one level down: a new `Platform` subclass in
`commonroom/platform/`, one line in `current_platform()`, a matching
`desktop/platform/<os>.cjs`, and install paths in providers' `ExecutableHints`.
See "Adding an OS" in [ARCHITECTURE.md](ARCHITECTURE.md#adding-an-os-eg-linux).
