---
name: supervise
description: Supervise another agent session through its Cardinal Investigation — watch everything it does (its checkpoints and its captured tool activity), validate every finding, question every hypothesis, challenge what is wrong or unreported, and review its artifacts when it is done, posting verdicts the owner sees on the Storyboard under a Supervisor tag. Use only when the user asks you to supervise, watch, review or check another session (by session id, inv_ id or Storyboard link); not for reviewing your own work.
---

# supervise — watch another session, check it independently

Follow [CORE.md](CORE.md), co-located in this directory, for the whole workflow: set up,
the review loop, verdicts, rulings, the artifact review and the bounds. This file adds what
is specific to Cursor.

## Commands and state

- Run the commands from this installed plugin (not similarly named files in the working
  repository): `<this-skill-directory>/../../scripts/cardinal-storyboard …` and
  `<this-skill-directory>/../../scripts/cardinal-evidence …`, each on its own.
- They reach the network and the grant setup writes outside the workspace, so in Cursor's
  Auto-review sandbox ask to run them outside the sandbox, once, for the supervision run.
- `<dir>` (CORE.md) is `~/.cardinal/supervise/<inv>/` (`mkdir -p -m 700`). Never put the
  grant file in the repository.

## Waking

Block on the watch until there is something to review:

```
<this-skill-directory>/../../scripts/cardinal-storyboard investigation events <inv> --credential-file <dir>/grant.json --cursor-file <dir>/cursor.json --follow --timeout 600 --max 1
```

Starting from your cursor file, the watch signals only events you have not committed, so
re-arming it never replays the history; it never commits anything itself.

It returns as soon as new events arrive (one line each, your own posts excluded) or after
ten quiet minutes. Then run one pass of CORE.md's review loop through your cursor file,
sweeping the worker's new activity even when the watch timed out, and start the watch
again. Never write sleep-and-reread loops.

## Doing your own checks

Reproduce claims read-only: read files, `git log` / `git diff`, Cardinal query tools
(their witnessed `rcpt_…` receipts can be cited). Run builds or tests only in your own
worktree, never in the worker's checkout, and only the targeted test.
