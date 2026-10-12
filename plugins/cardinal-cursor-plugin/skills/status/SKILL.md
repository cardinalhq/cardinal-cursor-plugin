---
name: cardinal-status
description: Check the Cardinal Cursor connection, the owner-input setting, and probe configured telemetry and MCP endpoints.
---

# Cardinal Status

Use this skill when the user asks whether Cursor is connected to Cardinal, whether Cardinal telemetry or MCP is configured, whether their prompts are recorded as owner input, or to verify Cardinal setup.

The script is `scripts/cardinal-status` in the Cardinal plugin directory (two levels up from this skill's directory):

```bash
python3 <plugin directory>/scripts/cardinal-status
```

**Sandbox.** The script reads `~/.cursor` and probes Cardinal's endpoints over the network. In Cursor's default Auto-review mode the sandbox denies network access, so the probes fail inside it. Ask to run it outside the sandbox (Cursor asks the user to approve), or give the user the command for their own terminal. Don't report a network failure from inside the sandbox as a broken connection.

Surface the script output clearly. The `Owner input:` line says whether this machine's Cursor sessions record typed prompts. Only the user turns that on, in their own terminal, with `cardinal-connect --owner-input on`. If the output says Cursor needs a restart, tell the user that MCP and hook config are loaded at startup.
