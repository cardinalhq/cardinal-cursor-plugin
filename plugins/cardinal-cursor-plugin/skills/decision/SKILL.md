---
name: cardinal-decision
description: Show Cardinal decision-capture status for Cursor and tell the user how to turn it on or off.
---

# Cardinal Decision Capture

Use this skill when the user asks to turn Cardinal decision capture on or off, or asks whether it is on.

The script is `scripts/cardinal-decision` in the Cardinal plugin directory (two levels up from this skill's directory):

```bash
python3 <plugin directory>/scripts/cardinal-decision status
```

Surface the output. In Cursor's default Auto-review mode the sandbox may not let `status` read `~/.cursor/cardinal`. If it can't, ask to run it outside the sandbox, or give the user the command to run in their own terminal.

Do not run `on` or `off` yourself: they write `~/.cursor/cardinal`, which the sandbox blocks, and the setting is the user's. Give the user the exact terminal command instead (`python3 <plugin directory>/scripts/cardinal-decision on` or `off`). Turning capture on affects only new Cursor chats: the instructions arrive at session start.

When capture is on, the Cardinal sessionStart hook gives you the exact `record` command to run whenever a material decision is made. Follow those instructions: `record` only prints a line that the plugin's hook records, so it is safe inside the sandbox.
