# Contributing to Commonroom

Thanks for helping. Commonroom is small on purpose: a Python service with no
runtime dependencies, one web page, and a thin Electron shell. Please keep it
that way.

## Dev setup

You need **Python 3.10+** and **Node.js 20+**. At least one agent tool (Claude
Code, Codex CLI, Antigravity) installed and signed in is useful for manual
checks, but the test suite never needs one.

```powershell
git clone https://github.com/OpenMontageInc/Commonroom.git
cd Commonroom

# Service + web UI on a throwaway port and data folder, so you never touch your real chats:
py -3 -m commonroom --port 8741 --data "$env:TEMP\commonroom-dev"
# then open http://127.0.0.1:8741

# Optional: the desktop app
cd desktop
npm ci
npm start
```

On macOS use a Python 3.10+ interpreter (for example `python3.12`; the system
`python3` is 3.9) instead of `py -3`, and any temporary folder for `--data`:

```bash
python3.12 -m commonroom --port 8741 --data "$TMPDIR/commonroom-dev"
```

To point the desktop app at a throwaway service too, set `COMMONROOM_PORT` and
`COMMONROOM_DATA` before `npm start` (see "Desktop shell" in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

> Never develop against the real data folder (`%LOCALAPPDATA%\Commonroom` on
> Windows, `~/Library/Application Support/Commonroom` on macOS) or the default
> port 8731. That is where your installed Commonroom keeps your rooms.

## Project layout

| Path | What lives there |
|---|---|
| `commonroom/core/` | Rooms, transcript, delivery, prompts, mentions, settings. Provider- and OS-neutral. |
| `commonroom/providers/` | One module per agent tool (Codex, Claude Code, Antigravity) behind `providers/base.py`. |
| `commonroom/platform/` | One module per OS (Windows, macOS) behind `platform/base.py`. |
| `commonroom/server.py`, `app.py` | HTTP routes, and the composition root that wires everything. |
| `commonroom/web/` | The UI: `index.html`, `appearance.css`, fonts. |
| `desktop/` | Electron shell; OS specifics in `desktop/platform/`. |
| `tests/` | Python tests, mirroring the package layout. |

The full picture, and the rules that keep the layers apart, are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). To add an agent tool, read
[docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md).

## Running tests

```powershell
py -3 -m pytest tests -q          # Python (pip install pytest first)
cd desktop; npm test              # desktop shell
```

On macOS, `python3.12 -m pytest tests -q` (in a virtual environment if Homebrew's
Python refuses a global `pip install`).

CI runs the Python suite on Windows, macOS and Linux and the desktop tests on
Windows and macOS.
Both must pass before a PR can merge.

### Testing rules

- **Fakes only in unit tests.** Use `tests/fakes.py` (fake providers, settings,
  platform) and `tests/providers/fakes.py` (fake platform, `turn()` and
  `context()` helpers). Script a provider's protocol by patching its
  `RpcProcess` with a fake, as the existing provider tests do.
- **Never launch a real provider CLI in a test.** No `claude`, `codex`, `agy`
  or any other agent tool, not even `--version`.
- **Never use the real data folder or port 8731.** Use temporary folders and
  port `0` (let the OS pick). The fake platforms raise if a test asks for the
  real data folder.
- **OS-specific tests** go behind `unittest.skipUnless(sys.platform == ...)`
  so the suite stays green on every CI runner.
- **Before opening a PR that touches a provider**, also do a manual end-to-end
  check: start the service with a temporary `--data` folder and a spare
  `--port`, add an agent from that provider (real, signed-in CLI), have it reply,
  stop it mid-turn, and restart the service to confirm it resumes its session.
  Say what you checked in the PR.

## Code style

- Match the existing compact style: short modules, small functions, no
  speculative abstraction. Read the neighbouring code first.
- Relative imports inside the `commonroom` package.
- **No new runtime dependencies** without discussing it in an issue first. The
  service runs on the standard library only.
- **OS-specific code only** in `commonroom/platform/` and `desktop/platform/`.
  No `sys.platform` / `os.name` checks anywhere else.
- **Provider-specific code only** in `commonroom/providers/`. No
  `if provider == "codex"` in core, server or UI.
- Database changes are additive migrations: existing rooms must keep working.

## Pull requests

1. Open an issue first for anything bigger than a small fix, so we can agree on
   the approach.
2. Fork, then branch from `main` (`fix/stop-button`, `provider/copilot`, ...).
3. Keep each PR to **one logical change**. Unrelated clean-ups go in their own PR.
4. Fill in the PR template, including which providers and OS you checked by hand.
5. CI must pass, and a code-owner review is required before merge.
6. Never force-push to `main`. Force-pushing your own PR branch is fine.

### Commit messages

Short imperative subject line (about 72 characters max), e.g. `Resume Claude
sessions after a restart`. Add a body when the *why* is not obvious. Reference
issues with `Refs #123` or `Closes #123`.

## Agents run with full autonomy

Commonroom deliberately starts every agent in its tool's most autonomous mode
(for example Claude Code's `bypassPermissions`, Codex's `danger-full-access`,
Antigravity's `--dangerously-skip-permissions`). Any change to how agents are
invoked (command-line flags, permission, sandbox or approval modes, environment
passed to the tool) needs explicit discussion in an issue before a PR, and must
be called out in the PR description.

Found a security problem? Please don't open a public issue. See
[SECURITY.md](SECURITY.md).

## Releases (maintainers only)

Releases are cut by maintainers. Anyone can build an unsigned installer with
`npm run package:win` in `desktop/`. Signed releases are built with
`desktop/scripts/release-win.ps1`, which reads a git-ignored local signing
configuration and verifies every signature before it finishes. Keep
`desktop/package.json` `version` equal to `commonroom.__version__`.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating you agree to uphold it.
