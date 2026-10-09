"""InvestigationState: the machine-readable twin of a published storyboard.

A storyboard is the human explanation of an investigation; InvestigationState
is the same work for an agent that has to continue it without the original
transcript. It is two halves in one JSON document:

  derived   projected deterministically from the published acts of the
            storyboard (storyboard__get): the question, every scene as a
            finding (its state, statement, claims with scope and evidence
            roles, cited receipts), every rules_out claim as a ruled-out
            hypothesis, every open question, and each cited receipt's
            evidence tier exactly as the server reports it.
  authored  written by the agent that ran the investigation, for what the
            storyboard grammar has no place for: who the actors are,
            constraints (who set them, from what source, and any explicit
            scoped exception), decisions (who proposed, who decided, how it
            was approved), hypotheses the storyboard cut and how each was
            established (tested, judgement, review), what each receipt is,
            which later item resolved an earlier one, what an open question
            is waiting on, typed references to docs / commits / PRs / code,
            and the re-typing of open questions that are really a finding's
            limits, a constraint or a decision.

Authority comes from the cited source (v1.1). The author may always record
its own interpretation as its own; it may not upgrade it to an owner
constraint, an owner decision, an explicit approval, a tested hypothesis, a
verified population or an owner deadline unless the cited source says so.
Owner authority rests only on what the owner actually said: a message quote
that check() verifies against the session transcripts (the transcript is an
authoring-time oracle, like a receipt; only the short quote is kept). A doc,
commit or PR the agent may have written never confers owner authority.
One narrow exception, plan approval: the owner's verified reply approves
items of the agent's plan message it directly answers; the item stays the
agent's words, approved by the owner (never the owner's words). Validation
may lower authority (downgrade()); it never erases what the state records.

Three layers, never merged: a finding's statement is storyboard prose, the
evidence binding says which receipt value backs it, the receipt is the
evidence. The prose is not itself evidence. storyboard__get does not expose
the bindings yet, so v1 keeps the prose verbatim and records, as a finding's
`discrepancies`, any place where the prose and a receipt disagree, anchored
to the exact words; nothing rewrites the prose.

project() builds the derived half (and empty authored sections); check()
re-projects from the server and refuses a document whose derived half was
edited, whose references do not resolve, that drops an open question, that
claims an owner decision without its provenance, or that carries a field the
schema does not know (no summary, context or reasoning blob). Findings exist
only in the derived half: what the state calls established is exactly what
passed the storyboard's publish checks. Evidence is referenced by receipt id,
never copied, and its tier is never authored.

Before any storyboard exists, the state belongs to an Investigation (a
server object with a question and a window): it is checked against a
virtual empty storyboard (virtual_storyboard()), so the derived half is
empty, source is {storyboard_id: null, acts: []}, and the authored sections
are held to every rule above. Once a storyboard is attached, refresh()
re-projects from it and carries the authored sections over. The
investigation's id is never part of the document.

The schema and its authority model are frozen at investigation-state/v1.1:
a change to either is a new schema id, not an edit.

Standard library only; never imports the adapter.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

SCHEMA = "investigation-state/v1.1"

STORYBOARD_ID_RE = re.compile(r"^sb_[0-9a-f]{24}$")
INVESTIGATION_ID_RE = re.compile(r"^inv_[0-9a-f]{24}\Z")
RECEIPT_ID_RE = re.compile(r"^rcpt_[0-9a-f]{24}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_./-]{1,96}$")

STATUSES = ("open", "blocked", "concluded")
HYPOTHESIS_STATUSES = ("open", "untested", "supported", "ruled_out")
# proposed: put forward, nobody has decided it yet.
DECISION_OUTCOMES = ("proposed", "adopted", "rejected", "superseded")
# explicit: the decider said so (source quotes or records it); by_action: the
# decider's own later action implies it (source names the action); pending:
# not decided; unknown: the provenance is not known. Never inferred upward.
APPROVALS = ("explicit", "by_action", "pending", "unknown")
# Plan approval: the owner's verified reply approves an item of the agent
# message it directly answers. {kind, by, plan_source, approval_source}.
PLAN_APPROVAL = "plan_approval"
PLAN_APPROVAL_KEYS = ("kind", "by", "plan_source", "approval_source")
# An agent message this long is a candidate antecedent: two of them before
# one owner reply make the reply ambiguous, and plan approval fails closed.
MIN_PLAN_CHARS = 400
# How a hypothesis or limit was established, distinct from its status: a
# ruled_out judgement is not a test. unknown when no source says how.
BASIS_METHODS = ("tested", "judgement", "review", "unknown")
# Where a constraint's force comes from, separate from who wrote its words:
# owner_stated (the owner's own words), owner_ratified (someone else's words
# the owner adopted), agent_interpretation (the agent's working reading of
# the task), unknown.
AUTHORITIES = ("owner_stated", "owner_ratified", "agent_interpretation", "unknown")
OWNER_AUTHORITIES = ("owner_stated", "owner_ratified")
ACTOR_ROLES = ("owner", "agent", "reviewer", "other")
TIERS = ("witnessed", "captured", "reported")
UNKNOWN_ACTOR = "unknown"

MAX_TEXT = 600
MAX_WHAT = 200
MAX_QUOTE = 200  # enough words to establish provenance, never a passage
QUOTE_SLACK_SECONDS = 3
MAX_RESPONSE_BYTES = 4 << 20

# A typed reference: kind -> (required keys, optional keys). pr needs number or url.
REF_KINDS = {
    "doc": (("path",), ("repo", "commit", "anchor")),
    "commit": (("sha",), ("repo", "subject")),
    "pr": ((), ("repo", "number", "url")),
    "code": (("path",), ("repo", "commit", "symbol", "lines")),
    "receipt": (("id",), ("pointer",)),
    "message": (("from", "at", "quote"), ("session",)),
}

# Authored keys per record. Anything else is refused: a record carries what
# a successor acts on, not a summary, context or reasoning blob.
EVIDENCE_KEYS = ("tier", "what", "tool", "artifact")
FINDING_KEYS = ("id", "act", "state", "title", "statement", "claims", "evidence")
FINDING_OVERLAY_KEYS = ("limits", "resolved_by", "discrepancies")
LIMIT_KEYS = ("id", "text", "basis", "verification", "evidence", "refs")
DISCREPANCY_KEYS = ("id", "prose", "receipt", "pointer", "shows")
DERIVED_HYPOTHESIS_KEYS = ("id", "statement", "status", "for", "scope", "evidence", "finding")
HYPOTHESIS_OVERLAY_KEYS = ("basis", "refs")
HYPOTHESIS_KEYS = ("id", "statement", "status", "basis", "verification", "evidence", "because", "refs")
BASIS_KEYS = ("method", "by", "source", "evidence", "criterion")
# A quantified scope: how many there are, and how many were actually checked.
VERIFICATION_KEYS = ("population", "verified", "evidence", "source")
COUNT_KEYS = ("count", "of")
DUE_KEYS = ("date", "set_by", "source")
QUESTION_BASE_KEYS = ("id", "text", "about", "next", "finding")
QUESTION_OVERLAY_KEYS = ("awaiting", "due", "resolved_by", "source", "refs")
CONSTRAINT_KEYS = ("id", "statement", "authored_by", "authority", "approval", "source", "because", "exceptions", "refs")
EXCEPTION_KEYS = ("id", "permits", "authorized_by", "source")
DECISION_KEYS = ("id", "statement", "outcome", "proposed_by", "decided_by", "approval", "source",
                 "because", "rationale", "superseded_by", "refs")
ACTOR_KEYS = ("id", "role", "identity")
TERM_KEYS = ("term", "means")
# Top-level keys of a v1.1 document (project()'s output), and of its source.
STATE_KEYS = ("schema", "source", "question", "status", "window", "context", "actors", "evidence", "findings",
              "hypotheses", "open_questions", "constraints", "decisions", "terms")
SOURCE_KEYS = ("storyboard_id", "acts")

# First-person process narration: what the state must never carry. A hint
# for the author, not a proof of absence (a warning, never an error).
NARRATION_RE = re.compile(
    r"\b(?:I|we)\s+(?:first|then|initially|started|began|thought|tried|noticed|looked|"
    r"wondered|realized|realised|guessed|suspected)\b"
    r"|\b(?:at first|my reasoning|chain of thought|in the transcript|this session)\b",
    re.IGNORECASE,
)
# Opaque labels (F2, P3, R4a, GEN_PENDING): defined elsewhere, meaningless to
# a successor. A warning: say what it is where it is used, or define it in terms.
LABEL_RE = re.compile(r"\b(?:[A-Z]{1,2}\d{1,3}[a-z]?|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
# A limit or hypothesis that claims verification says how much was verified.
VERIFY_RE = re.compile(r"\b(?:verif(?:ied|ication)|confirmed|proven|exact(?:ly)?|correct|checked)\b", re.IGNORECASE)
# What in a transcript is not anybody's own words: injected context, command
# output, subagent results relayed in the user role.
NOT_SAID_RE = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|local-command-caveat|command-name|"
    r"command-message|command-args|task-notification|bash-stdout|bash-stderr|bash-input)>.*?</\1>",
    re.DOTALL)


# What `state init` prints after the path: the authoring half of the
# storyboard skill, shown when the agent needs it (the skill stays slim).
AUTHORING_GUIDE = """\
Its derived half (question, every scene as a finding with its state, claims, scope and
receipts, every rules_out claim as a ruled-out hypothesis, every open question, each
receipt's tier) is projected from the storyboard. Edit only the authored parts, from what
this investigation established and decided, never the process. Authority comes from the
cited source: record your own reading as yours; never upgrade it to the owner's or to a test.
- actors: [{id, role: owner|agent|reviewer|other, identity?}]. Every by-field names one, or
  "unknown". The agent is never the owner.
- evidence[rcpt]: keep tier; add what (one line: what the receipt shows, so a successor
  knows whether to fetch it), tool (the call or command) and artifact (file or object).
- status: open, blocked (on a person's ruling or an outside event) or concluded.
- source: the owner's authority is only ever their own words, a message {from: owner, at,
  quote}: the shortest exact excerpt of their message (<=200 chars, same timestamp) that
  itself says the thing. check verifies it against the session transcript. A doc, commit
  or PR (even a decision record) never makes something the owner's: cite it in refs.
- Re-type, under the same id, each open question that is not a question: a caveat goes to
  that finding's limits ({id, text, basis?, verification?}); a rule to constraints; a choice
  already made to decisions.
- constraints: {id, statement, authored_by, authority, source, because?, exceptions?}.
  authority: owner_stated (the rule is the owner's words), owner_ratified (another's words the
  owner's own message adopts), agent_interpretation (your working reading), unknown. Include
  rules that live only in repo docs and deliberate non-actions, with their real authority.
  An exception {id, permits, authorized_by, source} waives an owner rule; an owner
  instruction that contradicts an agent_interpretation is not an exception: fix the rule.
- plan approval: the owner's reply approves items of the agent message it directly answers
  (the agent's last message before it; one substantial message, not several). approval:
  {kind: plan_approval, by: owner, plan_source: {agent message quote of the item},
  approval_source: {the owner's reply}} on a decision (decided_by owner) or a constraint
  (authored_by agent, authority owner_ratified). The item stays the agent's words.
- decisions: {id, statement, outcome: proposed|adopted|rejected|superseded, proposed_by,
  decided_by?, approval: explicit|by_action|pending|unknown, source?, because?, rationale?,
  superseded_by?}. decided_by owner + explicit: the owner's quoted words approve THIS
  decision (not a GO on something else, not silence, not a later agent action). by_action:
  the owner's own action. Otherwise decided_by unknown, approval unknown.
- hypotheses the scenes cut: {id, statement, status: open|untested|supported|ruled_out,
  basis: {method: tested|judgement|review|unknown, by, source?, evidence?, criterion?}}.
  tested: a check whose result decides the hypothesis: evidence (receipt) + criterion (the
  result that decides it). A measurement judged against no criterion is judgement.
  judgement / review: by + the source where that actor said it. Nobody said it: unknown.
  Never strengthen a hypothesis beyond what a source states.
- verification on a limit or hypothesis that says verified/checked/exact/correct:
  {population: {count, of}, verified: {count, of}, evidence?, source?}: how many there
  are and how many were actually checked (1,203 converted, 50 checked: say so).
- open_questions: most important first; add any the scenes left out, including asks put to
  a person that are still unanswered. awaiting: {actor, ask}. due {date, set_by, source}
  only when a source imposes the deadline; a date in evidence (an expiry) is not one.
  resolved_by: [ids] on an item (or an open finding) a later act settled.
- discrepancies on a finding: where its prose and a receipt disagree, {id, prose (the exact
  words of the statement), receipt, pointer?, shows (what the receipt says)}. Never reword.
- refs: typed {kind: doc {path, repo?, commit?, anchor?} | commit {sha} | pr {repo,
  number|url} | code {path, symbol?, lines?} | receipt {id, pointer?} | message}.
- Say what a label is where you use it; terms: [{term, means}] only for labels the
  storyboard prose itself uses.
Never delete an item because its authority cannot be shown: keep it at the authority its
sources support. state check --downgrade lowers unsupported claims and keeps the content.
because / resolved_by / superseded_by name items of the state or cited receipts. No
summary, context or reasoning fields; no "first I tried". Never edit a derived field or a
tier. Findings come only from scenes. Then: cardinal-storyboard state check <path> until ok.
"""


# ---------------------------------------------------------------------------
# Transcript oracle: what the owner and the agent actually said
# ---------------------------------------------------------------------------

def _parse_ts(v: Any) -> Optional[float]:
    import datetime
    if not isinstance(v, str):
        return None
    try:
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _norm(t: str) -> str:
    t = t.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return " ".join(t.split())


def session_utterances(lines) -> list:
    """[(epoch, role, text)] from a Claude Code session JSONL: what the person
    typed (role owner; their AskUserQuestion answers included) and the
    agent's own text (role agent). Not anybody's words, so never matched:
    subagent (sidechain) turns, compact summaries, meta entries, tool results,
    injected reminders, command output and relayed task notifications."""
    out: list = []
    tools: dict = {}
    for line in lines:
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
            continue
        ts = _parse_ts(d.get("timestamp"))
        msg = d.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if d.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tools[b.get("id")] = b.get("name")
                elif b.get("type") == "text" and ts is not None:
                    out.append((ts, "agent", b.get("text") or ""))
        elif d.get("type") == "user" and ts is not None:
            texts = []
            if isinstance(content, str):
                texts.append(content)
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(b.get("text") or "")
                elif b.get("type") == "tool_result" and tools.get(b.get("tool_use_id")) == "AskUserQuestion":
                    # The person's choices only: the questions are the agent's words.
                    r = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
                    texts += [str(a) for a in (r.get("answers") or {}).values()]
                    texts += [str(n.get("notes")) for n in (r.get("annotations") or {}).values()
                              if isinstance(n, dict) and n.get("notes")]
            for t in texts:
                t = NOT_SAID_RE.sub("", t)
                if t.strip() and not t.lstrip().startswith(("This session is being continued", "[Request interrupted")):
                    out.append((ts, "owner", t))
    return out


def load_sessions(paths: dict, reader: Callable[[str], Any]) -> dict:
    """{session_id: utterances} for the transcripts found ({session_id: path})."""
    out = {}
    for sid, path in paths.items():
        with reader(path) as fh:
            out[sid] = session_utterances(fh)
    return out


def _locate(ref: dict, role: str, sessions: dict) -> Optional[tuple]:
    """(session_id, index) of the utterance of that role at that time holding the quote."""
    q = _norm(str(ref.get("quote") or ""))
    at = _parse_ts(ref.get("at"))
    if not q or at is None:
        return None
    pool = {ref["session"]: sessions[ref["session"]]} if ref.get("session") in sessions else sessions
    for sid, u in pool.items():
        for i, (ts, r, t) in enumerate(u):
            if r == role and abs(ts - at) <= QUOTE_SLACK_SECONDS and q in _norm(t):
                return sid, i
    return None


def verify_plan_approval(appr: Any, role_of: Callable[[Any], Optional[str]], sessions: Optional[dict]) -> Optional[str]:
    """None when the owner's quoted reply approves the agent message holding
    the quoted item; else why not. Fails closed: the reply must come after the
    plan in the same session, no agent message may come between them, and the
    plan must be the only substantial agent message the reply could answer."""
    if not isinstance(appr, dict) or appr.get("kind") != PLAN_APPROVAL:
        return f"approval must be one of {', '.join(APPROVALS)} or {{kind: {PLAN_APPROVAL}, ...}}"
    if role_of(appr.get("by")) != "owner":
        return "a plan approval is given by the owner"
    for name, role in (("plan_source", "agent"), ("approval_source", "owner")):
        ref = appr.get(name)
        if not isinstance(ref, dict) or ref.get("kind") != "message" or role_of(ref.get("from")) != role:
            return f"{name} is a message quote from the {role}"
    if sessions is None:
        return "no session transcript is available here to verify the plan and its approval: pass --session <file> (that session's transcript JSONL)"
    plan = _locate(appr["plan_source"], "agent", sessions)
    ok = _locate(appr["approval_source"], "owner", sessions)
    if plan is None:
        return "plan_source is not in an agent message at that time: the item must be quoted from the plan"
    if ok is None:
        return "approval_source is not the owner's words at that time"
    if plan[0] != ok[0]:
        return "the approval is in another session than the plan"
    u, i, j = sessions[plan[0]], plan[1], ok[1]
    if j <= i:
        return "the approval comes before the plan"
    if any(r == "agent" for _, r, _ in u[i + 1:j]):
        return "the owner's reply answers a later agent message, not this plan"
    k = i - 1
    while k >= 0 and u[k][1] != "owner":
        if len(u[k][2]) >= MIN_PLAN_CHARS:
            return "ambiguous antecedent: the owner's reply follows more than one substantial agent message"
        k -= 1
    return None


def verify_quote(ref: dict, role: str, sessions: dict) -> Optional[str]:
    """None when the quote is in a message of that role at that time; else why not."""
    q = _norm(str(ref.get("quote") or ""))
    at = _parse_ts(ref.get("at"))
    if at is None:
        return "at is not an ISO timestamp"
    pool = [sessions[ref["session"]]] if ref.get("session") in sessions else list(sessions.values())
    if ref.get("session") and ref["session"] not in sessions:
        return f"session {ref['session']} is not loaded here: pass --session <file> (that session's transcript JSONL)"
    said = [(ts, r, t) for u in pool for ts, r, t in u if q and q in _norm(t)]
    if any(r == role and abs(ts - at) <= QUOTE_SLACK_SECONDS for ts, r, t in said):
        return None
    if any(r == role for ts, r, t in said):
        return "those words were said, but not at that time"
    if said:
        return "those are not the " + ("owner's words (the agent said them)" if role == "owner" else "agent's words")
    return ("no message in the session transcripts contains those words (if they were said in a session "
            "not loaded here, pass --session <file> (that session's transcript JSONL))")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _published_scenes(got: dict) -> list:
    return [s for s in got.get("scenes") or [] if isinstance(s, dict) and s.get("act_status") == "published"]


def _published_acts(got: dict) -> list:
    return sorted(a["number"] for a in got.get("acts") or []
                  if isinstance(a, dict) and a.get("status") == "published" and isinstance(a.get("number"), int))


def _claim(c: dict) -> dict:
    out = {"kind": c.get("kind"), "from": c.get("from"), "to": c.get("to")}
    if c.get("scope"):
        out["scope"] = c["scope"]
    out["evidence"] = [{"id": e.get("receipt_id"), "role": e.get("role") or "supports"}
                       for e in c.get("evidence") or [] if isinstance(e, dict)]
    return out


def _finding(s: dict) -> dict:
    return {
        "id": s.get("id"),
        "act": s.get("act"),
        "state": s.get("state"),
        "title": s.get("title"),
        "statement": s.get("statement"),
        "claims": [_claim(c) for c in s.get("claims") or [] if isinstance(c, dict)],
        "evidence": sorted(r for r in s.get("receipt_ids") or [] if isinstance(r, str)),
    }


def _scene_key(s: dict) -> str:
    """Scene ids are unique per act, not per storyboard: a later act may
    reuse one. The first act keeps the bare id."""
    return str(s.get("id"))


def _keys(scenes: list) -> list:
    seen: set = set()
    out = []
    for s in scenes:
        k = _scene_key(s)
        if k in seen:
            k = f"{k}@{s.get('act')}"
        seen.add(k)
        out.append(k)
    return out


def _context(got: dict) -> list:
    out = []
    for a in got.get("acts") or []:
        if not isinstance(a, dict) or a.get("status") != "published":
            continue
        c = a.get("context") or {}
        row = {"act": a.get("number")}
        for k in ("repo", "branch", "pr_url", "head_sha"):
            if c.get(k):
                row[k] = c[k]
        about = [{"kind": x.get("kind"), "value": x.get("value")} for x in a.get("about") or []
                 if isinstance(x, dict) and x.get("kind") != "repo"]
        if about:
            row["about"] = about
        if a.get("summary"):
            row["summary"] = a["summary"]
        out.append(row)
    return out


def derive(got: dict) -> dict:
    """The derived half of the state, from a storyboard__get result."""
    scenes = _published_scenes(got)
    keys = _keys(scenes)
    findings, hypotheses, questions = [], [], []
    for key, s in zip(keys, scenes):
        f = _finding(s)
        f["id"] = key
        findings.append(f)
        n = 0
        for c in f["claims"]:
            if c.get("kind") != "rules_out":
                continue
            n += 1
            h = {"id": f"{key}.h{n}", "statement": c.get("from"), "status": "ruled_out", "for": c.get("to")}
            if c.get("scope"):
                h["scope"] = c["scope"]
            h["evidence"] = sorted({e["id"] for e in c["evidence"] if e.get("role") in ("supports", "cross_check")})
            h["finding"] = key
            hypotheses.append(h)
        for q in s.get("open_questions") or []:
            if not isinstance(q, dict) or not q.get("id"):
                continue
            row = {"id": f"{key}/{q['id']}", "text": q.get("text"), "finding": key}
            if q.get("about"):
                row["about"] = q["about"]
            if q.get("next"):
                row["next"] = q["next"]
            questions.append(row)
    cited = sorted({r for f in findings for r in f["evidence"]}
                   | {e["id"] for f in findings for c in f["claims"] for e in c["evidence"]})
    tiers = got.get("receipt_tiers") or {}
    evidence = {r: {"tier": tiers.get(r, "unknown")} for r in cited}
    return {
        "question": got.get("question"),
        "window": got.get("window"),
        "context": _context(got),
        "evidence": evidence,
        "findings": findings,
        "hypotheses": hypotheses,
        "open_questions": questions,
    }


def virtual_storyboard(investigation: dict) -> dict:
    """What check() and project() take for an investigation no storyboard is
    attached to yet: a storyboard__get with nothing published. The question
    and window are the investigation's own; the derived half is empty."""
    return {"storyboard_id": None, "question": investigation.get("question"),
            "window": investigation.get("window"), "acts": [], "scenes": [], "receipt_tiers": {}}


def project(got: dict) -> dict:
    """A new state for a storyboard: the derived half, empty authored
    sections, and a status the author must confirm (open while any scene or
    question is open, or while nothing is published yet)."""
    d = derive(got)
    still_open = (bool(d["open_questions"]) or any(f["state"] == "open" for f in d["findings"])
                  or not _published_acts(got))
    return {
        "schema": SCHEMA,
        "source": {"storyboard_id": got.get("storyboard_id"), "acts": _published_acts(got)},
        "question": d["question"],
        "status": "open" if still_open else "concluded",
        "window": d["window"],
        "context": d["context"],
        "actors": [],
        "evidence": d["evidence"],
        "findings": d["findings"],
        "hypotheses": d["hypotheses"],
        "open_questions": d["open_questions"],
        "constraints": [],
        "decisions": [],
        "terms": [],
    }


def _overlay(fresh: dict, old: Optional[dict], keys: tuple) -> dict:
    if isinstance(old, dict):
        for k in keys:
            if k in old:
                fresh[k] = old[k]
    return fresh


def refresh(old: dict, got: dict) -> dict:
    """project() again (a new act was published) keeping what was authored:
    status, actors, terms, constraints, decisions, authored hypotheses and
    open questions, every overlay on a derived record (a receipt's what /
    tool / artifact, a finding's limits / resolved_by / discrepancies, a
    hypothesis's basis, an open question's awaiting / due / resolved_by),
    and every re-typing of a storyboard open question (a re-typed id is not
    listed as open again)."""
    new = project(got)
    derived_h = {h["id"] for h in new["hypotheses"]}
    derived_q = {q["id"] for q in derive(got)["open_questions"]}
    old_ev = old.get("evidence") if isinstance(old.get("evidence"), dict) else {}
    for r, e in new["evidence"].items():
        prev = old_ev.get(r)
        if isinstance(prev, dict):
            new["evidence"][r] = dict(e, **{k: prev[k] for k in ("what", "tool", "artifact") if k in prev})
    old_f = {f.get("id"): f for f in old.get("findings") or [] if isinstance(f, dict)}
    for f in new["findings"]:
        _overlay(f, old_f.get(f["id"]), FINDING_OVERLAY_KEYS)
    retyped = {lim.get("id") for f in new["findings"] for lim in f.get("limits") or [] if isinstance(lim, dict)}
    for k in ("actors", "constraints", "decisions", "terms"):
        new[k] = [x for x in old.get(k) or [] if isinstance(x, dict)]
    retyped |= {x.get("id") for k in ("constraints", "decisions") for x in new[k]}
    old_q = [q for q in old.get("open_questions") or [] if isinstance(q, dict)]
    kept_q = {q.get("id"): q for q in old_q}
    order = [q.get("id") for q in old_q] + [q["id"] for q in new["open_questions"] if q["id"] not in kept_q]
    fresh = {q["id"]: _overlay(dict(q), kept_q.get(q["id"]), QUESTION_OVERLAY_KEYS) for q in new["open_questions"]}
    new["open_questions"] = [fresh.get(i) or kept_q[i] for i in order
                             if i not in retyped and (i in fresh or i not in derived_q)]
    old_h = {h.get("id"): h for h in old.get("hypotheses") or [] if isinstance(h, dict)}
    for h in new["hypotheses"]:
        _overlay(h, old_h.get(h["id"]), HYPOTHESIS_OVERLAY_KEYS)
    # Derived hypotheses carry `finding`; authored ones never do.
    new["hypotheses"] += [h for h in old_h.values() if "finding" not in h and h.get("id") not in derived_h]
    if old.get("status") in STATUSES:
        new["status"] = old["status"]
    return new


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------

def summary(result: dict) -> str:
    s = result.get("stats") or {}
    return (f"{s.get('findings', 0)} findings, {s.get('hypotheses', 0)} hypotheses, "
            f"{s.get('open_questions', 0)} open questions, {s.get('limits', 0)} limits, "
            f"{s.get('constraints', 0)} constraints, {s.get('decisions', 0)} decisions, "
            f"{s.get('receipts', 0)} receipts ({s.get('described', 0)} described); "
            f"{s.get('owner_quotes_verified', 0)} owner / {s.get('agent_quotes_verified', 0)} agent quotes verified; "
            f"{s.get('bytes', 0)} bytes (~{s.get('approx_tokens', 0)} tokens)")


MALFORMED = (TypeError, AttributeError, KeyError, ValueError, IndexError)


def check(state: Any, got: dict, sessions: Optional[dict] = None, previous: Optional[dict] = None,
          allow_removed: tuple = ()) -> dict:
    """_check(), except that a document too malformed to walk (a list
    where an object belongs, an id that is not a string) is an error, never a
    crash: the check fails closed."""
    try:
        return _check(state, got, sessions, previous, allow_removed)
    except MALFORMED as e:
        return {"errors": [f"the state is malformed ({type(e).__name__}: {e}): fix its shape against the schema"],
                "warnings": [], "stats": {}}


def _check(state: Any, got: dict, sessions: Optional[dict] = None, previous: Optional[dict] = None,
           allow_removed: tuple = ()) -> dict:
    """{errors, warnings, stats}. errors: the document is not a faithful
    InvestigationState of this storyboard. warnings: worth a look.
    sessions: {session_id: session_utterances(...)} of the transcripts this
    machine holds, the oracle for every owner and agent quote; None when none
    is available (then nothing can rest on the owner's words). previous: the
    last state that checked ok; an authored item it had may not disappear
    (validation lowers authority, it never erases) unless allow_removed."""
    errors: list = []
    warnings: list = []
    texts: list = []  # (where, text) of every authored text, for the text rules
    verified = {"owner": 0, "agent": 0}
    unverifiable: list = []
    owner_said: set = set()  # id() of message refs whose owner quote verified

    def err(msg: str) -> None:
        errors.append(msg)

    def text(where: str, t: Any, limit: int = MAX_TEXT, required: bool = False) -> None:
        if t is None and not required:
            return
        if not isinstance(t, str) or not t.strip():
            err(f"{where}: text is required")
            return
        if len(t) > limit:
            err(f"{where}: text over {limit} characters; split it or cut it")
        texts.append((where, t))

    def keys(x: dict, allowed: tuple, where: str) -> None:
        extra = sorted(set(x) - set(allowed))
        if extra:
            err(f"{where}: unknown fields {extra} (allowed: {', '.join(allowed)})")

    if not isinstance(state, dict):
        return {"errors": ["the state must be a JSON object"], "warnings": [], "stats": {}}
    keys(state, STATE_KEYS, "state")
    if state.get("schema") != SCHEMA:
        err(f'schema must be "{SCHEMA}"')
    src = state.get("source") or {}
    if isinstance(state.get("source"), dict):
        keys(src, SOURCE_KEYS, "source")
    if "storyboard_id" in src and src["storyboard_id"] is None and got.get("storyboard_id") is not None:
        err("source.storyboard_id is null but a storyboard is attached to the investigation now: "
            "re-run state init --investigation with --refresh to carry the authored sections over")
    elif src.get("storyboard_id", "") != got.get("storyboard_id"):
        err("source.storyboard_id does not match the storyboard")
    if src.get("acts") != _published_acts(got):
        err(f"source.acts must be the published acts {_published_acts(got)}: a new act was published, re-run init and carry the authored sections over")
    if state.get("status") not in STATUSES:
        err(f"status must be one of {', '.join(STATUSES)}")

    d = derive(got)
    for k in ("question", "window", "context"):
        if state.get(k) != d[k]:
            err(f"{k} differs from the storyboard; derived fields are not edited")

    # Evidence: the tier is derived; what / tool / artifact describe it.
    evidence = state.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(d["evidence"]):
        err("evidence must list exactly the receipts the published storyboard cites")
        evidence = evidence if isinstance(evidence, dict) else {}
    described = 0
    for r, e in evidence.items():
        if not isinstance(e, dict):
            err(f"evidence[{r}] must be {{tier, what?, tool?, artifact?}}")
            continue
        keys(e, EVIDENCE_KEYS, f"evidence[{r}]")
        if r in d["evidence"] and e.get("tier") != d["evidence"][r]["tier"]:
            err(f"evidence[{r}].tier differs from the storyboard; a tier is never authored")
        text(f"evidence[{r}].what", e.get("what"), MAX_WHAT)
        for k in ("tool", "artifact"):
            text(f"evidence[{r}].{k}", e.get(k), MAX_WHAT)
        if e.get("what"):
            described += 1
    undescribed = len(evidence) - described
    if undescribed:
        warnings.append(f"{undescribed} receipt(s) have no `what`: a successor cannot tell whether to fetch them")

    # Actors: every by-field resolves to one.
    actors: dict = {}
    for a in state.get("actors") or []:
        if not isinstance(a, dict):
            err("actors must be a list of objects")
            continue
        keys(a, ACTOR_KEYS, f"actors[{a.get('id')}]")
        if a.get("role") not in ACTOR_ROLES:
            err(f"actors[{a.get('id')}]: role must be one of {', '.join(ACTOR_ROLES)}")
        if not isinstance(a.get("id"), str) or not ITEM_ID_RE.match(a["id"]) or a["id"] == UNKNOWN_ACTOR:
            err(f"actors: id {a.get('id')!r} must match {ITEM_ID_RE.pattern} and not be {UNKNOWN_ACTOR!r}")
            continue
        actors[a["id"]] = a.get("role")

    def actor(v: Any, where: str, required: bool = True) -> Optional[str]:
        if v is None and not required:
            return None
        if v == UNKNOWN_ACTOR:
            return UNKNOWN_ACTOR
        if v not in actors:
            err(f"{where}: {v!r} is not an actor (list it in actors, or say \"{UNKNOWN_ACTOR}\")")
            return None
        return actors[v]

    def typed_refs(v: Any, where: str, required: bool = False) -> list:
        if v is None:
            if required:
                err(f"{where}: required (a typed list of references)")
            return []
        if not isinstance(v, list) or (required and not v):
            err(f"{where}: must be a {'non-empty ' if required else ''}list of typed references")
            return []
        out = []
        for i, ref in enumerate(v):
            w = f"{where}[{i}]"
            if not isinstance(ref, dict) or ref.get("kind") not in REF_KINDS:
                err(f"{w}: kind must be one of {', '.join(REF_KINDS)}")
                continue
            need, opt = REF_KINDS[ref["kind"]]
            keys(ref, ("kind",) + need + opt, w)
            for k in need:
                if ref.get(k) in (None, ""):
                    err(f"{w}: {ref['kind']} needs {k}")
            if ref["kind"] == "pr" and not (ref.get("number") or ref.get("url")):
                err(f"{w}: pr needs number or url")
            if ref["kind"] == "receipt" and ref.get("id") not in d["evidence"]:
                err(f"{w}: {ref.get('id')} is not a receipt this storyboard cites")
            if ref["kind"] == "message":
                role = actor(ref.get("from"), f"{w}.from")
                text(f"{w}.quote", ref.get("quote"), MAX_QUOTE, required=True)
                if role in ("owner", "agent") and isinstance(ref.get("quote"), str) and ref["quote"].strip():
                    if sessions is None:
                        unverifiable.append(w)
                    else:
                        why = verify_quote(ref, role, sessions)
                        if why:
                            err(f"{w}: quote not verified: {why}")
                        else:
                            verified[role] += 1
                            if role == "owner":
                                owner_said.add(id(ref))
            out.append(ref)
        return out

    def plan_approval(appr: Any, where: str) -> bool:
        """Validate a plan approval; True when it holds."""
        if not isinstance(appr, dict):
            err(f"{where}.approval must be {{kind: {PLAN_APPROVAL}, by, plan_source, approval_source}}")
            return False
        keys(appr, PLAN_APPROVAL_KEYS, f"{where}.approval")
        actor(appr.get("by"), f"{where}.approval.by")
        for name in ("plan_source", "approval_source"):
            typed_refs([appr.get(name)] if isinstance(appr.get(name), dict) else appr.get(name),
                       f"{where}.approval.{name}", required=True)
        why = verify_plan_approval(appr, actors.get, sessions)
        if why:
            err(f"{where}: plan approval not established: {why}")
        return why is None

    def owner_words(refs: list, where: str, claim: str) -> None:
        """Owner authority rests only on the owner's verified words."""
        if any(id(r) in owner_said for r in refs):
            return
        quoted = [r for r in refs if r.get("kind") == "message" and actors.get(r.get("from")) == "owner"]
        if quoted and sessions is None:
            err(f"{where}: {claim} rests on the owner's words, and no session transcript is available here to verify them: "
                "pass --session <file> (that session's transcript JSONL), or record it at the authority you can show (agent_interpretation / unknown; "
                "state check --downgrade)")
        elif not quoted:
            err(f"{where}: {claim} needs the owner's own words in source: a verified message quote from the owner "
                "(a doc, commit, PR or receipt does not make it the owner's)")

    def basis(b: Any, where: str, item_ev: Any) -> None:
        if not isinstance(b, dict):
            err(f"{where}.basis must be {{method: {'|'.join(BASIS_METHODS)}, by, source?, evidence?, criterion?}}")
            return
        keys(b, BASIS_KEYS, f"{where}.basis")
        m = b.get("method")
        if m not in BASIS_METHODS:
            err(f"{where}.basis.method must be one of {', '.join(BASIS_METHODS)}")
        actor(b.get("by"), f"{where}.basis.by", required=m in ("judgement", "review"))
        typed_refs(b.get("source"), f"{where}.basis.source", required=m in ("judgement", "review"))
        deferred_refs.append((b, f"{where}.basis", ("evidence",)))
        if m == "tested":
            if not b.get("evidence") and not item_ev:
                err(f"{where}: basis tested names the receipt whose result decides it (otherwise it is judgement, review or unknown)")
            text(f"{where}.basis.criterion", b.get("criterion"), required=True)

    def verification(v: Any, where: str) -> None:
        w = f"{where}.verification"
        if not isinstance(v, dict):
            err(f"{w} must be {{population: {{count, of}}, verified: {{count, of}}, evidence?, source?}}")
            return
        keys(v, VERIFICATION_KEYS, w)
        counts = {}
        for k in ("population", "verified"):
            c = v.get(k)
            if not isinstance(c, dict) or not isinstance(c.get("count"), int) or c["count"] < 0 \
                    or not isinstance(c.get("of"), str) or not c["of"].strip():
                err(f"{w}.{k} must be {{count, of}}")
                continue
            keys(c, COUNT_KEYS, f"{w}.{k}")
            counts[k] = c["count"]
        if len(counts) == 2 and counts["verified"] > counts["population"]:
            err(f"{w}: more verified than there are")
        if not v.get("evidence") and not v.get("source"):
            err(f"{w}: cite what checked them (evidence or source)")
        typed_refs(v.get("source"), f"{w}.source")
        deferred_refs.append((v, w, ("evidence",)))

    def claims_verification(item: dict, field: str, where: str) -> None:
        t = item.get(field)
        if isinstance(t, str) and VERIFY_RE.search(t) and "verification" not in item:
            err(f"{where} says verified/checked/exact/correct: add verification {{population, verified}} "
                "with how many were actually checked, or say only what the source establishes")
        if "verification" in item:
            verification(item["verification"], where)

    deferred_refs: list = []  # (item, where, fields) resolved once every id is known

    # Findings: the derived fields verbatim; limits / resolved_by / discrepancies authored.
    got_findings = {f["id"]: f for f in d["findings"]}
    findings = state.get("findings")
    if not isinstance(findings, list):
        err("findings must be a list")
        findings = []
    seen_f = set()
    limit_ids: list = []
    discrepancy_ids: list = []
    for f in findings:
        fid = f.get("id") if isinstance(f, dict) else None
        if fid not in got_findings:
            err(f"finding {fid!r} is not a scene of the published storyboard: findings come only from the storyboard")
            continue
        seen_f.add(fid)
        keys(f, FINDING_KEYS + FINDING_OVERLAY_KEYS, f"finding {fid}")
        if {k: f.get(k) for k in FINDING_KEYS} != got_findings[fid]:
            err(f"finding {fid} differs from its scene; add a limit or a discrepancy instead of editing it")
        deferred_refs.append((f, f"finding {fid}", ("resolved_by",)))
        for lim in f.get("limits") or []:
            if not isinstance(lim, dict) or not isinstance(lim.get("id"), str):
                err(f"finding {fid}: each limit is {{id, text, basis?, verification?, evidence?, refs?}}")
                continue
            w = f"limit {lim['id']}"
            keys(lim, LIMIT_KEYS, w)
            text(w, lim.get("text"), required=True)
            if "basis" in lim:
                basis(lim["basis"], w, lim.get("evidence"))
            typed_refs(lim.get("refs"), f"{w}.refs")
            deferred_refs.append((lim, w, ("evidence",)))
            claims_verification(lim, "text", w)
            limit_ids.append(lim["id"])
        prose = " ".join(str(got_findings[fid].get(k) or "") for k in ("title", "statement"))
        prose += " " + " ".join(str(c.get(k) or "") for c in got_findings[fid]["claims"] for k in ("from", "to", "scope"))
        for x in f.get("discrepancies") or []:
            if not isinstance(x, dict) or not isinstance(x.get("id"), str):
                err(f"finding {fid}: each discrepancy is {{id, prose, receipt, pointer?, shows}}")
                continue
            w = f"discrepancy {x['id']}"
            keys(x, DISCREPANCY_KEYS, w)
            if not isinstance(x.get("prose"), str) or not x["prose"] or x["prose"] not in prose:
                err(f"{w}: prose must be the exact words of finding {fid} it disputes")
            if x.get("receipt") not in d["evidence"]:
                err(f"{w}: receipt must be one this storyboard cites")
            text(f"{w}.shows", x.get("shows"), required=True)
            discrepancy_ids.append(x["id"])
    for fid in got_findings:
        if fid not in seen_f:
            err(f"finding {fid} is missing")

    ids: dict = {}

    def claim_id(i: Any, where: str) -> None:
        if not isinstance(i, str) or not ITEM_ID_RE.match(i):
            err(f"{where}: id {i!r} must match {ITEM_ID_RE.pattern}")
            return
        if i in ids:
            err(f"duplicate id {i!r} ({ids[i]} and {where})")
        ids[i] = where

    for fid in got_findings:
        claim_id(fid, "finding")
    for i in limit_ids:
        claim_id(i, "limit")
    for i in discrepancy_ids:
        claim_id(i, "discrepancy")

    sections = {k: state.get(k) for k in ("hypotheses", "open_questions", "constraints", "decisions")}
    for k, v in sections.items():
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            err(f"{k} must be a list of objects")
            sections[k] = []
    for k in ("hypotheses", "open_questions", "constraints", "decisions"):
        for x in sections[k]:
            claim_id(x.get("id"), k)
            for e in x.get("exceptions") or [] if k == "constraints" else []:
                claim_id(e.get("id") if isinstance(e, dict) else None, f"constraint {x.get('id')} exception")

    # Hypotheses: derived ones verbatim (+ basis); authored ones say how they were established.
    got_h = {h["id"]: h for h in d["hypotheses"]}
    for h in sections["hypotheses"]:
        hid = h.get("id")
        w = f"hypothesis {hid}"
        if hid in got_h:
            keys(h, DERIVED_HYPOTHESIS_KEYS + HYPOTHESIS_OVERLAY_KEYS, w)
            if {k: h.get(k) for k in DERIVED_HYPOTHESIS_KEYS if k in got_h[hid] or k in h} != got_h[hid]:
                err(f"{w} is a storyboard rules_out claim; it is not edited (add basis)")
            if "basis" in h:
                basis(h["basis"], w, h.get("evidence"))
            else:
                warnings.append(f"{w} has no basis: say whether ruling it out was a test, a judgement or a review")
            typed_refs(h.get("refs"), f"{w}.refs")
            continue
        if "finding" in h:
            err(f"{w}: `finding` marks a storyboard rules_out claim; an authored hypothesis cites with because / evidence")
        keys({k: v for k, v in h.items() if k != "finding"}, HYPOTHESIS_KEYS, w)
        if h.get("status") not in HYPOTHESIS_STATUSES:
            err(f"{w}: status must be one of {', '.join(HYPOTHESIS_STATUSES)}")
        text(w, h.get("statement"), required=True)
        if h.get("status") in ("supported", "ruled_out"):
            if "basis" not in h:
                err(f"{w} is {h.get('status')}: basis says how (tested, judgement, review, or unknown)")
            elif isinstance(h["basis"], dict) and h["basis"].get("method") == "unknown":
                warnings.append(f"{w} is {h.get('status')} on an unknown basis: a successor treats it as unestablished")
            if not h.get("evidence") and not h.get("because"):
                warnings.append(f"{w} is {h.get('status')} with no evidence or because: a successor cannot check it")
            claims_verification(h, "statement", w)
        elif "verification" in h:
            verification(h["verification"], w)
        if "basis" in h:
            basis(h["basis"], w, h.get("evidence"))
        typed_refs(h.get("refs"), f"{w}.refs")
        deferred_refs.append((h, w, ("because", "evidence")))
    for hid in got_h:
        if hid not in ids:
            err(f"hypothesis {hid} (a storyboard rules_out claim) is missing")

    # Constraints: who wrote the words, where the force comes from, and every
    # exception the rule's owner granted.
    for c in sections["constraints"]:
        w = f"constraint {c.get('id')}"
        keys(c, CONSTRAINT_KEYS, w)
        text(w, c.get("statement"), required=True)
        authority = c.get("authority")
        if authority not in AUTHORITIES:
            err(f"{w}: authority must be one of {', '.join(AUTHORITIES)}")
        author = actor(c.get("authored_by"), f"{w}.authored_by")
        if "approval" in c:
            plan_approval(c["approval"], w)
            if authority != "owner_ratified" or author != "agent":
                err(f"{w}: a plan approval ratifies the agent's words: authored_by an agent, authority owner_ratified")
        src = typed_refs(c.get("source"), f"{w}.source", required=authority in OWNER_AUTHORITIES and "approval" not in c)
        if authority in OWNER_AUTHORITIES and "approval" not in c:
            owner_words(src, w, f"authority {authority}")
        if authority == "owner_stated" and author not in (None, "owner"):
            err(f"{w}: owner_stated means the rule is the owner's own words; written by {c.get('authored_by')}, it is owner_ratified at most")
        if authority == "owner_ratified" and author == "owner":
            err(f"{w}: written by the owner, the rule is owner_stated")
        typed_refs(c.get("refs"), f"{w}.refs")
        deferred_refs.append((c, w, ("because",)))
        for x in c.get("exceptions") or []:
            if not isinstance(x, dict):
                err(f"{w}: each exception is {{id, permits, authorized_by, source}}")
                continue
            we = f"{w} exception {x.get('id')}"
            keys(x, EXCEPTION_KEYS, we)
            text(f"{we}.permits", x.get("permits"), required=True)
            by = actor(x.get("authorized_by"), f"{we}.authorized_by")
            xsrc = typed_refs(x.get("source"), f"{we}.source", required=True)
            if by == "owner":
                if authority not in OWNER_AUTHORITIES:
                    err(f"{we}: an owner instruction is not an exception to a rule the owner never set "
                        f"({authority}); the instruction is the authority: revise the constraint")
                owner_words(xsrc, we, "an owner's exception")
            elif authority in OWNER_AUTHORITIES:
                warnings.append(f"{we}: an owner's constraint excepted by someone who is not the owner")

    # Decisions: proposal, decision and approval are separate facts.
    for x in sections["decisions"]:
        did = x.get("id")
        w = f"decision {did}"
        keys(x, DECISION_KEYS, w)
        text(w, x.get("statement"), required=True)
        text(f"{w}.rationale", x.get("rationale"))
        outcome, approval = x.get("outcome"), x.get("approval")
        if outcome not in DECISION_OUTCOMES:
            err(f"{w}: outcome must be one of {', '.join(DECISION_OUTCOMES)}")
        if isinstance(approval, dict):
            plan_approval(approval, w)
            if x.get("decided_by") != approval.get("by"):
                err(f"{w}: decided_by is who gave the plan approval ({approval.get('by')!r})")
            if outcome in ("proposed",):
                err(f"{w}: an approved plan item is not a bare proposal")
            approval = PLAN_APPROVAL
        elif approval not in APPROVALS:
            err(f"{w}: approval must be one of {', '.join(APPROVALS)} or {{kind: {PLAN_APPROVAL}, ...}}")
        actor(x.get("proposed_by"), f"{w}.proposed_by")
        decider = actor(x.get("decided_by"), f"{w}.decided_by", required=False)
        src = typed_refs(x.get("source"), f"{w}.source", required=approval in ("explicit", "by_action"))
        typed_refs(x.get("refs"), f"{w}.refs")
        if decider == "owner" and approval == "explicit":
            owner_words(src, w, "explicit owner approval")
        if decider == "owner" and approval == "by_action" and src \
                and not any(id(r) in owner_said or r.get("kind") == "receipt" for r in src):
            err(f"{w}: by_action rests on the owner's own action: cite the owner's verified words or a receipt "
                "of the action (an agent-written doc, PR or commit does not show it)")
        if outcome == "proposed":
            if approval != "pending" or "decided_by" in x:
                err(f"{w}: a proposal is approval pending with no decided_by")
        elif outcome in DECISION_OUTCOMES:
            if approval == "pending":
                err(f"{w}: {outcome} is decided; pending is only for outcome proposed")
            if approval in ("explicit", "by_action", PLAN_APPROVAL) and x.get("decided_by") is None:
                err(f"{w}: approval {approval} names who decided (decided_by)")
        if decider == "owner" and approval == "unknown":
            err(f"{w}: decided_by an owner with approval unknown is an inferred approval; say unknown for decided_by too")
        if not x.get("because") and not x.get("rationale"):
            err(f"{w}: say why (because and/or rationale)")
        if outcome == "superseded" and not x.get("superseded_by"):
            warnings.append(f"{w} is superseded without superseded_by")
        deferred_refs.append((x, w, ("because", "superseded_by")))

    # Open questions: derived ones verbatim (+ what they wait on); authored ones well-formed.
    got_q = {q["id"]: q for q in d["open_questions"]}
    for q in sections["open_questions"]:
        w = f"open question {q.get('id')}"
        base = got_q.get(q.get("id"))
        keys(q, QUESTION_BASE_KEYS + QUESTION_OVERLAY_KEYS, w)
        if base:
            if {k: q.get(k) for k in QUESTION_BASE_KEYS if k in base or k in q} != base:
                err(f"{w} differs from the storyboard's; re-type it under the same id instead of rewording it")
        else:
            if "finding" in q:
                err(f"{w}: `finding` marks a storyboard open question; an authored one cites with refs / source")
            text(w, q.get("text"), required=True)
            text(f"{w}.next", q.get("next"))
        aw = q.get("awaiting")
        if aw is not None:
            if not isinstance(aw, dict):
                err(f"{w}.awaiting must be {{actor, ask}}")
            else:
                keys(aw, ("actor", "ask"), f"{w}.awaiting")
                actor(aw.get("actor"), f"{w}.awaiting.actor")
                text(f"{w}.awaiting.ask", aw.get("ask"), required=True)
        due = q.get("due")
        if due is not None:
            if not isinstance(due, dict) or not isinstance(due.get("date"), str):
                err(f"{w}.due must be {{date, set_by, source}}")
            else:
                keys(due, DUE_KEYS, f"{w}.due")
                setter = actor(due.get("set_by"), f"{w}.due.set_by")
                dsrc = typed_refs(due.get("source"), f"{w}.due.source", required=True)
                if dsrc and all(r.get("kind") == "receipt" for r in dsrc):
                    err(f"{w}.due: a date in evidence is not a deadline; due needs the source that imposes it")
                if setter == "owner":
                    owner_words(dsrc, f"{w}.due", "an owner deadline")
        typed_refs(q.get("source"), f"{w}.source")
        typed_refs(q.get("refs"), f"{w}.refs")
        deferred_refs.append((q, w, ("resolved_by",)))

    # Validation lowers authority; it never erases. An authored item of the
    # last state that checked ok is still here (at whatever authority).
    if isinstance(previous, dict):
        exception_ids = {e.get("id") for c in sections["constraints"] for e in c.get("exceptions") or []
                         if isinstance(e, dict)}
        for i in sorted(_authored_ids(previous) - set(ids) - exception_ids - set(allow_removed)):
            err(f"{i} was removed: keep it at the authority its sources support (state check --downgrade), "
                "or name it in --allow-remove if it was a duplicate or a mistake")

    # Every storyboard open question is kept: as an open question, or
    # re-typed (same id) as a finding's limit, a constraint or a decision.
    for q in d["open_questions"]:
        if q["id"] not in ids:
            err(f"open question {q['id']} was dropped: keep it, or move it under the same id to a finding's limits, constraints or decisions")

    # References resolve: to an item of this state, or a cited receipt.
    for item, where, fields in deferred_refs:
        for field in fields:
            v = item.get(field)
            if v is None:
                continue
            for r in v if isinstance(v, list) else [v]:
                if not isinstance(r, str):
                    err(f"{where}.{field}: {r!r} is not a reference")
                elif RECEIPT_ID_RE.match(r):
                    if r not in d["evidence"]:
                        err(f"{where}.{field}: {r} is not a receipt this storyboard cites")
                elif field == "evidence":
                    err(f"{where}.evidence: {r!r} is not a receipt id (rcpt_…)")
                elif r not in ids:
                    err(f"{where}.{field}: {r!r} names no item of this state")

    if state.get("status") == "concluded":
        for f in findings:
            if isinstance(f, dict) and f.get("state") == "open" and not f.get("resolved_by"):
                warnings.append(f"finding {f.get('id')} is open but the state is concluded: resolved_by names what settled it")

    # Terms: only for labels the state actually uses.
    terms = state.get("terms") or []
    defined = set()
    corpus = json.dumps({k: v for k, v in state.items() if k != "terms"}, ensure_ascii=False)
    for t in terms if isinstance(terms, list) else []:
        if not isinstance(t, dict) or not isinstance(t.get("term"), str):
            err("terms: each is {term, means}")
            continue
        keys(t, TERM_KEYS, f"term {t['term']}")
        text(f"term {t['term']}", t.get("means"), MAX_WHAT, required=True)
        if t["term"] not in corpus:
            err(f"term {t['term']!r} is not used anywhere in the state")
        defined.add(t["term"])
    for where, t in texts:
        if NARRATION_RE.search(t):
            warnings.append(f"{where} reads like process narration: state what was learned, not how")
    derived_text = json.dumps([d["question"], d["findings"], d["hypotheses"], d["open_questions"]], ensure_ascii=False)
    labels = sorted({m for _, t in texts for m in LABEL_RE.findall(t)} | set(LABEL_RE.findall(derived_text)))
    undefined = [x for x in labels if x not in defined]
    if undefined:
        warnings.append(f"labels used without saying what they are: {', '.join(undefined)}: name each where it is used, or define it in terms")

    if unverifiable:
        warnings.append(f"{len(unverifiable)} message quote(s) not verified: no session transcript is available here")

    for r, e in d["evidence"].items():
        if e["tier"] not in TIERS:
            warnings.append(f"{r}: the server reported no tier ({e['tier']})")

    body = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    stats = {
        "bytes": len(body.encode("utf-8")),
        "approx_tokens": (len(body) + 3) // 4,
        "findings": len(got_findings),
        "hypotheses": len(sections["hypotheses"]),
        "open_questions": len(sections["open_questions"]),
        "limits": len(limit_ids),
        "constraints": len(sections["constraints"]),
        "decisions": len(sections["decisions"]),
        "receipts": len(d["evidence"]),
        "described": described,
        "owner_quotes_verified": verified["owner"],
        "agent_quotes_verified": verified["agent"],
    }
    return {"errors": errors, "warnings": warnings, "stats": stats}


def _authored_ids(state: dict) -> set:
    out = set()
    for k in ("constraints", "decisions", "open_questions"):
        out |= {x.get("id") for x in state.get(k) or [] if isinstance(x, dict)}
    out |= {h.get("id") for h in state.get("hypotheses") or [] if isinstance(h, dict) and "finding" not in h}
    for f in state.get("findings") or []:
        for k in ("limits", "discrepancies"):
            out |= {x.get("id") for x in (f.get(k) or []) if isinstance(x, dict)} if isinstance(f, dict) else set()
    for c in state.get("constraints") or []:
        out |= {e.get("id") for e in (c.get("exceptions") or []) if isinstance(e, dict)} if isinstance(c, dict) else set()
    out.discard(None)
    return out


def downgrade(state: dict, sessions: Optional[dict]) -> tuple:
    """_downgrade(), except that a document too malformed to walk is
    returned unchanged (no changes) for check() to report."""
    import copy
    try:
        return _downgrade(state, sessions)
    except MALFORMED:
        return copy.deepcopy(state), []


def _downgrade(state: dict, sessions: Optional[dict]) -> tuple:
    """(state, changes): every authority claim its sources do not establish,
    lowered to the strongest level they do; nothing is removed. An owner
    instruction filed as an exception to a rule the owner never set becomes
    a decision of its own. Unverifiable quotes are not repaired here: they
    stay errors for the author."""
    import copy
    s = copy.deepcopy(state)
    changes: list = []
    roles = {a.get("id"): a.get("role") for a in s.get("actors") or [] if isinstance(a, dict)}

    def said(refs: Any) -> bool:
        return sessions is not None and any(
            isinstance(r, dict) and r.get("kind") == "message" and roles.get(r.get("from")) == "owner"
            and verify_quote(r, "owner", sessions) is None for r in refs or [])

    def plan_ok(appr: Any) -> bool:
        return verify_plan_approval(appr, roles.get, sessions) is None

    def unplan(item: dict) -> None:
        appr = item.pop("approval")
        refs = [appr.get(k) for k in ("plan_source", "approval_source") if isinstance(appr, dict) and isinstance(appr.get(k), dict)]
        if refs:
            item["source"] = (item.get("source") or []) + refs

    for c in s.get("constraints") or []:
        if not isinstance(c, dict):
            continue
        author = roles.get(c.get("authored_by"))
        # Only the source the author cited: a failed plan approval's quotes,
        # kept in source as provenance below, never establish the rule.
        cited = list(c.get("source") or [])
        if "approval" in c and not plan_ok(c["approval"]):
            unplan(c)
            changes.append(f"constraint {c.get('id')}: plan approval not established; its quotes kept in source")
        auth = c.get("authority")
        if auth == "owner_stated" and author not in ("owner", None) and said(cited):
            c["authority"] = "owner_ratified"
            changes.append(f"constraint {c.get('id')}: owner_stated -> owner_ratified (the words are the {author}'s)")
        elif auth in OWNER_AUTHORITIES and not ("approval" in c or said(cited)):
            c["authority"] = "agent_interpretation" if author == "agent" else "unknown"
            changes.append(f"constraint {c.get('id')}: {auth} -> {c['authority']} (no verified owner words)")
        owner_rule = c.get("authority") in OWNER_AUTHORITIES
        kept = []
        for e in c.get("exceptions") or []:
            if not isinstance(e, dict) or roles.get(e.get("authorized_by")) != "owner":
                kept.append(e)
            elif not said(e.get("source")):
                e["authorized_by"] = UNKNOWN_ACTOR
                kept.append(e)
                changes.append(f"exception {e.get('id')}: authorized_by owner -> unknown (no verified owner words)")
            elif not owner_rule:
                s.setdefault("decisions", []).append({
                    "id": e.get("id"), "statement": e.get("permits"), "outcome": "adopted",
                    "proposed_by": UNKNOWN_ACTOR, "decided_by": e.get("authorized_by"), "approval": "explicit",
                    "source": e.get("source"), "because": [c.get("id")]})
                changes.append(f"exception {e.get('id')}: an owner instruction, not an exception to "
                               f"{c.get('authority')} {c.get('id')}: now a decision of its own")
            else:
                kept.append(e)
        if "exceptions" in c:
            c["exceptions"] = kept

    for d in s.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        appr = d.get("approval")
        if isinstance(appr, dict) and not plan_ok(appr):
            unplan(d)
            d["approval"] = "unknown"
            d["decided_by"] = UNKNOWN_ACTOR
            changes.append(f"decision {d.get('id')}: plan approval not established -> decided_by unknown, approval unknown")
        elif roles.get(d.get("decided_by")) == "owner" and (
                (appr == "explicit" and not said(d.get("source")))
                or (appr == "by_action" and not (said(d.get("source")) or any(
                    isinstance(r, dict) and r.get("kind") == "receipt" for r in d.get("source") or [])))
                or appr == "unknown"):
            d["decided_by"], d["approval"] = UNKNOWN_ACTOR, "unknown"
            changes.append(f"decision {d.get('id')}: owner {appr} not established -> decided_by unknown, approval unknown")

    def lower(item: dict, where: str) -> None:
        b = item.get("basis")
        if not isinstance(b, dict):
            return
        m = b.get("method")
        if m == "tested" and not ((b.get("evidence") or item.get("evidence")) and b.get("criterion")):
            why = "no receipt and criterion that decide it"
        elif m in ("judgement", "review") and not (b.get("by") and b.get("source")):
            why = "no actor and source for it"
        else:
            return
        b["method"] = "unknown"
        changes.append(f"{where}: basis {m} -> unknown ({why})")

    for h in s.get("hypotheses") or []:
        if isinstance(h, dict):
            lower(h, f"hypothesis {h.get('id')}")
    for f in s.get("findings") or []:
        for lim in (f.get("limits") or []) if isinstance(f, dict) else []:
            if isinstance(lim, dict):
                lower(lim, f"limit {lim.get('id')}")
    for q in s.get("open_questions") or []:
        due = q.get("due") if isinstance(q, dict) else None
        if isinstance(due, dict) and roles.get(due.get("set_by")) == "owner" and not said(due.get("source")):
            due["set_by"] = UNKNOWN_ACTOR
            changes.append(f"open question {q.get('id')}: due set_by owner -> unknown (no verified owner words)")
    return s, changes


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

class FetchError(Exception):
    pass


def fetch_storyboard(conn: dict, storyboard_id: str, *, client: str, opener: Optional[Any] = None,
                     timeout: float = 20.0) -> dict:
    """storyboard__get over maestro's direct tool route, with the MCP key."""
    if not STORYBOARD_ID_RE.match(storyboard_id or ""):
        raise FetchError(f"not a storyboard id: {storyboard_id!r}")
    if not (conn.get("origin") and conn.get("org") and conn.get("key")):
        raise FetchError("not connected to Cardinal (run /cardinal:connect)")
    if opener is None:
        from .evidence_promote import no_redirect_opener
        opener = no_redirect_opener()
    url = (conn["origin"] + "/api/orgs/" + urllib.parse.quote(conn["org"], safe="")
           + "/storyboards/mcp-tools/get")
    req = urllib.request.Request(url, data=json.dumps({"storyboard_id": storyboard_id}).encode("utf-8"),
                                 method="POST", headers={
                                     "X-CardinalHQ-API-Key": conn["key"],
                                     "Content-Type": "application/json",
                                     "Accept": "application/json",
                                     "X-Cardinal-Client": client,
                                 })
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(64 << 10).decode("utf-8", "replace")
        finally:
            e.close()
        raise FetchError(f"storyboard__get answered {e.code}: {body[:300]}")
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"storyboard__get failed: {e}")
    try:
        got = json.loads(raw)
    except ValueError:
        raise FetchError("storyboard__get answered with something that is not JSON")
    if not isinstance(got, dict) or got.get("storyboard_id") != storyboard_id:
        raise FetchError("storyboard__get answered for another storyboard")
    return got


def load_get(path: str, loader: Callable[[str], str]) -> dict:
    """A storyboard__get result saved to a file (offline / tests)."""
    got = json.loads(loader(path))
    if not isinstance(got, dict) or not STORYBOARD_ID_RE.match(str(got.get("storyboard_id"))):
        raise FetchError(f"{path} is not a storyboard__get result")
    return got
