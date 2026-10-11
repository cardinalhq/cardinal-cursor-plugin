"""Resolve local capture IDs and capability-gated Cardinal receipt reads."""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from . import evidence
from . import evidence_promote
from . import investigation_events as ie
from . import investigation_consumer as consumer
from . import investigation_grants as grants
from . import investigation_state as state
from . import investigation_state_sync as sync


def rows_arg(value):
    try:
        offset, limit = map(int, value.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError("--rows is offset:limit")
    if offset < 0 or not 1 <= limit <= 200:
        raise argparse.ArgumentTypeError("--rows needs offset >= 0 and limit 1..200")
    return {"offset": offset, "limit": limit}


def local(root, ev_id, *, outline=False, rows=None):
    entry = evidence.read_entry(root, ev_id)
    if entry is None:
        raise ValueError(f"{ev_id} is not in the spool on this machine")
    promotions = {
        target: records[ev_id]
        for target, records in evidence.read_promoted(root, entry.get("session_id")).items()
        if ev_id in records
    }
    result = {"evidence_id": ev_id, "entry": entry, "receipts": promotions}
    if outline:
        result["outline"] = evidence_promote._outline(entry.get("result"))
        result["entry"] = {k: v for k, v in entry.items() if k != "result"}
    elif rows:
        value = entry.get("result")
        # Spool results are normalized objects; text-only rows are lines.
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            value = value["text"].splitlines()
        elif isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            raise ValueError("--rows needs an array or text result; use --outline to inspect its shape")
        result["entry"] = {k: v for k, v in entry.items() if k != "result"}
        result["rows"] = [
            {"index": i, "value": v}
            for i, v in enumerate(value)
            if rows["offset"] <= i < rows["offset"] + rows["limit"]
        ]
    return result


def latest_binding(home, org):
    """Standalone shell receipt reads use the latest binding for this org.

    This is feature detection only; the connection still determines authority.
    Auxiliary inbox/failure files are never candidates.
    """
    directory = ie.sessions_dir(home)
    candidates = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if ie.valid_session(candidate.stem):
            binding = ie.read_binding(home, candidate.stem)
            if binding and binding.get("org") == org:
                return binding
    return None


def cli_main(argv, wiring):
    p = argparse.ArgumentParser(prog="cardinal-storyboard receipt")
    p.add_argument("receipt_id", help="rcpt_ receipt or locally captured ev_ id")
    p.add_argument("--rows", type=rows_arg)
    p.add_argument("--outline", action="store_true")
    p.add_argument("--pointer")
    p.add_argument("--credential-file", type=Path)
    p.add_argument("--investigation")
    p.add_argument(
        "--session",
        default=next(
            (
                wiring.environ.get(k)
                for k in (
                    "CODEX_SESSION_ID",
                    "OPENAI_CODEX_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_SESSION_ID",
                    "CURSOR_SESSION_ID",
                    "GEMINI_SESSION_ID",
                )
                if wiring.environ.get(k)
            ),
            None,
        ),
    )
    args = p.parse_args(argv)
    home = wiring.paths.home.parent
    try:
        if ie.CAPTURED_REF_RE.fullmatch(args.receipt_id):
            out = local(evidence.default_root(home), args.receipt_id, outline=args.outline, rows=args.rows)
        elif ie.RECEIPT_REF_RE.fullmatch(args.receipt_id):
            scoped = args.credential_file is not None or grants.token_set(wiring.environ)
            if scoped:
                conn = (
                    consumer.credential_connection(args.credential_file)
                    if args.credential_file
                    else grants.token_connection(wiring.environ)
                )
                inv = conn["investigation_id"]
                if args.investigation and args.investigation != inv:
                    raise ValueError("credential belongs to another investigation")
                if grants.EVIDENCE_READ not in conn.get("scopes", []):
                    raise ValueError("receipt reads require an evidence:read grant (and read)")
                binding = sync.get_investigation(conn, inv, client=wiring.client)
            else:
                conn = wiring.connection()
                binding = ie.read_binding(home, args.session) if ie.valid_session(args.session) else None
                if binding and binding.get("org") != conn.get("org"):
                    binding = None
                if binding is None and not args.investigation:
                    binding = latest_binding(home, conn.get("org"))
                inv = args.investigation or (binding or {}).get("investigation_id")
                if args.investigation and (not binding or binding.get("investigation_id") != inv):
                    binding = sync.get_investigation(conn, inv, client=wiring.client)
            if not binding:
                raise ValueError("no investigation binding for this session; pass --org/--session or run link")
            if not ie.capability(binding, "receipt_read"):
                raise ValueError(
                    "receipt reads aren't enabled on this Cardinal server; it needs a newer Maestro"
                )
            payload = {"receipt_id": args.receipt_id}
            if scoped:
                payload["investigation_id"] = inv
            if args.rows:
                payload["rows"] = args.rows
            if args.outline:
                payload["outline"] = True
            if args.pointer:
                payload["pointer"] = args.pointer
            out = sync._post(conn, "get-receipt", payload, client=wiring.client)
        else:
            raise ValueError("expected rcpt_ + 24 hex or ev_ + 12 hex")
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, state.FetchError, sync.ServerError) as e:
        print(ie._one_line(e, 1000), file=sys.stderr)
        return 1
