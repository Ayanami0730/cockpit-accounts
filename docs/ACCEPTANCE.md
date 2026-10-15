# 0.1.1 acceptance checks (2026-10-15)

This is a macOS Apple Silicon preview. The owner requested safe-scope testing without using existing accounts or interrupting active sessions. No spare provider accounts or isolated macOS environment were available. **Real authenticated login and A → B → A client switching remain unverified.**

## What was actually exercised

- Downloaded the published 0.1.0 installer from GitHub and ran it with a temporary installation destination. The installed app had the expected version and identifier, executable permissions and a valid ad-hoc signature. A second install correctly refused to overwrite it.
- macOS security assessment rejected the unnotarized app, as expected. No Gatekeeper setting or quarantine protection was changed. This is not a clean-machine “no prompt” installation pass.
- Started the downloaded app with an empty manager account store. Generated a Codex authorization URL without opening it, verified that the callback listener belonged to the test manager, then cancelled. The listener and pending file were removed, and no account was added.
- Created two explicitly named, unauthenticated Claude test profiles; selected the next default; restarted only the test manager; verified that profiles and selection persisted. Profile directories were owner-only and contained no official credentials.
- Existing Claude Code activity was detected and Desktop launch controls were disabled. No launch, login, quit, switch, import, quota refresh or conversation-continuation action was requested for the official clients.
- Built and started 0.1.1. Repeated the Codex entry/cancel check. Opened the Cursor authorization dialog without opening its external URL, then cancelled; cancellation was logged, its network connections disappeared and no Cursor account index was created.

These are installation, configuration and authorization-entry/cancellation checks—not successful provider sign-ins. Current client processes were retained. Temporary test profiles are removed from the working manager store after the checks, retaining recoverable test evidence outside the repository.

## Fixes made during this review

- Do not mount the Claude page or its timers before its first visit.
- Avoid the `/usr/bin/python3` developer-tool launcher, including symlinks to it. Discover an installed interpreter without executing a candidate. Missing Python keeps saved profiles visible with a specific dependency message.
- Preserve a successful profile mutation when a subsequent passive read fails; show a warning rather than inviting duplicate creation. Do not expose raw errors.
- Claude Desktop completion requires matching Desktop identity, rather than treating a CLI-only login as sufficient. Standard-profile verification reads the standard global metadata file.
- Cursor cancellation interrupts in-flight polling and waits. Saving a completed response and cancellation share a lock, preventing a cancelled flow from later saving an account.
- Codex cancellation now waits for the backend result and displays an explicit cancelled state.

## Automated coverage

- Frontend: 268 tests passed, including saved-mutation and controlled-error regressions.
- Claude Python helpers: 128 tests passed, including Desktop identity and standard-profile regressions.
- Rust Claude module: 18 tests passed, including interpreter discovery and “never execute the system launcher” checks.
- Rust Cursor cancellation: 6 tests passed using controlled state and fake futures, with no real credentials or authentication requests.
- Typecheck, macOS build, package structure and signature checks passed. Existing unused-code and chunk-size warnings remain.

## What is still required for full acceptance

Use an independent macOS virtual machine or spare Mac, with separate test accounts A and B for each provider. Do not copy live auth files, cookies or refresh tokens. Install the official clients, complete each provider's own authorization, then check identity and session behavior after A → B → A, restart and busy-session refusal.

Changing `HOME` or `CODEX_HOME` in the active desktop session is not sufficient isolation: default Keychain access, process discovery, browser/Terminal launch and application quit requests can still reach the current login session. No fresh macOS VM, Intel hardware, real account switch or signed/notarized distribution was tested here.

For the original release's checks, see [0.1.0 validation](VALIDATION.md).
