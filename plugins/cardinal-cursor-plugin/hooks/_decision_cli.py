"""The `cardinal-decision record` argv contract, shared by the CLI
(scripts/cardinal-decision) and the postToolUse recorder
(hooks/cardinal-cursor-telemetry.py).

The CLI parses its argv with `build_parser()` and prints
`MARKER_PREFIX + json(build_marker(...))`. The hook re-parses the argv of
the shell command the agent actually ran with the SAME parser and builds
the same marker; a marker line in the command output is accepted only
when it equals that argv-derived marker (ignoring `cwd`, which only the
running CLI knows). So the decision content always comes from the real
invocation, and the printed line only confirms the CLI ran and validated.

Stdlib + cardinal_core only; no filesystem or network access (the CLI
imports this inside Cursor's agent sandbox).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from typing import Any

from cardinal_core import decisions

MARKER_PREFIX = "cardinal-decision-record:v1 "
MAX_ANCHOR_SPEC = 500


class ArgvError(ValueError):
    """The argv is not a valid `cardinal-decision record` invocation."""


class _RaisingParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ArgvError(message)


def build_parser(raising: bool = False) -> argparse.ArgumentParser:
    cls = _RaisingParser if raising else argparse.ArgumentParser
    parser = cls(
        prog="cardinal-decision",
        description="Record the decisions an agent makes, tagged for the Cardinal Agent Outcomes dashboard.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{record,on,off,status}", parser_class=cls)

    record = sub.add_parser(
        "record",
        help="(agent) submit one decision; side-effect free, recorded by the Cardinal hook",
    )
    record.add_argument("--session", help="optional; the hook uses the Cursor conversation id")
    record.add_argument("--choice", required=True, help="the option chosen, in a few words")
    record.add_argument("--question", help="the question this decision settles")
    record.add_argument("--why", help="one sentence on why this option won")
    record.add_argument("--alt", action="append", default=[], metavar="OPTION",
                        help="an option that was considered and rejected (repeatable)")
    record.add_argument("--by", choices=decisions.DECIDED_BY, default="agent",
                        help="who made the call (default: agent)")
    record.add_argument("--anchor", action="append", default=[], metavar="ANCHOR",
                        help="file, dir/, file::Symbol, or <kind>:<identifier>[@path] the decision governs (repeatable)")
    record.add_argument("--follows", action="append", default=[], metavar="ID",
                        help="an earlier decision this one only makes sense because of")
    record.add_argument("--refines", action="append", default=[], metavar="ID",
                        help="an earlier decision this one narrows")
    record.add_argument("--supersedes", action="append", default=[], metavar="ID",
                        help="an earlier decision this one replaces")
    record.add_argument("--id", help="decision id; reuse an existing id to revise that decision")

    sub.add_parser("on", help="(user, own terminal) turn decision capture on")
    sub.add_parser("off", help="(user, own terminal) turn decision capture off")
    status = sub.add_parser("status", help="(user, own terminal) show whether capture is on")
    status.add_argument("--session", help="also list this session's decisions")
    return parser


def build_marker(args: argparse.Namespace, cwd: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(marker, validated decision) for parsed `record` args. Raises
    decisions.DecisionError on invalid content. Validation only: the hook
    re-parses anchors against the repo root and derives the final id
    against the session ledger."""
    anchor_specs = [spec[:MAX_ANCHOR_SPEC] for spec in args.anchor][: decisions.MAX_ANCHORS]
    for spec in anchor_specs:
        decisions.parse_anchor(spec, None, cwd)
    decision = decisions.build_decision(
        choice=args.choice,
        question=args.question,
        rationale=args.why,
        decided_by=args.by,
        alternatives=args.alt,
        follows_from=args.follows,
        refines=args.refines,
        supersedes=args.supersedes,
        decision_id=args.id,
    )

    def ids(relation: str) -> list[str]:
        return [link["to"] for link in decision["links"] if link["relation"] == relation]

    marker = {
        "v": 1,
        "session": args.session,
        "cwd": cwd,
        "id": decision["id"] if args.id else None,
        "choice": decision["choice"],
        "question": decision["question"],
        "why": decision["rationale"],
        "alt": decision["alternatives"],
        "by": decision["decided_by"],
        "anchor": anchor_specs,
        "follows": ids("follows_from"),
        "refines": ids("refines"),
        "supersedes": ids("supersedes"),
    }
    return marker, decision


def marker_line(marker: dict[str, Any]) -> str:
    return MARKER_PREFIX + json.dumps(marker, separators=(",", ":"), ensure_ascii=True)


def expected_marker(argv: list[str]) -> dict[str, Any]:
    """The marker a `cardinal-decision <argv>` run would print (cwd blank).
    Raises ArgvError / decisions.DecisionError exactly where the CLI
    would refuse to print one. Never writes to the caller's stdio."""
    parser = build_parser(raising=True)
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            args = parser.parse_args(argv)
    except SystemExit:
        raise ArgvError("invalid arguments") from None
    if args.command != "record":
        raise ArgvError("not a record invocation")
    return build_marker(args, "")[0]


def comparable(marker: dict[str, Any]) -> dict[str, Any]:
    """Marker fields that must match between argv and output."""
    return {key: value for key, value in marker.items() if key != "cwd"}
