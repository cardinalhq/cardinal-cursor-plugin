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


def read(conn: dict, path: Path, *, client: str) -> dict:
    with locked(path):
        state = load(path, conn)
        if state.get("pending") is not None:
            return state["pending"]
        page = dict(ie.read_events(conn, conn["investigation_id"], after=state["after"], limit=100, client=client))
        if page["last_seq"] > page["head_seq"] or page["head_seq"] < state["after"]:
            raise ValueError("server returned an invalid WAL watermark")
        page["after"] = state["after"]
        page["next_after"] = page.pop("last_seq")
        page["batch"] = hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()
        if page["next_after"] > state["after"]:
            state["pending"] = page
            private_write(path, state)
        return page


def reviewed(conn: dict, path: Path, batch: str) -> dict:
    with locked(path):
        state = load(path, conn)
        pending = state.get("pending")
        if not pending or pending.get("batch") != batch:
            raise ValueError("batch is not the outstanding read; cursor unchanged")
        state["after"] = pending["next_after"]
        state["pending"] = None
        private_write(path, state)
        return {"investigation_id": conn["investigation_id"], "after": state["after"]}


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
        result = ie.append_event(conn, conn["investigation_id"], type_, payload,
                                 idempotency_key=key, to_session_id=target, client=client,
                                 producer_client=client, **producer)
        saved["result"] = result
        private_write(outbox, saved)
        return result
