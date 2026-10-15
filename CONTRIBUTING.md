# Contributing

Use the build and test commands in README.md. This fork supports a restricted macOS UI for Codex, Cursor and Claude; do not enable upstream background services, cleanup jobs, account imports or remote synchronization implicitly.

Use synthetic accounts and temporary directories for tests. Never commit credential exports, authentication files, logs, screenshots containing real accounts, personal network configuration or local Git history. Run `python3 scripts/privacy-check.py` after staging changes and inspect a redacted secret-scan report before publishing.

Retain the CC BY-NC-SA 4.0 terms and third-party notices. Describe tested behavior separately from behavior requiring real-account verification.
