"""Intent-based coordination, durable consumer routing and delivery ownership.

Ordinary tool invocations are never classified or intercepted. Explicit sync/wait
calls select their authenticated context; no runtime process probes are needed.
"""
from __future__ import annotations

import contextlib
import os
import json
import time
from pathlib import Path

from . import investigation_consumer as c
from . import investigation_events as ie
from . import investigation_gate as engine

STATE_DIR = "CARDINAL_COORDINATION_STATE_DIR"
SUPPORTED = ("codex", "claude")
GATE_RULE = "Coordinate on intent at any point in your work. Use investigation sync --intent PURPOSE to catch up before reasoning, deciding or continuing. When delegating required work, create an investigation request with its assignee and intent; use wait --intent PURPOSE for the required result. Respond complete, blocked, abstain or reject; only exact-result acceptance or reasoned cancellation resolves the dependency. Ordinary tools are not automatically blocked."
PREFIX = "Coordination: gate-read --batch B for every chunk; handle/review, then choose again.\n"


def route_path(home, sid):
    if not ie.valid_session(sid):
        raise ValueError("invalid gate session")
    override = os.environ.get(STATE_DIR)
    directory = Path(override) if override else ie.sessions_dir(Path(home))
    if not directory.is_absolute():
        raise ValueError(STATE_DIR + " must be an absolute private directory")
    return directory / (sid + ".gate-route.json")


def ready_path(home, sid):
    return route_path(home, sid).with_suffix(".ready.json")


def _read_route(home, sid, path=None):
    try:
        out = c.private_read(path or route_path(home, sid))
    except FileNotFoundError:
        return None
    if (out.get("version") != 1 or out.get("session") != sid or type(out.get("enabled")) is not bool
            or out.get("runtime") not in SUPPORTED or out.get("state") not in ("activating", "active", "superseded")
            or not ie.valid_investigation(out.get("investigation_id")) or not Path(out.get("gate_file", "")).is_absolute()):
        raise ValueError("invalid coordination route; repair or disable it explicitly")
    if out.get("policy_hash") not in engine.compatible_policy_hashes(engine.policy(out.get("policy"))):
        raise ValueError("coordination route policy hash mismatch")
    if out.get("previous_policy") and out.get("previous_policy_hash") not in engine.compatible_policy_hashes(engine.policy(out["previous_policy"])):
        raise ValueError("previous coordination route policy hash mismatch")
    return out


def index_path(home, sid):
    return route_path(home, sid).with_suffix(".consumers.json")


def all_routes(home, sid):
    paths = [route_path(home, sid)]
    # Include a persisted activating route left before its index publication.
    # These are our own typed route records, never a scan for credentials.
    if paths[0].parent.exists():
        paths.extend(paths[0].parent.glob(sid + ".consumer-*.json"))
    if len(paths) > 128:
        raise ValueError("too many coordination consumers; resolve retired associations")
    try:
        index = c.private_read(index_path(home, sid))
        for name in index["routes"]:
            p = Path(name)
            if p.parent != paths[0].parent or not p.name.startswith(sid + ".consumer-") or p.suffix != ".json":
                raise ValueError("invalid coordination consumer route")
            if not p.exists():
                raise ValueError("indexed coordination consumer activation is incomplete")
            paths.append(p)
    except FileNotFoundError:
        pass
    return [(p, r) for p in dict.fromkeys(paths) if (r := _read_route(home, sid, p)) is not None]


def route(home, sid, investigation=None, batch=None, credential_file=None):
    candidates = [r for _, r in all_routes(home, sid)
                  if r["state"] != "superseded" and (not investigation or r["investigation_id"] == investigation)]
    if credential_file:
        candidates = [r for r in candidates if r.get("credential_file") == str(Path(credential_file).absolute())]
    if batch:
        matching = []
        for candidate in candidates:
            pending = c.private_read(Path(candidate["gate_file"])).get("gate", {}).get("batch")
            if pending and pending["page"]["batch"] == batch:
                matching.append(candidate)
        candidates = matching
    if len(candidates) > 1:
        raise ValueError("multiple coordination consumers; specify --investigation/--credential-file or the exact --batch")
    return candidates[0] if candidates else None


def owns_delivery(home, sid, investigation=None, runtime=None):
    """Conservatively retain legacy queues on unreadable/activating routes."""
    try:
        for _, selected in all_routes(home, sid):
            if selected["state"] == "superseded":
                continue
            if runtime and selected["runtime"] != runtime:
                continue
            if investigation and selected["investigation_id"] != investigation:
                continue
            if selected["state"] == "activating":
                return True
            if selected.get("automatic") or selected["enabled"]:
                state = c.private_read(Path(selected["gate_file"]))
                # Catch-up owns only its immutable outstanding batch. Outside
                # that exchange normal delivery can notify the agent that it
                # should choose to synchronize; no tool category is involved.
                if state.get("pending") or state.get("gate", {}).get("batch"):
                    return True
        return False
    except (ValueError, OSError):
        return True


@contextlib.contextmanager
def delivery_guard(home, sid, investigation=None, runtime=None):
    path = route_path(home, sid)
    if not path.parent.exists():
        yield True
        return
    lock = c.locked(path)
    try:
        lock.__enter__()
    except BlockingIOError:
        yield False
        return
    try:
        yield not owns_delivery(home, sid, investigation, runtime)
    finally:
        lock.__exit__(None, None, None)


def hook_path(cli, runtime):
    # Legacy caller compatibility; no tool action or executable recognition.
    return Path(cli).resolve().parent.parent / "hooks" / (
        "cardinal-codex-telemetry.py" if runtime == "codex" else "coordination-gate.py")


def activate(gate, value, *, runtime, cli, environ, new_epoch=False, budget_file=None, argv=None):
    value = engine.policy(value)
    if value.get("runtime") != runtime:
        raise ValueError("policy runtime does not match this CLI adapter")
    p = route_path(gate.home, gate.session)
    c.private_directory(p.parent)
    if gate.path == p or gate.path == ready_path(gate.home, gate.session):
        raise ValueError("gate state must be separate from routing/readiness files")
    with c.locked(p):
        old = route(gate.home, gate.session)
        selected = {"version": 1, "session": gate.session, "runtime": runtime, "enabled": True,
                    "state": "activating", "investigation_id": gate.conn["investigation_id"],
                    "policy": engine.policy(value),
                    "policy_hash": engine.policy_hash(value),
                    "gate_file": str(gate.path), "credential_file": str(gate.credential_file) if gate.credential_file else None}
        if old and old["enabled"] and any(old.get(k) != selected[k] for k in
                ("runtime", "gate_file", "investigation_id", "credential_file")):
            raise ValueError("another gate owns this session; disable it explicitly before replacing its route")
        if old and old["enabled"]:
            selected["previous_policy"] = old.get("previous_policy", old["policy"])
            selected["previous_policy_hash"] = engine.policy_hash(selected["previous_policy"])
        engine._save(p, selected)
        result = gate.enable(value, new_epoch=new_epoch, budget_file=budget_file)
        selected["state"] = "active"
        selected.pop("previous_policy", None)
        selected.pop("previous_policy_hash", None)
        engine._save(p, selected)
    return {**result, "guidance": GATE_RULE}


def deactivate(gate):
    p = route_path(gate.home, gate.session)
    c.private_directory(p.parent)
    with c.locked(p):
        selected = route(gate.home, gate.session)
        if selected and selected["gate_file"] != str(gate.path):
            raise ValueError("this session routes another gate file")
        result = {"enabled": False, "activation_cancelled": selected["state"] == "activating", "missing_state": True} if (
            selected and not gate.path.exists()) else gate.disable()
        if selected:
            selected["enabled"] = False
            engine._save(p, selected)
        return result


def denial(message):
    if len(message.encode()) > 1000:
        message = "Coordination feedback exceeded its byte budget; use gate-status/gate-read and resolve before retrying."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": message}}


def default_policy(runtime, hook=None, role="worker"):
    """Stable peer selection; purposes are supplied by the agent, never tools."""
    return engine.policy({"version": 1, "runtime": runtime, "role": role,
        "authenticated_peers": 1, "peers": []})


def ensure_automatic(home, sid, runtime, conn, client, hook, credential_file=None, deadline=1.0):
    """Bind one verified consumer without replacing another consumer's state."""
    role = "supervisor" if conn.get("grant_id") and "supervise" in conn.get("scopes", []) else "participant" if conn.get("grant_id") else "worker"
    value = default_policy(runtime, hook, role)
    identity = {"connection": c.identity(conn), "runtime": runtime,
                "credential": engine.digest({k: conn.get(k) for k in ("key", "token", "api_key")}),
                "credential_file": str(Path(credential_file).absolute()) if credential_file else None}
    consumer = engine.digest(identity)
    parent = c.private_directory(route_path(home, sid).parent)
    path = parent / (sid + ".consumer-" + consumer + ".json")
    # Reuse a legacy route for this exact consumer so outstanding hashes remain
    # readable until explicit handling. Its next clean epoch adopts defaults.
    for candidate, selected in all_routes(home, sid):
        if (selected["investigation_id"] == conn["investigation_id"] and selected["runtime"] == runtime
                and selected.get("credential_file") == identity["credential_file"]
                and selected.get("consumer_identity") in (None, identity)):
            if selected["state"] == "superseded":
                continue
            path = candidate
            break
    with c.locked(path):
        old = _read_route(home, sid, path)
        if old and old.get("automatic") and not conn.get("grant_id"):
            value["role"] = old["policy"]["role"]
        if old:
            for name in ("git", "gh", "cli_prefixes"):
                for item in old["policy"][name]:
                    if item not in value[name]:
                        value[name].append(item)
        gate_file = Path(old["gate_file"]) if old else parent / (sid + ".state-" + consumer + ".json")
        gate = engine.Gate(conn, gate_file, session=sid, client=client, home=home,
                           credential_file=credential_file, deadline=deadline)
        if old and old["state"] == "active" and not gate_file.exists():
            raise ValueError("active coordination state is missing; restore it before continuing")
        if old and old.get("consumer_identity") not in (None, identity):
            raise ValueError("coordination credentials changed; retain old obligations and use a new consumer")
        if old and gate_file.exists():
            with c.locked(gate_file):
                state = gate._load()
                if state["gate"].get("batch"):
                    # Do not rewrite pending pages, policy hashes or chunks.
                    if not state["gate"]["enabled"]:
                        state["gate"]["enabled"] = True
                        engine._save(gate_file, state)
                    old.update(enabled=True, automatic=True)
                    engine._save(path, old)
                    return old, gate
        selected = {"version": 1, "session": sid, "runtime": runtime, "enabled": True,
            "automatic": True, "consumer_identity": identity, "state": "activating",
            "investigation_id": conn["investigation_id"], "policy": value,
            "policy_hash": engine.policy_hash(value), "gate_file": str(gate_file),
            "credential_file": identity["credential_file"]}
        # Publish discovery before network activation: a crash cannot silently
        # drop a scoped consumer from later covered calls.
        if not old:
            engine._save(path, selected)
        index = index_path(home, sid)
        with c.locked(index):
            try:
                saved = c.private_read(index)
            except FileNotFoundError:
                saved = {"routes": []}
            if path != route_path(home, sid) and str(path) not in saved["routes"]:
                saved["routes"].append(str(path))
            engine._save(index, saved)
        if old and old.get("automatic") and old.get("policy_hash") == selected["policy_hash"] and old.get("state") == "active":
            # Identity was checked above. Every action still samples a fresh
            # versioned snapshot; there is no cached permit.
            with c.locked(gate_file):
                state = gate._load()
                if not state["gate"]["enabled"]:
                    state["gate"]["enabled"] = True
                    engine._save(gate_file, state)
            return old, gate
        engine._save(path, selected)
        gate.enable(value, new_epoch=gate_file.exists(), budget_file=conn.get("_coordination_budget"))
        actual = c.private_read(gate_file)["gate"]
        selected["policy"], selected["policy_hash"] = actual["policy"], actual["policy_hash"]
        selected["state"] = "active"
        engine._save(path, selected)
        return selected, gate


def prepare_intent(*, session, runtime, home, conn, client, cli, credential_file=None):
    """Prepare explicit authenticated consumers, retaining verified succession."""
    end = time.monotonic() + 10.0
    sid, hook = session, hook_path(cli, runtime)
    if runtime not in SUPPORTED or not ie.valid_session(sid):
        raise ValueError("coordination needs a valid session and adapter")
    known = [(p, r) for p, r in all_routes(home, sid)
        if r["state"] != "superseded" and r["investigation_id"] == conn["investigation_id"]]
    if any(r["runtime"] != runtime for _, r in known):
        raise ValueError("this coordination consumer belongs to another adapter")
    scoped = bool(conn.get("grant_id"))
    if scoped and credential_file is None:
        credential_file = route_path(home, sid).with_name(sid + ".scope-" + conn["grant_id"] + ".json")
        c.private_directory(credential_file.parent)
        c.private_write(credential_file, {"token": conn["token"], "origin": conn["origin"]})
    contexts = [(conn, credential_file)]
    connection = lambda: conn
    for _, selected in known:
        cred = selected.get("credential_file")
        if cred:
            credential = Path(cred)
            expected = selected.get("consumer_identity", {}).get("connection")
            ancestors = []
            for _ in range(32):
                if credential.exists():
                    break
                successor = c.private_read(credential.with_suffix(credential.suffix + ".successor.json"))
                if successor.get("old_identity") != expected:
                    raise ValueError("credential succession identity mismatch")
                ancestors.append(expected["grant_id"])
                next_identity = successor.get("new_identity") or {}
                if any(next_identity.get(k) != expected.get(k) for k in ("origin", "org", "investigation_id")):
                    raise ValueError("credential succession changed Investigation/endpoint")
                expected = next_identity
                credential = Path(successor["credential_file"])
            else:
                raise ValueError("credential succession chain is too long")
            loaded = c.credential_connection(credential)
            if ancestors and expected != c.identity(loaded):
                raise ValueError("successor credential identity differs from its explicit association")
            previous = c.private_read(Path(selected["gate_file"]))["gate"]
            old_conn = selected.get("consumer_identity", {}).get("connection", c.identity(loaded))
            if old_conn.get("grant_id") and (old_conn.get("grant_id") != loaded.get("grant_id")
                    or selected.get("consumer_identity", {}).get("credential") != engine.digest({k: loaded.get(k) for k in ("key", "token", "api_key")})):
                loaded["_coordination_budget"] = previous.get("budget_file")
                loaded["_previous_grant_ids"] = sorted(set(ancestors + [old_conn["grant_id"]]))
                loaded["_supersedes_gate_files"] = [selected["gate_file"]]
            contexts.append((loaded, credential))
        elif not scoped:
            own = connection()
            if not own or not own.get("key"):
                raise ValueError("existing author consumer lost its connection")
            contexts.append(({**own, "investigation_id": selected["investigation_id"]}, None))
    # Merge proof and pinned-budget metadata before activation. Explicit NEW
    # command order must not discard the OLD consumer's required response guard.
    grouped = {}
    for conn, credential in contexts:
        ident = engine.digest({"conn": c.identity(conn), "credential": str(credential) if credential else None,
            "secret": engine.digest({k: conn.get(k) for k in ("key", "token", "api_key")})})
        if ident not in grouped:
            grouped[ident] = (dict(conn), credential)
        target = grouped[ident][0]
        pins = {x for x in (target.get("_coordination_budget"), conn.get("_coordination_budget")) if x}
        if len(pins) > 1:
            raise ValueError("incompatible pinned supervisor response guards")
        if pins:
            target["_coordination_budget"] = next(iter(pins))
        target["_previous_grant_ids"] = sorted(set(target.get("_previous_grant_ids", [])) | set(conn.get("_previous_grant_ids", [])))
        target["_supersedes_gate_files"] = sorted(set(target.get("_supersedes_gate_files", [])) | set(conn.get("_supersedes_gate_files", [])))
    contexts = list(grouped.values())
    seen, gates = set(), []
    for conn, credential in contexts:
        ident = engine.digest({"conn": c.identity(conn), "credential": str(credential) if credential else None})
        if ident in seen:
            continue
        seen.add(ident)
        remaining = end - time.monotonic()
        if remaining <= 0.15:
            raise TimeoutError("coordination consumers are still initializing; retry sync")
        _, gate = ensure_automatic(home, sid, runtime, conn, client, hook, credential, deadline=min(1.0, remaining))
        gates.append(gate)
        if conn.get("grant_id"):
            new_state = c.private_read(gate.path)
            for old_path, old in known:
                old_identity = old.get("consumer_identity", {}).get("connection", {})
                if old_identity.get("grant_id") is None or old["gate_file"] == str(gate.path):
                    continue
                if any(old_identity.get(k) != conn.get(k) for k in ("origin", "org", "investigation_id")):
                    continue
                # Matching lineage alone is insufficient: a still-live token
                # copied to another path is another consumer, not renewal.
                if (old["gate_file"] in conn.get("_supersedes_gate_files", [])
                        and old_identity["grant_id"] in new_state["gate"].get("previous_grant_ids", [])):
                    # Preserve declared prerequisites as well as server-folded
                    # requests. Legacy requests have no intent to rediscover.
                    previous = c.private_read(Path(old["gate_file"]))
                    with c.locked(gate.path):
                        fresh = c.private_read(gate.path)
                        for key, context in previous.get("intents", {}).items():
                            target = fresh.setdefault("intents", {}).setdefault(key,
                                {"intent": context["intent"], "required": []})
                            target["required"] = sorted(set(target["required"]) | set(context["required"]))
                        engine._save(gate.path, fresh)
                    with c.locked(old_path):
                        old.update(state="superseded", successor_file=str(gate.path))
                        engine._save(old_path, old)
    checked = {str(gate.path) for gate in gates}
    for old_path, old in known:
        current = _read_route(home, sid, old_path)
        if current["state"] != "superseded" and old["gate_file"] not in checked:
            raise ValueError("a prior consumer needs its credential or authenticated successor before sync can complete")
    return gates


def dispatch(payload, **kwargs):
    """Stale installed pre-tool registrations are harmless during upgrade."""
    return None
