---
name: cardinal-storyboard
description: Author or update a Cardinal storyboard visualization in this session when the user asks or accepts the end-of-work offer. Includes evidence-bound scenes and rendered preview review. Updating visuals leaves the storyboard private; publishing and sharing need separate requests. Not needed to return a link or record investigation activity.
---

# Storyboard visualization

Follow [CORE.md](CORE.md) for the authoring, evidence, preview and completion workflow. Use the bound storyboard in this session's context.

Save the JSON returned by `storyboard__preview` to a local file, preserving the storyboard id, revision, and every scene's preview_bundle exactly. Render with the bundled helper below; use the current agent's Cardinal connection.

Run the script from this installed skill directory (not a similarly named file in the working repository):

```sh
python3 -I <this-skill-directory>/scripts/render_preview.py --runtime cursor --from-json <preview.json>
```

Read the resulting PNGs before considering the visualization reviewed. If Chrome/Chromium is unavailable, report that preview could not be verified. Do not publish just because the visualization is complete, and do not repeat the update offer after this skill finishes.
