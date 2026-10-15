"""Supervise step backs: a per-milestone rate trigger (docs/specs/supervise-stepback-trigger.md).

A direction of the worker's work has an estimator's plan: milestones with
P50 / P80 agent-active hours and 2-6 deliverables each. Every review pass
(`investigation reviewed`) tags each worker event with the milestone(s) it
advances (or none) and marks deliverables done / reopened / redone with a
ref. The time leading up to a worker event (the gap since the previous
worker event, over the union of event and captured-call timestamps; a gap
over idle_min is idle) is charged to its milestones, split evenly. Then,
at each tagged worker event, for each of its milestones m below 100%
delivered:

- ratio_m = (spent_m / P50_m) / delivered_m; FIRE when ratio_m >= T;
- at 0% delivered, FIRE when spent_m >= Z x P50_m;
- no firing while spent_m < min_spent_h;
- after a firing at ratio r_f, m re-arms only at ratio >= rearm x r_f (at
  0% delivered after a 0% firing: spent >= rearm x the spent then; a drop
  to 0% counts as an infinite ratio) or when m is re-planned;
- one firing per milestone per review pass;
- backstop: the direction's worker-active hours reach backstop x the P80
  total: one direction-wide firing.

Plans, attribution, marks, firings, verdicts and outcomes are `supervision`
events in the supervisor's OWN investigation (maestro record-supervision,
wire contract v1, spec 4a). budget.json is their cache (0600, flock'd);
from a server without the class it is the only copy (said once). The worker
never sees any of it. Reads of the worker's investigation use the grant;
the spool is read only through evidence.list_entries. Stdlib only.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import evidence
from . import investigation_consumer as consumer
from . import investigation_events as ie
from . import investigation_state_sync as sync

VERSION = 2
HISTORY_MAX = 20
MAX_MILESTONES = 20
MAX_TEXT = 1000
MAX_TITLE = 200
MAX_PAGES = 200
MAX_EVENTS = 50                      # record-supervision accepts 1-50 events
MILESTONE_ID = r"[a-z0-9-]{1,24}"
MILESTONE_ID_RE = re.compile(MILESTONE_ID)
DIRECTION_ID_RE = re.compile(r"dir_[0-9a-f]{16}")
SEQ = r"[1-9][0-9]{0,5}"             # Cardinal's seqs (and work.attributed tag keys) have at most 6 digits
REF_RE = re.compile(rf"#{SEQ}|ev_[0-9a-f]{{12}}")
TAG_RE = re.compile(rf"(all|{SEQ}(?:-{SEQ})?):(none|{MILESTONE_ID}(?:\+{MILESTONE_ID})*)")
MARK_RE = re.compile(rf"({MILESTONE_ID})\.([1-9])@(#{SEQ}|ev_[0-9a-f]{{12}})")
MAX_REFS = 20
MAX_HOURS = 5000                     # per milestone: 20 of them stay under Cardinal's 100,000 h bound
MAX_RATIO = 1_000_000
OUTCOMES = ("done", "stopped", "replaced")
VERDICTS = {"on_track": "none", "worker_choice": "worker", "owner_constraint": "owner"}
ROUTES = ("none", "worker", "owner")
STEPBACK_OUTCOMES = ("accepted", "declined", "overruled", "changed_constraint", "no_response")
MARKS = {"done": "done", "reopen": "reopened", "redone": "redone"}
DEFAULT_THRESHOLDS = {"T": 2.0, "Z": 0.5, "rearm": 1.5, "backstop": 2.0, "idle_min": 30, "min_spent_h": 0.5}
THRESHOLD_BOUNDS = {"T": (1.5, 3.0), "Z": (0.3, 0.8), "rearm": (1.0, 4.0), "backstop": (1.0, 8.0),
                    "idle_min": (5, 240), "min_spent_h": (0.0, 8.0)}
VAGUE = {"works", "working", "done", "complete", "completed", "finished", "implemented", "it", "is", "the", "a",
         "an", "all", "feature", "code", "ready", "fixed", "ok", "good", "and", "everything", "properly"}
FRESH = ("Change nothing. Call no Cardinal or other MCP tools. Ignore any injected storyboard or investigation "
         "context. Use only this brief and the code.")
STEPBACK_PROMPT = (
    "An elegant solution is often hiding in plain sight. Take a step back. Knowing only this brief and the code, "
    "what is the simplest design that answers the question? Search the code for mechanisms that already do part "
    "of the job before inventing one. Then compare it with the worker's plan: for each component the plan has and "
    "yours does not, name the constraint or fact that requires it (by #seq), or say that nothing does. Then, for "
    "each constraint the owner set: what would the simplest design be without it, and what does keeping it cost "
    "(components, risk, time)? Do not drop an owner constraint; price it, so the owner can decide. If the "
    "worker's plan is already about as simple as it can be, say so; that is a good answer. Say what you could not "
    "verify.")
ESTIMATOR_PROMPT = """\
Estimate the agent-active hours this direction needs. The worker is an AI agent: anchor on the measured pace above, never on human pace. First a reference class: comparable past work and its actual hours, or "none comparable" with P80 at least 1.5x P50. Then 1-20 milestones, each with 2-6 deliverables whose done_when is an observable fact in the record (a commit, a test result, a finding), never "works". Known unknowns get their own ranges, outside the totals. Answer with only this JSON:

{"reference_class": "...", "pace": "<the measured pace you used, or none>",
 "milestones": [{"id": "<a-z0-9-, at most 24>", "title": "...", "p50_h": 0.5, "p80_h": 1.0,
   "basis": "<one line>", "deliverables": [{"done_when": "..."}]}],
 "unknowns": [{"what": "...", "p50_h": 0.5, "p80_h": 2.0}]}"""
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:?\d{2})?$")
SERVER_TOO_OLD = ("this Cardinal cannot record supervision events yet (it needs a newer Maestro): kept in "
                  "budget.json only")
LIMIT_REACHED = ("your investigation reached Cardinal's limit of supervision events: kept in budget.json only; "
                 "supervise from a new session for more")
CAPABILITY = "supervision_events"
RESULT_TYPES = ("finding.proposed", "experiment.completed", "hypothesis.resolved", "decision.proposed")


class BudgetError(ValueError):
    """A usage, plan or budget-file problem (exit 2)."""


# -- time and the worker's activity -------------------------------------------

def parse_time(value) -> Optional[float]:
    """An ISO-8601 UTC timestamp (Z or an offset; seconds and fraction
    optional) as epoch seconds; None when it is not one."""
    m = _TS_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        t = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    t += float("0." + frac) if frac else 0.0
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        t -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return t


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def active_hours(times: Iterable[float], idle_min: float) -> float:
    """Sum of the gaps between consecutive timestamps (sorted, merged) that
    are at most the idle gap; a longer gap is idle and not counted."""
    ts = sorted(set(times))
    gap = idle_min * 60
    return sum(b - a for a, b in zip(ts, ts[1:]) if b - a <= gap) / 3600


def worker_event(e: dict, worker_session: Optional[str]) -> bool:
    """An event the worker session wrote: its claims and acknowledgements,
    never owner input, owner decisions or a grantee's (a supervisor's) posts.
    Without a worker session: the investigation author's session events."""
    if not isinstance(e, dict) or ie.event_class(e) not in (ie.SEMANTIC, ie.CONTROL):
        return False
    p = e.get("producer") if isinstance(e.get("producer"), dict) else {}
    if ie.is_grantee(p) or not p.get("session_id"):
        return False
    if worker_session:
        return p.get("session_id") == worker_session
    return p.get("is_investigation_author") is True


def worker_times(events: Iterable[dict], worker_session: Optional[str], since_seq: int) -> list:
    """[(seq, epoch)] of the worker's events at or after since_seq."""
    out = []
    for e in events:
        seq = e.get("seq") if isinstance(e, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < since_seq or not worker_event(e, worker_session):
            continue
        t = parse_time(e.get("created_at"))
        if t is not None:
            out.append((seq, t))
    return sorted(out)


def spool_times(entries: Iterable[dict], since: float) -> list:
    """Epochs of captured calls after `since`."""
    out = []
    for e in entries:
        t = parse_time(e.get("called_at")) if isinstance(e, dict) else None
        if t is not None and t > since:
            out.append(t)
    return sorted(out)


def spool_entries(home: Path, worker_session: Optional[str]) -> Optional[list]:
    """The worker session's captured calls on this machine, through the
    evidence module's listings only (None: no worker session, or its spool
    is not on this machine)."""
    if not worker_session:
        return None
    root = evidence.default_root(home)
    entries = evidence.list_entries(root, worker_session)
    if entries or any(name == worker_session for name, _ in evidence.session_dirs(root)):
        return entries
    return None


def read_worker_events(conn: dict, inv: str, after: int, *, client: str, reader: Callable = None) -> tuple:
    """(events after `after`, head_seq), paging the grant's read."""
    reader = reader or ie.read_events
    events, head = [], None
    for _ in range(MAX_PAGES):
        page = reader(conn, inv, after=after, limit=ie.PAGE_LIMIT, client=client)
        events.extend(page["events"])
        head = page["head_seq"]
        if page["page_size"] < ie.PAGE_LIMIT or page["last_seq"] >= head or page["last_seq"] <= after:
            break
        after = page["last_seq"]
    return events, head


# -- the plan (the estimator's JSON) ---------------------------------------------

def _text(name: str, value, cap: int = MAX_TEXT, required: bool = True) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BudgetError(f"{name} is required (1 to {cap} characters)")
        return None
    if not isinstance(value, str) or len(value) > cap:
        raise BudgetError(f"{name} is text of 1 to {cap} characters")
    if any(ord(c) < 32 and c != "\t" for c in value):
        raise BudgetError(f"{name}: one line, no control characters")
    return value.strip()


def _hours(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.001 <= value <= MAX_HOURS:
        raise BudgetError(f"{what} is a number of hours from 0.001 to {MAX_HOURS}, not {json.dumps(value)}")
    return round(float(value), 3)


def _observable(done_when: str) -> bool:
    words = re.findall(r"[a-z0-9#_./-]+", done_when.lower())
    return len(words) >= 4 and any(w not in VAGUE for w in words)


def _plan_errors(raw) -> tuple:
    """(plan, errors): every problem with the estimator's JSON, each naming the fix."""
    errs = []

    def err(msg):
        errs.append(msg)

    if not isinstance(raw, dict):
        return None, ["the plan is a JSON object {reference_class, pace, milestones, unknowns}: re-ask the estimator "
                      "for only that JSON"]
    plan = {"milestones": [], "unknowns": []}
    for name, required in (("reference_class", True), ("pace", False)):
        try:
            plan[name] = _text(name, raw.get(name), required=required)
        except BudgetError as e:
            err(f"{e}" + (": comparable past work and its actual hours, or \"none comparable\""
                          if name == "reference_class" else ""))
    extra = sorted(set(raw) - {"reference_class", "pace", "milestones", "unknowns"})
    if extra:
        err(f"unknown key {extra[0]!r}: the plan has only reference_class, pace, milestones and unknowns")
    ms = raw.get("milestones")
    if not isinstance(ms, list) or not 1 <= len(ms) <= MAX_MILESTONES:
        err(f"milestones is a list of 1 to {MAX_MILESTONES}")
        ms = []
    seen = set()
    for i, m in enumerate(ms):
        where = f"milestones[{i}]"
        if not isinstance(m, dict):
            err(f"{where} is an object {{id, title, p50_h, p80_h, basis, deliverables}}")
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not MILESTONE_ID_RE.fullmatch(mid):
            err(f"{where}.id {json.dumps(mid)}: 1-24 of a-z, 0-9 and -")
            mid = f"m{i}"
        elif mid in seen:
            err(f"{where}.id {mid!r} repeats: milestone ids are distinct")
        seen.add(mid)
        where = f"milestone {mid}"
        out = {"id": mid}
        for name, cap in (("title", MAX_TITLE), ("basis", MAX_TEXT)):
            try:
                out[name] = _text(f"{where} {name}", m.get(name), cap)
            except BudgetError as e:
                err(str(e) + (" (one line: why these hours)" if name == "basis" else ""))
        try:
            out["p50_h"], out["p80_h"] = _hours(m.get("p50_h"), f"{where} p50_h"), _hours(m.get("p80_h"),
                                                                                         f"{where} p80_h")
            if out["p80_h"] < out["p50_h"]:
                err(f"{where}: p80_h {out['p80_h']} is below p50_h {out['p50_h']}: P80 >= P50")
        except BudgetError as e:
            err(str(e))
        extra = sorted(set(m) - {"id", "title", "p50_h", "p80_h", "basis", "deliverables"})
        if extra:
            err(f"{where}: unknown key {extra[0]!r} (a milestone has id, title, p50_h, p80_h, basis, deliverables)")
        ds = m.get("deliverables")
        if not isinstance(ds, list) or not 2 <= len(ds) <= 6:
            err(f"{where} has 2-6 deliverables, not {len(ds) if isinstance(ds, list) else 'none'}: split or merge them")
            ds = []
        out["deliverables"] = []
        for n, d in enumerate(ds, 1):
            did = f"{mid}.{n}"
            dw = d if isinstance(d, str) else d.get("done_when") if isinstance(d, dict) else None
            if isinstance(d, dict) and (set(d) - {"id", "done_when"} or d.get("id", did) != did):
                err(f"deliverable {did}: an object {{\"done_when\": ...}} (its id is {did}, by position)")
            try:
                dw = _text(f"deliverable {did} done_when", dw)
            except BudgetError as e:
                err(str(e))
                continue
            if not _observable(dw):
                err(f"deliverable {did} done_when {json.dumps(dw)} is not observable: name the commit, test result "
                    "or finding in the record that shows it")
            out["deliverables"].append({"id": did, "done_when": dw})
        plan["milestones"].append(out)
    us = raw.get("unknowns", [])
    if not isinstance(us, list) or len(us) > 20:
        err("unknowns is a list of at most 20 {what, p50_h, p80_h}")
        us = []
    for i, u in enumerate(us):
        try:
            if not isinstance(u, dict):
                raise BudgetError(f"unknowns[{i}] is {{what, p50_h, p80_h}}: its own range, out of the totals")
            lo, hi = _hours(u.get("p50_h"), f"unknowns[{i}].p50_h"), _hours(u.get("p80_h"), f"unknowns[{i}].p80_h")
            if hi < lo:
                raise BudgetError(f"unknowns[{i}]: p80_h is below p50_h")
            plan["unknowns"].append({"what": _text(f"unknowns[{i}].what", u.get("what")), "p50_h": lo, "p80_h": hi})
        except BudgetError as e:
            err(str(e))
    if not errs and plan.get("reference_class", "").lower().startswith("none comparable"):
        p50, p80 = totals(plan)
        if p80 < 1.5 * p50:
            err(f"reference_class is \"none comparable\": widen P80 to at least 1.5x P50 (now {p80 / p50:.2f}x)")
    return plan, errs


def load_plan(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BudgetError(f"--plan {path}: no such file; save the estimator's JSON there") from None
    except (OSError, ValueError) as e:
        raise BudgetError(f"--plan {path} is not JSON ({ie._one_line(e, 120)}): save only the estimator's JSON") \
            from None
    plan, errs = _plan_errors(raw)
    if errs:
        more = f" (+{len(errs) - 2} more)" if len(errs) > 2 else ""
        raise BudgetError("plan: " + errs[0] + ("\n" + errs[1] + more if len(errs) > 1 else ""))
    return plan


def totals(d: dict) -> tuple:
    return (round(sum(m["p50_h"] for m in d["milestones"]), 3), round(sum(m["p80_h"] for m in d["milestones"]), 3))


def thresholds(overrides: Iterable[str] = ()) -> dict:
    th = dict(DEFAULT_THRESHOLDS)
    for o in overrides or ():
        k, _, v = o.partition("=")
        if k not in th:
            raise BudgetError(f"--threshold {o!r}: one of {', '.join(th)}=<number>")
        try:
            x = float(v)
        except ValueError:
            raise BudgetError(f"--threshold {o!r}: {k}=<number>") from None
        lo, hi = THRESHOLD_BOUNDS[k]
        if not lo <= x <= hi:
            raise BudgetError(f"--threshold {k} is {lo} to {hi}")
        if k == "idle_min":
            if x != int(x):
                raise BudgetError("--threshold idle_min is a whole number of minutes")
            x = int(x)
        th[k] = x
    return th


def direction_id(inv: str, since_seq: int, question: str, plan: dict) -> str:
    """Deterministic, so a retried --set is the same direction (and the same
    idempotency key)."""
    canon = json.dumps([inv, since_seq, question, plan], sort_keys=True, separators=(",", ":"))
    return "dir_" + hashlib.sha256(canon.encode()).hexdigest()[:16]


def new_direction(*, inv: str, question: str, plan: dict, since_seq: int, set_at: float, worker_session: str,
                  worker_estimate: Optional[str] = None, th: Optional[dict] = None) -> dict:
    question = _text("--question", question)
    if not ie.valid_session(worker_session):
        raise BudgetError(f"--worker-session: not a session id: {worker_session!r}")
    pace = plan.get("pace")
    return {
        "direction_id": direction_id(inv, since_seq, question, plan),
        "question": question,
        "since_seq": since_seq,
        "set_at": iso(set_at),
        "worker_session": worker_session,
        "worker_estimate": _text("--worker-estimate", worker_estimate, required=False),
        "reference_class": plan["reference_class"],
        "pace": pace,
        "calibration": "calibrated" if pace and pace.strip().lower() not in ("none", "n/a") else "uncalibrated",
        "thresholds": th or dict(DEFAULT_THRESHOLDS),
        "milestones": plan["milestones"],
        "unknowns": plan["unknowns"],
        "marks": {},
        "charged": {m["id"]: 0.0 for m in plan["milestones"]},
        "none_h": 0.0,
        "active_h": 0.0,
        "last_t": None,
        "through_seq": None,
        "last_fire": {},
        "backstop_fired": False,
        "firings": [],
    }


# -- the engine ------------------------------------------------------------------

def milestone(d: dict, mid: str) -> Optional[dict]:
    return next((m for m in d["milestones"] if m["id"] == mid), None)


def mark_state(d: dict, did: str, seq: Optional[int] = None) -> Optional[dict]:
    """The deliverable's latest mark (at or before seq)."""
    hist = [x for x in d["marks"].get(did, []) if seq is None or x["seq"] <= seq]
    return hist[-1] if hist else None


def delivered(d: dict, mid: str, seq: Optional[int] = None) -> tuple:
    """(done, total) deliverables of mid at seq: done while the latest mark
    is done or redone."""
    m = milestone(d, mid)
    ds = m["deliverables"] if m else []
    n = sum(1 for x in ds if (mark_state(d, x["id"], seq) or {}).get("mark") in ("done", "redone"))
    return n, len(ds)


def _ratio(spent: float, est: float, p: float) -> float:
    return math.inf if p == 0 else (spent / est) / p


def rearm_threshold(last: list, r: float, s: float, p: float, e: float, th: dict) -> Optional[float]:
    """THE place for the re-arm rule (owner, 2026-10-15). After a firing
    `last` = [r_f (None: infinite), s_f, p_f], the threshold m now crosses,
    or None while it is not re-armed:
    - at 0% after a 0% firing: spent >= rearm x s_f;
    - after a 0% firing, once something is delivered: ratio >= rearm x T (a
      zero-progress firing counts as one at ratio T);
    - otherwise ratio >= rearm x r_f, and a reopen back to 0% after a >0%
      firing is an infinite ratio (it re-arms)."""
    rf, sf, pf = (math.inf if last[0] is None else last[0]), last[1], last[2]
    if pf == 0:
        if p == 0:
            return th["rearm"] * sf / e if s >= th["rearm"] * sf else None
        rf = th["T"]
    if r < th["rearm"] * rf:
        return None
    return th["rearm"] * rf if p > 0 else th["Z"]


def _evaluate(d: dict, mid: str, seq: int) -> Optional[dict]:
    """A firing of mid at seq, or None (records it in last_fire)."""
    m, th = milestone(d, mid), d["thresholds"]
    if not m:
        return None
    n, total = delivered(d, mid, seq)
    p = n / total
    if p >= 1:
        return None
    e, s = m["p50_h"], d["charged"].get(mid, 0.0)
    r = _ratio(s, e, p)
    if s < th["min_spent_h"] or not ((p > 0 and r >= th["T"]) or (p == 0 and s >= th["Z"] * e)):
        return None
    crossed = th["T"] if p > 0 else th["Z"]
    last = d["last_fire"].get(mid)
    if last:
        crossed = rearm_threshold(last, r, s, p, e, th)
        if crossed is None:
            return None
    d["last_fire"][mid] = [None if r == math.inf else round(r, 6), round(s, 6), round(p, 6)]
    return {"scope": "milestone", "milestone_id": mid, "at_seq": seq, "last_seq": seq,
            "snapshot": {"spent_h": round(s, 4), "estimate_h": e, "delivered": round(p, 4),
                         "ratio": None if r == math.inf else round(min(r, MAX_RATIO), 4),
                         "threshold": round(min(crossed, MAX_RATIO), 4)},
            "worker_active_h": round(d["active_h"], 4), "fired_seq": None, "verdict": None, "outcome": None}


def direction_delivered(d: dict, seq: Optional[int] = None) -> float:
    done = total = 0
    for m in d["milestones"]:
        n, t = delivered(d, m["id"], seq)
        done, total = done + n, total + t
    return done / total if total else 0.0


def apply_batch(d: dict, wt: list, spool: list, tags: dict, marks: list) -> tuple:
    """One review pass over the worker events `wt` [(seq, epoch)], with the
    captured-call epochs `spool`, `tags` {seq: [milestone, ...] | "none"} and
    validated `marks` [{deliverable_id, mark, seq, ref}]. Returns (the
    direction after it, {charged: {m: h}, none_h, firings, active}); `d`
    itself is not changed."""
    d = copy.deepcopy(d)
    th = d["thresholds"]
    for mk in marks:
        d["marks"].setdefault(mk["deliverable_id"], []).append({"mark": mk["mark"], "seq": mk["seq"], "ref": mk["ref"]})
    charged, none_h, firings, fired_now = {}, 0.0, [], {}
    prev = d["last_t"]
    spool = sorted(spool)
    _, p80 = totals(d)
    for seq, t in sorted(wt):
        gap = 0.0
        if prev is not None and t > prev:
            gap = active_hours([prev, t] + [x for x in spool if prev < x < t], th["idle_min"])
        prev = t if prev is None else max(prev, t)
        tg = tags.get(seq, "none")
        if gap:
            d["active_h"] += gap
            if isinstance(tg, list) and tg:
                for mid in tg:
                    share = gap / len(tg)
                    d["charged"][mid] = d["charged"].get(mid, 0.0) + share
                    charged[mid] = charged.get(mid, 0.0) + share
            else:
                d["none_h"] += gap
                none_h += gap
        for mid in (tg if isinstance(tg, list) else []):
            f = _evaluate(d, mid, seq)
            if f and mid in fired_now:
                # One firing per milestone per pass: a later crossing in the same pass moves it there (the
                # first at_seq stays), so a page's size never drops a crossing.
                fired_now[mid].update(snapshot=f["snapshot"], worker_active_h=f["worker_active_h"], last_seq=seq)
            elif f:
                fired_now[mid] = f
                firings.append(f)
        if not d["backstop_fired"] and p80 and d["active_h"] >= th["backstop"] * p80:
            d["backstop_fired"] = True
            p = direction_delivered(d, seq)
            firings.append({"scope": "backstop", "milestone_id": None, "at_seq": seq, "last_seq": seq,
                            "snapshot": {"spent_h": round(d["active_h"], 4), "estimate_h": p80, "delivered": round(p, 4),
                                         "ratio": None if p == 0 else round(min((d["active_h"] / p80) / p, MAX_RATIO), 4),
                                         "threshold": th["backstop"]},
                            "worker_active_h": round(d["active_h"], 4), "fired_seq": None, "verdict": None,
                            "outcome": None})
    d["last_t"] = prev
    if wt:
        d["through_seq"] = max(s for s, _ in wt)
        d["worker_seqs"] = sorted(set(d.get("worker_seqs", [])) | {s for s, _ in wt})
    end = max([s for s, _ in wt] + [mk["seq"] for mk in marks] + [0])
    active = [m["id"] for m in d["milestones"]
              if (charged.get(m["id"], 0) > 0 and delivered(d, m["id"], end)[0] < len(m["deliverables"]))
              or any(f["milestone_id"] == m["id"] for f in firings)]
    d["firings"].extend(firings)
    return d, {"charged": charged, "none_h": none_h, "firings": firings, "active": active}


# -- tags and marks (reviewed) ------------------------------------------------------

def _ranges(seqs: list, ordered: list) -> list:
    """Runs of consecutive worker events (in `ordered`) among seqs, as "a-b" / "a"."""
    pos = {s: i for i, s in enumerate(ordered)}
    out, run = [], []
    for s in sorted(seqs):
        if run and pos[s] != pos[run[-1]] + 1:
            out.append(run)
            run = []
        run.append(s)
    if run:
        out.append(run)
    return [f"{r[0]}-{r[-1]}" if len(r) > 1 else f"{r[0]}" for r in out]


def open_milestones(d: dict) -> list:
    return [m["id"] for m in d["milestones"] if delivered(d, m["id"])[0] < len(m["deliverables"])]


def parse_tags(specs: list, wseqs: list, d: dict) -> dict:
    ids = {m["id"] for m in d["milestones"]}
    opened = ", ".join(open_milestones(d)) or "none"
    tags, everything = {}, None
    for spec in specs or []:
        m = TAG_RE.fullmatch(spec) if isinstance(spec, str) else None
        if not m:
            raise BudgetError(f"--tag {spec!r}: <seq>[-<seq>]:<milestone>[+<milestone>] or :none, or all:<milestone>")
        rng, what = m.groups()
        val = "none" if what == "none" else list(dict.fromkeys(what.split("+")))
        bad = [x for x in val if x not in ids] if val != "none" else []
        if bad:
            raise BudgetError(f"--tag {spec}: no milestone {bad[0]!r}; open: {opened}")
        if rng == "all":
            if everything is not None:
                raise BudgetError("one --tag all:<milestone> per pass; tag the other events by seq")
            everything = val
            continue
        lo, _, hi = rng.partition("-")
        lo, hi = int(lo), int(hi or lo)
        hit = [s for s in wseqs if lo <= s <= hi]
        if hi < lo or not hit:
            raise BudgetError(f"--tag {spec}: no worker event of this page there (worker events: "
                              f"{', '.join(_ranges(wseqs, wseqs)) or 'none'})")
        for s in hit:
            if s in tags:
                raise BudgetError(f"#{s} is tagged twice (--tag {spec}): one --tag per worker event")
            tags[s] = val
    rest = [s for s in wseqs if s not in tags]
    if everything is not None:
        tags.update({s: everything for s in rest})
        rest = []
    if rest:
        rs = _ranges(rest, wseqs)
        listed = ", ".join(f"#{s}" for s in rest[:8]) + (", …" if len(rest) > 8 else "")
        adds = " ".join(f"--tag {r}:<milestone|none>" for r in rs[:3]) + (" …" if len(rs) > 3 else "")
        raise BudgetError(f"{listed} untagged: add `{adds}`; open: {opened}")
    return tags


def parse_marks(args, d: dict, wseqs: list, page_lo: int, through: int, worker_inv: str) -> list:
    """The pass's --done / --reopen / --redone, validated and in effect
    order: [{deliverable_id, milestone_id, mark, seq, ref, refs}]."""
    raw = []
    for flag, kind in MARKS.items():
        for spec in getattr(args, flag, None) or []:
            m = MARK_RE.fullmatch(spec) if isinstance(spec, str) else None
            if not m:
                raise BudgetError(f"--{flag} {spec!r}: <milestone>.<n>@#<seq> (a worker event) or @ev_<12 hex>")
            mid, n, ref = m.groups()
            ms = milestone(d, mid)
            if not ms or int(n) > len(ms["deliverables"]):
                have = f"{mid} has {mid}.1-{mid}.{len(ms['deliverables'])}" if ms else \
                    f"open: {', '.join(open_milestones(d)) or 'none'}"
                raise BudgetError(f"--{flag} {spec}: no deliverable {mid}.{n} ({have})")
            if ref.startswith("#"):
                seq = int(ref[1:])
                if seq > through:
                    raise BudgetError(f"--{flag} {spec}: #{seq} is past this page (through #{through})")
                if seq < d["since_seq"]:
                    raise BudgetError(f"--{flag} {spec}: #{seq} is before this direction (from #{d['since_seq']})")
                if seq not in wseqs and (seq >= page_lo or seq not in d.get("worker_seqs", [])):
                    raise BudgetError(f"--{flag} {spec}: #{seq} is not a worker event of this direction; cite the "
                                      "worker's own event or its ev_ call")
                refs = [{"investigation_id": worker_inv, "seq": seq}]
            else:
                seq, refs = through, [{"evidence_id": ref}]
            raw.append({"deliverable_id": f"{mid}.{n}", "milestone_id": mid, "mark": kind, "seq": seq, "ref": ref,
                        "refs": refs, "flag": f"--{flag} {spec}"})
    raw.sort(key=lambda x: x["seq"])
    latest = {}
    for mk in raw:
        did = mk["deliverable_id"]
        cur = latest.get(did, mark_state(d, did))
        state = cur["mark"] if cur else None
        if cur and mk["seq"] < cur["seq"]:
            raise BudgetError(f"{mk['flag']}: {did}'s latest mark is at #{cur['seq']}, after #{mk['seq']}")
        ok = {"done": state is None, "reopened": state in ("done", "redone"), "redone": state == "reopened"}
        if not ok[mk["mark"]]:
            now = {None: "open", "reopened": "reopened"}.get(state, "done")
            fix = {"done": "--redone it" if state == "reopened" else "drop this mark",
                   "reopened": "--done it first, or drop this mark",
                   "redone": "--done it" if state is None else "--reopen it first, or drop this mark"}[mk["mark"]]
            raise BudgetError(f"{mk['flag']}: {did} is {now}: {fix}")
        latest[did] = mk
    return raw


# -- supervision events (wire contract v1) ----------------------------------------------

def ev_direction_set(d: dict, inv: str) -> dict:
    p = {"direction_id": d["direction_id"], "question": d["question"],
         "subject": {"investigation_id": inv, "since_seq": d["since_seq"]}, "worker_session_id": d["worker_session"],
         "reference_class": d["reference_class"], "calibration": d["calibration"],
         "thresholds": dict(d["thresholds"])}
    if d.get("worker_estimate"):
        p["worker_estimate"] = d["worker_estimate"]
    return {"type": "direction.set", "payload": p}


def ev_milestone_planned(d: dict, m: dict, replaces: Optional[str] = None) -> dict:
    p = {"direction_id": d["direction_id"], "milestone_id": m["id"], "title": m["title"], "p50_h": m["p50_h"],
         "p80_h": m["p80_h"], "basis": m["basis"], "deliverables": [dict(x) for x in m["deliverables"]]}
    if replaces:
        p["replaces"] = replaces
    return {"type": "milestone.planned", "payload": p}


def ev_fired(d: dict, f: dict) -> dict:
    p = {"direction_id": d["direction_id"], "scope": f["scope"], "snapshot": f["snapshot"],
         "worker_active_h": f["worker_active_h"]}
    if f.get("milestone_id"):
        p["milestone_id"] = f["milestone_id"]
    return {"type": "stepback.fired", "payload": p}


def ev_closed(d: dict, outcome: str, missed: list) -> dict:
    return {"type": "direction.closed", "payload": {
        "direction_id": d["direction_id"], "outcome": outcome, "missed": missed,
        "per_milestone": [{"id": m["id"], "estimate_h": m["p50_h"], "spent_h": round(d["charged"].get(m["id"], 0), 4),
                           "delivered": round(delivered(d, m["id"])[0] / len(m["deliverables"]), 4)}
                          for m in d["milestones"]]}}


def _route_missing(err: "sync.ServerError") -> bool:
    """A Cardinal without record-supervision (or without its storyboards
    store): the plugin key's allowlist refuses it, the route is missing, or
    501 storyboards_unavailable."""
    code = err.body.get("error")
    return (err.status == 403 and code == "insufficient_scope") or err.status == 501 or \
        code == "storyboards_unavailable" or (err.status in (404, 405) and code != "investigation_not_found")


_REFUSED = {
    "not_author": "--session is not an author of its investigation: pass your own session id",
    "unknown_event_type": "this Cardinal does not know that supervision event type: update the plugin",
    "invalid_payload": "Cardinal refused the record as invalid",
    "ref_not_readable": "Cardinal cannot let this machine cite the worker's investigation: check --credential-file",
    "ref_out_of_range": "a #seq is past the worker investigation's head",
    "idempotency_conflict": "this record was already sent with other content",
}


def definite(err: Exception) -> bool:
    """A refusal the same body will always get (a 4xx other than an
    idempotency conflict): nothing is pending after it."""
    status = getattr(err, "status", None)
    return isinstance(status, int) and 400 <= status < 500 and getattr(err, "code", None) != "idempotency_conflict"


def record(own: dict, key: str, events: list) -> Optional[list]:
    """record-supervision into the supervisor's own investigation: the
    seqs of the stored events, or None when this Cardinal has no supervision
    class or the investigation is at its limit (own["why"] says which; the
    caller keeps its cache). subject_grant (the grant token that reads the
    worker's investigation) lets refs into it pass; it is never stored. A
    refusal raises FetchError with .code and .status."""
    if not events:
        return []
    if own.get("capable") is False:
        own["why"] = SERVER_TOO_OLD
        return None
    body = {"investigation_id": own["investigation_id"], "session_id": own["session_id"], "idempotency_key": key,
            "client": own["client"], "events": events}
    if own.get("subject_grant"):
        body["subject_grant"] = own["subject_grant"]
    try:
        out = sync._post(own["conn"], "record-supervision", body, client=own["client"])
    except sync.ServerError as e:
        code = e.body.get("error")
        if code == "supervision_unsupported" or _route_missing(e):
            own["why"] = SERVER_TOO_OLD
            return None
        if code == "event_limit_reached":
            own["why"] = LIMIT_REACHED
            return None
        said = _REFUSED.get(code) or sync.plain(e)
        where = e.body.get("path") if isinstance(e.body.get("path"), str) else ""
        msg = e.body.get("message") if isinstance(e.body.get("message"), str) else ""
        detail = ": ".join(x for x in (where, msg) if x)
        err = ist_fetch(f"not recorded: {said}{' (' + ie._one_line(detail, 200) + ')' if detail else ''}")
        err.code, err.status = code, e.status
        raise err from None
    got = out.get("events")
    if out.get("investigation_id") != own["investigation_id"] or not isinstance(got, list) or len(got) != len(events) \
            or any(not isinstance(g, dict) or not isinstance(g.get("seq"), int) or g.get("type") != s["type"]
                   for g, s in zip(got, events)):
        raise ist_fetch("not recorded: record-supervision answered without these events")
    return [g["seq"] for g in got]


def ist_fetch(msg: str) -> Exception:
    from . import investigation_state as ist
    return ist.FetchError(msg)


def key_for(prefix: str, material) -> str:
    return f"sup:{prefix}:" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:48]


def _planned_all(d: dict, inv: str) -> list:
    return [ev_direction_set(d, inv)] + [ev_milestone_planned(d, m) for m in d["milestones"]]


def ensure_direction(own: dict, d: dict, inv: str) -> bool:
    """The direction's own records (direction.set, milestone.planned) in
    own's investigation, sent now when they are not there: it was set while
    this Cardinal had no supervision class, or from another session's
    investigation. False: the cache stays the only copy."""
    if d.get("recorded_in") == own["investigation_id"]:
        return True
    pre = _planned_all(d, inv)
    if record(own, key_for("dir", [own["investigation_id"], pre]), pre) is None:
        return False
    d["recorded_in"], d["live_ids"] = own["investigation_id"], [m["id"] for m in d["milestones"]]
    return True


def ensure_fired(own: dict, d: dict, f: dict, inv: str) -> bool:
    """f's stepback.fired in own's investigation (a verdict needs its seq there)."""
    if f.get("fired_seq") is not None and f.get("recorded_in") == own["investigation_id"]:
        return True
    if not ensure_direction(own, d, inv):
        return False
    ev = ev_fired(d, f)
    seqs = record(own, key_for("fired", [own["investigation_id"], ev, f["at_seq"]]), [ev])
    if seqs is None:
        return False
    f["fired_seq"], f["recorded_in"] = seqs[0], own["investigation_id"]
    return True


def ensure_verdict(own: dict, d: dict, f: dict, inv: str) -> bool:
    """f's stepback.verdict in own's investigation (an outcome needs its seq there)."""
    v = f["verdict"]
    if v.get("verdict_seq") is not None and v.get("recorded_in") == own["investigation_id"]:
        return True
    if not ensure_fired(own, d, f, inv):
        return False
    p = {k: v[k] for k in ("verdict", "routed_to", "post_ref") if k in v}
    p = {"fired_seq": f["fired_seq"], **p}
    seqs = record(own, key_for("verdict", [own["investigation_id"], p]), [{"type": "stepback.verdict", "payload": p}])
    if seqs is None:
        return False
    f["verdict"] = {**p, "verdict_seq": seqs[0], "recorded_in": own["investigation_id"]}
    return True


# -- the cache (budget.json) -------------------------------------------------------------

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _shape_errors(state: dict) -> list:
    errs = []

    def need(cond, what):
        if not cond:
            errs.append(what)

    need(isinstance(state.get("investigation_id"), (str, type(None))), "investigation_id")
    need(isinstance(state.get("history", []), list), "history")
    sup = state.get("supervisor")
    need(sup is None or (isinstance(sup, dict) and ie.valid_investigation(sup.get("investigation_id"))
                         and ie.valid_session(sup.get("session_id"))), "supervisor")
    for i, h in enumerate(state.get("history") if isinstance(state.get("history"), list) else []):
        need(isinstance(h, dict) and isinstance(h.get("outcome"), str) and isinstance(h.get("per_milestone"), list)
             and isinstance(h.get("firings", []), list), f"history[{i}]")
    d = state.get("current")
    if d is None:
        return errs
    if not isinstance(d, dict):
        return errs + ["current"]
    for k in ("direction_id", "question", "set_at", "worker_session", "reference_class", "calibration"):
        need(isinstance(d.get(k), str), f"current.{k}")
    need(isinstance(d.get("since_seq"), int) and not isinstance(d.get("since_seq"), bool), "current.since_seq")
    ms = d.get("milestones")
    need(isinstance(ms, list) and ms and all(
        isinstance(m, dict) and isinstance(m.get("id"), str) and _num(m.get("p50_h")) and _num(m.get("p80_h"))
        and isinstance(m.get("deliverables"), list) and m["deliverables"]
        and all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in m["deliverables"]) for m in ms),
        "current.milestones")
    th = d.get("thresholds")
    need(isinstance(th, dict) and all(_num(th.get(k)) for k in DEFAULT_THRESHOLDS), "current.thresholds")
    mk = d.get("marks")
    need(isinstance(mk, dict) and all(isinstance(v, list) and all(
        isinstance(x, dict) and x.get("mark") in MARKS.values() and isinstance(x.get("seq"), int) for x in v)
        for v in mk.values()), "current.marks")
    need(isinstance(d.get("charged"), dict) and all(_num(v) for v in d["charged"].values()), "current.charged")
    for k in ("none_h", "active_h"):
        need(_num(d.get(k)), f"current.{k}")
    need(d.get("last_t") is None or _num(d.get("last_t")), "current.last_t")
    need(isinstance(d.get("last_fire"), dict), "current.last_fire")
    need(isinstance(d.get("backstop_fired"), bool), "current.backstop_fired")
    need(isinstance(d.get("firings"), list) and all(isinstance(f, dict) and f.get("scope") in
                                                     ("opening", "milestone", "backstop") for f in d["firings"]),
         "current.firings")
    return errs


def load(path: Path, investigation_id: Optional[str]) -> dict:
    try:
        state = consumer.private_read(path)
    except FileNotFoundError:
        return {"version": VERSION, "investigation_id": investigation_id, "supervisor": None, "current": None,
                "history": []}
    except OSError as e:
        raise BudgetError(f"{path}: cannot open the budget file ({e.strerror}); it must be a regular file "
                          "you own, not a link") from None
    if state.get("version") != VERSION:
        raise BudgetError(f"{path} is not a version {VERSION} budget file: use a new --budget-file")
    errs = _shape_errors(state)
    if errs:
        raise BudgetError(f"{path} is not a valid budget file (bad or missing: {', '.join(errs[:5])})")
    if investigation_id and state.get("investigation_id") not in (None, investigation_id):
        raise BudgetError(f"{path} is the budget of {state.get('investigation_id')}, not {investigation_id}")
    return state


def marker_path(path: Path) -> Path:
    """Beside the cache: which investigation holds its records, so a lost
    budget.json is rebuilt from them (the server's records are the truth)."""
    return path.with_name(path.stem + "-supervisor.json")


def save(path: Path, state: dict) -> None:
    consumer.private_write(path, state)
    sup = state.get("supervisor")
    if sup:
        want = {"version": 1, "investigation_id": state.get("investigation_id"), "supervisor": sup}
        m = marker_path(path)
        try:
            same = consumer.private_read(m) == want
        except (OSError, ValueError):
            same = False
        if not same:
            consumer.private_write(m, want)


@contextlib.contextmanager
def _locked(path: Path):
    lock = consumer.locked(path)
    try:
        lock.__enter__()
    except BlockingIOError:
        raise BudgetError("the budget file is in use by another command; retry") from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _recorded(state: dict, seqs: Optional[list], out: Callable, own: Optional[dict] = None) -> None:
    """Say once per reason that records stay in the cache only."""
    why = (own or {}).get("why") or SERVER_TOO_OLD
    flag = "limit_noted" if why == LIMIT_REACHED else "unsupported_noted"
    if seqs is None and not state.get(flag):
        state[flag] = True
        out(why)


# -- rendering -------------------------------------------------------------------------

def _n(x: float) -> str:
    s = f"{x:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def milestone_line(d: dict, mid: str, fired: bool) -> str:
    m = milestone(d, mid)
    n, total = delivered(d, mid)
    s = d["charged"].get(mid, 0.0)
    parts = [f"{mid} {s:.2f}/{_n(m['p50_h'])} h", f"{n}/{total} done"]
    if n and n < total:
        parts.append(f"{_ratio(s, m['p50_h'], n / total):.1f}×")
    if fired:
        parts.append("FIRE step back")
    return " · ".join(parts)


def backstop_line(d: dict) -> str:
    _, p80 = totals(d)
    done = sum(delivered(d, m["id"])[0] for m in d["milestones"])
    total = sum(len(m["deliverables"]) for m in d["milestones"])
    return f"all {d['active_h']:.2f}/{_n(p80)} h P80 · {done}/{total} done · FIRE step back"


def pass_lines(d: dict, res: dict, through: int) -> list:
    fired = {f["milestone_id"] for f in res["firings"] if f["scope"] == "milestone"}
    lines = [milestone_line(d, mid, mid in fired) for mid in res["active"]]
    if any(f["scope"] == "backstop" for f in res["firings"]):
        lines.append(backstop_line(d))
    tail = f"reviewed through #{through}"
    if d["calibration"] == "uncalibrated":
        tail += " · plan uncalibrated"
    return lines + [tail]


def show_lines(state: dict) -> list:
    out = []
    d = state.get("current")
    if d:
        p50, p80 = totals(d)
        out.append(f"{d['direction_id']} {state.get('investigation_id')} from #{d['since_seq']}: "
                   f"{ie.json_text(d['question'])}; P50 {_n(p50)} h / P80 {_n(p80)} h; {d['calibration']}; "
                   f"{d['active_h']:.2f} h active ({d['none_h']:.2f} h none)")
        out.append(f"  reference class: {ie.json_text(d['reference_class'])}")
        if d.get("worker_estimate"):
            out.append(f"  worker's own estimate: {ie.json_text(d['worker_estimate'])}")
        for m in d["milestones"]:
            out.append("  " + milestone_line(d, m["id"], False) + f" — {ie.json_text(m['title'])}")
        for u in d.get("unknowns", []):
            out.append(f"  unknown {u['p50_h']}-{u['p80_h']} h: {ie.json_text(u['what'])}")
        for f in d["firings"]:
            label = (f.get("verdict") or {}).get("verdict", "no verdict")
            if f.get("outcome"):
                label += f", {f['outcome']['outcome']}"
            out.append(f"  fired {f.get('milestone_id') or f['scope']} at #{f['at_seq']} ({label})")
    else:
        out.append(f"no open direction for {state.get('investigation_id') or 'this budget file'}")
    for h in state.get("history") or []:
        out.append(f"closed {h['outcome']} {h.get('direction_id')}: {ie.json_text(h.get('question', ''))}; "
                   f"P50 {_n(h.get('p50_total_h', 0))} h, spent {h.get('spent_h', 0):.2f} h")
    return out


# -- passes (fresh-context briefs) ---------------------------------------------------------

def _local_states(home: Path) -> list:
    out = []
    for f in sorted((home / ".cardinal" / "supervise").glob("*/budget.json")):
        try:
            st = consumer.private_read(f)
        except (OSError, ValueError):
            continue
        if st.get("version") == VERSION and not _shape_errors(st):
            out.append(st)
    return out


def closed_pace(home: Path) -> Optional[str]:
    """Hours per met deliverable across the closed directions on this machine."""
    spent = met = est = 0.0
    n = 0
    for st in _local_states(home):
        for h in st.get("history") or []:
            n += 1
            for pm in h.get("per_milestone", []):
                k = pm.get("deliverables", 0) or 0
                spent += pm.get("spent_h", 0) or 0
                met += (pm.get("delivered", 0) or 0) * k
                est += (pm.get("estimate_h", 0) or 0) * (pm.get("delivered", 0) or 0)
    if not n or not met:
        return None
    return (f"{n} closed direction(s) on this machine: {met:.0f} deliverables met in {spent:.2f} agent-active h "
            f"({spent / met:.2f} h each; actual ÷ P50 {spent / est:.2f})" if est else
            f"{n} closed direction(s): {spent / met:.2f} h per deliverable")


def record_pace(events: list, worker_session: str, idle_min: float) -> Optional[str]:
    wt = worker_times(events, worker_session, 1)
    if len(wt) < 2:
        return None
    h = active_hours([t for _, t in wt], idle_min)
    results = sum(1 for e in events if isinstance(e, dict) and e.get("type") in RESULT_TYPES
                  and worker_event(e, worker_session))
    if not h:
        return None
    each = f"; {h / results:.2f} h per result" if results else ""
    return (f"the worker's record so far: {h:.2f} agent-active h over {len(wt)} events, {results} results "
            f"(findings, completed experiments, resolved hypotheses, decisions){each}")


def write_estimator(brief: Path, paces: list) -> Path:
    out = brief.with_name(brief.stem + "-estimator.md")
    measured = "\n".join(f"- {p}" for p in paces) if paces else \
        "- none: no record and no closed direction to measure. Answer \"pace\": \"none\"."
    out.write_text(f"{FRESH}\n\n{brief.read_text(encoding='utf-8').strip()}\n\n## Measured pace\n\n{measured}\n\n"
                   f"## Your task\n\n{ESTIMATOR_PROMPT}\n", encoding="utf-8")
    return out


def status_block(d: dict, mid: Optional[str]) -> str:
    rows = []
    for m in d["milestones"] if mid in (None, "all") else [milestone(d, mid)]:
        n, total = delivered(d, m["id"])
        rows.append(f"{m['id']} ({ie._one_line(m['title'], 80)}): {d['charged'].get(m['id'], 0):.2f} of "
                    f"{_n(m['p50_h'])} h P50 spent, {n} of {total} deliverables done")
        if mid != "all":
            for x in m["deliverables"]:
                st = mark_state(d, x["id"])
                mark = f"{st['mark']} {st['ref']}" if st else "open"
                rows.append(f"- {x['id']} {mark}: {ie._one_line(x['done_when'], 100)}")
    return "\n".join(rows)


def write_stepback(brief: Path, d: Optional[dict], mid: Optional[str]) -> Path:
    body = brief.read_text(encoding="utf-8").strip()
    if mid is None:
        out = brief.with_name(brief.stem + "-stepback.md")
        text = f"{FRESH}\n\n{body}\n\n## Your task\n\n{STEPBACK_PROMPT}\n\nAnswer in at most 800 words.\n"
    else:
        what = "this direction" if mid == "all" else mid
        out = brief.with_name(brief.stem + f"-stepback-{mid}.md")
        text = (f"{FRESH}\n\nFirst: is {what} an ordinary overrun, most of its deliverables done and the rest close? "
                f"If so, answer \"on track\" and why, and stop.\n\n{body}\n\n## Status (the supervisor's record)\n\n"
                f"{status_block(d, mid)}\n\n## Your task\n\n{STEPBACK_PROMPT}\n\nAnswer in at most 800 words.\n")
    out.write_text(text, encoding="utf-8")
    return out


# -- calibrate ------------------------------------------------------------------------

def label(f: dict) -> Optional[str]:
    """useful / noise / other for a labeled firing; None while unlabeled.
    A final `declined` means the supervisor agreed with the decline (one it
    disagreed with went to the owner, whose answer is the outcome)."""
    v = (f.get("verdict") or {}).get("verdict")
    o = (f.get("outcome") or {}).get("outcome")
    if v == "on_track" or o == "declined":
        return "noise"
    if o in ("accepted", "changed_constraint"):
        return "useful" if v else None
    return "other" if o else None


def calibrate_lines(home: Path, state: dict) -> list:
    states = _local_states(home)
    if not any(s is state or s.get("investigation_id") == state.get("investigation_id") for s in states):
        states.append(state)
    firings, missed, th = [], [], None
    for st in states:
        for h in (st.get("history") or []):
            firings += [f for f in h.get("firings", []) if f.get("scope") != "opening"]
            missed.append((h.get("thresholds") or DEFAULT_THRESHOLDS, len(h.get("missed") or [])))
        if st.get("current"):
            firings += [f for f in st["current"]["firings"] if f.get("scope") != "opening"]
            th = st["current"]["thresholds"]
    th = th or DEFAULT_THRESHOLDS
    # Only misses under the thresholds in force count: a miss an earlier T or Z already answered must not
    # keep pulling T down.
    misses = sum(n for h_th, n in missed if (h_th.get("T"), h_th.get("Z")) == (th["T"], th["Z"]))
    labeled = [f for f in firings if label(f)]
    if len(labeled) < 5:
        return [f"{len(labeled)} labeled firing(s) of {len(firings)}; a suggestion needs 5 (T {th['T']}, Z {th['Z']})"]
    last = labeled[-5:]
    noise = sum(1 for f in last if label(f) == "noise")
    useful = sum(1 for f in last if label(f) == "useful")
    t = th["T"]
    if noise >= 4:
        t = min(3.0, t + 0.25)
    elif misses and noise <= 1:
        t = max(1.5, t - 0.25)
    zs = [f for f in labeled if f["snapshot"].get("delivered") == 0][-5:]
    z = th["Z"]
    if len(zs) == 5:
        zn = sum(1 for f in zs if label(f) == "noise")
        z = min(0.8, z + 0.1) if zn >= 4 else max(0.3, z - 0.1) if misses and zn <= 1 else z
    head = f"last 5 labeled firings: {useful} useful, {noise} noise; {misses} missed milestone(s) in closed directions"
    if (t, round(z, 2)) == (th["T"], th["Z"]):
        return [head, f"keep T {th['T']}, Z {th['Z']}"]
    return [head, f"suggest T {th['T']} -> {t}, Z {th['Z']} -> {round(z, 2)}: ask the owner; if approved, the next "
                  f"--set adds --threshold T={t} --threshold Z={round(z, 2)}"]


# -- the CLI: investigation budget ----------------------------------------------------------

def _own(state: dict, args, home: Path, own_conn: Callable, grant_token: Optional[Callable] = None) -> dict:
    """The supervisor's own investigation: by --session (as `supervising`
    finds it), else the cached one; with this machine's own key, and the
    grant's token as subject_grant (never stored)."""
    sid = getattr(args, "session", None)
    sup = state.get("supervisor")
    if sid and (not sup or sup["session_id"] != sid):
        if not ie.valid_session(sid):
            raise BudgetError(f"--session: not a session id: {sid!r}")
        b = ie.read_binding(home, sid)
        if not b:
            raise BudgetError(f"session {sid} has no investigation yet: run `cardinal-storyboard investigation link "
                              f"--session {sid}`; supervision records go to your own investigation")
        if b.get("is_author") is not True:
            raise BudgetError(f"session {sid} joined someone else's investigation; --session is your own session")
        sup = {"investigation_id": b["investigation_id"], "session_id": sid}
    if not sup:
        raise BudgetError("needs --session <your_sid>: supervision records go to your own investigation")
    if sup["investigation_id"] == state.get("investigation_id"):
        raise BudgetError("--session is the worker's; pass your own session id")
    conn, client = own_conn()
    if not isinstance(conn, dict) or not conn.get("key") or conn.get("token"):
        raise BudgetError("this machine is not connected to Cardinal with its own key (run cardinal-connect); "
                          "supervision records are written with it")
    state["supervisor"] = sup
    own = {**sup, "conn": conn, "client": client, "subject_grant": grant_token() if grant_token else None}
    b = ie.read_binding(home, sup["session_id"]) or {}
    if isinstance(b.get("capabilities"), dict) and CAPABILITY in b["capabilities"] and not ie.capability(b, CAPABILITY):
        own["capable"] = False          # this Cardinal said it has no supervision events
    return own


def rebuild(path: Path, grant: dict, own_conn: Callable, home: Path) -> dict:
    """budget.json from the supervisor's own records (class supervision),
    after it was lost: the open direction of this worker investigation, its
    milestones, and every review pass replayed from its tags and marks over
    the worker's event times (read with the grant). Raises on any read."""
    mk = consumer.private_read(marker_path(path))
    inv, sup = mk["investigation_id"], mk["supervisor"]
    if inv != grant.get("investigation_id"):
        raise BudgetError(f"{marker_path(path)} is for {inv}, not {grant.get('investigation_id')}")
    conn, client = own_conn()
    evs, after = [], 0
    for _ in range(MAX_PAGES):
        page = ie.read_events(conn, sup["investigation_id"], after=after, limit=ie.PAGE_LIMIT, client=client,
                              class_="supervision")
        evs += [e for e in page["events"] if isinstance(e.get("payload"), dict)]
        if page["page_size"] < ie.PAGE_LIMIT or page["last_seq"] >= page["head_seq"] or page["last_seq"] <= after:
            break
        after = page["last_seq"]
    state = {"version": VERSION, "investigation_id": inv, "supervisor": sup, "current": None, "history": []}
    closed = {e["payload"].get("direction_id"): e for e in evs if e.get("type") == "direction.closed"}
    sets = [e for e in evs if e.get("type") == "direction.set"
            and (e["payload"].get("subject") or {}).get("investigation_id") == inv]
    for e in sets:
        c = closed.get(e["payload"]["direction_id"])
        if c:
            pm = c["payload"].get("per_milestone") or []
            state["history"].append({
                "direction_id": c["payload"]["direction_id"], "question": e["payload"].get("question", ""),
                "outcome": c["payload"]["outcome"], "closed_at": c.get("created_at"),
                "since_seq": e["payload"]["subject"]["since_seq"], "thresholds": e["payload"].get("thresholds"),
                "p50_total_h": round(sum(x.get("estimate_h", 0) for x in pm), 3), "p80_total_h": 0,
                "spent_h": round(sum(x.get("spent_h", 0) for x in pm), 4), "none_h": 0,
                "missed": c["payload"].get("missed", []), "firings": [],
                "per_milestone": [{**x, "deliverables": 0} for x in pm]})
    state["history"] = state["history"][-HISTORY_MAX:]
    open_ = [e for e in sets if e["payload"]["direction_id"] not in closed]
    if not open_:
        return state
    ds = open_[-1]
    p, did = ds["payload"], ds["payload"]["direction_id"]
    mine = [e for e in evs if e["seq"] > ds["seq"] and (e["payload"].get("direction_id") == did
                                                        or e.get("type") in ("stepback.verdict", "stepback.outcome"))]
    ms = {}
    for e in [x for x in mine if x["type"] == "milestone.planned"]:
        q = e["payload"]
        if q.get("replaces") and q["replaces"] != q["milestone_id"]:
            ms.pop(q["replaces"], None)
        ms[q["milestone_id"]] = {"id": q["milestone_id"], "title": q["title"], "p50_h": q["p50_h"], "p80_h": q["p80_h"],
                                 "basis": q["basis"], "deliverables": [dict(x) for x in q["deliverables"]]}
    since = p["subject"]["since_seq"]
    events, _ = read_worker_events(grant, inv, max(0, since - 1), client=client)
    times = dict(worker_times(events, p["worker_session_id"], since))
    t0 = times.get(since) or parse_time(ds.get("created_at")) or time.time()
    we = p.get("worker_estimate")
    d = new_direction(inv=inv, question=p["question"], since_seq=since, set_at=t0, worker_session=p["worker_session_id"],
                      worker_estimate=we if isinstance(we, str) else None if we is None else f"{we} h",
                      plan={"reference_class": p["reference_class"], "pace": None, "milestones": list(ms.values()),
                            "unknowns": []}, th=dict(p["thresholds"]))
    d.update(direction_id=did, calibration=p["calibration"], recorded_in=sup["investigation_id"], live_ids=list(ms))
    entries = spool_entries(home, d["worker_session"]) or []
    batches, last_batch, through = [], None, None
    for e in mine:
        if e["type"] == "work.attributed":
            batches.append((e["payload"], []))
        elif e["type"] == "deliverable.marked" and batches:
            batches[-1][1].append(e["payload"])
    for wa, mks in batches:
        tags = {int(k): v for k, v in wa["tags"].items()}
        wt = sorted((sq, times[sq]) for sq in tags if sq in times)
        marks = []
        for m in mks:
            r = m["refs"][0]
            sq = r["seq"] if "seq" in r else wa["through_seq"]
            marks.append({"deliverable_id": m["deliverable_id"], "milestone_id": m["milestone_id"], "mark": m["mark"],
                          "seq": sq, "ref": f"#{r['seq']}" if "seq" in r else r["evidence_id"], "refs": m["refs"]})
        spool = [t for t in spool_times(entries, d["last_t"]) if wt and t < wt[-1][1]] if d["last_t"] else []
        d, _ = apply_batch(d, wt, spool, tags, marks)
        last_batch, through = wa["batch"], wa["through_seq"]
    stored = [e for e in mine if e["type"] == "stepback.fired"]
    for f in d["firings"]:
        hit = next((e for e in stored if e["payload"]["scope"] == f["scope"]
                    and e["payload"].get("milestone_id") == f.get("milestone_id")), None)
        if hit:
            stored.remove(hit)
            f.update(fired_seq=hit["seq"], recorded_in=sup["investigation_id"], snapshot=hit["payload"]["snapshot"])
    opening = next((e for e in stored if e["payload"]["scope"] == "opening"), None)
    if opening:
        d["firings"].insert(0, {"scope": "opening", "milestone_id": None, "at_seq": since, "last_seq": since,
                                "snapshot": opening["payload"]["snapshot"], "worker_active_h": 0,
                                "fired_seq": opening["seq"], "recorded_in": sup["investigation_id"],
                                "verdict": None, "outcome": None})
    by_fired = {f["fired_seq"]: f for f in d["firings"] if f.get("fired_seq") is not None}
    by_verdict = {}
    for e in mine:
        q = e["payload"]
        if e["type"] == "stepback.verdict" and q.get("fired_seq") in by_fired:
            f = by_fired[q["fired_seq"]]
            f["verdict"] = {**q, "verdict_seq": e["seq"], "recorded_in": sup["investigation_id"]}
            by_verdict[e["seq"]] = f
        elif e["type"] == "stepback.outcome" and q.get("verdict_seq") in by_verdict:
            by_verdict[q["verdict_seq"]]["outcome"] = q
    state["current"] = d
    if last_batch:
        state["applied_batch"], state["applied_lines"] = last_batch, [f"reviewed through #{through}"]
    return state


def _restored(path: Path, grant: Optional[dict], own_conn: Callable, home: Path) -> Optional[dict]:
    """The cache rebuilt from the server when budget.json is gone but its
    marker says records exist (None: nothing to rebuild); a read that fails
    refuses, never a silent empty cache."""
    if path.exists() or not marker_path(path).exists():
        return None
    try:
        if not grant:
            raise BudgetError("no --credential-file to read the worker's investigation with")
        return rebuild(path, grant, own_conn, home)
    except Exception as e:          # any failure to read them back refuses; never a silently empty cache
        why = sync.plain(e) if isinstance(e, sync.ServerError) else ie._one_line(e, 160)
        raise BudgetError(f"{path.name} is missing and your supervision records could not be read back ({why}): "
                          f"retry, restore it, or delete {marker_path(path)} to go on without the direction") from None


def run(args, *, target: Callable, own_conn: Callable, home: Path, client: str, out: Callable = print,
        now: Callable = time.time, reader: Callable = None, grant_token: Optional[Callable] = None) -> int:
    """`investigation budget`. target() -> (conn, client, inv) or None (the
    grant, for reading the worker); own_conn() -> (conn, client) (this
    machine's key, for writing to the supervisor's own investigation)."""
    path = Path(args.budget_file)
    reader = reader or ie.read_events
    as_json = bool(getattr(args, "json", False))
    hint = getattr(args, "investigation_id", None) or getattr(args, "investigation", None)
    _only(args)
    if args.show:
        state = load(path, hint)
        out(json.dumps(state, indent=2, ensure_ascii=False) if as_json else "\n".join(show_lines(state)))
        return 0
    if args.calibrate:
        state = load(path, hint)
        out("\n".join(calibrate_lines(home, state)))
        return 0
    if args.pass_:
        return _pass(args, path, hint, target, home, reader, out)
    if args.set and not args.amend:
        got = target()
        if got is None:
            return 2
        conn, rclient, inv = got
        with _locked(path):
            state = load(path, inv)
            state["investigation_id"] = inv
            return _set(args, state, path, conn, inv, rclient, home, lambda: _own(state, args, home, own_conn,
                                                                                lambda: conn.get("token")),
                        reader, out, now(), as_json)
    with _locked(path):
        lines = []
        if not path.exists() and marker_path(path).exists():
            got = target()
            state = _restored(path, got[0] if got and got[0].get("token") else None, own_conn, home)
            save(path, state)
            lines.append(f"rebuilt {path.name} from your supervision records")
        state = load(path, hint)
        d = state.get("current")
        if not d:
            raise BudgetError("no open direction: `budget --set --plan <file>` first")
        own = _own(state, args, home, own_conn, grant_token)
        mode = _set_amend if args.set else _close if args.close else _verdict if args.verdict else _outcome
        return mode(args, state, path, own, lambda x: out("\n".join(lines + [x])), now())


MODES = ("set", "show", "close", "pass_", "calibrate", "verdict", "stepback_outcome")
SET_FIELDS = ("plan", "question", "worker_session", "worker_estimate", "since_seq", "threshold", "amend")
ALLOWED = {"set": set(SET_FIELDS) | {"session"}, "close": {"outcome", "missed", "session"},
           "pass_": {"brief", "milestone", "worker_session"}, "verdict": {"routed_to", "post_ref", "session"},
           "stepback_outcome": {"ref", "session"}, "show": set(), "calibrate": set()}


def _only(args) -> None:
    mode = next(m for m in MODES if getattr(args, m, None))
    for name in SET_FIELDS + ("outcome", "missed", "brief", "milestone", "routed_to", "post_ref", "ref"):
        if getattr(args, name, None) not in (None, [], False) and name not in ALLOWED[mode]:
            flag = {"pass_": "pass", "stepback_outcome": "outcome", "verdict": "verdict",
                    "calibrate": "calibrate"}.get(mode, "--" + mode)
            raise BudgetError(f"--{name.replace('_', '-')} does not go with {flag}")
    if mode == "set":
        if not args.plan:
            raise BudgetError("--set needs --plan <file>: the estimator's JSON (`budget pass estimator` asks for it)")
        if args.amend:
            for name in ("question", "since_seq", "worker_session", "worker_estimate", "threshold"):
                if getattr(args, name, None) not in (None, []):
                    raise BudgetError(f"--{name.replace('_', '-')} does not go with --amend (it re-plans milestones "
                                      "only; a new direction is --set without --amend)")
        elif not args.question or not args.worker_session:
            raise BudgetError("--set needs --question \"<the owner's question>\" and --worker-session <worker_sid>")
    if mode == "close" and not args.outcome:
        raise BudgetError(f"--close needs --outcome {'|'.join(OUTCOMES)}")
    if mode == "pass_" and not args.brief:
        raise BudgetError("pass needs --brief <the facts brief you wrote>")
    if mode == "stepback_outcome" and len(args.ref or []) > MAX_REFS:
        raise BudgetError(f"at most {MAX_REFS} --ref")


def _pass(args, path, hint, target, home, reader, out) -> int:
    brief = Path(args.brief)
    if not brief.is_file():
        raise BudgetError(f"--brief {brief}: no such file; write the facts brief first")
    state = load(path, hint)
    d = state.get("current")
    if args.pass_ == "estimator":
        if args.milestone:
            raise BudgetError("--milestone goes with pass stepback")
        paces = []
        ws = args.worker_session or (d or {}).get("worker_session")
        got = target() if ws else None
        if got:
            conn, rclient, inv = got
            events, _ = read_worker_events(conn, inv, 0, client=rclient, reader=reader)
            p = record_pace(events, ws, DEFAULT_THRESHOLDS["idle_min"])
            if p:
                paces.append(p)
        p = closed_pace(home)
        if p:
            paces.append(p)
        f = write_estimator(brief, paces)
        out(f"wrote {f}: run the estimator pass on it; save its JSON for --set --plan"
            + ("" if paces else " (no measured pace: the plan will be uncalibrated)"))
        return 0
    if args.worker_session:
        raise BudgetError("--worker-session goes with pass estimator")
    mid = args.milestone
    if mid is not None:
        if not d:
            raise BudgetError("no open direction: the opening step back is pass stepback without --milestone")
        if mid != "all" and not milestone(d, mid):
            raise BudgetError(f"--milestone {mid!r}: one of {', '.join(m['id'] for m in d['milestones'])}, or all")
    f = write_stepback(brief, d, mid)
    out(f"wrote {f}: run the step-back pass on it")
    return 0


def _committed(conn: dict, cursor: Path) -> Optional[int]:
    """The review cursor's committed seq beside the budget file, when it is
    this grant's (None: no cursor here, or another identity's)."""
    try:
        return consumer.committed(conn, cursor) if cursor.exists() else None
    except (OSError, ValueError):
        return None


def _set(args, state, path, conn, inv, rclient, home, own_fn, reader, out, t, as_json) -> int:
    plan = load_plan(args.plan)
    th = thresholds(args.threshold)
    material = [inv, args.question, plan, args.since_seq, args.worker_session, args.worker_estimate, th]
    sent = state.get("sent_set")
    if sent and sent.get("material") == material:
        # A retry after an answer that never came: the same direction and body, wherever the head moved.
        d, events, key = sent["direction"], sent["events"], sent["key"]
    else:
        head = reader(conn, inv, after=0, limit=1, client=rclient)["head_seq"]
        if args.since_seq is not None and args.since_seq < 1:
            raise BudgetError("--since-seq is a positive event seq")
        since = args.since_seq if args.since_seq is not None else head
        if since > head:
            raise BudgetError(f"--since-seq {since} is past the head (#{head})")
        after = _committed(conn, path.parent / "cursor.json")
        if after is not None and since < after:
            raise BudgetError(f"--since-seq {since} is before your review cursor (#{after} is committed), so those "
                              f"events cannot be tagged: use --since-seq {after} or later, or set the direction "
                              "before reviewing them")
        page = reader(conn, inv, after=max(0, since - 1), limit=1, client=rclient)
        first = page["events"][0] if page["events"] else None
        set_at = (parse_time(first.get("created_at")) if first else None) or t
        d = new_direction(inv=inv, question=args.question, plan=plan, since_seq=since, set_at=set_at,
                          worker_session=args.worker_session, worker_estimate=args.worker_estimate, th=th)
        if after is not None and since <= after:
            d["last_t"] = set_at            # its start is already reviewed: the next worker event counts from it
        old = state.get("current")
        if old and old["direction_id"] == d["direction_id"]:
            raise BudgetError(f"{d['direction_id']} is already the open direction; a correction is --set --amend")
        p50, _ = totals(d)
        d["firings"] = [{"scope": "opening", "milestone_id": None, "at_seq": since, "last_seq": since,
                         "snapshot": {"spent_h": 0, "estimate_h": p50, "delivered": 0, "ratio": None, "threshold": 0},
                         "worker_active_h": 0, "fired_seq": None, "verdict": None, "outcome": None}]
        events = ([ev_closed(old, "replaced", [])] if old else []) + _planned_all(d, inv) + [ev_fired(d, d["firings"][0])]
        key = key_for("set", events)
    own = own_fn()
    state["sent_set"] = {"material": material, "direction": d, "events": events, "key": key}
    save(path, state)
    try:
        seqs = record(own, key, events)
    except Exception as e:
        if definite(e):
            state["sent_set"] = None
            save(path, state)
        raise
    if seqs:
        d["firings"][0].update(fired_seq=seqs[-1], recorded_in=own["investigation_id"])
        d["recorded_in"], d["live_ids"] = own["investigation_id"], [m["id"] for m in d["milestones"]]
    lines = []
    old = state.get("current")
    if old and old["direction_id"] != d["direction_id"]:
        _to_history(state, old, "replaced", [], t)
        lines.append(f"closed {old['direction_id']} as replaced ({old['active_h']:.2f} h active)")
    state.update(current=d, applied_batch=None, sent_set=None, sent=None)
    _recorded(state, seqs, lines.append, own)
    save(path, state)
    if as_json:
        out(json.dumps({"direction": d, "recorded": seqs is not None}, indent=2, ensure_ascii=False))
        return 0
    p50, p80 = totals(d)
    lines.append(f"{d['direction_id']} set: {len(d['milestones'])} milestones, P50 {_n(p50)} h / P80 {_n(p80)} h, "
                 f"counting from #{d['since_seq']}; {d['calibration']}"
                 + ("" if d["calibration"] == "calibrated" else " (no measured pace)"))
    out("\n".join(lines))
    return 0


def _to_history(state: dict, d: dict, outcome: str, missed: list, t: float) -> dict:
    p50, p80 = totals(d)
    entry = {"direction_id": d["direction_id"], "question": d["question"], "outcome": outcome, "closed_at": iso(t),
             "since_seq": d["since_seq"], "p50_total_h": p50, "p80_total_h": p80, "spent_h": round(d["active_h"], 4),
             "none_h": round(d["none_h"], 4), "thresholds": d["thresholds"], "missed": missed,
             "firings": d["firings"],
             "per_milestone": [{**p, "deliverables": len(milestone(d, p["id"])["deliverables"])}
                               for p in ev_closed(d, outcome, missed)["payload"]["per_milestone"]]}
    state["history"] = (state.get("history") or [])[-(HISTORY_MAX - 1):] + [entry]
    state["current"] = None
    return entry


def _set_amend(args, state, path, own, out, t) -> int:
    """Re-plan within the same direction: changed milestones `replace`
    themselves (which re-arms them); a new milestone takes a dropped one's
    place (`replaces` it) on Cardinal, where every milestone not replaced
    stays live within the cap of 20. Charges, marks of deliverables that
    remain, and firings stay."""
    d = state["current"]
    inv = state["investigation_id"]
    plan = load_plan(args.plan)
    old = {m["id"]: m for m in d["milestones"]}
    new_ids = [m["id"] for m in plan["milestones"]]
    changed = [m for m in plan["milestones"] if old.get(m["id"]) != m]
    if not changed and len(new_ids) == len(old):
        raise BudgetError("--amend: the plan is the same as the open direction's")
    recorded = d.get("recorded_in") == own["investigation_id"]
    events = []
    if recorded:
        live = list(d.get("live_ids") or list(old))
        free = [x for x in live if x not in new_ids]           # live on Cardinal, gone from the plan
        for m in changed:
            rep = m["id"] if m["id"] in old else (free.pop(0) if free else None)
            if rep and rep != m["id"]:
                live.remove(rep)
            if m["id"] not in live:
                live.append(m["id"])
            events.append(ev_milestone_planned(d, m, rep))
        if len(live) > MAX_MILESTONES:
            raise BudgetError(f"--amend: Cardinal would hold {len(live)} live milestones (a dropped one stays until a "
                              f"new one takes its place), at most {MAX_MILESTONES}: drop fewer new ones or merge some")
    seqs = record(own, key_for("amend", [own["investigation_id"], d["direction_id"], events]), events) \
        if events else []
    keep = {x["id"] for m in plan["milestones"] for x in m["deliverables"]}
    d.update(milestones=plan["milestones"], unknowns=plan["unknowns"], reference_class=plan["reference_class"],
             marks={k: v for k, v in d["marks"].items() if k in keep})
    if recorded and seqs is not None:
        d["live_ids"] = live
    elif not recorded:
        d["recorded_in"] = None                  # the next write sends the whole new plan first
        seqs = [] if ensure_direction(own, d, inv) else None
    for m in plan["milestones"]:
        d["charged"].setdefault(m["id"], 0.0)
        if m["id"] in old and m in changed:
            d["last_fire"].pop(m["id"], None)
    _, p80 = totals(d)
    if d["active_h"] < d["thresholds"]["backstop"] * p80:
        d["backstop_fired"] = False
    lines = []
    _recorded(state, seqs, lines.append, own)
    save(path, state)
    p50, p80 = totals(d)
    dropped = sorted(set(old) - set(new_ids))
    lines.append(f"{d['direction_id']} re-planned: {len(changed)} milestone(s) new or changed; P50 {_n(p50)} h / "
                 f"P80 {_n(p80)} h" + (f"; dropped {', '.join(dropped)}" if dropped else ""))
    out("\n".join(lines))
    return 0


def _close(args, state, path, own, out, t) -> int:
    d = state["current"]
    missed = list(dict.fromkeys(x for x in (args.missed or "").split(",") if x))
    bad = [x for x in missed if not milestone(d, x)]
    if bad:
        raise BudgetError(f"--missed {bad[0]!r}: one of {', '.join(m['id'] for m in d['milestones'])}")
    ev = ev_closed(d, args.outcome, missed)
    seqs = None
    if ensure_direction(own, d, state["investigation_id"]):
        seqs = record(own, key_for("close", [own["investigation_id"], ev]), [ev])
    lines = []
    _recorded(state, seqs, lines.append, own)
    entry = _to_history(state, d, args.outcome, missed, t)
    save(path, state)
    lines.append(f"closed {d['direction_id']} ({args.outcome}): {entry['spent_h']:.2f} h active against P50 "
                 f"{_n(entry['p50_total_h'])} h / P80 {_n(entry['p80_total_h'])} h")
    out("\n".join(lines))
    return 0


def _firing(d: dict, scope: str, want: str) -> dict:
    """The latest firing of scope (a milestone id, opening or all) that
    lacks `want` (verdict / outcome)."""
    sc = {"opening": ("opening", None), "all": ("backstop", None)}.get(scope, ("milestone", scope))
    cands = [f for f in d["firings"] if (f["scope"], f.get("milestone_id")) == sc]
    if want == "outcome":
        cands = [f for f in cands if f.get("verdict")]
    open_ = [f for f in cands if not f.get(want)]
    if not open_:
        what = "with a verdict and no outcome" if want == "outcome" else "without a verdict"
        raise BudgetError(f"no step back on {scope} {what}"
                          + ("; record its verdict first" if want == "outcome" and not cands else ""))
    return open_[-1]


def _ref(r: str, inv: str) -> dict:
    if not isinstance(r, str) or not REF_RE.fullmatch(r):
        raise BudgetError(f"ref {r!r}: \"#<seq>\" (the worker's investigation) or ev_<12 hex>")
    return {"investigation_id": inv, "seq": int(r[1:])} if r.startswith("#") else {"evidence_id": r}


def _verdict(args, state, path, own, out, t) -> int:
    d, inv = state["current"], state["investigation_id"]
    scope, verdict = args.verdict
    if verdict not in VERDICTS:
        raise BudgetError(f"verdict {verdict!r}: one of {', '.join(VERDICTS)}")
    routed = args.routed_to or VERDICTS[verdict]
    if routed not in ROUTES:
        raise BudgetError(f"--routed-to: one of {', '.join(ROUTES)}")
    f = _firing(d, scope, "verdict")
    f["verdict"] = {"verdict": verdict, "routed_to": routed}
    if args.post_ref:
        f["verdict"]["post_ref"] = _ref(args.post_ref, inv)
    ok = ensure_verdict(own, d, f, inv)
    lines = []
    _recorded(state, None if not ok else [], lines.append, own)
    save(path, state)
    lines.append(f"verdict on the {scope} step back (#{f['at_seq']}): {verdict}, routed to {routed}")
    out("\n".join(lines))
    return 0


def _outcome(args, state, path, own, out, t) -> int:
    d, inv = state["current"], state["investigation_id"]
    scope, outcome = args.stepback_outcome
    if outcome not in STEPBACK_OUTCOMES:
        raise BudgetError(f"outcome {outcome!r}: one of {', '.join(STEPBACK_OUTCOMES)}")
    f = _firing(d, scope, "outcome")
    refs = [_ref(r, inv) for r in args.ref or []]
    seqs = None
    if ensure_verdict(own, d, f, inv):
        p = {"verdict_seq": f["verdict"]["verdict_seq"], "outcome": outcome, "refs": refs}
        seqs = record(own, key_for("outcome", [own["investigation_id"], p]), [{"type": "stepback.outcome", "payload": p}])
    f["outcome"] = {"verdict_seq": f["verdict"].get("verdict_seq"), "outcome": outcome, "refs": refs}
    lines = []
    _recorded(state, seqs, lines.append, own)
    save(path, state)
    lines.append(f"outcome of the {scope} step back (#{f['at_seq']}): {outcome}")
    out("\n".join(lines))
    return 0


# -- the CLI: investigation reviewed (the review pass) ----------------------------------------

def _cursor(fn, *a):
    try:
        return fn(*a)
    except BlockingIOError:
        raise BudgetError("the cursor file is in use by another command (a watch or another review); retry") from None


def reviewed(args, *, grant: dict, own_conn: Callable, home: Path, out: Callable = print) -> int:
    """Commit the outstanding page. With an open direction in the budget file
    (--budget-file, default budget.json beside the cursor file): its worker
    events must all be tagged; the tags, the marks and any firings are
    recorded (idempotent on the batch hash) before the cursor moves. A lost
    budget.json is rebuilt from those records first."""
    cursor = Path(args.cursor_file)
    bfile = Path(args.budget_file) if getattr(args, "budget_file", None) else cursor.parent / "budget.json"
    flags = [f"--{k} {v}" for k in ("tag", "done", "reopen", "redone") for v in (getattr(args, k, None) or [])]
    inv = grant["investigation_id"]
    with _locked(bfile):
        pre = []
        state = _restored(bfile, grant, own_conn, home)
        if state is not None:
            save(bfile, state)
            pre.append(f"rebuilt {bfile.name} from your supervision records")
        state = load(bfile, inv)
        d = state.get("current")
        if not d:
            if flags:
                raise BudgetError("no open direction for --tag/--done: `budget --set --plan <file>` first, or drop "
                                  "them")
            out("\n".join(pre + [json.dumps(_cursor(consumer.reviewed, grant, cursor, args.batch), indent=2)]))
            return 0
        page = _cursor(consumer.pending, grant, cursor)
        if not page or page.get("batch") != args.batch:
            raise BudgetError("--batch is not the outstanding read (cursor unchanged): pass the batch of the page you "
                              "read last, or read it again with --cursor-file")
        through = page["next_after"]
        if state.get("applied_batch") == args.batch:
            lines = state.get("applied_lines") or [f"reviewed through #{through}"]
        else:
            lines = _apply(args, state, bfile, d, page, grant, flags, home, own_conn)
    _cursor(consumer.reviewed, grant, cursor, args.batch)
    out("\n".join(pre + lines))
    return 0


def _apply(args, state, bfile, d, page, grant, flags, home, own_conn) -> list:
    inv = grant["investigation_id"]
    through = page["next_after"]
    wt = worker_times(page["events"], d["worker_session"], d["since_seq"])
    wseqs = [s for s, _ in wt]
    tags = parse_tags(getattr(args, "tag", None), wseqs, d)
    marks = parse_marks(args, d, wseqs, page["after"] + 1, through, inv)
    if not (wt or marks):
        state.update(applied_batch=args.batch, applied_lines=[f"reviewed through #{through}"])
        save(bfile, state)
        return state["applied_lines"]
    own = _own(state, args, home, own_conn, lambda: grant.get("token"))
    sent = state.get("sent")
    if sent and sent.get("batch") == args.batch and sent.get("investigation_id") == own["investigation_id"]:
        if sent.get("flags") != flags:
            raise BudgetError("this page was already sent with other flags (the commit failed after): repeat them: "
                              + " ".join(sent["flags"]))
        # A retry resends the exact body: Cardinal hashes it, charged_h included.
        new, events, lines, nfired = sent["new"], sent["events"], sent["lines"], sent["nfired"]
    else:
        spool = []
        if wt and d["last_t"] is not None:
            entries = spool_entries(home, d["worker_session"]) or []
            spool = [t for t in spool_times(entries, d["last_t"]) if t < wt[-1][1]]
        new, res = apply_batch(d, wt, spool, tags, marks)
        lines, nfired = pass_lines(new, res, through), len(res["firings"])
        events = [{"type": "work.attributed", "payload": {
            "direction_id": d["direction_id"], "batch": args.batch, "through_seq": through,
            "tags": {str(s): tags[s] for s in wseqs},
            "charged_h": {m: round(h, 4) for m, h in res["charged"].items()}}}]
        events += [{"type": "deliverable.marked", "payload": {
            "direction_id": d["direction_id"], "milestone_id": mk["milestone_id"], "deliverable_id": mk["deliverable_id"],
            "mark": mk["mark"], "refs": mk["refs"]}} for mk in marks]
        events += [ev_fired(new, f) for f in res["firings"]]
        if len(events) > MAX_EVENTS:
            raise BudgetError(f"at most {MAX_EVENTS - 1 - nfired} marks in one pass")
    seqs = None
    if ensure_direction(own, new, inv):
        state["sent"] = {"batch": args.batch, "flags": flags, "investigation_id": own["investigation_id"],
                         "events": events, "new": new, "lines": lines, "nfired": nfired}
        save(bfile, state)
        try:
            seqs = record(own, "sup:rv:" + args.batch[:64], events)
        except Exception as e:
            if definite(e):
                state["sent"] = None            # refused for good: nothing pending, the page can be fixed
                save(bfile, state)
            if getattr(e, "code", None) == "idempotency_conflict":
                raise BudgetError("Cardinal already holds this page with other tags or marks (budget.json lost them?); "
                                  "repeat the flags you sent first") from None
            e.args = (f"{e}; cursor unchanged",)
            raise
    if seqs and nfired:
        for f, sq in zip(new["firings"][-nfired:], seqs[-nfired:]):
            f.update(fired_seq=sq, recorded_in=own["investigation_id"])
    pre = []
    _recorded(state, seqs, pre.append, own)
    lines = pre + lines
    state.update(current=new, applied_batch=args.batch, applied_lines=lines, sent=None)
    save(bfile, state)
    return lines
