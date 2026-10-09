# Session-owned storyboard visualization

Use this workflow when the user asks for a storyboard visualization or asks to publish the storyboard. Giving a link, recording findings, or finishing an investigation does not itself authorize visualization authoring.

## Author the same storyboard

Use the session's bound `sb_…` and `inv_…` from its context. Never create a duplicate. If this runtime has no bound storyboard, use `storyboard__find` with the session id and context; create only when no suitable storyboard exists. Never edit another author's investigation just because it matches a search.

Read the existing storyboard with `storyboard__get`, then fetch `storyboard__describe_grammar` sections `authoring`, `evidence`, and `canvas`. Preserve useful existing scenes and their ids; revise the argument where the findings changed. The session supplies the explanation, while evidence supports the facts. Keep private prompts, credentials, and unrelated transcript content out of the storyboard.

Use `storyboard__define_surface` and `storyboard__upsert_scene` to author the draft. Bind quantitative claims to evidence fields or deterministic derivations. Promote only the captured `ev_…` results you cite using the installed `cardinal-evidence promote --storyboard <sb> …` helper. Reuse witnessed `rcpt_…` receipts. Withheld evidence cannot support a claim, and checkpoint/event records are claims rather than evidence.

Draw what makes the finding understandable: a comparison, sequence, dependency, or before/after. Read the server's canvas guide for the available primitives, bindings, libraries and validation rules. Do not replace a clear visual with raw logs or decorative charts.

## Preview and revise

Call `storyboard__preview`. Fix validation errors. Render the returned preview bundles using the adapter instructions in SKILL.md, then inspect every scene and reveal step. Check whether the point is clear, labels are readable, and nothing clips or overlaps. Revise and preview again as needed. Never claim visual review when rendering was unavailable; report that limitation.

Keep the same storyboard and leave the result private. Updating a visualization does not authorize publishing or sharing. If every act is published, use `storyboard__add_act` to open a new draft on the same storyboard; preserve published acts. State the question and reviewed time window with `storyboard__set_frame` when needed for publication. For a publish request, complete the workflow below; preserve public-link and raw-evidence confirmations.

## Publish after updating

A request to publish authorizes updating and reviewing the visualization, then publishing that reviewed draft. Refresh the scenes to reflect the latest findings and evidence before calling `storyboard__publish`; do not publish an empty or stale draft. Complete the preview and revision loop above first. If authoring, validation, or rendering fails, leave the storyboard in draft and report what remains.

Call `storyboard__publish` only after those steps succeed, and verify that its response reports publication. Draft storyboards are visible only to their author. Publication makes the reviewed visualization visible to members of the organization; creating or extending public share links requires a separate explicit request. Honor any public-link and raw-evidence confirmations returned by the server.

Return the storyboard link and briefly describe whether it was updated privately or published. Do not prompt for an update at the end of ordinary work or after completing this skill.
