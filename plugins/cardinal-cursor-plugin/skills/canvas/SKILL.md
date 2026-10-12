---
name: canvas
description: Draw and preview the Canvas of a Cardinal Investigation Storyboard scene — free-form HTML/CSS/SVG/d3/Plot in Cardinal's sandboxed frame, with the five cv operations (cv.data, cv.mark, cv.embed, cv.reveal, cv.highlight) and the in-frame prefabs (timeline, trace-waterfall, world-graph, compare, diff) — then read the PNGs rendered locally with Chromium after every storyboard__preview, and critique every reveal step. Use whenever you write or revise a storyboard surface (storyboard__define_surface), pick a prefab for a scene, style or animate a scene, or need to see what a storyboard scene actually looks like before storyboard__publish. Pairs with the storyboard skill, which owns the argument; this one owns the visual.
---

# canvas — draw the scene, then look at it

Follow [CORE.md](CORE.md), co-located in this directory, for the Canvas workflow: fetch the
`canvas` guide first, the exemplars, and the preview-then-critique loop. This file adds what
is specific to Cursor.

## The hook renders, when Cursor passes it on

After `storyboard__preview` returns, the Cardinal plugin's postToolUse hook (its
storyboard authoring entry, 150 s timeout) renders every scene and reveal step with the
storyboard skill's renderer. It writes the PNGs to
`~/.cursor/cardinal/storyboards/<storyboard_id>/r<revision>/` and reports, as additional
context, the directory, each scene's files (`<scene>-<step>.png`, step 0 first), any render
error, then the cover and the link-preview mock when the preview carried `card`. Read every
PNG it names, as CORE.md says.

If no such report follows the preview, render by hand (below). Cursor builds have been
reported to drop a postToolUse hook's additional context, and an install from before this
hook existed doesn't have the entry.

## Rendering by hand (fallback)

Run the renderer yourself when the report is missing, the hook timed out or left scenes
unrendered, for the dark theme, or to re-render a subset. It is `render_preview.py` in the
**storyboard** skill's `scripts/`, next to this skill's directory
(`<this skill's directory>/../storyboard/scripts/render_preview.py`). Use that path. Never
search the workspace for it: a repo can contain a file with the same name, and the renderer
handles your Cardinal key.

Save the `storyboard__preview` result to a file, keeping the storyboard id, the revision
and every scene's `preview_bundle` exactly, then run:

```bash
python3 -I "<this skill's directory>/../storyboard/scripts/render_preview.py" \
  --from-json <preview.json> [--scene <id>]… [--theme dark] --runtime cursor
```

Keep the `-I`: without it Python imports modules from the working directory first.
`--runtime cursor` uses this machine's Cursor connection to Cardinal and writes to
`~/.cursor/cardinal/storyboards/`.

**Sandbox.** In Cursor's default Auto-review mode, agent shell commands have no network
and cannot write outside the workspace and `/tmp`. The renderer fetches each scene's
bundle from Cardinal and writes under `~/.cursor`, so ask to run it outside the sandbox
(Cursor asks the user to approve). Give the command a long timeout: the run stops itself at
9 minutes.

Without Chrome or Chromium on the machine, say that the preview could not be verified, and
leave the storyboard unpublished unless the user decides otherwise.
