---
name: cardinal-disconnect
description: Disconnect Cursor from Cardinal by revoking keys and removing managed Cursor MCP and hook config.
---

# Cardinal Disconnect

Use this skill when the user asks to disconnect Cardinal, remove Cardinal telemetry hooks or MCP tools from Cursor, or revoke Cardinal keys.

The script is `scripts/cardinal-disconnect` in the Cardinal plugin directory (two levels up from this skill's directory):

```bash
python3 <plugin directory>/scripts/cardinal-disconnect
```

**Sandbox.** In Cursor's default Auto-review mode the agent's shell has no network and cannot write outside the workspace and `/tmp`. This script revokes keys on Cardinal and edits `~/.cursor`, so ask to run it outside the sandbox (Cursor asks the user to approve), or give the user the command to run in their own terminal.

If the state file is missing and the user still wants cleanup, rerun with `--force`.

Disconnecting also removes `~/.cursor/cardinal/`, including the owner-input consent: after a later connect, owner input stays off until the user runs `cardinal-connect --owner-input on` again.

After success, tell the user to restart Cursor so it reloads MCP and hook configuration. If the connection was installed with `--project`, remind them to commit the removal of `.cursor/mcp.json` and `.cursor/hooks.json`.
