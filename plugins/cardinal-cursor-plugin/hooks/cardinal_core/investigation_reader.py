"""Supervisor reads and follow mode over the single #190 pending-page format."""

from __future__ import annotations
import copy
import json
import os
import sys
import time
from pathlib import Path
from . import investigation_events as ie
from . import investigation_consumer as consumer
from . import investigation_state as ist
from . import investigation_state_sync as sync
from . import owner_relay
from . import supervision_status as sv


def relay_mark(event, mine):
    """For a relayed owner decision (authority owner_relayed): "by_you" when
    this supervisor session's own prompt hook sent it (owner_relay.sent_by),
    else "unverified": the agent holding the owner's key can relay one
    itself, naming any supervisor session. None for any other event, or
    when `mine` is None (not a supervisor's read)."""
    if mine is None or event.get("type") != ie.OWNER_DECIDED or event.get("authority") != ie.OWNER_RELAYED:
        return None
    return "by_you" if (event.get("investigation_id"), event.get("seq")) in mine else "unverified"


RELAY_TAGS = {"by_you": " [relayed by you]",
              "unverified": " [relayed, not by this supervisor session: unverified; tell the owner]"}


def status_suffix(event, statuses):
    """' [status: …]' for a challenge whose status the events read so far
    determine (supervision_status), else ''."""
    info = statuses.get(event.get("seq")) if event.get("type") == ie.CHALLENGE else None
    if not info:
        return ""
    extra = "".join(f", {label} #{info[k]}" for k, label in (("ruling_seq", "ruling"), ("owner_seq", "owner"))
                    if isinstance(info.get(k), int))
    return f" [status: {info['status']}{extra}]"


def principal_key(principal):
    if isinstance(principal, str):
        return principal
    if isinstance(principal, dict) and principal.get("kind") and principal.get("id"):
        return f"{principal['kind']}:{principal['id']}"
    return None


def is_self(event, viewer, session):
    return consumer.is_self(event, viewer, session)


def compact_event(event, principals, mine=None):
    producer = event.get("producer") if isinstance(event.get("producer"), dict) else {}
    author = principal_key(producer.get("principal"))
    info = principals.get(author) if isinstance(principals.get(author), dict) else {}
    name = info.get("name") or info.get("label") or info.get("email") or author
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    # The required field is the primary text (a resolution's statement is secondary).
    field = ie.SEMANTIC_FIELDS.get(event.get("type"), ("text",))[0]
    text = payload.get(field) or ""
    secondary = [f"{name}: {payload[name]}" for name in ie.SEMANTIC_FIELDS.get(event.get("type"), (None, (), ()))[1]
                 if isinstance(payload.get(name), str) and payload[name]]
    if secondary:
        text = "\n".join([text, *secondary])
    if event.get("type") == ie.ACKNOWLEDGED:
        text = f"ack of #{payload.get('ack_of')}: {payload.get('disposition')}" + (
            f" (conflicts with #{payload['conflicts_with']})" if payload.get("conflicts_with") else ""
        ) + (f" {payload['note']}" if payload.get("note") else "")
    elif event.get("type") == ie.RULING:
        text = f"{payload.get('outcome')} ruling on #{payload.get('ruling_of')}: {text}"
    if ie.event_class(event) is None:
        text, payload = "[unknown class]", {}
    return {
        "seq": event.get("seq"),
        "class": ie.event_class(event),
        "type": event.get("type"),
        "at": event.get("created_at"),
        "author": name,
        "session": producer.get("session_id"),
        "to": event.get("to_session_id"),
        "semantic_id": payload.get("semantic_id"),
        "text": text,
        "evidence": payload.get("evidence", []),
        "refs": payload.get("refs", []),
        "artifacts": payload.get("artifacts", []),
        "gate": payload.get("gate"),
        **{name: payload[name] for name in ("tests", "based_on") if name in payload},
        **({"relayed": relay_mark(event, mine)} if relay_mark(event, mine) else {}),
    }


def compact_line(event, principals, mine=None):
    e = compact_event(event, principals, mine)
    at = str(e["at"] or "")
    clock = at[11:16] if len(at) >= 16 else at
    to = f" · to {str(e['to'])[:8]}" if e["to"] else ""
    # Inert one-line strings prevent hostile event text from forging an envelope.
    details = {k: e[k] for k in ("semantic_id", "evidence", "refs", "tests", "based_on", "artifacts", "gate") if e.get(k)}
    suffix = " · " + ie.json_text(json.dumps(details, ensure_ascii=False)) if details else ""
    return (f"#{e['seq']} {clock} {ie._tok(e['type'])} · {ie._one_line(e['author'])}{to}: "
            f"{ie.json_text(str(e['text']))}{RELAY_TAGS.get(e.get('relayed'), '')}{suffix}")


def display(page, args, *, legacy_line=None, maximum=None, statuses=None):
    viewer = page.get("viewer") or {}
    grantee = (principal_key(viewer.get("principal")) or "").startswith("grantee:")
    # A supervisor (a grantee read) is told which relayed decisions its own session sent.
    mine = owner_relay.sent_by(Path(os.environ.get("HOME") or str(Path.home())),
                               getattr(args, "session", None)) if grantee else None
    exclude = args.exclude_self
    if exclude is None:
        exclude = (args.follow or bool(args.cursor_file)) and (bool(args.session) or grantee)
    events = [
        e
        for e in page["events"]
        if (not args.event_class or ie.event_class(e) in args.event_class)
        and (not args.event_type or e.get("type") in args.event_type)
        and (not exclude or not is_self(e, viewer, args.session))
    ]
    complete = maximum is None or len(events) <= maximum
    shown = events if maximum is None else events[:maximum]
    if args.json:
        answer = copy.deepcopy(page)
        answer["events"] = (
            [compact_event(e, page.get("principals") or {}, mine) for e in shown]
            if args.fields == "compact"
            else shown
        )
        for e in answer["events"]:
            if isinstance(e.get("producer"), dict):
                e["producer"].pop("_resolved", None)
            mark = relay_mark(e, mine) if args.fields != "compact" else None
            if mark:
                e["relayed"] = mark
        shown_seqs = {e.get("seq") for e in shown}
        known = {str(k): v for k, v in (statuses or {}).items() if k in shown_seqs}
        if known:
            answer["challenge_statuses"] = known
        print(json.dumps(answer, indent=2, ensure_ascii=False))
    else:
        for e in shown:
            line = legacy_line(e) + RELAY_TAGS.get(relay_mark(e, mine), "") if legacy_line and not args.cursor_file \
                else compact_line(e, page.get("principals") or {}, mine)
            print(line + status_suffix(e, statuses or {}))
    sys.stdout.flush()  # Commit only after successful printing; a crash replays the pending page.
    return len(shown), complete


def run(args, conn, inv, *, client, legacy_line=None):
    signal_only = getattr(args, "signal_only", False)
    save_directory = getattr(args, "save_page", None)
    if args.cursor_file and args.auto_review and (args.event_class or args.event_type or args.max is not None):
        raise ValueError("cursor-backed --auto-review cannot use class/type/max filters: hidden events must not be reviewed")
    if signal_only and (not args.cursor_file or not args.follow or args.auto_review or args.json or
                        args.event_class or args.event_type or args.exclude_self is False):
        raise ValueError("--signal-only requires a cursor-backed follow watch without JSON, filters or auto-review")
    if save_directory and (not args.cursor_file or args.follow or args.auto_review or args.event_class or
                           args.event_type or args.max is not None or args.exclude_self is True):
        raise ValueError("--save-page requires an unfiltered, non-follow cursor read")
    if args.after < 0:
        raise ValueError("--after is a seq, 0 or more")
    if (
        args.interval < 2
        or (args.timeout is not None and args.timeout <= 0)
        or (args.max is not None and args.max <= 0)
    ):
        raise ValueError("--interval must be at least 2s; --timeout and --max must be positive")
    if args.cursor_file:
        if not conn.get("token"):
            raise ValueError("--cursor-file requires a scoped credential")
        if args.after:
            raise ValueError("--after cannot override a review cursor")
    # --follow --cursor-file without --auto-review is a watch: it starts at the
    # committed review position and never reads or writes the pending page. It
    # is a wake signal, so --max never cuts a page: it returns after the page
    # that reaches it (--max 1: the first non-empty page).
    watch = bool(args.cursor_file) and args.follow and not args.auto_review
    if args.exclude_self and not args.session and not conn.get("token"):
        print("--exclude-self needs --session for a member reader; nothing is excluded", file=sys.stderr)
    deadline = time.monotonic() + args.timeout if args.timeout else None
    cursor, count, failures = (consumer.committed(conn, args.cursor_file) if watch else args.after), 0, 0
    # Claude compatibility lists all pages as one answer. Explicit [inv] also drains the stream.
    explicit = bool(args.investigation_id or args.investigation)
    drain = not args.follow and not args.cursor_file and (explicit or legacy_line is not None)
    target = None if conn.get("token") or explicit or legacy_line is not None else args.session
    collected, principals, viewer = [], {}, {}
    seen: dict = {}   # every event read in this run, for the challenges' statuses
    pages = 0
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            try:
                if args.cursor_file and not watch:
                    page = consumer.read(conn, args.cursor_file, client=client,
                                         skip_self=args.exclude_self is not False and not args.auto_review and
                                         not args.event_class and not args.event_type and args.max is None)
                else:
                    timeout = min(30.0, max(0.1, deadline - time.monotonic())) if deadline else 30.0
                    # Multiple classes form a union. The route accepts one class; union reads stay unfiltered.
                    cls = args.event_class[0] if len(args.event_class) == 1 else None
                    page = ie.read_events(
                        conn,
                        inv,
                        after=cursor,
                        limit=ie.CLI_PAGE_LIMIT if drain else 100,
                        to_session_id=target,
                        client=client,
                        class_=cls,
                        types=args.event_type or None,
                        timeout=timeout,
                    )
                    page["next_after"] = page.pop("last_seq")
                failures = 0
            except (ist.FetchError, sync.ServerError, OSError) as err:
                if not args.follow or (isinstance(err, sync.ServerError) and err.status in (401, 403, 404)):
                    print(
                        f"cannot read the events of {inv}: "
                        + (ie.plain(err) if isinstance(err, sync.ServerError) else ie._one_line(err)),
                        file=sys.stderr,
                    )
                    return 1
                failures += 1
                print("event read failed; retrying: " + ie._one_line(err), file=sys.stderr)
                delay = min(60.0, args.interval * 2 ** min(failures, 5))
                if isinstance(err, sync.ServerError) and err.retry_after:
                    delay = max(delay, err.retry_after)
                pause(delay, deadline)
                continue
            if drain:
                collected.extend(page["events"])
                principals.update(page.get("principals") or {})
                viewer = page.get("viewer") or viewer
                pages += 1
                if page["next_after"] > cursor and page["next_after"] < page["head_seq"] and pages < 1000:
                    cursor = page["next_after"]
                    continue
                page = {
                    "investigation_id": inv,
                    "events": collected,
                    "next_after": page["next_after"],
                    "head_seq": page["head_seq"],
                    "principals": principals,
                    "viewer": viewer,
                }
            for e in page["events"]:
                if isinstance(e, dict) and isinstance(e.get("seq"), int):
                    seen[e["seq"]] = e
            if signal_only:
                visible = [e for e in page["events"] if not is_self(e, page.get("viewer") or {}, args.session)]
                n, complete = len(visible), True
                if n:
                    print(f"Review pending: #{visible[0]['seq']}–#{visible[-1]['seq']} ({n} events). "
                          "Read the full page through your review cursor; this watch has not reviewed it.")
                    sys.stdout.flush()
            elif save_directory and page["next_after"] > page["after"]:
                saved = consumer.save_page(conn, args.cursor_file, save_directory, page)
                n, complete = len(page["events"]), True
                print(json.dumps({"page_file": str(saved), "events": n, "after": page["after"],
                                  "next_after": page["next_after"]}) if args.json else f"Read full review page: {saved}")
                sys.stdout.flush()
            else:
                n, complete = display(
                    page, args, legacy_line=legacy_line, maximum=args.max - count if args.max and not watch else None,
                    statuses=sv.challenge_statuses(list(seen.values())),
                )
            count += n
            if args.cursor_file and args.auto_review and complete and page["next_after"] > page["after"]:
                consumer.reviewed(conn, args.cursor_file, page["batch"])
            cursor = page["next_after"]
            if not args.follow:
                if legacy_line and not args.json:
                    print(
                        f"{n} event{'s' if n != 1 else ''} after #{args.after}; head #{page['head_seq']}",
                        file=sys.stderr,
                    )
                return 0
            if args.max is not None and count >= args.max:
                return 0
            if cursor < page["head_seq"]:
                continue
            pause(args.interval, deadline)
    except KeyboardInterrupt:
        return 0


def pause(seconds, deadline):
    if deadline is not None:
        seconds = min(seconds, max(0, deadline - time.monotonic()))
    # Short chunks keep Ctrl-C responsive even on a long Retry-After.
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))
