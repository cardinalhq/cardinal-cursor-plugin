"""Private grant files and crash-safe review cursors for WAL consumers.

Reading persists a pending page, not a reviewed cursor. Explicit review commits
that exact batch. A retry replays the pending page, including after a crash.
These files are credentials/state, not an OS isolation boundary.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from . import investigation_events as ie
from . import investigation_grants as grants


def private_read(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077 or mode.st_uid != os.getuid():
            raise ValueError("credential/state file must be owned by you and private (mode 0600)")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("credential/state file must contain a JSON object")
    return value


def private_write(path: Path, value: dict, *, create=False) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def credential_connection(path: Path) -> dict:
    raw = private_read(path)
    conn = grants.token_connection({grants.TOKEN_ENV: raw.get("token"),
                                    grants.ORIGIN_ENV: raw.get("origin")})
    if not conn:
        raise ValueError("credential file contains no investigation token")
    return conn


def identity(conn: dict) -> dict:
    return {k: conn.get(k) for k in ("origin", "org", "investigation_id", "grant_id")}


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_mode & 0o077 or mode.st_uid != os.getuid():
        raise ValueError("state directory must be owned by you, private (mode 0700), and not a symlink")
    return path.absolute()


def principal_key(principal):
    if isinstance(principal, str):
        return principal
    if isinstance(principal, dict) and principal.get("kind") and principal.get("id"):
        return f"{principal['kind']}:{principal['id']}"
    return None


def is_self(event, viewer, session=None):
    known = {ie.CONTROL: ie.CONTROL_TYPES, ie.SEMANTIC: ie.SEMANTIC_TYPES}
    if event.get("type") not in known.get(ie.event_class(event), ()):
        return False
    producer = event.get("producer") if isinstance(event.get("producer"), dict) else {}
    who = principal_key(viewer.get("principal"))
    if not who or principal_key(producer.get("principal")) != who:
        return False
    return who.startswith("grantee:") or (bool(session) and producer.get("session_id") == session)


def batch_hash(page: dict) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in page.items() if k != "batch"},
                                    sort_keys=True).encode()).hexdigest()


def self_only(page: dict) -> bool:
    events = page.get("events") or []
    seqs = [e.get("seq") for e in events]
    return bool(events) and all(type(s) is int for s in seqs) and (
        len(seqs) == page["next_after"] - page["after"] and
        all(s == page["after"] + i + 1 for i, s in enumerate(seqs))
    ) and all(is_self(e, page.get("viewer") or {}) for e in events)


@contextlib.contextmanager
def locked(path: Path):
    import fcntl
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def load(path: Path, conn: dict) -> dict:
    try:
        state = private_read(path)
    except FileNotFoundError:
        return {"version": 1, "identity": identity(conn), "after": 0, "pending": None}
    if state.get("version") != 1 or state.get("identity") != identity(conn):
        raise ValueError("cursor belongs to another investigation, origin, or grant")
    after = state.get("after")
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise ValueError("invalid review cursor")
    return state


def committed(conn: dict, path: Path) -> int:
    """The reviewed position, read-only and unlocked (a review may hold the
    lock): where a watch starts, so re-arming it signals only unreviewed
    events instead of the whole history."""
    return load(path, conn)["after"]


def read(conn: dict, path: Path, *, client: str, skip_self: bool = False) -> dict:
    with locked(path):
        state = load(path, conn)
        if state.get("pending") is not None:
            return state["pending"]
        for _ in range(10):
            page = dict(ie.read_events(conn, conn["investigation_id"], after=state["after"], limit=100, client=client))
            if not state["after"] <= page["last_seq"] <= page["head_seq"]:
                raise ValueError("server returned an invalid WAL watermark")
            page["after"] = state["after"]
            page["next_after"] = page.pop("last_seq")
            page["batch"] = batch_hash(page)
            if skip_self and self_only(page):
                state["after"] = page["next_after"]
                private_write(path, state)
                if state["after"] < page["head_seq"]:
                    continue
            else:
                if page["next_after"] > state["after"]:
                    state["pending"] = page
                    private_write(path, state)
                return page
            break
        # Only our own, fully covered events were advanced; nothing was reviewed.
        page.update(after=state["after"], next_after=state["after"], events=[])
        page["batch"] = batch_hash(page)
        return page


def pending(conn: dict, path: Path):
    """The outstanding page (None when there is none), read under the lock."""
    with locked(path):
        return load(path, conn).get("pending")


def reviewed(conn: dict, path: Path, batch: str) -> dict:
    with locked(path):
        return reviewed_locked(conn, path, batch)


def reviewed_locked(conn: dict, path: Path, batch: str) -> dict:
    """Caller holds the cursor lock continuously through budget writes and commit."""
    state = load(path, conn)
    pending = state.get("pending")
    if not pending or pending.get("batch") != batch:
        raise ValueError("--batch is not the outstanding read (cursor unchanged): pass the batch of the page you "
                         "read last, or read it again with --cursor-file")
    state["after"] = pending["next_after"]
    state["pending"] = None
    private_write(path, state)
    return {"investigation_id": conn["investigation_id"], "after": state["after"]}


def save_page(conn: dict, cursor: Path, directory: Path, page: dict) -> Path:
    with locked(cursor):
        if load(cursor, conn).get("pending") != page or page.get("batch") != batch_hash(page):
            raise ValueError("page is no longer the outstanding read; read it again")
        directory = private_directory(directory)
        path = directory / (page["batch"] + ".json")
        snapshot = {"version": 1, "identity": identity(conn), "page": page}
        try:
            private_write(path, snapshot, create=True)
        except FileExistsError:
            if private_read(path) != snapshot:
                raise ValueError("saved page contains different content")
        return path


def snapshot_batch(conn: dict, path: Path, pending: dict) -> str:
    snapshot = private_read(path)
    page = snapshot.get("page")
    if snapshot.get("version") != 1 or snapshot.get("identity") != identity(conn) or not isinstance(page, dict):
        raise ValueError("saved page belongs to another investigation, origin, or grant")
    if page != pending or page.get("batch") != batch_hash(page):
        raise ValueError("saved page is stale or changed; cursor unchanged")
    return page["batch"]


def renew_cursor(old: dict, new: dict, path: Path, *, client: str) -> dict:
    if any(old.get(k) != new.get(k) for k in ("origin", "org", "investigation_id")):
        raise ValueError("renewal requires the same origin, organization and investigation")
    if not old.get("scopes") or set(old["scopes"]) != set(new.get("scopes") or []):
        raise ValueError("renewal requires identical scope sets; start a new cursor for changed scopes")
    with locked(path):
        private_read(path)  # Renewal requires an existing cursor.
        state = load(path, old)
        page = ie.read_events(new, new["investigation_id"], after=state["after"], limit=1, client=client)
        if (page.get("investigation_id") != new["investigation_id"] or
                principal_key((page.get("viewer") or {}).get("principal")) != "grantee:" + str(new.get("grant_id")) or
                not state["after"] <= page["last_seq"] <= page["head_seq"]):
            raise ValueError("new grant validation returned an invalid identity or watermark")
        state.update(identity=identity(new), pending=None)
        private_write(path, state)
        return {"after": state["after"], "investigation_id": new["investigation_id"],
                "note": "Unreviewed events will be fetched again. Old-grant challenges retain their original ruling authority."}


def outbox_path(conn: dict, credential: Path, type_: str, text: str, target: str, refs: list) -> Path:
    payload = ie.text_payload(text, refs or None)
    key = hashlib.sha256(json.dumps([identity(conn), type_, payload, target], sort_keys=True).encode()).hexdigest()
    return private_directory(credential.parent / "outbox") / (key + ".json")


def post(conn: dict, type_: str, text: str, target: str, refs: list, *, client: str, outbox: Path, session_id=None) -> dict:
    payload = ie.text_payload(text, refs or None)
    # Content-addressed key survives a lost HTTP response and process restart.
    material = [identity(conn), type_, payload, target]
    key = "supervisor:" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    intent = {"identity": identity(conn), "type": type_, "payload": payload, "target": target, "key": key}
    with locked(outbox):
        try:
            saved = private_read(outbox)
        except FileNotFoundError:
            saved = {"intent": intent}
            private_write(outbox, saved, create=True)
        if saved.get("intent") != intent:
            raise ValueError("outbox contains another advisory; retry its exact content or use a new file")
        # Always retry through the server: it checks revocation/expiry and deduplicates.
        producer = {"session_id": session_id} if not conn.get("token") and session_id else {}
        try:
            result = ie.append_event(conn, conn["investigation_id"], type_, payload,
                                     idempotency_key=key, to_session_id=target, client=client,
                                     producer_client=client, **producer)
        except Exception as e:
            settle_refused(outbox, saved, e)
            raise
        saved["result"] = result
        private_write(outbox, saved)
        return result


def settle_refused(outbox: Path, saved: dict, err: Exception) -> None:
    """A definite refusal (a 4xx) resolves an outbox: nothing was recorded,
    so it no longer blocks a grant succession (unresolved_outboxes). A 5xx
    or a network failure leaves it unresolved."""
    status = getattr(err, "status", None)
    if isinstance(status, int) and 400 <= status < 500:
        saved["refused"] = {"status": status, "error": (getattr(err, "body", None) or {}).get("error")}
        private_write(outbox, saved)


def unresolved_outboxes(credential: Path, conn: dict) -> list:
    """The outboxes beside `credential` (outbox/*.json) written under this
    grant's identity that have neither a result nor a definite refusal: an
    uncertain post or verdict (the server may have it)."""
    directory = credential.parent / "outbox"
    out = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
        try:
            saved = private_read(path)
        except (OSError, ValueError):
            out.append(path)   # unreadable: it may be unresolved
            continue
        intent = saved.get("intent") if isinstance(saved.get("intent"), dict) else {}
        if intent.get("identity") == identity(conn) and "result" not in saved and "refused" not in saved:
            out.append(path)
    return out


def verdict_outbox_path(conn: dict, credential: Path, unresolved: dict) -> Path:
    """verdict-<sha256 of the grant identity and the unresolved verdict
    input>.json in outbox/ beside the credential: the same command finds its
    earlier resolved payload, whatever HEAD is now."""
    key = hashlib.sha256(json.dumps([identity(conn), "verdict", unresolved], sort_keys=True).encode()).hexdigest()
    return private_directory(credential.parent / "outbox") / f"verdict-{key}.json"


def verdict(conn: dict, outbox: Path, build, *, client: str) -> tuple:
    """(answer, replayed): record a verdict through its outbox. A new outbox
    gets build() -> (payload, key), written BEFORE the first POST; an
    existing one resends exactly what it holds (or answers its stored
    result). A definite refusal is recorded in it (settle_refused)."""
    with locked(outbox):
        try:
            saved = private_read(outbox)
            replayed = True
        except FileNotFoundError:
            payload, key = build()
            saved = {"intent": {"identity": identity(conn), "type": ie.VERIFICATION, "payload": payload, "key": key}}
            private_write(outbox, saved, create=True)
            replayed = False
        intent = saved.get("intent") if isinstance(saved.get("intent"), dict) else {}
        if intent.get("identity") != identity(conn) or intent.get("type") != ie.VERIFICATION:
            raise ValueError("verdict outbox belongs to another grant or holds something else")
        if "result" in saved:
            return saved["result"], True
        saved.pop("refused", None)
        try:
            result = grants.record_verification(conn, conn["investigation_id"], intent["payload"],
                                                idempotency_key=intent["key"], client=client)
        except Exception as e:
            settle_refused(outbox, saved, e)
            raise
        saved["result"] = result
        private_write(outbox, saved)
        return result, replayed


def resolve_verdict(outbox: Path, result: dict) -> None:
    """Record `result` (the stored identical verdict a 409 named) as the
    outbox's answer."""
    with locked(outbox):
        saved = private_read(outbox)
        saved.pop("refused", None)
        saved["result"] = result
        private_write(outbox, saved)
