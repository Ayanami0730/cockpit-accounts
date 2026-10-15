# Attribution and changes

Cockpit Accounts is a modified distribution of [Cockpit Tools](https://github.com/jlcodes99/cockpit-tools), by jlcodes99 and contributors, based on upstream v1.3.57. It is not an official OpenAI, Cursor or Anthropic product.

The original project declares **CC BY-NC-SA 4.0**. This distribution preserves that license for the original code and modifications. Noncommercial redistribution and adaptation require attribution, an indication of changes and the same license. Commercial use requires separate permission from the relevant rights holders. This is source-available software; the noncommercial restriction does not meet the OSI definition of open source.

Changes in this distribution: restricted three-provider UI; local credential storage protections; manual account switching; Claude profile and session protection; removal of personal network and remote-host assumptions; standalone macOS installation and privacy checks. No personal account database, credentials, conversation history, screenshots or original Git history is distributed.

Third-party components retain their own licenses. In particular, the vendored CLIProxyAPI code retains its MIT license in `sidecars/cockpit-cliproxy/third_party/CLIProxyAPI/LICENSE`. Third-party icons and product names belong to their respective owners. See upstream for the full component acknowledgements.

Unused Google OAuth client constants and Windsurf Firebase constants were removed from inherited source. Their integrations are not supported by this fork. The unused proxy sidecar is not built or bundled.
