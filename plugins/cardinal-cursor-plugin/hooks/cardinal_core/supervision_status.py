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
  overruled            an owner.decided whose `answers` is the challenge or
                       its ruling; wins over every other status (owner_seq)

Each status is {"status", "ruling_seq", "owner_seq"} (None when absent).
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
STATUSES = (OPEN, ACCEPTED, ADDRESSED, DECLINED, WITHDRAWN, DIRECTIVE, DIRECTIVE_ACCEPTED, DIRECTIVE_ADDRESSED,
            CONFLICTED, OVERRULED)

# Still owed work: re-surfaced to the worker until addressed / overruled.
RESURFACE = (ACCEPTED, DIRECTIVE, DIRECTIVE_ACCEPTED)
# A directive the worker has not finished: blocks Stop once.
OPEN_DIRECTIVE = (DIRECTIVE, DIRECTIVE_ACCEPTED)

SEMANTIC_CLASS = "semantic"
SEMANTIC_TYPES = ("hypothesis.proposed", "hypothesis.resolved", "experiment.started", "experiment.completed",
                  "experiment.revised", "finding.proposed", "finding.revised", "finding.retracted",
                  "decision.proposed", "decision.revised", "question.opened", "question.resolved")


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


def _addressed(semantic: List[dict], after: int, seqs: tuple) -> bool:
    return any(e["seq"] > after and cites(e, seqs) for e in semantic)


def challenge_statuses(events: Any) -> Dict[int, dict]:
    """{challenge seq: {"status", "ruling_seq", "owner_seq"}} for every
    challenge.added in `events` (any order; non-dicts and events without an
    integer seq are ignored). For challenge seq c, first match wins:

    1. overruled: an owner.decided answers c or c's ruling (owner_seq: the
       lowest such decision).
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

    ruling_seq: c's ruling (any outcome) or None."""
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
        elif e.get("type") == OWNER_DECIDED:
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

        def status(s: str, owner: Optional[int] = None) -> dict:
            return {"status": s, "ruling_seq": r, "owner_seq": owner}

        overrule = [x for x in (answered_by.get(c), answered_by.get(r) if r is not None else None) if x is not None]
        if overrule:
            out[c] = status(OVERRULED, min(overrule))
        elif ruling is not None:
            if _payload(ruling).get("outcome") != UPHELD:
                out[c] = status(WITHDRAWN)
                continue
            pos = _latest_position(acks, r, DIRECTIVE_POSITIONS)
            if pos is None:
                out[c] = status(DIRECTIVE)
            elif _payload(pos).get("disposition") == CONFLICTED_DISPOSITION:
                out[c] = status(CONFLICTED, _seq(_payload(pos).get("conflicts_with")))
            else:
                out[c] = status(DIRECTIVE_ADDRESSED if _addressed(semantic, pos["seq"], (r, c)) else DIRECTIVE_ACCEPTED)
        else:
            pos = _latest_position(acks, c, CHALLENGE_POSITIONS)
            if pos is None:
                out[c] = status(OPEN)
            elif _payload(pos).get("disposition") == DECLINED:
                out[c] = status(DECLINED)
            else:
                out[c] = status(ADDRESSED if _addressed(semantic, pos["seq"], (c,)) else ACCEPTED)
    return out


def resurface_target(challenge_seq: int, info: dict) -> Optional[int]:
    """The event re-shown for a status that still owes work: the challenge
    while it is `accepted`, its ruling while it is a directive; else None."""
    s = info.get("status")
    if s == ACCEPTED:
        return challenge_seq
    if s in OPEN_DIRECTIVE:
        return _int(info.get("ruling_seq"))
    return None
