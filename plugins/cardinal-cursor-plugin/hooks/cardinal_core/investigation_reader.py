"""Supervisor reads and follow mode over the single #190 pending-page format."""

from __future__ import annotations
import copy
import json
import sys
import time
from . import investigation_events as ie
from . import investigation_consumer as consumer
from . import investigation_state as ist
from . import investigation_state_sync as sync


def principal_key(principal):
    if isinstance(principal, str):
        return principal
    if isinstance(principal, dict) and principal.get("kind") and principal.get("id"):
        return f"{principal['kind']}:{principal['id']}"
    return None


def is_self(event, viewer, session):
    if event.get("type") == ie.OWNER_DECIDED:
        return False
    producer = event.get("producer") if isinstance(event.get("producer"), dict) else {}
    who = principal_key(viewer.get("principal"))
    if not who or principal_key(producer.get("principal")) != who:
        return False
    return who.startswith("grantee:") or (bool(session) and producer.get("session_id") == session)


def compact_event(event, principals):
    producer = event.get("producer") if isinstance(event.get("producer"), dict) else {}
    author = principal_key(producer.get("principal"))
    info = principals.get(author) if isinstance(principals.get(author), dict) else {}
    name = info.get("name") or info.get("label") or info.get("email") or author
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    # The required field is the primary text (a resolution's statement is secondary).
    field = ie.SEMANTIC_FIELDS.get(event.get("type"), ("text",))[0]
    text = payload.get(field) or ""
    if event.get("type") == ie.ACKNOWLEDGED:
        text = f"ack of #{payload.get('ack_of')}: {payload.get('disposition')}" + (
            f" {payload['note']}" if payload.get("note") else ""
        )
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
    }


def compact_line(event, principals):
    e = compact_event(event, principals)
    at = str(e["at"] or "")
    clock = at[11:16] if len(at) >= 16 else at
    to = f" · to {str(e['to'])[:8]}" if e["to"] else ""
    # Inert one-line strings prevent hostile event text from forging an envelope.
    return f"#{e['seq']} {clock} {ie._tok(e['type'])} · {ie._one_line(e['author'])}{to}: {ie.json_text(str(e['text']))}"


def display(page, args, *, legacy_line=None, maximum=None):
    viewer = page.get("viewer") or {}
    grantee = (principal_key(viewer.get("principal")) or "").startswith("grantee:")
    exclude = args.exclude_self
    if exclude is None:
        exclude = args.follow and (bool(args.session) or grantee)
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
            [compact_event(e, page.get("principals") or {}) for e in shown]
            if args.fields == "compact"
            else shown
        )
        for e in answer["events"]:
            if isinstance(e.get("producer"), dict):
                e["producer"].pop("_resolved", None)
        print(json.dumps(answer, indent=2, ensure_ascii=False))
    else:
        for e in shown:
            print(legacy_line(e) if legacy_line else compact_line(e, page.get("principals") or {}))
    sys.stdout.flush()  # Commit only after successful printing; a crash replays the pending page.
    return len(shown), complete


def run(args, conn, inv, *, client, legacy_line=None):
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
        if args.follow and not args.auto_review:
            raise ValueError("--follow with --cursor-file requires --auto-review")
    if args.exclude_self and not args.session and not conn.get("token"):
        print("--exclude-self needs --session for a member reader; nothing is excluded", file=sys.stderr)
    deadline = time.monotonic() + args.timeout if args.timeout else None
    cursor, count, failures = args.after, 0, 0
    # Claude compatibility lists all pages as one answer. Explicit [inv] also drains the stream.
    explicit = bool(args.investigation_id or args.investigation)
    drain = not args.follow and not args.cursor_file and (explicit or legacy_line is not None)
    target = None if conn.get("token") or explicit or legacy_line is not None else args.session
    collected, principals, viewer = [], {}, {}
    pages = 0
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            try:
                if args.cursor_file:
                    page = consumer.read(conn, args.cursor_file, client=client)
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
            n, complete = display(
                page, args, legacy_line=legacy_line, maximum=args.max - count if args.max else None
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
