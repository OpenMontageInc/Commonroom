# Commonroom architecture

Commonroom is a local common workspace per project folder. You and any number of AI
agents (each one a *member* running on a *provider* such as Codex, Claude Code
or Antigravity, with its own model and effort) share one ordered transcript.
Every member sees every message and independently decides whether to reply.

## Layers

```
desktop/ (Electron shell)                 commonroom/web/ (UI: index.html, appearance.css, fonts)
   │ spawns `python -m commonroom`                │ HTTP + JSON on 127.0.0.1
   ▼                                              ▼
commonroom/server.py      HTTP routes only. No business rules.
commonroom/app.py         Composition root: builds Platform, ProviderRegistry,
                          Settings, ModelCatalog, ProjectRooms and wires them.
commonroom/core/          Provider- and OS-neutral domain:
   room.py                  transcript, members, delivery jobs, retry/stop, requests
   prompt.py                builds the text a member receives each turn
   mentions.py              @mention parsing (UTF-16 offsets from the browser)
   projects.py              registry of project folder -> room
   settings.py              user settings (display name, default lead agent)
   identity.py              HUMAN_ID and other shared constants
commonroom/providers/     One module per agent harness behind providers/base.py
   base.py                  Provider, TurnRequest/TurnContext/TurnResult, Model, Detection
   registry.py              ProviderRegistry + BUILTIN_PROVIDERS
   catalog.py               ModelCatalog (discovers models via providers, async, retryable)
   rpc.py                   JSON-lines child-process transport shared by providers
   codex.py claude.py antigravity.py
   logos/                   each provider's SVG mark (Provider.logo); licences in NOTICE
commonroom/platform/      One module per OS behind platform/base.py
   base.py                  Platform, ExecutableHints
   windows.py macos.py      OS specifics (data folder, PATH search, process kill, locks, folder picker)
   __init__.py              current_platform()
commonroom/setup.py       Machine scan: which providers are installed, versions, Python, OS.
```

### Rules that keep it SOLID

* **Single responsibility.** Room does delivery; prompt.py writes prompts;
  mentions.py parses mentions; server.py only maps HTTP to calls.
* **Open/closed.** A new provider is a new file in `providers/` plus one entry in
  `BUILTIN_PROVIDERS`. A new OS is a new file in `platform/` plus one line in
  `current_platform()`. No `if provider == "codex"` or `sys.platform` checks
  anywhere else.
* **Liskov.** Every provider honours the same contract: return `TurnResult`,
  raise `SessionLost` for a missing saved session, raise `ProviderNotInstalled`
  when absent, call `context.activity` / `context.ask` / `context.remember_session`,
  stop promptly when `context.cancel` is set (raise `RuntimeError("Turn stopped.")`).
* **Interface segregation.** Providers receive `TurnRequest` (data) and
  `TurnContext` (callbacks), never the room or a member row with magic keys.
* **Dependency inversion.** core/ depends on abstractions it is handed
  (a provider lookup, a settings reader), never imports a concrete provider or
  platform. Only `app.py` knows the concrete classes.

### No privileged agent or person

* No provider or model is special in code. The lead agent a new room starts with
  comes from user settings (`settings.lead`), chosen on first run.
* The human is `HUMAN_ID = "human"` in the database; their display name comes from
  settings (default: the OS user name).

## Data

Data folder from `Platform.data_dir()` (Windows `%LOCALAPPDATA%\Commonroom`,
macOS `~/Library/Application Support/Commonroom`), overridable with `--data`.

* `settings.json` user settings (see below)
* `projects.sqlite3` project registry
* `room.sqlite3` (id `default`) or `projects/<id>/room.sqlite3` per room, plus
  `workspace/room-history.jsonl` transcript archive agents can read.
* `room.lock` single-service lock.

Existing databases must keep working: schema changes are additive migrations.

### settings.json

```json
{
  "displayName": "Sam",
  "lead": {"name": "Lead", "provider": "claude", "model": "opus", "effort": "high"},
  "setupComplete": true
}
```

`lead` may be `null` (rooms then start empty). Settings never contain secrets.

## HTTP API (127.0.0.1 only; same trust checks as today)

Existing routes keep their shapes unless listed here. Room routes work both as
`/api/<action>` (default project) and `/api/projects/<projectId>/<action>`.

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/health` | `{"service":"commonroom","version":"0.1.0"}` |
| POST | `/api/shutdown` | `{"ok":true}`, then the service stops. Used by a newer desktop shell to replace an older service; turns in flight are marked interrupted on next start. |
| GET | `/api/setup` | `{platform:{name,label}, python:{version}, providers:[Detection], settings:Settings, setupComplete:bool}` |
| POST | `/api/setup/rescan` | re-detects providers, restarts model discovery for installed ones; returns same as GET `/api/setup` |
| GET | `/api/settings` | `Settings` |
| PUT | `/api/settings` | partial `{displayName?, lead?, setupComplete?}` → full `Settings`. `lead.provider` must be a registered id. |
| GET | `/api/providers` | `{providers:[{id,label,installed,logo}]}` (registry order). `logo` is `/api/providers/<id>/logo.svg` or `null` (the UI then draws the provider's initial). |
| GET | `/api/providers/<id>/logo.svg` | The SVG a registered provider declares in `Provider.logo` (`image/svg+xml`, cached for an hour, `Content-Security-Policy: default-src 'none'`). 404 for an unknown provider or one without a logo; only plain `.svg` file names inside `providers/logos/` are read. |
| GET | `/api/models` | `{models:{<provider>:[Model]}, errors:{}, loading:[], defaults:{<provider>:{model,effort}}}` — `defaults` now comes from `settings.lead` only (no external files). A provider is listed in `loading` until its first discovery finishes (all providers while the first machine scan runs; providers that are not installed never). |
| POST | `/api/members` | `{name, provider, model, effort, role}`; provider validated against the registry |
| PATCH | `/api/members/<id>` | any of `{name, model, effort, role}`. Changing model/effort keeps the session. |
| GET | `/api/state` | as today, plus `human:{id:"human", name:<displayName>}`; each member gains `lead:bool` |

`Detection` = `{id,label,installed,installUrl,path,version,error}`; in `/api/setup` each entry also
carries `logo` (as in `/api/providers`).

Member dict in state: `{id,name,provider,model,effort,role,lead,aliases}` (the old
`permanent` field is dropped from the API; the lead member can be removed).

## Desktop shell

`desktop/main.cjs` is OS-neutral. OS differences live in
`desktop/platform/windows.cjs` and `desktop/platform/macos.cjs`, both exporting
the same shape:

```js
module.exports = {
  findPython(): Promise<{command, args} | null>,   // e.g. {command:'py', args:['-3']}
  pythonInstallHint(): string,                      // e.g. 'winget install -e --id Python.Python.3.12'
  serviceEnvironment(): NodeJS.ProcessEnv,
  spawnOptions(): object,                           // windowsHide / detached etc.
  stopServiceOnPort(port): Promise<{stopped, reason?}>, // ends an older Commonroom service on the port;
                                                    // only a Python process running server.py/commonroom
  profileFolder?: string,                           // Electron profile under appData (default 'Commonroom';
                                                    // macOS uses 'Commonroom Desktop' to stay apart from the data folder)
};
```

The shell only reuses a running service whose `/api/health` `version` equals its own
app version (`desktop/package.json`, kept equal to `commonroom.__version__`). Otherwise it
asks to restart it, calls `POST /api/shutdown`, falls back to `stopServiceOnPort`, waits for
the port to free and starts its own service. `COMMONROOM_PORT` and `COMMONROOM_DATA` point
the shell at a throwaway port and data folder (with its own temporary profile) for testing.
If the service cannot start, its last log lines are shown in the error dialog (for example
"Port 8731 is already used by another program...").

The shell finds Python, and if missing shows a dialog with the one install
command and a Retry button. It then runs `<python> -m commonroom --port 8731`
with `cwd` = the folder that contains the `commonroom` package. The packaged app
ships the `commonroom/` package as an extra resource.

## Adding a provider (e.g. GitHub Copilot CLI)

1. Create `commonroom/providers/copilot.py` with `class CopilotProvider(Provider)`.
2. Declare `id = "copilot"`, `label`, `install_url`, `hints = ExecutableHints(commands=("copilot",))`,
   and optionally `logo = "copilot.svg"` with the file in `providers/logos/` (see ADDING_A_PROVIDER.md).
3. Implement `run_turn` (map its streaming protocol onto `TurnContext`) and `list_models`.
4. Add it to `BUILTIN_PROVIDERS` in `registry.py`. Add tests in `tests/providers/`.

The UI, setup scan, model picker and room pick it up automatically.

## Adding an OS (e.g. Linux)

1. `commonroom/platform/linux.py` implementing `Platform`; add to `current_platform()`.
2. `desktop/platform/linux.cjs` with the same exports; select it in `main.cjs`.
3. Add any Linux install paths to providers' `ExecutableHints.paths["linux"]`.

## Releasing a signed Windows installer

`npm run package:win` (in `desktop/`) builds an unsigned installer; anyone can
do this. Maintainers sign releases with `desktop/scripts/release-win.ps1`, which
reads `desktop/signing.local.ps1` (git-ignored; start from
`signing.example.ps1`), builds with signing forced on, and fails unless the
installer and app are validly signed and timestamped by the expected publisher.
Azure Artifact Signing is the default (`az login` with the Certificate Profile
Signer role; the TrustedSigning module needs a .NET SDK); a `.pfx` certificate
also works. Build settings live in `desktop/electron-builder.config.cjs`.
