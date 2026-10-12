"""cardinal-checks: the Checks preview opt-in, for every adapter.

Each adapter ships a thin `cardinal-checks` launcher (Claude: bin/; Codex,
Cursor, Gemini: scripts/) that calls main(<runtime>, plugin_root=<its root>).

The user runs, in their own terminal:
    cardinal-checks on        install the checks skill (the next session uses it)
    cardinal-checks off       remove it
    cardinal-checks status    show the setting and the accepted checks

Also kept for Claude Code's Monitor and for people (the checks skill itself
uses the cardinal-behavior MCP tools, which run outside any agent sandbox):
    cardinal-checks example set|approve|remove|expected|show --draft <id> ...
    cardinal-checks accepted [--service S]
    cardinal-checks range watch|status <range_id>

The ledger and range reads are lib/behavior/checks_ledger.py, the same code
the MCP tools run; it is loaded from the plugin (lib/behavior in a release,
common/behavior in a source checkout).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import sys
from pathlib import Path
from typing import List, Optional

from . import checks_install

EXIT_USAGE = 2
EXIT_REFUSED = 3
LEDGER_MODULE = "cardinal_checks_ledger"
VERDICTS = ("MATCH", "NON_MATCH", "UNKNOWN", "NOT_APPLICABLE")


def behavior_lib(plugin_root: Path) -> Optional[Path]:
    """The behavior runtime next to this plugin: <root>/lib/behavior (a
    release), else the monorepo's common/behavior (a source checkout)."""
    root = Path(plugin_root).resolve()
    candidates = [root / "lib" / "behavior"]
    if len(root.parents) > 1:
        candidates.append(root.parents[1] / "common" / "behavior")
    for candidate in candidates:
        if (candidate / "checks_ledger.py").is_file():
            return candidate
    return None


def load_ledger(plugin_root: Path):
    """lib/behavior/checks_ledger.py as a module (one instance per process)."""
    loaded = sys.modules.get(LEDGER_MODULE)
    if loaded is not None:
        return loaded
    lib = behavior_lib(plugin_root)
    if lib is None:
        raise FileNotFoundError("the Cardinal behavior runtime (lib/behavior) is missing; reinstall the plugin")
    spec = importlib.util.spec_from_file_location(LEDGER_MODULE, str(lib / "checks_ledger.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[LEDGER_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(LEDGER_MODULE, None)
        raise
    return module


def _on_text(runtime: str) -> str:
    if runtime == "claude":
        return ("Checks preview is on. Start a new session (or run /reload-plugins) and ask a per-trace "
                "question, e.g. \"find traces where …\". Judge-based checks are experimental.")
    if runtime == "gemini":
        return ("Checks preview is on. Start a new Gemini CLI session (or type /skills reload in a running one) "
                "and ask a per-trace question, e.g. \"find traces where …\". Judge-based checks are experimental.")
    return (f"Checks preview is on. Start a new {checks_install.DISPLAY[runtime]} session (a running one does "
            "not pick the skill up) and ask a per-trace question, e.g. \"find traces where …\". "
            "Judge-based checks are experimental.")


def _off_text(runtime: str) -> str:
    if runtime == "claude":
        return ("Checks preview is off. Claude can no longer invoke the checks skill; the raw "
                "cardinal-behavior tools stay available.")
    if runtime == "gemini":
        return ("Checks preview is off. Gemini CLI can no longer activate the checks skill; a running session "
                "lists it until you type /skills reload. The raw cardinal-behavior tools stay available.")
    return (f"Checks preview is off. New {checks_install.DISPLAY[runtime]} sessions no longer have the checks "
            "skill; a session already running keeps it until it ends. The raw cardinal-behavior tools stay "
            "available.")


class Cli:
    def __init__(self, runtime: str, plugin_root: Path, home: Optional[Path] = None, out=None, err=None):
        self.runtime = runtime
        self.root = Path(plugin_root)
        self.home = home
        self.out = out or sys.stdout
        self.err = err or sys.stderr

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def fail(self, text: str) -> None:
        print(f"cardinal-checks: {text}", file=self.err)

    @property
    def dest(self) -> Optional[Path]:
        return checks_install.install_dir(self.runtime, self.root, self.home)

    @property
    def state(self) -> Path:
        return checks_install.state_dir(self.runtime, self.home)

    def ledger(self):
        return load_ledger(self.root)

    @property
    def output(self) -> Path:
        """The behavior MCP's output directory (shared by every runtime)."""
        return self.ledger().behavior_output_dir(home=self.home)

    # --- opt-in -------------------------------------------------------------

    def cmd_on(self, _args) -> int:
        checks_install.set_enabled(self.runtime, True, self.home)
        dest = self.dest
        if dest is None:
            # Gemini without Cardinal's extension: nothing loads a skill from here.
            checks_install.set_enabled(self.runtime, False, self.home)
            self.fail("the checks skill needs Cardinal's Gemini CLI extension; run cardinal-connect "
                      "(without --no-extension), then cardinal-checks on again")
            return 1
        try:
            checks_install.install(self.root, dest)
        except OSError as err:
            checks_install.set_enabled(self.runtime, False, self.home)
            self.fail(f"could not install the checks skill: {err}")
            return 1
        self.say(_on_text(self.runtime))
        self._registration_advice()
        return 0

    def _registration_advice(self) -> bool:
        """Say how to register cardinal-behavior when the runtime's config
        lacks it (an upgrade from before the Checks preview). True when
        registered."""
        if checks_install.behavior_registered(self.runtime, self.home):
            return True
        display = checks_install.DISPLAY[self.runtime]
        connect = Path(self.root) / "scripts" / "cardinal-connect"
        if not checks_install.is_connected(self.runtime, self.home):
            self.say(f"Not ready: Cardinal is not connected for {display}, so the cardinal-behavior MCP server "
                     "the checks skill uses is not registered. Connect, then restart "
                     f"{display}:\n  python3 {shlex.quote(str(connect))}")
            return False
        self.say(f"Not ready: the cardinal-behavior MCP server the checks skill uses is not registered for "
                 f"{display} (a connection made before the Checks preview). Register it (no new key), then "
                 f"restart {display}:\n  {checks_install.repair_command(self.runtime, self.root)}")
        return False

    def cmd_off(self, _args) -> int:
        checks_install.set_enabled(self.runtime, False, self.home)
        try:
            checks_install.uninstall(self.dest)
        except OSError as err:
            self.fail(f"preview is off, but {err}")
            return 1
        self.say(_off_text(self.runtime))
        return 0

    def cmd_status(self, _args) -> int:
        enabled = checks_install.is_enabled(self.runtime, self.home)
        dest = self.dest
        installed = checks_install.is_installed(dest)
        self.say(f"Checks preview: {'on' if enabled else 'off'}")
        self.say(f"Checks skill installed: {'yes' if installed else 'no'} ({dest or 'no skills directory'})")
        if enabled != installed:
            self.say("The skill and the setting disagree; the next session start fixes it.")
        if self.runtime != "claude":
            registered = checks_install.behavior_registered(self.runtime, self.home)
            self.say(f"cardinal-behavior MCP server: {'registered' if registered else 'not registered'}")
            if not registered:
                self._registration_advice()
        try:
            checks = self.ledger().accepted_checks(self.output)
        except (OSError, ImportError) as err:
            self.say(f"Accepted checks: unavailable ({err})")
            return 0
        self.say(f"Accepted checks: {len(checks)}")
        for check in checks[:20]:
            self.say(f"  {check['service_name']}: {check['description']} ({(check['diagnostic_version'] or '')[:12]})")
        return 0

    # --- ledger and reads (the same code as the MCP tools) -------------------

    def cmd_accepted(self, args) -> int:
        self.say(json.dumps(self.ledger().accepted_checks(self.output, service=args.service), indent=2))
        return 0

    def cmd_range(self, args) -> int:
        ledger = self.ledger()
        try:
            if args.action == "watch":
                return ledger.watch_range(args.range_id, self.out, output=self.output)
            self.say(json.dumps(ledger.range_status(args.range_id, self.output), indent=2))
            return 0
        except (ValueError, OSError) as err:
            self.fail(str(err))
            return EXIT_USAGE

    def _print(self, obj) -> None:
        self.say(json.dumps(obj, indent=2, sort_keys=True))

    def cmd_example(self, args) -> int:
        ledger = self.ledger()
        state = self.state
        try:
            if args.action == "set":
                if args.trace is None or args.evidence is None or args.proposed is None:
                    raise ValueError("set needs --trace, --evidence and --proposed")
                self._print(ledger.set_example(args.draft, args.trace, args.evidence == "sufficient", args.proposed,
                                               args.reason or "", args.missing, state=state))
            elif args.action == "approve":
                if args.trace is None:
                    raise ValueError("approve needs --trace")
                self._print(ledger.approve(args.draft, args.trace, args.label, state=state, approved_via="cli",
                                           user_reply=args.user_reply))
            elif args.action == "remove":
                if args.trace is None:
                    raise ValueError("remove needs --trace")
                self._print({"removed": ledger.remove_example(args.draft, args.trace, state=state)})
            elif args.action == "expected":
                self._print(ledger.expected_verdicts(args.draft, state=state))
            else:
                self._print(ledger.read_draft(args.draft, state))
        except ledger.LabelRefused as err:
            self.fail(str(err))
            return EXIT_REFUSED
        except ValueError as err:
            self.fail(str(err))
            return EXIT_USAGE
        return 0


def build_parser(cli: Cli) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cardinal-checks",
                                     description="Checks preview opt-in and teaching-label ledger.")
    sub = parser.add_subparsers(dest="command", metavar="{on,off,status}")
    sub.add_parser("on", help="turn the Checks preview on").set_defaults(func=cli.cmd_on)
    sub.add_parser("off", help="turn the Checks preview off").set_defaults(func=cli.cmd_off)
    sub.add_parser("status", help="show the setting and accepted checks").set_defaults(func=cli.cmd_status)

    accepted = sub.add_parser("accepted", help=argparse.SUPPRESS)
    accepted.add_argument("--service")
    accepted.set_defaults(func=cli.cmd_accepted)

    rng = sub.add_parser("range", help=argparse.SUPPRESS)
    rng.add_argument("action", choices=("watch", "status"))
    rng.add_argument("range_id")
    rng.set_defaults(func=cli.cmd_range)

    example = sub.add_parser("example", help=argparse.SUPPRESS)
    example.add_argument("action", choices=("set", "approve", "remove", "expected", "show"))
    example.add_argument("--draft", required=True, help="the check being built")
    example.add_argument("--trace", help="teaching trace id")
    example.add_argument("--evidence", choices=("sufficient", "insufficient"),
                         help="the mechanical evidence-sufficiency result")
    example.add_argument("--missing", help="the missing evidence (insufficient only)")
    example.add_argument("--proposed", choices=VERDICTS, help="the proposed label")
    example.add_argument("--reason", help="why, under the approved contract")
    example.add_argument("--label", choices=VERDICTS, help="the user's correction (approve)")
    example.add_argument("--user-reply", help="the user's reply, quoted (approve)")
    example.set_defaults(func=cli.cmd_example)
    return parser


def main(runtime: str, argv: Optional[List[str]] = None, *, plugin_root: Path,
         home: Optional[Path] = None) -> int:
    if runtime not in checks_install.RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}")
    cli = Cli(runtime, plugin_root, home)
    parser = build_parser(cli)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except (FileNotFoundError, ImportError) as err:
        cli.fail(str(err))
        return 1
