# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report them privately through GitHub:
[**Report a vulnerability**](https://github.com/OpenMontageInc/Commonroom/security/advisories/new)
(repository **Security** tab → **Report a vulnerability**).

Include what you found, how to reproduce it, the Commonroom version or commit,
your OS, and the impact you think it has. We will acknowledge the report, keep
you updated while we work on a fix, and credit you in the advisory if you wish.

## Supported versions

Security fixes go into the latest release and `main`. Please check that the
problem still exists there before reporting.

## Scope

Commonroom is a local app. Things worth knowing when assessing a finding:

- **Local service.** The Python service listens on the loopback interface only
  (`127.0.0.1`, default port 8731) and refuses requests whose `Host`, `Origin`
  or `Sec-Fetch-Site` headers show they did not come from Commonroom's own page.
  Ways around those checks (for example from a web page in the user's browser,
  DNS rebinding, or another user on the same machine) are in scope.
- **Agents run with full autonomy by design.** Every agent is started in its
  tool's most autonomous mode and can read, change and run anything the user's
  account can. That an agent can do these things is not a vulnerability.
  Ways for *someone other than the user* to make agents act (for example
  injecting messages into a room, or a remote page triggering a turn) are in
  scope.
- **Your agent tools are out of scope.** Problems in Claude Code, Codex,
  Antigravity or other harnesses themselves should be reported to their
  vendors. How Commonroom invokes them (flags, environment, arguments built
  from room content) is in scope.
- **Local data.** Rooms, settings and transcripts are stored unencrypted in the
  user's data folder, protected by the operating system's account permissions.
  Settings never contain secrets; Commonroom uses no API keys of its own.
