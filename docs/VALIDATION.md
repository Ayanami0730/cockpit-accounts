# 0.1.0 validation

Validated on macOS / Apple Silicon on 2026-10-14.

| Check | Result |
| --- | --- |
| TypeScript typecheck | Passed |
| Frontend tests (`npm test`) | 265 passed |
| Claude helper tests with system Python 3.9 | 125 passed |
| Loopback read-only middleware tests | 8 passed |
| Rust local account/startup tests | 28 passed |
| Rust diagnostic privacy tests | 6 passed |
| Rust Cursor injection/rollback tests | 4 passed |
| Rust encrypted storage tests | 3 passed |
| Shell installer syntax | Passed |
| macOS arm64 build | Passed |
| Signed ZIP extraction and executable permissions | Passed |
| Ad-hoc signature verification after extraction | Passed |
| Native empty-account launch and three-provider navigation | Passed |
| Native Claude new-profile form | Passed without creating an account |
| Original private source checkout | Unmodified |

439 automated tests passed across the listed suites. Build warnings remain for unused upstream code and large frontend chunks; those warnings are not represented as clean builds.

The source filename/credential guard and private deployment-marker scan passed. Initial Gitleaks findings were inspected without publishing values. GitHub push protection then rejected inherited Google OAuth client constants; those unused constants and Windsurf Firebase constants were removed, and the unused proxy sidecar was excluded from the build and package. Remaining scanner findings are metadata, localization strings and synthetic fixtures. Excluded build/dependency outputs were not committed. The release archive is checked separately for private deployment markers and build-home paths. See [privacy review](PRIVACY_REVIEW.md).

## Limits

- Real OAuth login, credential refresh and actual cross-account switching were **not** executed for this public build. Tests use synthetic credentials and temporary profiles. The native smoke test used empty account lists and did not import existing accounts.
- No Intel hardware or fresh macOS installation was tested. The downloadable package is arm64; Intel support is source-build only and unverified.
- The application is ad-hoc signed, not Apple Developer ID signed or notarized. Gatekeeper can require the user's explicit approval.
- Claude usage/control responses depend on the installed official client's capabilities and may change. Unavailable data is not treated as a successful live verification.
- Installation scripts do not install the official Codex, Cursor or Claude clients and do not create provider accounts. Each provider needs its own legitimate sign-in and entitlement.
