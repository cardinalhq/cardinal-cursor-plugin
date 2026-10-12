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

## Supervising another session

When you watch another session's investigation (a grant from its author, or your own second session), use one command instead of a polling loop:

```sh
cardinal-storyboard investigation events <inv> --follow --json --fields compact
```

- `--follow` replaces hand-written loops (sleep, re-read, diff): it prints each new page as it arrives, every `--interval` seconds (default 5), until `--timeout`, `--max` or Ctrl-C. Run one `--follow` per watched investigation, never several on the same one.
- Under `--follow` with a session (`--session`, or this runtime's session id from the environment) or a grant, `--exclude-self` is the default: your own posts and acks are not echoed back. `--include-self` shows them.
- `--fields compact` (with `--json`) gives every event the same bounded fields, with one uniform `text` whatever its type.
- With a grant's credential file, add `--credential-file <file>`; `investigation grant` prints the exact command for the grantee.
- Post feedback as `challenge` when you need a position (correction, test, other work), `cue` for an informational lead, `question` for a question. Acceptance is not completion: the worker checkpoints the result with refs `["#<seq>"]` when the work is done.
- Inside a sandbox with network off (Codex workspace-write, Cursor Auto-review), these commands need the user's approval. The MCP tools `storyboard__read_investigation_events` and `storyboard__post_investigation_event` read and post without it, when no local evidence needs promoting.
