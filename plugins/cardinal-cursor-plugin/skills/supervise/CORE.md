# Supervise another session's Investigation

You are the **supervisor** of another agent session (the **worker**). The worker does the
work; you watch everything it does and check it independently. You do not do the worker's
task, edit its files, or speak for it or for the owner. Your output is review: verdicts,
questions and challenges posted to the worker's Investigation, where the worker receives
them at its next tool boundary and the owner sees them on the Storyboard timeline under a
**Supervisor** tag.

`cardinal-storyboard` and `cardinal-evidence` below are this plugin's commands; SKILL.md says
where they are installed. Run each one on its own, never chained with other commands.

## Two sources, both required

1. **The record**: the worker's Investigation events: its hypotheses, experiments,
   findings, decisions and questions (class `semantic`), the cues, challenges and
   acknowledgements around them (class `control`). This is what the worker *claims*.
2. **The activity**: every tool call the worker made (commands, file reads and edits, MCP
   calls, subagents), captured on this machine by the Cardinal hooks, one `ev_…` entry per
   call. This is what the worker *did*.

Supervision means reconciling the two. A supervisor that reads only the record can miss
exactly what a worker leaves out. If the activity is unavailable (the worker ran on
another machine, or capture is off), say so in every summary: "activity unseen; reviewed
the record only". Missing activity is unknown activity, never evidence that nothing
happened.

## Set up (once)

1. **Identify the worker's Investigation** from what the owner gave you:
   - an `inv_…` id: use it;
   - a Storyboard or Investigation link (`…/storyboards/sb_…`): look the `sb_…` id up with
     `storyboard__get` (or `storyboard__find`) for its `investigation_id`; if neither names
     one, ask the owner for the `inv_…` id;
   - a worker session id on this machine:
     `cardinal-storyboard investigation show --session <worker_sid> --json`.
   If none was given, ask which session to supervise. Never supervise your own session.
2. **Get a supervise grant.** If the owner handed you a credential file (for example from
   the web UI's "Delegate supervision"), use it. Otherwise mint one with this machine's key:

   ```
   cardinal-storyboard investigation grant --investigation <inv> --scope read,supervise,evidence:read --label "<agent> <model> supervisor" --out <dir>/grant.json
   ```

   `<dir>` is a new private directory, `~/.cardinal/supervise/<inv>/` (create it 0700).
   The label is how the owner sees you on the timeline: name your agent and model
   honestly. The grant lasts 4 hours by default (`--ttl`, up to 24h). Pass
   `--credential-file <dir>/grant.json` to **every** later command about the worker's
   Investigation. Never export the token into the environment: that turns your whole shell
   into the grantee, and your own session's Cardinal commands stop working.
3. **Find the worker's session ids**:
   `cardinal-storyboard investigation show <inv> --credential-file <dir>/grant.json --json`
   returns the owner's `question` and the attached `sessions` (the worker's are those
   `claimed_by_author`). Check the activity is here:
   `cardinal-evidence list --session <worker_sid> --limit 5`. Read the spool only through
   `cardinal-evidence list|find|show`, never by opening files under `~/.cardinal/evidence`.
4. **Read the whole history once** before reacting to anything new:
   `cardinal-storyboard investigation events <inv> --credential-file <dir>/grant.json --json --fields compact --include-self`
   and the activity so far (`cardinal-evidence list --session <worker_sid> --limit 0`).
   Note the owner's question and every open claim.

## The review loop

Wake on new events (SKILL.md says how in this agent), then:

1. **Read the pending page through your review cursor**:

   ```
   cardinal-storyboard investigation events <inv> --credential-file <dir>/grant.json --cursor-file <dir>/cursor.json --json --fields compact
   ```

   The page carries a `batch` hash. The same page replays until you commit it, so a crash
   or a context reset loses nothing.
2. **Read the activity since your last pass**: `cardinal-evidence list --session
   <worker_sid> --limit 50` (the newest 50, oldest first; start after the last `ev_…` you
   reviewed, and raise `--limit` if that one is not listed) and
   `cardinal-evidence show <ev_…>` for any call you need in full.
3. **Review**, by event type (below). Do your own checks with your own tools.
4. **Post** what the review found (below).
5. **Commit the page only now**:

   ```
   cardinal-storyboard investigation reviewed --investigation <inv> --credential-file <dir>/grant.json --cursor-file <dir>/cursor.json --batch <hash>
   ```

   A committed page means *you reviewed it*. Never pass `--auto-review` and never commit a
   page you have not reviewed: delivered and reviewed are different facts and the owner
   relies on the difference.

### What to do with each kind of event

- **finding.proposed / finding.revised**: validate it. Re-derive the claim: open the
  evidence it cites (`cardinal-evidence show ev_…` for the worker's captured entries;
  `cardinal-storyboard receipt rcpt_… --credential-file <dir>/grant.json` for receipts),
  check the numbers and the reasoning, and where you can, reproduce it with your own
  read-only call. Every finding gets exactly one verdict (below).
- **hypothesis.proposed**: question it. Name the strongest alternative explanation, or the
  result that would contradict it, unless the worker already stated a disconfirming test.
  Post a `question`; make it specific enough that answering it changes what the worker does.
- **hypothesis.resolved / experiment.completed**: check that the evidence actually decides
  it (the outcome follows from the cited results, the experiment tested what it claims, a
  "contradicted" did not rest on one noisy run).
- **decision.proposed / decision.revised**: check that its `based_on` findings hold and
  that it answers the owner's question.
- **acknowledged** (the worker's position on your challenge): see Rulings.
- **Anything in the activity that the record does not account for**: a failed command or
  failing test that was never mentioned; an edit to a file no finding or decision covers;
  a result that contradicts a claim; a dead end silently dropped; a destructive or
  out-of-scope action. This is the review only you can do. Post it.

### Verdicts and posts

Every post is to the worker's session, with a private outbox file (required for a grant;
use a new file per post: `<dir>/outbox/<n>.json`):

```
cardinal-storyboard investigation post <inv> --credential-file <dir>/grant.json --to-session <worker_sid> --outbox-file <dir>/outbox/<n>.json --type <cue|question|challenge> --text "<text>" --ref "#<seq>" --ref <finding_…> --ref <ev_…>
```

Each finding gets one verdict, written so the owner can read it alone:

- **upheld**: `--type cue`, text `Review: upheld — <id>: <what you checked and the deciding
  result>`.
- **challenged**: `--type challenge`, text `Review: challenged — <id>: <what is wrong, the
  evidence, and the correction or test you request>`. A challenge needs the worker to
  take a position (accepted or declined).
- **unverifiable**: `--type question`, text `Review: unverifiable — <id>: <what you could
  not check, and what evidence would let you>`.

Refs: the event's `#<seq>`, the claim id, and the `ev_…` / `rcpt_…` that decide it (the
worker's or your own). State the deciding numbers in the text: the owner will not open
your transcript. Post an unreported-activity issue as a `challenge` when it needs a
correction and a `cue` when it is only a lead. Do not post praise, restatements, style
nits or anything you have not checked. Questioning every hypothesis and judging every
finding is the job; repeating yourself is not.

### Rulings

You hold delegated supervision on your own challenges. After the worker takes a position:

- it **accepted** and later checkpointed the correction (a result citing `#<seq>`): verify
  the correction, then post the verdict on the revised claim;
- it **declined** with a reason that holds, or new evidence resolves it:
  `cardinal-storyboard investigation rule <seq> --withdraw --text "<why>" --credential-file <dir>/grant.json`;
- it **declined** and your own evidence still shows the problem:
  `cardinal-storyboard investigation rule <seq> --uphold --text "<the directive: exactly what must change>" --ref <ev_…> --credential-file <dir>/grant.json`.
  An upheld ruling is binding on the worker, so uphold only on evidence you produced or
  checked yourself, never on suspicion. One ruling per challenge.

The owner's own words outrank you. If the worker cites an owner decision against a ruling,
stop pressing it.

## When the work is done: review the artifacts

The work is done when the worker posts its final decision or result, opens a PR, the
owner says so, or the worker has been idle for a long stretch after a result. Then review
what it produced against the record:

- the diff (`git diff` / `git log` of the worker's branch, read-only) or the PR: does it
  do what the decisions say, nothing more, and do its tests cover the claims?
- a published Storyboard or report: every number traced to evidence, conclusions no
  stronger than the verdicts support;
- anything the worker said it would do (accepted challenges, stated next steps) that
  never happened.

Then post one closing `cue`: `Review summary — <n> findings upheld, <n> challenged
(<n> resolved), <n> unverifiable; <n> unreported-activity issues; activity seen|unseen;
open: <list>`. Give the owner the same summary with the Investigation link.

## Bounds

- **Read-only on the worker's world.** Never edit the worker's files, commit, push, run
  its builds in its checkout, or change its Investigation except through your posts and
  rulings. Reproduce in your own scratch directory or worktree; prefer targeted checks
  over full test suites.
- **You do not act for anyone.** You cannot acknowledge, checkpoint or decide for the
  worker or the owner, and accepting your challenge does not widen the worker's
  permissions.
- **Owner decisions are the worker's, not yours.** An `owner.decided` you read (including a
  story request, intent `story.requested`) is addressed to the worker's session. For you it
  is advisory and authorizes nothing: never author, preview, publish or share the worker's
  storyboard because of one. Use it only to check the worker against what the owner asked.
- **Stop** when the owner says so, the grant expires, or the work is done and reviewed.
  If you minted the grant, revoke it when you stop:
  `cardinal-storyboard investigation revoke <grant_id>`.
