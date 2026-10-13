# Session-owned storyboard visualization

Use this workflow when the user asks for a storyboard visualization or asks to publish the storyboard. Giving a link, recording findings, or finishing an investigation does not itself authorize visualization authoring.

An owner decision with intent `story.requested` (delivered as `OWNER DECISION (story requested)`, from the Story tab's Request story) is the owner asking: it authorizes updating this storyboard's visuals and previewing them. It does not authorize publishing (finalizing the act) or sharing unless the decision text asks for that.

## Author the same storyboard

Use the session's bound `sb_…` and `inv_…` from its context, unless the person names an earlier session's storyboard to continue (see "Continue an earlier session's storyboard"). Never create a duplicate. If this runtime has no bound storyboard, use `storyboard__find` with the session id and context; create only when no suitable storyboard exists. Never edit another author's investigation just because it matches a search.

Read the existing storyboard with `storyboard__get`, then fetch `storyboard__describe_grammar` sections `authoring`, `evidence`, and `canvas`. Preserve useful existing scenes and their ids; revise the argument where the findings changed. The session supplies the explanation, while evidence supports the facts. Keep private prompts, credentials, and unrelated transcript content out of the storyboard.

Use `storyboard__define_surface` and `storyboard__upsert_scene` to author the draft. Bind quantitative claims to evidence fields or deterministic derivations. Promote only the captured `ev_…` results you cite using the installed `cardinal-evidence promote --storyboard <sb> …` helper. Reuse witnessed `rcpt_…` receipts. Withheld evidence cannot support a claim, and checkpoint/event records are claims rather than evidence.

Draw what makes the finding understandable: a comparison, sequence, dependency, or before/after. Read the server's canvas guide for the available primitives, bindings, libraries and validation rules. Do not replace a clear visual with raw logs or decorative charts.

## Tag the storyboard

Tags are org-wide labels people filter and group storyboards by. Once the question is framed:

1. Call `storyboard__list_tags`.
2. Pick 1 to 3 tags that fit the investigation. Reuse an existing tag whenever one fits, even if its name is not your first choice.
3. Call `storyboard__create_tag` only if none fit, with a lowercase-kebab name and a one-line description. If it returns an existing tag or lists similar ones, prefer those.
4. Call `storyboard__set_tags`, and revisit the tags before publishing.

If `storyboard__list_tags` is not available (an older Cardinal), skip tagging.

## Preview and revise

Call `storyboard__preview`. Fix validation errors. Render the returned preview bundles using the adapter instructions in SKILL.md, then inspect every scene and reveal step. Check whether the point is clear, labels are readable, and nothing clips or overlaps. Revise and preview again as needed. Never claim visual review when rendering was unavailable; report that limitation.

Keep the same storyboard and leave the result private. Updating a visualization does not authorize publishing or sharing. If every act is published, use `storyboard__add_act` to open a new draft on the same storyboard; preserve published acts. State the question and reviewed time window with `storyboard__set_frame` when needed for publication. For a publish request, complete the workflow below; preserve public-link and raw-evidence confirmations.

## Publish after updating

A request to publish authorizes updating and reviewing the visualization, then publishing that reviewed draft. Refresh the scenes to reflect the latest findings and evidence before calling `storyboard__publish`; do not publish a stale draft. An act with no scenes can be published: it publishes the investigation record so another agent can pick it up, but a public link still needs a published scene. Complete the preview and revision loop above first. If authoring, validation, or rendering fails, leave the storyboard in draft and report what remains.

Call `storyboard__publish` only after those steps succeed, and verify that its response reports publication. Draft storyboards are visible only to their author. Publication makes the reviewed visualization visible to members of the organization; creating or extending public share links requires a separate explicit request. Honor any public-link and raw-evidence confirmations returned by the server.

Return the storyboard link and briefly describe whether it was updated privately or published. Do not prompt for an update at the end of ordinary work or after completing this skill.

## Continue an earlier session's storyboard

When the person names an earlier storyboard to pick up (an `sb_…` id, its link, or the session that made it), author that one instead of this session's:

1. Find it with `storyboard__find` and that session id (or take the named `sb_…`), then `storyboard__get` it. Never `storyboard__create` a duplicate; this session's own automatic draft stays empty.
2. If every act is published, call `storyboard__add_act` before any `cardinal-evidence promote` or `storyboard__preview`: uploads to a published storyboard are refused.
3. Cite the old session's `ev_…` ids as usual. If promote reports one missing (aged out after 14 days, or evicted under disk pressure), say so and replace it with a witnessed query or `storyboard__record_evidence`.
4. Preview, revise and publish as above. If the server refuses the update from this session, say so.

To continue the old investigation's timeline too, the person starts a new session with `CARDINAL_INVESTIGATION_ID=inv_…` to join it. Do not run `cardinal-storyboard investigation bind` from a running session: it replaces this session's binding, including its storyboard and capabilities.

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
