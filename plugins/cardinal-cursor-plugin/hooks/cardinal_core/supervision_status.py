"""Delegated supervision: the status of each challenge of an Investigation,
derived from its event stream. A pure function, the Python mirror of
@cardinalhq/shared `challengeStatuses` (conductor
packages/shared/src/investigation/supervision.ts); both are pinned to the
same JSON fixture of event streams -> statuses (vendored as
core/tests/testdata/supervision-statuses.json).

  open                 no position yet (a `noted` ack is not one)
  accepted             the worker (the investigation's author) accepted it,
                       with a note: in progress
  addressed            after that, an author semantic event lists
                       "#<challenge seq>" in its payload.refs
  declined             the worker declined it, with a reason (the supervisor
                       may rule on it, as on an accepted one)
  withdrawn            the supervisor withdrew it (ruling.issued, withdrawn)
  directive            the supervisor upheld it (ruling.issued, upheld); not
                       yet accepted
  directive_accepted   the worker accepted the directive
  directive_addressed  after that, an author semantic event lists
                       "#<ruling seq>" or "#<challenge seq>"
  conflicted           the worker answered the directive `conflicts`, citing
                       the owner's words (owner_seq)
  overruled            an owner decision posted from the web (authority
                       owner_decision) whose `answers` is the challenge or its
                       ruling; wins over every other status (owner_seq). A
                       relayed one (owner_relayed) or one without an
                       authority overrules nothing (overrules_supervision)

Once addressed / directive_addressed, the supervisor's verdicts
(verification.recorded) on the challenge's current correction
(correction_of) decide (conductor docs/specs/supervision-verdicts.md §3):

  verified             a verified verdict on it, and no rejected one
  rejected             a rejected verdict on it (any scope: rejected wins)
  directive_rejected   the same, on the directive path
  unverifiable         only unverifiable verdicts on it

Only ACTIVE verdicts combine: one a later verdict of the same grant lineage
replaced (payload.supersedes_verdict, same verifies / correction / scope) is
history; it still counts as a review for correction_of and can still be
answered by "#v". A verdict's lineage is producer.lineage_root_id, else its
grantee id (the server's COALESCE(lineage_root_id, id)).

Each status is {"status", "ruling_seq", "owner_seq", "correction_seq",
"verification_seq"} (None when absent).
The server validates every write (one position per session, one ruling per
challenge, upheld only once the worker took a position, conflicts only
citing an owner.decided); this derivation reads the stream as stored and does not re-judge it.
Standard library only; Python 3.9.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

CHALLENGE = "challenge.added"
ACKNOWLEDGED = "acknowledged"
RULING = "ruling.issued"
OWNER_DECIDED = "owner.decided"
VERIFICATION = "verification.recorded"
VERDICTS = ("verified", "rejected", "unverifiable")
VERIFICATION_SCOPES = ("code_review", "test_run", "fixture_validation", "merge_readiness", "deployment",
                       "rollout_observed")
ANSWERABLE_VERDICTS = ("rejected", "unverifiable")   # a later event citing one by "#v" answers it
UPHELD = "upheld"
WITHDRAWN_OUTCOME = "withdrawn"
OUTCOMES = (UPHELD, WITHDRAWN_OUTCOME)
CONFLICTED_DISPOSITION = "conflicts"   # the ack disposition (status: conflicted)
CHALLENGE_POSITIONS = ("accepted", "declined")
DIRECTIVE_POSITIONS = ("accepted", CONFLICTED_DISPOSITION)

OPEN = "open"
ACCEPTED = "accepted"
ADDRESSED = "addressed"
DECLINED = "declined"
WITHDRAWN = "withdrawn"
DIRECTIVE = "directive"
DIRECTIVE_ACCEPTED = "directive_accepted"
DIRECTIVE_ADDRESSED = "directive_addressed"
CONFLICTED = "conflicted"
OVERRULED = "overruled"
VERIFIED = "verified"
REJECTED = "rejected"
DIRECTIVE_REJECTED = "directive_rejected"
UNVERIFIABLE = "unverifiable"
STATUSES = (OPEN, ACCEPTED, ADDRESSED, DECLINED, WITHDRAWN, DIRECTIVE, DIRECTIVE_ACCEPTED, DIRECTIVE_ADDRESSED,
            CONFLICTED, OVERRULED, VERIFIED, REJECTED, DIRECTIVE_REJECTED, UNVERIFIABLE)
# A correction exists (correction_seq is set): the statuses a verdict may judge.
CORRECTED = (ADDRESSED, DIRECTIVE_ADDRESSED, VERIFIED, REJECTED, DIRECTIVE_REJECTED, UNVERIFIABLE)

# Still owed work: re-surfaced to the worker until addressed / overruled.
RESURFACE = (ACCEPTED, DIRECTIVE, DIRECTIVE_ACCEPTED, REJECTED, DIRECTIVE_REJECTED)
# A directive the worker has not finished: blocks Stop once (a rejected
# directive correction once more).
OPEN_DIRECTIVE = (DIRECTIVE, DIRECTIVE_ACCEPTED, DIRECTIVE_REJECTED)

SEMANTIC_CLASS = "semantic"
SEMANTIC_TYPES = ("hypothesis.proposed", "hypothesis.resolved", "experiment.started", "experiment.completed",
                  "experiment.revised", "finding.proposed", "finding.revised", "finding.retracted",
                  "decision.proposed", "decision.revised", "question.opened", "question.resolved")


def overrules_supervision(e: dict) -> bool:
    """An owner.decided that may overrule a challenge or ruling: posted from
    the owner's signed-in web session (authority owner_decision). A relayed
    one carries the owner's machine key, which the supervised agent holds
    too, so it overrules nothing."""
    return e.get("type") == OWNER_DECIDED and e.get("authority") == "owner_decision"


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _payload(e: dict) -> dict:
    p = e.get("payload")
    return p if isinstance(p, dict) else {}


def by_author(e: dict) -> bool:
    """Written by the investigation's author (producer.is_investigation_author
    true, server-computed): the derivation's only notion of the worker."""
    p = e.get("producer")
    return isinstance(p, dict) and p.get("is_investigation_author") is True


def author_session(e: dict) -> bool:
    """by_author, from a session (not a grantee, not the web): what this
    plugin's own acknowledgments and checkpoints look like as stored."""
    p = e.get("producer") if isinstance(e.get("producer"), dict) else {}
    pr = p.get("principal") if isinstance(p.get("principal"), dict) else {}
    return by_author(e) and pr.get("kind") != "grantee" \
        and isinstance(p.get("session_id"), str) and bool(p.get("session_id"))


def is_semantic(e: dict) -> bool:
    c = e.get("class")
    if isinstance(c, str):
        return c == SEMANTIC_CLASS
    return e.get("type") in SEMANTIC_TYPES


def _seq(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 2 ** 53 - 1 else None


def _refs(e: dict) -> List[str]:
    refs = _payload(e).get("refs")
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


def cites(e: dict, seqs: Iterable[int]) -> bool:
    """`e`'s payload.refs has "#<seq>" for one of `seqs` (whole strings)."""
    want = {f"#{s}" for s in seqs}
    return any(r in want for r in _refs(e))


def _ordered(events: Any) -> List[dict]:
    out = [e for e in (events or []) if isinstance(e, dict) and _int(e.get("seq")) is not None]
    out.sort(key=lambda e: e["seq"])
    return out


def _latest_position(acks: List[dict], target: int, positions: tuple) -> Optional[dict]:
    out = None
    for a in acks:
        if _seq(_payload(a).get("ack_of")) != target or _payload(a).get("disposition") not in positions:
            continue
        if out is None or a["seq"] > out["seq"]:
            out = a
    return out


def _citing(semantic: List[dict], after: int, seqs: tuple) -> List[dict]:
    return [e for e in semantic if e["seq"] > after and cites(e, seqs)]


def verdicts_on(events: Any, c: int) -> List[dict]:
    """The verification.recorded events of `events` whose payload.verifies
    is `c`, with a known verdict and scope and an integer correction (a
    verdict on a challenge always names one), ascending."""
    out = []
    for e in _ordered(events):
        p = _payload(e)
        if e.get("type") == VERIFICATION and _seq(p.get("verifies")) == c and p.get("verdict") in VERDICTS \
                and p.get("scope") in VERIFICATION_SCOPES and _seq(p.get("correction")) is not None:
            out.append(e)
    return out


def lineage(e: dict) -> Optional[str]:
    """The grant lineage a verdict was recorded by: producer.lineage_root_id,
    else the producer's principal id."""
    p = e.get("producer") if isinstance(e.get("producer"), dict) else {}
    root = p.get("lineage_root_id")
    if isinstance(root, str) and root:
        return root
    pr = p.get("principal") if isinstance(p.get("principal"), dict) else {}
    return pr.get("id") if isinstance(pr.get("id"), str) and pr.get("id") else None


def revised_by(verdicts: List[dict]) -> Dict[int, int]:
    """{verdict seq: the later verdict that supersedes it}: a
    supersedes_verdict naming an earlier verdict with the same verifies,
    correction and scope, recorded by the same lineage. Anything else
    supersedes nothing."""
    by_seq = {v["seq"]: v for v in verdicts}
    out: Dict[int, int] = {}
    for v in verdicts:
        old = by_seq.get(_seq(_payload(v).get("supersedes_verdict")))
        if old is None or old["seq"] >= v["seq"] or old["seq"] in out or lineage(old) is None \
                or lineage(old) != lineage(v):
            continue
        if all(_payload(old).get(f) == _payload(v).get(f) for f in ("verifies", "correction", "scope")):
            out[old["seq"]] = v["seq"]
    return out


def active(verdicts: List[dict]) -> List[dict]:
    """The verdicts no same-lineage verdict superseded."""
    gone = revised_by(verdicts)
    return [v for v in verdicts if v["seq"] not in gone]


MAX_REVISIONS = 3   # supersedes_verdict links per chain (the server answers verdict_revision_limit)


def chain(verdicts: List[dict], seq: int) -> List[int]:
    """The revision chain verdict `seq` belongs to, root first: its
    same-lineage supersedes_verdict links (revised_by). One lineage's chain
    never includes another's verdicts."""
    nxt = revised_by(verdicts)
    back = {new: old for old, new in nxt.items()}
    root = seq
    while root in back:
        root = back[root]
    out = [root]
    while out[-1] in nxt:
        out.append(nxt[out[-1]])
    return out


def substantive(e: dict, verdicts: List[dict], k: Optional[dict] = None) -> bool:
    """A citing event that is a correction in its own right, decided when
    the fold reaches it with current correction `k`: it carries `revision` or
    `artifacts` (or, slimmed by the plugin, has_revision / has_artifacts), or
    its refs cite "#v" for a rejected or unverifiable verdict v on k (of
    `verdicts`, this challenge's) active as of e: v < e's seq and not
    superseded by a verdict with a seq below e's. A ref to a rejection of an
    earlier, already-replaced correction answers nothing."""
    p = _payload(e)
    if isinstance(p.get("revision"), dict) or p.get("has_revision") is True:
        return True
    if (isinstance(p.get("artifacts"), list) and p["artifacts"]) or p.get("has_artifacts") is True:
        return True
    if k is None:
        return False
    answered = [v["seq"] for v in active([v for v in verdicts if v["seq"] < e["seq"]])
                if _payload(v).get("verdict") in ANSWERABLE_VERDICTS and _seq(_payload(v).get("correction")) == k["seq"]]
    return bool(answered) and cites(e, answered)


def correction_fold(citing: List[dict], verdicts: List[dict]) -> tuple:
    """(k, previous): the challenge's current correction k and the
    correction it replaced (None when k is the first citing event). A fold
    over the citing events (ascending) that only ever reads verdicts with
    lower seqs than the event it decides, so it agrees with the server's
    current-k rule. Whether an event is substantive is decided when the fold
    reaches it (against the k of that moment) and kept with it. An unjudged
    correction is displaced by a substantive event, or by any event when it
    is not substantive itself; a judged one only by a substantive event."""
    k: Optional[dict] = None
    k_sub, previous = False, None
    for e in citing:
        sub = substantive(e, verdicts, k)
        if k is None:
            take = True
        elif any(_seq(_payload(v).get("correction")) == k["seq"] and v["seq"] < e["seq"] for v in verdicts):
            take = sub
        else:
            take = sub or not k_sub
        if take:
            previous, k, k_sub = k, e, sub
    return k, previous


def correction_of(citing: List[dict], verdicts: List[dict]) -> Optional[dict]:
    """The challenge's current correction k (correction_fold)."""
    return correction_fold(citing, verdicts)[0]


def _judge(base: str, verdicts: List[dict]) -> tuple:
    """(status, verification_seq) from the verdicts on the current
    correction: rejected wins, then verified; only unverifiable ones ->
    unverifiable; none -> `base` (addressed / directive_addressed)."""
    for verdict, status in ((REJECTED, DIRECTIVE_REJECTED if base == DIRECTIVE_ADDRESSED else REJECTED),
                            (VERIFIED, VERIFIED), (UNVERIFIABLE, UNVERIFIABLE)):
        got = [v["seq"] for v in verdicts if _payload(v).get("verdict") == verdict]
        if got:
            return status, min(got)
    return base, None


def challenge_report(events: Any) -> Dict[int, dict]:
    """challenge_statuses with what the derivation read, for display:
    {c: {**status, "position": the deciding ack or None, "citing": [the
    citing events], "correction": the current correction or None,
    "previous": the correction it replaced or None, "verdicts": [every
    verdict on c, any correction]}}."""
    return _derive(events, detail=True)


def challenge_statuses(events: Any) -> Dict[int, dict]:
    """{challenge seq: {"status", "ruling_seq", "owner_seq",
    "correction_seq", "verification_seq"}} for every challenge.added in
    `events` (any order; non-dicts and events without an integer seq are
    ignored). For challenge seq c, first match wins:

    1. overruled: a web owner decision (overrules_supervision) answers c or
       c's ruling (owner_seq: the lowest such decision).
    2. The ruling = the lowest-seq ruling.issued with ruling_of c. Not upheld
       -> withdrawn. Upheld (seq r): the position is the LATEST author ack of
       r with disposition accepted / conflicts: none -> directive; conflicts
       -> conflicted (owner_seq: its conflicts_with); accepted ->
       directive_addressed when a later author semantic event cites "#r" or
       "#c", else directive_accepted.
    3. No ruling: the position is the latest author ack of c with
       disposition accepted / declined (`noted` is not one): none -> open;
       declined -> declined; accepted -> addressed when a later author
       semantic event cites "#c", else accepted.
    4. addressed / directive_addressed: correction_seq = correction_of(the
       citing events of 2 / 3); the active verdicts on it decide (_judge;
       verification_seq: the earliest deciding verdict).

    ruling_seq: c's ruling (any outcome) or None."""
    return _derive(events, detail=False)


def _derive(events: Any, detail: bool) -> Dict[int, dict]:
    evs = _ordered(events)
    acks = [e for e in evs if e.get("type") == ACKNOWLEDGED and by_author(e)]
    semantic = [e for e in evs if is_semantic(e) and by_author(e)]
    rulings: Dict[int, dict] = {}
    answered_by: Dict[int, int] = {}
    for e in evs:
        if e.get("type") == RULING:
            c = _seq(_payload(e).get("ruling_of"))
            if c is not None and c not in rulings:
                rulings[c] = e
        elif overrules_supervision(e):
            a = _seq(_payload(e).get("answers"))
            if a is not None and a not in answered_by:
                answered_by[a] = e["seq"]
    out: Dict[int, dict] = {}
    for ch in evs:
        if ch.get("type") != CHALLENGE:
            continue
        c = ch["seq"]
        ruling = rulings.get(c)
        r = ruling["seq"] if ruling is not None else None
        verdicts = verdicts_on(evs, c)
        pos: Optional[dict] = None
        citing: List[dict] = []
        k: Optional[dict] = None
        previous: Optional[dict] = None

        def status(s: str, owner: Optional[int] = None, verification: Optional[int] = None) -> dict:
            return {"status": s, "ruling_seq": r, "owner_seq": owner,
                    "correction_seq": k["seq"] if k is not None else None, "verification_seq": verification}

        overrule = [x for x in (answered_by.get(c), answered_by.get(r) if r is not None else None) if x is not None]
        if overrule:
            info = status(OVERRULED, min(overrule))
        elif ruling is not None and _payload(ruling).get("outcome") != UPHELD:
            info = status(WITHDRAWN)
        else:
            directive = ruling is not None
            pos = _latest_position(acks, r if directive else c, DIRECTIVE_POSITIONS if directive else CHALLENGE_POSITIONS)
            if pos is None:
                info = status(DIRECTIVE if directive else OPEN)
            elif _payload(pos).get("disposition") == CONFLICTED_DISPOSITION and directive:
                info = status(CONFLICTED, _seq(_payload(pos).get("conflicts_with")))
            elif _payload(pos).get("disposition") == DECLINED and not directive:
                info = status(DECLINED)
            else:
                citing = _citing(semantic, pos["seq"], (r, c) if directive else (c,))
                k, previous = correction_fold(citing, verdicts)
                if k is None:
                    info = status(DIRECTIVE_ACCEPTED if directive else ACCEPTED)
                else:
                    on_k = [v for v in active(verdicts) if _seq(_payload(v).get("correction")) == k["seq"]]
                    s, v = _judge(DIRECTIVE_ADDRESSED if directive else ADDRESSED, on_k)
                    info = status(s, verification=v)
        if detail:
            info = dict(info, position=pos, citing=citing, correction=k, previous=previous, verdicts=verdicts)
        out[c] = info
    return out


def resurface_target(challenge_seq: int, info: dict) -> Optional[int]:
    """The event re-shown for a status that still owes work: the challenge
    while it is `accepted` or `rejected`, its ruling while it is a directive
    (directive_rejected too); else None."""
    s = info.get("status")
    if s in (ACCEPTED, REJECTED):
        return challenge_seq
    if s in OPEN_DIRECTIVE:
        return _int(info.get("ruling_seq"))
    return None


# ---------------------------------------------------------------------------
# Next action (one rule for the timeline and the CLI): the mirror of
# @cardinalhq/shared nextAction / decidingStaleness / nextActions, pinned by
# the fixture's expected_next cases.
# ---------------------------------------------------------------------------

NEXT_ACTION_KINDS = ("take_position", "comply", "submit_correction", "verify_correction", "rule", "decide_conflict",
                     "reverify", "decide_unverifiable", "none")


def snapshot_unbound(r: Any) -> bool:
    """A dirty revision or subject without its tree: no exact snapshot."""
    return isinstance(r, dict) and r.get("dirty") is True and not r.get("tree")


def revision_mismatch(rev: Any, subject: Any) -> bool:
    """A verdict's subject and a correction's revision name different states
    of the same repo: another commit; exactly one side dirty (a clean commit
    and a dirty snapshot are different subjects); or both dirty with
    different tree ids."""
    if not isinstance(rev, dict) or not isinstance(subject, dict) or not subject.get("commit") \
            or subject.get("repo") != rev.get("repo"):
        return False
    if subject["commit"] != rev.get("commit"):
        return True
    dirty = (rev.get("dirty") is True, subject.get("dirty") is True)
    if dirty[0] != dirty[1]:
        return True
    return dirty[0] and subject.get("tree") != rev.get("tree")


def deciding_staleness(events: Any, status: dict) -> dict:
    """{"revision_mismatch", "snapshot_unbound"} of the deciding verdict
    (status's verification_seq) against the current correction's revision
    (correction_seq); both false without either."""
    by_seq = {e["seq"]: e for e in _ordered(events)}
    k, v = by_seq.get(status.get("correction_seq")), by_seq.get(status.get("verification_seq"))
    if k is None or v is None:
        return {"revision_mismatch": False, "snapshot_unbound": False}
    rev, subject = _payload(k).get("revision"), _payload(v).get("subject")
    return {"revision_mismatch": revision_mismatch(rev, subject),
            "snapshot_unbound": snapshot_unbound(rev) or snapshot_unbound(subject)}


def next_action(status: dict, staleness: Optional[dict], challenge_seq: int) -> dict:
    """{"actor": worker|supervisor|owner|nobody, "action_kind" (one of
    NEXT_ACTION_KINDS), "action" (display words), "seq" (what to act on: the
    challenge, its ruling, or the current correction; None for none)}. A
    verified challenge whose deciding verdict is mismatched or
    snapshot-unbound is never done: the supervisor re-verifies."""
    s, c = status.get("status"), challenge_seq
    r, k = _int(status.get("ruling_seq")), _int(status.get("correction_seq"))
    stale = isinstance(staleness, dict) and (staleness.get("revision_mismatch") or staleness.get("snapshot_unbound"))
    if s == OPEN:
        return _next("worker", "take_position", f"take a position on #{c}", c)
    if s == DIRECTIVE:
        return _next("worker", "comply", f"comply with directive #{r}", r)
    if s in (ACCEPTED, DIRECTIVE_ACCEPTED, REJECTED, DIRECTIVE_REJECTED):
        return _next("worker", "submit_correction", f"submit a correction for #{c}", c)
    if s in (ADDRESSED, DIRECTIVE_ADDRESSED):
        return _next("supervisor", "verify_correction", f"verify correction #{k}", k)
    if s == DECLINED:
        return _next("supervisor", "rule", f"rule on #{c}", c)
    if s == CONFLICTED:
        return _next("owner", "decide_conflict", f"decide the conflict on directive #{r}", r)
    if s == VERIFIED and stale:
        return _next("supervisor", "reverify", f"re-verify #{k} at its revision", k)
    if s == UNVERIFIABLE:
        return _next("owner", "decide_unverifiable", f"decide whether to accept #{k} unverified", k)
    return _next("nobody", "none", "none", None)


def _next(actor: str, kind: str, action: str, seq: Optional[int]) -> dict:
    return {"actor": actor, "action_kind": kind, "action": action, "seq": seq}


def next_actions(events: Any) -> Dict[int, dict]:
    """{challenge seq: next_action} for every challenge of `events`."""
    return {c: next_action(info, deciding_staleness(events, info), c) for c, info in challenge_statuses(events).items()}
