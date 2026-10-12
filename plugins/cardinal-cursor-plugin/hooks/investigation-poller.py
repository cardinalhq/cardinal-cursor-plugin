#!/usr/bin/env python3
"""Detached Cursor Investigation inbox/bootstrap worker; never reads transcripts."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cardinal_core import investigation_poller as poller
from cardinal_core.paths import AgentPaths
from cardinal_core.storyboard_agent import Wiring
from _plugin_version import plugin_version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--anchor", type=int)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    wiring = Wiring("cursor", AgentPaths(home=Path.home() / ".cursor"), plugin_version())
    anchor = poller.resolve_anchor(args.anchor) if args.anchor and args.anchor > 1 else None
    poller.run(Path.home(), args.session, connection=wiring.connection, client=wiring.client,
               anchor=anchor, connected=lambda: bool(wiring.connection()), once=args.once)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    os._exit(0)
