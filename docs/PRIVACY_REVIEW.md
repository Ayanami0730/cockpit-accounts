# Public distribution review

This repository was exported from tracked source files into a new repository. The original Git history, build directories, account databases, credentials, chat records and local deployment notes were not imported.

The review removed fixed private network addresses, remote SSH synchronization, private profile paths and identifiers. The app has a separate bundle identifier, data directory and Keychain service. Historical screenshots and donation images were excluded. All 107 retained binary source assets were compared byte-for-byte with the upstream source revision and matched; none came from a local account session. The additional README screenshot was captured from the new app with an empty account list and visually checked before publication.

A private deployment-marker scan found no remaining matching text or binary source assets. The public `scripts/privacy-check.py` also checks tracked filenames and credential-shaped material without displaying values. Gitleaks was run with redacted output: inherited Google OAuth and Windsurf Firebase constants were removed after GitHub push protection rejected the initial upload. Those integrations are outside this fork’s scope, and the unused proxy sidecar is no longer built or bundled. Remaining findings were dependency metadata, localization strings or synthetic test data. Do not blanket-allow future findings just because the initial source was reviewed.

Additional hardening removes raw OAuth error bodies and non-allowlisted JWT claims from diagnostic summaries, masks common credentials and user paths in logs, and excludes the legacy cookie-export helper from the application resources.

The compiled release is separately checked for private deployment markers and local build paths, its archive is inspected, and its ad-hoc code signature is verified. A public GitHub repository necessarily exposes its repository owner and commit metadata; commits for this distribution use a generic contributor identity rather than a personal email address.

These checks describe the release process and its scope, not an absolute security guarantee. Runtime data generated after someone installs the app belongs to that person and must never be committed or included in a release.
