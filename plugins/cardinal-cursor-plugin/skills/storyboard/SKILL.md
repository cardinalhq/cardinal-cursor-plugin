---
name: cardinal-storyboard
description: Publish a Cardinal storyboard by updating its evidence-bound scenes and visualization, rendering and reviewing previews, then changing the draft status to published. Also use for explicitly requested private visualization updates. Not needed to return a link or record investigation activity.
---

# Storyboard visualization

Follow [CORE.md](CORE.md) for the authoring, evidence, preview and completion workflow. Use the bound storyboard in this session's context.

After `storyboard__preview`, the Cardinal plugin's hook renders the scenes locally and reports the PNG directory as additional context (see the canvas skill). Read those PNGs. When no such report follows the preview, render by hand as below.

Save the JSON returned by `storyboard__preview` to a local file, preserving the storyboard id, revision, and every scene's preview_bundle exactly. Render with the bundled helper below; use the current agent's Cardinal connection.

Run the script from this installed skill directory (not a similarly named file in the working repository):

```sh
python3 -I <this-skill-directory>/scripts/render_preview.py --runtime cursor --from-json <preview.json>
```

The renderer needs network access and writes under `~/.cursor`, so in Cursor's Auto-review sandbox ask to run it outside the sandbox. Read the resulting PNGs before considering the visualization reviewed. If Chrome/Chromium is unavailable, report that preview could not be verified. For a publish request, finish the shared publish workflow after reviewing the PNGs. Otherwise leave the result in draft.
