"""Shared Investigation CLI. Claude's original presentation is a compatibility mode."""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from . import investigation_events as ie
from . import investigation_state as ist
from . import investigation_state_sync as sync
from . import investigation_bootstrap as boot
from . import investigation_consumer as consumer
from . import investigation_grants as gr
from . import investigation_text
from . import owner_relay

EXIT_USAGE = 2

CHECKPOINT_TIMING_ENV = "CARDINAL_CHECKPOINT_TIMING"
CHECKPOINT_HELP = """\
A JSON array of 1-20 events on stdin, appended to this session's Investigation atomically.
Each {"type", "id", ...fields} is your claim (not a fact), never reasoning. required; optional:
  hypothesis.proposed   hyp_*       statement; evidence, refs
  hypothesis.resolved   hyp_*       outcome supported|contradicted|inconclusive; statement, evidence, refs
  experiment.started    exp_*       statement; tests [hyp_*], refs
  experiment.completed  exp_*       outcome; evidence, refs
  experiment.revised    exp_*       outcome; evidence, refs, artifacts, gate
  finding.proposed      finding_*   statement; evidence, refs
  finding.revised       finding_*   statement; evidence, refs
  finding.retracted     finding_*   reason; evidence, refs
  decision.proposed     decision_*  statement; based_on [finding_*|hyp_*|exp_*], evidence, refs
  decision.revised      decision_*  statement; based_on, evidence, refs
  question.opened       question_*  statement; refs
  question.resolved     question_*  answer; evidence, refs
evidence: rcpt_ / ev_ ids (a cited ev_ is uploaded as an Investigation receipt first).
refs: earlier ids or "#<seq>". An id stays the same in its later events. Text <= 2000 chars. artifacts/gate: findings, results, resolutions and decisions (capability-gated)."""


BUDGET_HELP = """\
The supervisor's plan for the worker's current direction and its step-back trigger (supervise
skill, "Step backs"). The worker never sees it. Records go to your own investigation (--session);
--budget-file is their cache.
  --set --plan F --question Q --worker-session W --session S [--since-seq N]   a new direction
  --set --amend --plan F                                                      re-plan its milestones
  pass estimator|stepback --brief F [--milestone M|all]                       write a fresh pass's brief copy
  verdict M|opening|all on_track|worker_choice|owner_constraint [--post-ref "#N"]
  outcome M|opening|all accepted|declined|overruled|changed_constraint|no_response [--ref R]
  --show, calibrate, --close --outcome done|stopped|replaced [--missed M,...]
Each review pass tags and marks on `reviewed` (--tag, --done, --reopen, --redone).
Tags charge the preceding agent-active interval, not wall-clock soak duration. Before a soak
starts use a preparation/reporting milestone or explicit none; only current-page worker seqs
can be tagged. Recorded attribution is not silently retagged."""


class CLI:
    def __init__(self, wiring, args, legacy=False):
        self.wiring, self.args, self.legacy = wiring, args, legacy
        self.scoped = args.credential_file is not None or gr.token_set(wiring.environ)
        self.plugin_root = Path(wiring.cli).parent.parent

    def connection(self):
        return self.wiring.connection(), self.wiring.client

    def client(self):
        return self.wiring.client

    def refused(self, err, what):
        return (
            sync.INVESTIGATIONS_UNSUPPORTED
            if sync.investigations_unsupported(err)
            else f"{what}: {sync.plain(err)}"
        )

    def cmd_create(self, args: argparse.Namespace) -> int:
        window = None
        if args.window is not None:
            try:
                window = json.loads(args.window)
            except ValueError as e:
                sys.stderr.write(f"--window is not JSON: {e}\n")
                return EXIT_USAGE
        session_id = args.session
        conn, client = self.connection()
        try:
            out = sync.create_investigation(conn, args.question, window, client=client, session_id=session_id)
        except sync.ServerError as e:
            sys.stderr.write(self.refused(e, "investigation not created") + "\n")
            return 1
        except ist.FetchError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        inv_id = out["investigation_id"]
        sys.stdout.write(f"{inv_id}\n")
        if session_id:
            try:
                from cardinal_core import investigation_events as ie

                ie.bind(self._home(), session_id, inv_id, "create")
                sys.stderr.write(
                    f"bound session {session_id} to it: advisory events reach it at its next tool boundary\n"
                )
            except (OSError, ValueError) as e:
                sys.stderr.write(f"note: session {session_id} not bound to it ({e})\n")
        sys.stderr.write(f"next: cardinal-storyboard state init --investigation {inv_id}\n")
        return 0

    def cmd_attach(self, args: argparse.Namespace) -> int:
        conn, client = self.connection()
        try:
            out = sync.attach_storyboard(conn, args.investigation_id, args.storyboard_id, client=client)
        except sync.ServerError as e:
            sys.stderr.write(self.refused(e, "not attached") + "\n")
            return 1
        except ist.FetchError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        sys.stdout.write(
            f"attached {args.storyboard_id} to {args.investigation_id}. The state's next publish must reflect it: cardinal-storyboard state init --investigation {args.investigation_id} --refresh [--out PATH] keeps everything authored.\n"
        )
        if isinstance(out.get("note"), str) and out["note"]:
            sys.stdout.write(f"Cardinal: {out['note']}\n")
        return 0

    def cmd_show(self, args: argparse.Namespace) -> int:

        got = self._target(args)
        if got is None:
            return EXIT_USAGE
        conn, client, inv_id = got
        try:
            inv = sync.get_investigation(conn, inv_id, client=client)
        except sync.ServerError as e:
            sys.stderr.write(self.refused(e, f"cannot read investigation {inv_id}") + "\n")
            return 1
        except ist.FetchError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        sys.stdout.write(json.dumps(inv, indent=2, ensure_ascii=False) + "\n")
        return 0

    def _home(self) -> Path:
        return self.wiring.paths.home.parent

    def _session_arg(self, args):
        sid = args.session
        if not ie.valid_session(sid):
            print(
                (
                    "needs --session SID (an agent session id: letters, digits, _ and -; at most 128)"
                    if not sid
                    else f"not a session id: {sid!r}"
                ),
                file=sys.stderr,
            )
            return None
        return sid

    def _investigation_arg(self, inv_id: str) -> bool:

        if ie.valid_investigation(inv_id):
            return True
        sys.stderr.write(f"not an investigation id: {inv_id!r} (inv_ followed by 24 hex characters)\n")
        return False

    def _grantee(self):
        if not self.scoped:
            return None, None
        # No machine-key fallback, including invalid explicit grant files.
        conn = (
            consumer.credential_connection(self.args.credential_file)
            if self.args.credential_file
            else gr.token_connection(self.wiring.environ)
        )
        return conn, self.client()

    def _target(self, args):
        conn, client = self._grantee()
        inv = args.investigation_id or args.investigation
        if conn is not None:
            if inv is not None and inv != conn["investigation_id"]:
                print(
                    f"CARDINAL_INVESTIGATION_TOKEN grants access to {conn['investigation_id']}, not {inv!r}",
                    file=sys.stderr,
                )
                return None
            return conn, client, conn["investigation_id"]
        if inv is None:
            inv = self._bound_investigation(args)
            if inv is None:
                return None
        if not self._investigation_arg(inv):
            return None
        conn, client = self.connection()
        return conn, client, inv

    def _author_only(self, what):
        if self.scoped:
            print(
                f"{what}: cannot act as the author with a scoped credential; CARDINAL_INVESTIGATION_TOKEN "
                "is set, so this shell acts as a grantee; only the investigation's author can do this",
                file=sys.stderr,
            )
            return False
        return True

    def _bound_investigation(self, args: argparse.Namespace):
        """--investigation, else the bound investigation of --session / this
        session; None (message printed) when neither."""

        inv_id = getattr(args, "investigation", None)
        if inv_id is not None:
            return inv_id if self._investigation_arg(inv_id) else None
        sid = self._session_arg(args)
        if not sid:
            return None
        b = ie.read_binding(self._home(), sid)
        if not b:
            sys.stderr.write(
                f"session {sid} has no investigation yet: run `cardinal-storyboard investigation link`, or pass --investigation ID\n"
            )
            return None
        return b["investigation_id"]

    def cmd_bind(self, args: argparse.Namespace) -> int:

        sid = self._session_arg(args)
        if args.investigation_id is None:
            args.investigation_id = self._bound_investigation(args)
        if not sid or not self._investigation_arg(args.investigation_id):
            return EXIT_USAGE
        conn, client = self.connection()
        try:
            ie.read_events(conn, args.investigation_id, after=0, limit=1, to_session_id=sid, client=client)
        except sync.ServerError as e:
            sys.stderr.write(f"not bound: {ie.plain(e)}\n")
            return 1
        except ist.FetchError as e:
            sys.stderr.write(
                f"note: could not reach Cardinal to check the investigation ({e}); binding anyway\n"
            )
        try:
            binding, created = ie.bind(self._home(), sid, args.investigation_id, "cli")
        except (OSError, ValueError) as e:
            sys.stderr.write(f"not bound: {e}\n")
            return 1
        sys.stdout.write(
            f"{('bound' if created else 'already bound')}: session {sid} -> {args.investigation_id} (cursor {binding['cursor']}). Advisory events reach it at its next tool boundary.\n"
        )
        return 0

    def cmd_ack(self, args: argparse.Namespace) -> int:

        if not self._author_only("not acknowledged"):
            return EXIT_USAGE
        sid = self._session_arg(args)
        if args.investigation_id is None:
            args.investigation_id = self._bound_investigation(args)
        if not sid or not self._investigation_arg(args.investigation_id):
            return EXIT_USAGE
        conn, client = self.connection()
        try:
            payload = ie.ack_payload(args.seq, args.disposition, args.note, args.conflicts_with)
        except ist.FetchError as err:
            print("not acknowledged: " + str(err), file=sys.stderr)
            return EXIT_USAGE
        # The server's rules for a challenge / directive, checked first when
        # this session received the target (it is then in its binding).
        binding = ie.read_binding(self._home(), sid)
        if binding and binding.get("investigation_id") != args.investigation_id:
            binding = None
        why = None
        if binding and ie.capability(binding, ie.SUPERVISION_CAPABILITY):
            target = ie.tracked_event(binding, args.seq)
            why = ie.ack_rule(target, args.disposition, args.note, ie.tracked_status(binding, target))
        if why:
            print("not acknowledged: " + why, file=sys.stderr)
            return EXIT_USAGE
        try:
            out = ie.append_event(
                conn,
                args.investigation_id,
                ie.ACKNOWLEDGED,
                payload,
                idempotency_key=ie.ack_key(sid, args.seq),
                session_id=sid,
                client=client,
                producer_client=self.client(),
            )
        except sync.ServerError as e:
            if e.body.get("error") == "idempotency_conflict":
                sys.stderr.write(
                    f"event #{args.seq} was already acknowledged from session {sid} with another disposition or note; an acknowledgment is final\n"
                )
            else:
                sys.stderr.write(f"not acknowledged: {ie.plain(e)}\n")
            return 1
        except ist.FetchError as e:
            return not_recorded(e)
        ie.record_supervision(self._home(), sid, [out["event"]])
        try:
            ie.remove_pending_decision(self._home(), sid, args.seq)
        except OSError as err:
            print(
                "ack recorded; local pending state could not be cleared: " + ie._one_line(err),
                file=sys.stderr,
            )
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        ev = out["event"]
        if out.get("duplicate"):
            sys.stdout.write(
                f"event #{args.seq} was already acknowledged ({args.disposition}) as event #{ev['seq']}\n"
            )
        else:
            sys.stdout.write(f"acknowledged event #{args.seq} ({args.disposition}) as event #{ev['seq']}\n")
        if args.disposition == "accepted" and ie.tracked_event(binding, args.seq) is not None:
            sys.stdout.write(f"When it is done, checkpoint with refs [\"#{args.seq}\"]; until then it is shown again "
                             "every few tool calls.\n")
        return 0

    def cmd_rule(self, args: argparse.Namespace) -> int:
        """Supervisor (a supervise grant's token): one ruling on its own
        declined challenge: --uphold (a binding directive) or --withdraw."""
        got = self._target(args) if self.scoped else None
        conn, client, inv = got if got is not None else ({}, None, None)
        if not self.scoped or (got is not None and not conn.get("token")):
            print("not ruled: a ruling needs a supervise grant's token (CARDINAL_INVESTIGATION_TOKEN or "
                  "--credential-file); the investigation's author mints one with `cardinal-storyboard investigation "
                  "grant --scope supervise`", file=sys.stderr)
            return EXIT_USAGE
        if got is None:
            return EXIT_USAGE
        scopes = conn.get("scopes") if isinstance(conn.get("scopes"), list) else None
        if scopes and gr.SUPERVISE not in scopes:
            print(f"not ruled: this grant's scopes are {','.join(ie._tok(x) for x in scopes)}, without supervise; "
                  "ask the investigation's author for `investigation grant --scope supervise`", file=sys.stderr)
            return EXIT_USAGE
        outcome = "upheld" if args.uphold else "withdrawn"
        try:
            out = gr.issue_ruling(conn, inv, args.challenge_seq, outcome, args.text, refs=args.ref or None,
                                  client=client)
        except sync.ServerError as e:
            # An older Maestro: no route (404), or a grant token refused on a
            # route it does not know (401 / 403). Ask what it advertises
            # before blaming the grant.
            if gr.ruling_unsupported(e) or (
                e.status in (401, 403)
                and gr.supports_rulings(conn, inv, client=client) is False
            ):
                print(gr.RULING_UNSUPPORTED, file=sys.stderr)
                return 1
            if e.body.get("error") == "idempotency_conflict":
                print(f"not ruled: you already ruled on challenge #{args.challenge_seq} with another outcome or text; "
                      "a challenge is ruled on once", file=sys.stderr)
                return 1
            print("not ruled: " + ie.plain(e), file=sys.stderr)
            return 1
        except ist.FetchError as e:
            if getattr(e, "network", False):
                return not_recorded(e)
            print("not ruled: " + ie._one_line(e, 1000), file=sys.stderr)
            return EXIT_USAGE
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        ev = out["event"]
        said = "already ruled" if out.get("duplicate") else "ruled"
        what = ("upheld: a binding directive the worker must accept (or answer `conflicts`, citing its owner's "
                "words)" if outcome == "upheld" else "withdrawn: the challenge is closed")
        print(f"{said} on challenge #{args.challenge_seq} as event #{ev['seq']} — {what}")
        return 0

    def _print_link(self, b: dict, as_json: bool) -> None:

        f = boot.describe(b)
        if as_json:
            sys.stdout.write(
                json.dumps(
                    {
                        k: f[k]
                        for k in (
                            "investigation_id",
                            "storyboard_id",
                            "view_url",
                            "investigation_url",
                            "is_author",
                        )
                    },
                    indent=2,
                )
                + "\n"
            )
            return
        links = investigation_text.link_lines(f["view_url"], f["investigation_url"])
        if links:
            sys.stdout.write(links + "\n")
        sys.stdout.write(
            f"investigation {f['investigation_id']}"
            + (f" · storyboard {f['storyboard_id']}" if f["storyboard_id"] else " · no storyboard attached")
            + ("" if f["is_author"] else " · joined: someone else's investigation (not the author)")
            + "\n"
        )

    def cmd_repos(self, args: argparse.Namespace) -> int:
        """Replay saved repositories and explicitly attach one or more checkouts."""
        from . import session_repos
        sid = self._session_arg(args)
        if not sid:
            return EXIT_USAGE
        home, runtime = self._home(), self.wiring.runtime
        if session_repos.disabled(home, runtime, self.wiring.environ):
            raise ValueError("repository context collection is disabled")
        conn, client = self.connection()
        b = ie.read_binding(home, sid)
        if not b or b.get("is_author") is False or b.get("org") != conn.get("org"):
            raise ValueError("needs this author's bound session in the connected organization")
        session_repos.replay(home, sid, runtime, client, env=self.wiring.environ)
        for directory in args.cwd:
            context = self.wiring.collect(directory, sid, gh=False)
            if not context.get("repo"):
                raise ValueError(f"no repository origin found in {directory}")
            session_repos.stage(home, sid, context, runtime=runtime, env=self.wiring.environ)
        contexts = session_repos.pending(home, sid, conn.get("org"))
        # An explicit backfill always retries, including when a server was upgraded.
        boot.refresh_path(home, sid).touch()
        status = boot.refresh_capabilities(home, sid, conn, client)
        if status != "ok":
            raise ValueError(f"repository sync unavailable ({status})")
        b = ie.read_binding(home, sid) or {}
        supported = b.get("capabilities", {}).get("repository_provenance", {}).get("enabled") is True
        out = {"repos": [c["repo"] for c in contexts], "status": "submitted", "multi_repo_supported": supported}
        print(json.dumps(out) if args.json else "Repository labels submitted: " + ", ".join(out["repos"]))
        if not supported:
            print("This server supports only the original repo context; deploy repository provenance support for multiple repo labels.", file=sys.stderr)
        return 0

    def cmd_link(self, args: argparse.Namespace) -> int:

        sid = self._session_arg(args)
        if not sid:
            return EXIT_USAGE
        home = self._home()
        b = ie.read_binding(home, sid)
        conn, client = self.connection()
        wanted = None if self.legacy else self.wiring.environ.get("CARDINAL_INVESTIGATION_ID") or None
        if wanted and not ie.valid_investigation(wanted):
            raise ValueError("invalid CARDINAL_INVESTIGATION_ID")
        ok = b is not None and isinstance(b.get("bootstrap"), dict) and (b["bootstrap"].get("status") == "ok")
        if not self.legacy or not ok or (b.get("storyboard_id") and (not b.get("view_url"))):
            res = boot.ensure(home, sid, conn, client, wanted=wanted, timeout=15.0, force=True)
            b = res.get("binding") or b
            if res["status"] == "invalid":
                sys.stderr.write("not connected to Cardinal (run /cardinal:connect)\n")
                if not b:
                    return 1
            elif res["status"] == "unsupported":
                sys.stderr.write(
                    "this Cardinal server does not create session investigations yet (it needs a newer Maestro)\n"
                )
                if not b:
                    return 1
            elif res["status"] in ("failed", "busy") and (not (b and b.get("view_url"))):
                sys.stderr.write(
                    f"Cardinal could not set up this session's investigation: {res.get('error') or 'another process is doing it; try again in a moment'}\n"
                )
                if not b:
                    return 1
            else:
                try:
                    import subprocess
                    from cardinal_core import investigation_poller as ip

                    if (
                        (self.plugin_root / "hooks" / "investigation-poller.py").is_file()
                        and self.wiring.environ.get("CARDINAL_INVESTIGATION_POLLER", "").strip() != "0"
                        and not ip.running(home, sid)
                    ):
                        subprocess.Popen(
                            [
                                sys.executable,
                                str(self.plugin_root / "hooks" / "investigation-poller.py"),
                                "--session",
                                sid,
                                "--anchor",
                                str(os.getppid()),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            close_fds=True,
                            start_new_session=True,
                        )
                except Exception:
                    pass
        if b and b.get("org") == conn.get("org"):
            if self.legacy and ok:
                # Claude's unchanged parity gate keeps cached link network-free.
                # The existing poller consumes this durable refresh marker;
                # client-refused writes refresh immediately even without a poller.
                boot.refresh_path(home, sid).touch()
            else:
                ie.refresh_binding_capabilities(home, sid, conn, client)
                b = ie.read_binding(home, sid) or b
        if not self.legacy and (
            not b or b.get("org") != conn.get("org") or (wanted and b.get("investigation_id") != wanted)
        ):
            print(
                "session has no Investigation for this connection; run investigation link --session SID",
                file=sys.stderr,
            )
            return 1
        self._print_link(b, args.json)
        return 0

    def cmd_question(self, args: argparse.Namespace) -> int:

        if not self._author_only("question not set"):
            return EXIT_USAGE
        inv_id = args.investigation
        sid = args.session
        if inv_id is None:
            sid = self._session_arg(args)
            if not sid:
                return EXIT_USAGE
            b = ie.read_binding(self._home(), sid)
            if not b:
                sys.stderr.write(
                    f"session {sid} has no investigation yet: run `cardinal-storyboard investigation link`, or pass --investigation ID\n"
                )
                return 1
            inv_id = b["investigation_id"]
        elif not self._investigation_arg(inv_id):
            return EXIT_USAGE
        if not args.text.strip() or len(args.text) > sync.MAX_INVESTIGATION_QUESTION:
            sys.stderr.write(
                f"question not set: the question is 1 to {sync.MAX_INVESTIGATION_QUESTION} characters (this one has {len(args.text)})\n"
            )
            return EXIT_USAGE
        if any(ord(c) < 32 and c not in "\n\t" for c in args.text):
            raise ValueError("the question may not contain control characters other than newline and tab")
        conn, client = self.connection()
        try:
            out = sync.set_investigation_question(
                conn, inv_id, args.text, client=client, session_id=sid if ie.valid_session(sid) else None
            )
        except sync.ServerError as e:
            sys.stderr.write(self.refused(e, "question not set") + "\n")
            return 1
        except ist.FetchError as e:
            return not_recorded(e)
        # Only a confirmed server write ends naming reminders. Resolve the
        # environment-derived session too, and never update a different binding.
        if ie.valid_session(sid):
            try:
                with ie.locked(self._home(), sid, wait=0.5) as got:
                    b = ie.read_binding(self._home(), sid) if got else None
                    if b and b["investigation_id"] == inv_id and b.get("org") == conn.get("org"):
                        b["question_status"] = "stated"
                        ie.write_binding(self._home(), sid, b)
            except OSError:
                pass  # Naming succeeded remotely; a local cache failure cannot undo it.
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        sys.stdout.write(f"investigation {inv_id} now asks: {ie.json_text(args.text)}\n")
        if isinstance(out.get("note"), str) and out["note"]:
            sys.stdout.write(f"Cardinal: {ie.json_text(out['note'])}\n")
        return 0

    def _event_line(self, e: dict) -> str:

        p = e.get("producer") if isinstance(e.get("producer"), dict) else {}
        pr = p.get("principal") if isinstance(p.get("principal"), dict) else {}
        grantee = ie.is_grantee(p)
        who = f"{ie._tok(pr.get('kind'))}:{ie._tok(pr.get('id'))}"
        if grantee:
            who += f" ({ie.grantee_line(p)})"
        if p.get("session_id"):
            who += f" claimed session {ie._claimed(p['session_id'])}"
        if p.get("is_investigation_author") is True and (not grantee):
            who += " (author)"
        to = f" to session {ie._tok(e['to_session_id'])}" if e.get("to_session_id") else ""
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        cls = ie.event_class(e)
        if cls is None:
            return f"#{e.get('seq')} {ie._tok(e.get('created_at'))} {ie._tok(e.get('type'))} from {who}{to}: [unknown class]"
        if cls == ie.OWNER_INPUT:
            turn = payload.get("turn")
            extra = [
                f"turn {(turn if isinstance(turn, int) and (not isinstance(turn, bool)) else ie._tok(turn))}"
            ]
            if payload.get("slash_command"):
                extra.append(f"command {ie._claimed(payload['slash_command'])}")
            if payload.get("truncated") is True:
                n = payload.get("original_length")
                extra.append(
                    f"truncated (original {(n if isinstance(n, int) and (not isinstance(n, bool)) else '?')} bytes)"
                )
            extra.append(f"authority {ie._tok(e.get('authority'))}")
            text, cut = ie.clipped_json_text(str(payload.get("text", "")), ie.MAX_TEXT_RENDERED)
            return (
                f"#{e.get('seq')} {ie._tok(e.get('created_at'))} {ie._tok(e.get('type'))} (owner input, client-attested; {', '.join(extra)}) from {who}: {text}"
                + (f" …[{cut} more chars; --json for all]" if cut else "")
            )
        if cls == ie.SEMANTIC:
            ident, body = ie.semantic_body(e)
            return (
                f"#{e.get('seq')} {ie._tok(e.get('created_at'))} {ie._tok(e.get('type'))} {ident} from {who}{to}"
                + (f": {body}" if body else "")
            )
        if e.get("type") == ie.ACKNOWLEDGED:
            body = f"ack of #{payload.get('ack_of')}: {ie._tok(payload.get('disposition'))}"
            if payload.get("conflicts_with") is not None:
                body += f" (conflicts with #{ie._tok(str(payload['conflicts_with']))})"
            if isinstance(payload.get("note"), str):
                body += f" {ie.json_text(payload['note'])}"
        elif e.get("type") == ie.RULING:
            body = (f"{ie._tok(payload.get('outcome'))} ruling on #{ie._tok(str(payload.get('ruling_of')))}"
                    f" (authority {ie._tok(e.get('authority'))}): {ie.json_text(str(payload.get('text', '')))}")
        else:
            body = ie.json_text(str(payload.get("text", "")))
        return (
            f"#{e.get('seq')} {ie._tok(e.get('created_at'))} {ie._tok(e.get('type'))} from {who}{to}: {body}"
        )

    def cmd_events(self, args):
        from . import investigation_reader

        got = self._target(args)
        if got is None:
            return EXIT_USAGE
        conn, client, inv = got
        return investigation_reader.run(
            args, conn, inv, client=client, legacy_line=self._event_line if self.legacy else None
        )

    def cmd_renew_cursor(self, args):
        if not args.credential_file or not args.previous_credential_file or not args.cursor_file:
            raise ValueError("renew-cursor requires --credential-file, --previous-credential-file and --cursor-file")
        new, client = self._grantee()
        old = consumer.credential_connection(args.previous_credential_file)
        result = consumer.renew_cursor(old, new, args.cursor_file, client=client)
        print(json.dumps(result, indent=2))
        return 0

    def cmd_post(self, args):
        import secrets

        got = self._target(args)
        if got is None:
            return EXIT_USAGE
        conn, client, inv = got
        if args.idempotency_key and args.outbox_file:
            raise ValueError("--idempotency-key and --outbox-file cannot be combined")
        # The unchanged Claude parity gate includes its pre-#190 environment-token
        # broadcast form. New credential files/outboxes always use #190 rules.
        legacy_token_post = self.legacy and args.credential_file is None and args.outbox_file is None
        if conn.get("token") and args.credential_file and not args.outbox_file and args.to_session:
            if args.idempotency_key:
                raise ValueError("generated outboxes use content-addressed keys; omit --idempotency-key")
            args.outbox_file = consumer.outbox_path(conn, args.credential_file, ie.POST_TYPES[args.type],
                                                   args.text, args.to_session, args.ref)
        if conn.get("token") and not legacy_token_post and (not args.to_session or not args.outbox_file):
            raise ValueError("scoped post requires --to-session and --outbox-file")
        own = None if conn.get("token") else args.session
        key = args.idempotency_key or "post:" + secrets.token_hex(16)
        if not ie.IDEMPOTENCY_KEY_RE.fullmatch(key) or ie.reserved_key(key):
            raise ValueError("invalid or reserved --idempotency-key")
        if args.to_session is not None and not ie.valid_session(args.to_session):
            raise ValueError("invalid --to-session")
        try:
            payload = ie.text_payload(args.text, args.ref)
        except ist.FetchError as err:
            print("not posted: " + str(err), file=sys.stderr)
            return 1 if self.legacy else EXIT_USAGE
        try:
            if args.outbox_file:
                out = consumer.post(
                    {**conn, "investigation_id": inv},
                    ie.POST_TYPES[args.type],
                    args.text,
                    args.to_session,
                    args.ref,
                    client=client,
                    outbox=args.outbox_file,
                    session_id=own if ie.valid_session(own) else None,
                )
                key = consumer.private_read(args.outbox_file)["intent"]["key"]
            else:
                out = ie.append_event(
                    conn,
                    inv,
                    ie.POST_TYPES[args.type],
                    payload,
                    idempotency_key=key,
                    to_session_id=args.to_session,
                    session_id=own if ie.valid_session(own) else None,
                    client=client,
                    producer_client=client,
                )
        except sync.ServerError as e:
            print("not posted: " + ie.plain(e), file=sys.stderr)
            return 1
        except ist.FetchError as e:
            return not_recorded(e, outbox=args.outbox_file)
        if args.json or not self.legacy:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            ev = out["event"]
            print(
                f"{'already posted' if out.get('duplicate') else 'posted'}: event #{ev['seq']} "
                f"({ev.get('type')}, idempotency key {key})"
            )
        return 0

    def _grant_supervisor(self, args, inv: str, scopes: list, conn: dict):
        """{investigation_id, session_id} of the session minting a supervise
        grant on an explicit --investigation with an explicit --session (never
        the defaulted agent session: an owner minting for someone else from
        an unrelated session must not link it): its own investigation from
        the local session binding, so the worker's storyboard links to it as
        "Supervised by". None for any other grant: without --investigation,
        --session names the granted investigation; a session bound to the
        granted investigation itself is its owner minting for someone else
        (both silently); a session with no usable binding (with a note)."""
        sid = getattr(args, "session", None)
        if (gr.SUPERVISE not in scopes or not sid or not getattr(args, "session_explicit", False)
                or getattr(args, "investigation", None) is None):
            return None
        why = None
        if not ie.valid_session(sid):
            why = f"not a session id: {sid!r}"
        else:
            b = ie.read_binding(self._home(), sid)
            if not b:
                why = f"session {sid} has no investigation of its own yet"
            elif b.get("is_author") is not True:
                why = f"session {sid} joined someone else's investigation"
            elif b.get("org") not in (None, conn.get("org")):
                why = f"session {sid} is bound in another organization"
            elif b["investigation_id"] == inv:
                return None
            else:
                return {"investigation_id": b["investigation_id"], "session_id": sid}
        print(f"not linking a supervisor storyboard: {why}", file=sys.stderr)
        return None

    def cmd_grant(self, args):
        if not self._author_only("no access granted"):
            return EXIT_USAGE
        ttl = gr.parse_ttl(args.ttl)
        scopes = gr.parse_scopes(args.scope, owner_input=args.owner_input)
        if not gr.TTL_MIN <= ttl <= gr.TTL_MAX:
            raise ValueError("--ttl is 5m to 24h")
        if args.label is not None and not gr.valid_label(args.label):
            raise ValueError(f"--label is 1 to {gr.MAX_LABEL} printable characters")
        inv = self._bound_investigation(args)
        if inv is None:
            return EXIT_USAGE
        conn, client = self.connection()
        supervisor = self._grant_supervisor(args, inv, scopes, conn)
        if args.out:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        remote_ok = False
        try:
            try:
                out = gr.grant(conn, inv, scopes, ttl_seconds=ttl, label=args.label, client=client,
                               supervisor=supervisor)
            except sync.ServerError as e:
                if supervisor is None or not gr.supervisor_unsupported(e):
                    raise
                # An older Cardinal: its strict schema rejects the two fields.
                print("this Cardinal does not link supervisors yet; minted without the supervisor link",
                      file=sys.stderr)
                supervisor = None
                out = gr.grant(conn, inv, scopes, ttl_seconds=ttl, label=args.label, client=client)
            remote_ok = True
            if args.out:
                consumer.private_write(args.out, {"token": out["token"], "origin": conn["origin"]})
        except Exception as e:
            ambiguous = remote_ok or getattr(e, "ambiguous", False)
            if args.out:
                if ambiguous:
                    print(
                        f"a grant may exist — check with grants / revoke it, then delete {args.out}",
                        file=sys.stderr,
                    )
                else:
                    args.out.unlink()
            elif not sys.stdout.isatty():
                print("no file written: the grant failed (see above)", file=sys.stderr)
                args.grant_failure_notice = True
            if supervisor and gr.supervisor_invalid(e):
                print(
                    f"no access granted: Cardinal refused to link {supervisor['investigation_id']} (session "
                    f"{supervisor['session_id']}) as the supervisor: it must be your own investigation and one "
                    f"of its sessions, never {inv} itself. Pass --session <your own session id>.",
                    file=sys.stderr,
                )
                return 1
            if isinstance(e, sync.ServerError):
                said = gr.refusal(e)
                print(
                    "no access granted: " + said if said else self.refused(e, "no access granted"),
                    file=sys.stderr,
                )
                return 1
            raise
        watch = investigation_text.watch_command(inv, credential_file=str(args.out) if args.out else None)
        if args.out:
            shown = {k: v for k, v in out.items() if k != "token"}
            shown["credential_file"] = str(args.out)
            print(json.dumps(shown, indent=2, ensure_ascii=False))
        else:
            print(gr.env_line(out["token"], conn.get("origin") or ""))
            shown = out.get("scopes") if isinstance(out.get("scopes"), list) else scopes
            print(
                f"granted {out['grant_id']} on {inv}: scopes {','.join(shown)}; expires "
                f"{ie._tok(out.get('expires_at'))}; principal {ie._tok(out.get('principal'))}. "
                + ("It can read the owner's recorded prompts. " if gr.OWNER_INPUT_READ in shown else "")
                + "The token is shown once: give it only to the grantee. Revoke it with `cardinal-storyboard "
                f"investigation revoke {out['grant_id']}`.",
                file=sys.stderr,
            )
        if supervisor:
            print(f"{inv}'s storyboard now links to this session's ({supervisor['investigation_id']}) as "
                  "\"Supervised by\".", file=sys.stderr)
        print(f"The grantee watches it with `{watch}` (one --follow per investigation; it replaces polling loops; "
              "`events --help` says more).", file=sys.stderr)
        return 0

    def cmd_grants(self, args: argparse.Namespace) -> int:

        if not self._author_only("cannot list access grants"):
            return EXIT_USAGE
        inv_id = self._bound_investigation(args)
        if inv_id is None:
            return EXIT_USAGE
        conn, client = self.connection()
        try:
            grants = gr.list_grants(conn, inv_id, client=client)
        except sync.ServerError as e:
            sys.stderr.write(self.refused(e, f"cannot list the access grants of {inv_id}") + "\n")
            return 1
        except ist.FetchError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        if args.json:
            sys.stdout.write(
                json.dumps({"investigation_id": inv_id, "grants": grants}, indent=2, ensure_ascii=False)
                + "\n"
            )
            return 0
        for g in grants:
            sys.stdout.write(gr.grant_line(g) + "\n")
        sys.stderr.write(f"{len(grants)} access grant{('s' if len(grants) != 1 else '')} on {inv_id}\n")
        return 0

    def cmd_revoke(self, args: argparse.Namespace) -> int:

        if not self._author_only("not revoked"):
            return EXIT_USAGE
        if not gr.valid_grant_id(args.grant_id):
            sys.stderr.write(f"not a grant id: {args.grant_id!r} (grt_ followed by 24 hex characters)\n")
            return EXIT_USAGE
        conn, client = self.connection()
        try:
            if not self.legacy or args.investigation:
                inv = self._bound_investigation(args)
                if inv is None:
                    return EXIT_USAGE
                known = gr.list_grants(conn, inv, client=client)
                if args.grant_id not in [gr.grant_id_of(g) for g in known]:
                    raise ValueError("grant does not belong to this investigation")
            gr.revoke(conn, args.grant_id, client=client)
        except sync.ServerError as e:
            if e.body.get("error") == "revoke_requires_owner_web":
                sys.stderr.write("not revoked: the owner minted this grant from the signed-in Cardinal web session "
                                 "(delegated supervision); revoke it from the Investigation page\n")
                return 1
            sys.stderr.write(self.refused(e, "not revoked") + "\n")
            return 1
        except ist.FetchError as e:
            sys.stderr.write(f"not revoked: {e}\n")
            return 1
        sys.stdout.write(f"revoked {args.grant_id}: its token no longer works\n")
        return 0

    def cmd_checkpoint(self, args: argparse.Namespace) -> int:
        import time

        t0 = time.monotonic()
        timing = {"upload": 0.0}
        try:
            return self._checkpoint(args, timing)
        finally:
            if os.environ.get(CHECKPOINT_TIMING_ENV) == "1":
                ms = {
                    k: round(v * 1000)
                    for k, v in (
                        ("total", time.monotonic() - t0),
                        ("upload", timing["upload"]),
                        ("request", timing.get("request", 0.0)),
                    )
                }
                sys.stderr.write(
                    f"checkpoint timing: total {ms['total']} ms, upload {ms['upload']} ms, request {ms['request']} ms, events {timing.get('events', 0)}\n"
                )

    def _checkpoint(self, args: argparse.Namespace, timing: dict) -> int:
        import time

        if not self._author_only("not checkpointed"):
            return EXIT_USAGE
        sid = self._session_arg(args)
        if not sid:
            return EXIT_USAGE
        b = ie.read_binding(self._home(), sid)
        if not b:
            sys.stderr.write(
                f"not checkpointed: session {sid} has no investigation (not connected, or its setup failed: `cardinal-storyboard investigation link` retries it)\n"
            )
            return EXIT_USAGE
        if b.get("is_author") is False:
            sys.stderr.write(
                f"not checkpointed: session {sid} joined someone else's investigation; only its author's session records checkpoints (only the investigation's author can record them; post a cue, question or challenge instead)\n"
            )
            return EXIT_USAGE
        if args.key is not None and (not ie.CHECKPOINT_KEY_RE.fullmatch(args.key)):
            sys.stderr.write("not checkpointed: --key is 1-100 characters of A-Z a-z 0-9 . _ : -\n")
            return EXIT_USAGE
        if sys.stdin.isatty():
            sys.stderr.write(
                "not checkpointed: pipe a JSON array of events on stdin (--help lists the types)\n"
            )
            return EXIT_USAGE
        try:
            data = json.loads(sys.stdin.read())
        except ValueError as e:
            sys.stderr.write(f"not checkpointed: stdin is not JSON ({e})\n")
            return EXIT_USAGE
        conn, client = self.connection()
        t = time.monotonic()
        try:
            out = ie.checkpoint(
                conn,
                b["investigation_id"],
                sid,
                data,
                idempotency_key=args.key,
                client=client,
                producer_client=self.client(),
                timing=timing,
                home=self._home(),
            )
        except ie.CheckpointInputError as e:
            sys.stderr.write(f"not checkpointed: {e}\n")
            return EXIT_USAGE
        except ie.EvidenceRefused as e:
            if e.code == "evidence_unsupported":
                sys.stderr.write(f"{e}\n")
                return EXIT_USAGE
            sys.stderr.write(f"not checkpointed (cited evidence): {e}\n")
            return EXIT_USAGE if e.code == "evidence_not_citable" else 1
        except sync.ServerError as e:
            if ie.checkpoint_unsupported(e):
                sys.stderr.write(ie.CHECKPOINT_UNSUPPORTED + "\n")
                return EXIT_USAGE
            sys.stderr.write(f"not checkpointed: {ie.checkpoint_refusal(e)}\n")
            return 1
        except ist.FetchError as e:
            return not_recorded(e)
        finally:
            timing["request"] = time.monotonic() - t - timing["upload"]
        timing["events"] = len(out["events"])
        ie.record_supervision(self._home(), sid, out["events"])   # a checkpoint citing "#<seq>" addresses it
        if args.json:
            sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(ie.checkpoint_line(out) + "\n")
        return 0

    # -- relayed owner decisions (supervisor side). Nothing here sends a
    # decision: only the prompt hook relays one, when the owner approves it.

    def cmd_propose_decision(self, args: argparse.Namespace) -> int:
        sid = self._session_arg(args)
        if not sid:
            return EXIT_USAGE
        worker = args.investigation_id or args.investigation
        if worker is None or not self._investigation_arg(worker):
            if worker is None:
                print("not proposed: name the worker's investigation (inv_...)", file=sys.stderr)
            return EXIT_USAGE
        owner_relay.validate(worker, args.to_session, args.text, args.answers, args.ref)
        home = self._home()
        code = self._can_relay(home, sid, worker, "not proposed")
        if code is not None:
            return code
        answers = None
        if args.answers is not None:
            answers = self._answered(worker, args.answers)
            if answers is None:
                return EXIT_USAGE
        entry = owner_relay.propose(home, sid, worker, args.to_session, args.text, answers=args.answers,
                                    refs=args.ref or None)
        if args.json:
            out = {**entry, "instruction": owner_relay.INSTRUCTION.format(id=entry["id"])}
            out.update(relay_status=self._relay_status(), relay_guidance=self._relay_guidance())
            if answers:
                out["answers_line"] = answers
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        when = time.strftime("%H:%M", time.localtime(entry["expires_at"]))
        print(f"proposed {entry['id']}: an owner decision for session {args.to_session} of {worker} "
              f"(expires at {when} local time, in 30 minutes; nothing was sent)")
        print("--- decision text ---")
        print(entry["text"])
        print("--- end ---")
        if answers:
            print(answers)
        print(owner_relay.INSTRUCTION.format(id=entry["id"]) + f" Tell the owner it expires at {when}.")
        print(self._relay_guidance())
        return 0

    def _answered(self, worker: str, seq: int):
        """The line saying what --answers points at ("answers #N: <type>
        <excerpt>"), read with this machine's key; None (printed why) when it
        is a supervisor's challenge or ruling, which a relayed decision
        cannot answer (the server refuses it: only a web decision overrules
        those). Unreadable: a line saying so (the server checks it)."""
        conn, client = self.connection()
        try:
            page = ie.read_events(conn, worker, after=seq - 1, limit=1, client=client, timeout=5.0)
            e = next((e for e in page["events"] if e.get("seq") == seq), None)
        except Exception:
            e = None
        if e is None:
            return f"answers #{seq} (not read here; Cardinal checks it when the owner approves)"
        typ = e.get("type") if isinstance(e.get("type"), str) else "event"
        if typ in (ie.CHALLENGE, ie.RULING):
            print(f"not proposed: #{seq} is a supervisor's {'challenge' if typ == ie.CHALLENGE else 'ruling'}; a "
                  "relayed decision cannot answer or overrule it (only a decision posted from the Investigation "
                  "page on the web can). Drop --answers, or tell the owner to overrule it on the web.",
                  file=sys.stderr)
            return None
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        text = payload.get("text") if isinstance(payload.get("text"), str) else ""
        return f"answers #{seq}: {ie._tok(typ)} {owner_relay.excerpt(text, 80)}" if text \
            else f"answers #{seq}: {ie._tok(typ)}"

    def _can_relay(self, home: Path, sid: str, worker: str, what: str):
        """None when supervisor session `sid` can relay owner decisions to
        `worker`, else the exit code (the reason printed)."""
        b = ie.read_binding(home, sid)
        if not b:
            print(f"{what}: session {sid} has no investigation yet; the relay names this supervisor session's "
                  "own Investigation (run `cardinal-storyboard investigation link`)", file=sys.stderr)
            return 1
        if b.get("is_author") is not True:
            print(f"{what}: session {sid} joined someone else's investigation; only a session that authors "
                  "its own Investigation can relay an owner decision", file=sys.stderr)
            return 1
        if b["investigation_id"] == worker:
            print(f"{what}: {worker} is this session's own investigation; a relayed decision goes to the "
                  "worker you supervise", file=sys.stderr)
            return EXIT_USAGE
        conn, client = self.connection()
        if not isinstance(conn, dict) or not conn.get("key") or conn.get("token"):
            print(f"{what}: this machine is not connected to Cardinal with its own key (run cardinal-connect); "
                  "the owner's decisions are relayed with it", file=sys.stderr)
            return 1
        if not ie.capability(b, owner_relay.CAPABILITY):
            b = ie.refresh_binding_capabilities(home, sid, conn, client) or b
        if not ie.capability(b, owner_relay.CAPABILITY):
            print(f"{what}: " + owner_relay.NOT_CAPABLE, file=sys.stderr)
            return 1
        self._relay_binding = b
        return None

    def _relay_off_line(self) -> str:
        """The owner's relay setting as this Cardinal reported it
        (capabilities.owner_relay.on_for_caller): a line when it is off,
        "" when on or unknown."""
        caps = (getattr(self, "_relay_binding", None) or {}).get("capabilities") or {}
        cap = caps.get(owner_relay.CAPABILITY) if isinstance(caps, dict) else None
        if not isinstance(cap, dict) or cap.get("on_for_caller") is not False:
            return ""
        conn, _ = self.connection()
        link = owner_relay.settings_link(cap.get("settings_url"), (conn or {}).get("origin"))
        where = link or "your Cardinal settings page (Settings, Investigations)"
        return f"Relayed decisions are off for your account: turn them on once at {where} (web only)."

    def _relay_status(self) -> str:
        caps = (getattr(self, "_relay_binding", None) or {}).get("capabilities") or {}
        cap = caps.get(owner_relay.CAPABILITY) if isinstance(caps, dict) else None
        on = cap.get("on_for_caller") if isinstance(cap, dict) else None
        return "ready" if on is True else "disabled" if on is False else "unknown"

    def _relay_guidance(self) -> str:
        status = self._relay_status()
        if status == "disabled":
            return ("Relay status: disabled. " + self._relay_off_line() +
                    " Until enabled, `decide:` and `approve <id>` cannot relay. Use the worker's "
                    "Investigation web page to post an owner decision, or instruct the worker directly; "
                    "direct owner prompts in its chat need no decide: prefix.")
        if status == "unknown":
            return ("Relay status: unknown. This Cardinal did not report your opt-in status; do not promise "
                    "delivery. Check Settings, Investigations in your signed-in web session and confirm the prompt-hook outcome.")
        return "Relay status: ready. The owner can use `decide: <text>` or `approve <id>`; confirm the prompt-hook outcome."

    def cmd_supervising(self, args: argparse.Namespace) -> int:
        sid = self._session_arg(args)
        if not sid:
            return EXIT_USAGE
        home = self._home()
        worker = args.investigation_id or args.investigation
        if args.remove:
            if args.name is None and worker is None:
                raise ValueError("--remove takes --name, or the worker's investigation (and --to-session)")
            gone = owner_relay.remove_target(home, sid, name=args.name, investigation_id=worker,
                                             to_session_id=args.to_session)
            if not gone:
                print("no such target in this session", file=sys.stderr)
                return 1
            print("removed " + ", ".join(owner_relay._target_label(t) for t in gone))
            return 0
        if worker is None:
            ts = owner_relay.targets(home, sid)
            if args.json:
                print(json.dumps({"targets": ts}, indent=2))
                return 0
            if not ts:
                print(f"no worker target in session {sid}")
            for t in ts:
                print(f"{t.get('name') or '(no name)'} -> session {t['to_session_id']} of {t['investigation_id']}")
            return 0
        if args.to_session is None:
            raise ValueError("supervising <worker_inv> needs --to-session <worker_sid>")
        owner_relay.validate_target(worker, args.to_session, args.name)
        code = self._can_relay(home, sid, worker, "not registered")
        if code is not None:
            return code
        t = owner_relay.add_target(home, sid, worker, args.to_session, args.name, replace=args.replace)
        if args.json:
            print(json.dumps({**t, "relay_status": self._relay_status(),
                              "relay_guidance": self._relay_guidance()}, indent=2))
            return 0
        n = len(owner_relay.targets(home, sid))
        form = f"decide {t['name']}: <text>" if t.get("name") else "decide: <text>"
        print(f"registered {owner_relay._target_label(t)} of {worker} as a worker target ({n} in this session; "
              "nothing was sent).")
        print(f"When relay is ready, the owner can type `{form}` here to send that worker their own words as an owner decision"
              + (" (bare `decide:` while it is the only target)." if t.get("name") else
                 " (while it is the only target; add --name to tell several apart).")
              + " Never type it for them.")
        print(self._relay_guidance())
        return 0

    def cmd_proposals(self, args: argparse.Namespace) -> int:
        sid = self._session_arg(args)
        if not sid:
            return EXIT_USAGE
        home = self._home()
        entries = owner_relay.pending(home, sid)
        done = owner_relay.recent(home, sid)
        ts = owner_relay.targets(home, sid)
        if args.json:
            print(json.dumps({"proposals": entries, "targets": ts, "recent": done}, indent=2, ensure_ascii=False))
            return 0
        for t in ts:
            print(f"target {t.get('name') or '(no name)'} -> session {t['to_session_id']} of {t['investigation_id']}")
        if not entries:
            print(f"no pending owner-decision proposal in session {sid}")
        for e in entries:
            line = (f"{e['id']} -> session {e['to_session_id']} of {e['investigation_id']}, expires "
                    f"{ie._now_iso(e['expires_at'])}")
            if e.get("answers") is not None:
                line += f", answers #{e['answers']}"
            if e.get("uncertain"):
                line += ", may already have been delivered (a send got no answer)"
            print(line)
            print(f"  Text (verbatim JSON string): {ie.json_text(e['text'])}")
        for r in done:
            print(f"recent {r['id']} ({ie._tok(r.get('outcome'))}, {ie._now_iso(r['at'])}): "
                  f"{ie._one_line(r['message'], 600)}")
        return 0

    def cmd_budget(self, args: argparse.Namespace) -> int:
        from . import investigation_budget as budget

        return budget.run(args, target=lambda: self._target(args), own_conn=self.connection, home=self._home(),
                          client=self.client(), grant_token=lambda: (self._grantee()[0] or {}).get("token"))

    def cmd_withdraw_proposal(self, args: argparse.Namespace) -> int:
        sid = self._session_arg(args)
        if not sid:
            return EXIT_USAGE
        pid = args.proposal_id
        if not isinstance(pid, str) or not owner_relay.ID_RE.fullmatch(pid):
            print(f"not a proposal id: {pid!r} (dp_ followed by 16 hex characters)", file=sys.stderr)
            return EXIT_USAGE
        entry = owner_relay.withdraw(self._home(), sid, pid)
        if entry is None:
            print(f"{pid} is not pending in session {sid} (expired, withdrawn or already handled)", file=sys.stderr)
            return 1
        if entry.get("uncertain"):
            print(f"withdrew {pid}; it may already have been delivered (an earlier send got no answer): check the "
                  "worker's Investigation")
        else:
            print(f"withdrew {pid}; nothing was sent")
        return 0


def not_recorded(err, *, outbox=None):
    reason = ie._one_line(err, 500)
    if not getattr(err, "network", False):
        print("not recorded: " + reason, file=sys.stderr)
        return 1
    line = (
        f"NOT RECORDED — Cardinal is unreachable from here ({reason}). The investigation is not being updated. "
        "If you run in a sandbox without network, tell the user; do not continue as if this was recorded."
    )
    if outbox:
        line += " Retry with the same --outbox-file."
    print(line, file=sys.stderr)
    return 3


class _ExplicitSession(argparse.Action):
    """--session that records it was given (grant: only an explicit session
    is linked as the supervisor)."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.session_explicit = True


def build_parser(wiring, session_env=()):
    parser = argparse.ArgumentParser(
        prog="cardinal-storyboard investigation",
        description=(
            "Owner authority comes from the owner's own words: in the session transcript, or in an owner decision "
            "posted from the owner's signed-in Cardinal web session or relayed from their supervisor session. "
            "Advisory input carries no owner authority."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    default_sid = next((wiring.environ.get(k) for k in session_env if wiring.environ.get(k)), None)
    for action in (
        "link",
        "repos",
        "question",
        "checkpoint",
        "create",
        "attach",
        "bind",
        "events",
        "ack",
        "post",
        "grant",
        "grants",
        "revoke",
        "rule",
        "reviewed",
        "renew-cursor",
        "show",
        "verify",
        "propose-decision",
        "supervising",
        "proposals",
        "withdraw-proposal",
        "budget",
    ):
        p = sub.add_parser(
            action,
            description=(CHECKPOINT_HELP if action == "checkpoint" else
                         ie.ACK_GUIDANCE if action == "ack" else
                         ie.POST_GUIDANCE if action == "post" else None),
            epilog=investigation_text.WATCH_GUIDANCE if action == "events" else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        if action == "checkpoint":
            p.usage = "%(prog)s [--session SID] [--key K] [--json]"
        if action == "grant":
            p.add_argument(
                "--session", default=default_sid, action=_ExplicitSession,
                help="this session (default: the agent's own); passed explicitly with --investigation and "
                     "--scope supervise, its own investigation is recorded as the supervisor, linking the two "
                     "storyboards",
            )
            p.set_defaults(session_explicit=False)
        else:
            p.add_argument("--session", default=default_sid)
        p.add_argument("--json", action="store_true")
        p.add_argument(
            "--credential-file",
            type=Path,
            help=(
                argparse.SUPPRESS
                if action == "checkpoint"
                else "private scoped grant JSON; no author-key fallback"
            ),
        )
        p.add_argument(
            "--investigation",
            help=(
                argparse.SUPPRESS
                if action == "checkpoint"
                else "explicit investigation instead of session binding"
            ),
        )
        p.set_defaults(investigation_id=None)
        if action in ("events", "show", "post", "bind", "attach", "propose-decision", "supervising", "budget"):
            p.add_argument("investigation_id", nargs=None if action in ("bind", "attach") else "?")
        if action in ("events", "reviewed", "renew-cursor"):
            p.add_argument(
                "--cursor-file", type=Path,
                help="#190 durable pending-page review state (requires grant)"
                     + ("; with --follow and no --auto-review, a watch from the committed position"
                        if action == "events" else ""),
            )
        if action == "repos":
            p.add_argument("--cwd", action="append", default=[], help="checkout to attach; repeat for multiple repos; saved edits are replayed too")
        elif action == "reviewed":
            commit = p.add_mutually_exclusive_group(required=True)
            commit.add_argument("--batch", help="exact hash of the reviewed pending page")
            commit.add_argument("--page-file", type=Path, help="immutable full page saved by events --save-page")
            p.add_argument("--budget-file", type=Path, help="the step-back cache (default: budget.json beside the "
                           "cursor file); with an open direction every worker event needs a --tag")
            p.add_argument("--tag", action="append", default=[], metavar="SEQS:M",
                           help="current-page worker seqs only; charges the preceding active interval to <m>: "
                                "208-215:<m>, 255:<m>+<m2>, 220:none or all:<m>. Before a soak starts use "
                                "a preparation milestone or none; this is not a wall-clock soak timer")
            for flag in ("done", "reopen", "redone"):
                p.add_argument("--" + flag, action="append", default=[], metavar="M.N@REF",
                               help=f"mark a deliverable {flag}: <m>.<n>@#<seq> or @ev_<12 hex> (repeat)")
        elif action == "renew-cursor":
            p.add_argument("--previous-credential-file", type=Path, required=True,
                           help="old private grant (may be expired); scopes must match; old challenges keep old ruling authority")
        elif action == "question":
            p.add_argument("text", help="the question (1 to 1000 characters)")
        elif action == "checkpoint":
            p.add_argument("--key")
        elif action == "create":
            p.add_argument("--question", required=True)
            p.add_argument("--window")
        elif action == "attach":
            p.add_argument("storyboard_id")
        elif action == "grant":
            p.add_argument(
                "--out", type=Path, help="new 0600 credential file; otherwise print an export line once"
            )
            p.add_argument("--ttl", default="4h")
            p.add_argument("--label")
            p.add_argument(
                "--scope",
                help="read,advise,owner_input:read,evidence:read,supervise (owner_input:read and evidence:read "
                "require read; supervise implies read,advise and adds binding rulings on its own challenges)",
            )
            p.add_argument("--owner-input", action="store_true")
        elif action == "revoke":
            p.add_argument("grant_id")
        elif action == "post":
            p.add_argument("--type", choices=tuple(ie.POST_TYPES))
            p.add_argument("--text")
            p.add_argument("--to-session")
            p.add_argument("--ref", action="append", default=[])
            p.add_argument("--outbox-file", type=Path, help="durable intent; with --credential-file defaults to a private "
                           "content-addressed file in outbox/ beside the credential")
            p.add_argument("--idempotency-key")
        elif action == "ack":
            p.add_argument("seq", type=int)
            p.add_argument(
                "--disposition",
                choices=ie.DISPOSITIONS,
                required=True,
                help="a challenge: accepted|declined with --note; a directive: accepted, or conflicts "
                "--conflicts-with SEQ; cues and questions: accepted|declined|noted",
            )
            p.add_argument("--note")
            p.add_argument(
                "--conflicts-with",
                type=int,
                metavar="SEQ",
                help="with --disposition conflicts: the owner.decided event the directive contradicts "
                "(owner input recorded from a session is not accepted)",
            )
        elif action == "rule":
            p.add_argument("challenge_seq", type=int, help="your own challenge's #seq")
            outcome = p.add_mutually_exclusive_group(required=True)
            outcome.add_argument("--uphold", action="store_true",
                                 help="a binding directive (once the worker took a position)")
            outcome.add_argument("--withdraw", action="store_true", help="close the challenge")
            p.add_argument("--text", required=True, help="the directive, or why it is withdrawn (1-4000 chars)")
            p.add_argument("--ref", action="append", default=[])
        elif action == "events":
            p.add_argument("--after", type=int, default=0)
            p.add_argument(
                "--class", dest="event_class", choices=ie.READ_CLASSES, action="append", default=[]
            )
            p.add_argument("--type", dest="event_type", action="append", default=[])
            p.add_argument("--follow", action="store_true")
            p.add_argument("--signal-only", action="store_true", help="cursor-backed watch: emit short wake signals, "
                           "never event bodies; read the full pending page after waking")
            p.add_argument("--save-page", type=Path, help="save the exact full pending page as a private immutable "
                           "<batch>.json in this directory; prints its path for review and reviewed --page-file")
            p.add_argument(
                "--auto-review", action="store_true",
                help="with --cursor-file: print then commit each full pending batch (without it, "
                     "--follow is a watch that never commits)"
            )
            p.add_argument("--interval", type=float, default=5)
            p.add_argument("--timeout", type=float)
            p.add_argument("--max", type=int, help="stop after N displayed events (a watch: after that page)")
            p.add_argument("--exclude-self", action="store_true", default=None)
            p.add_argument("--include-self", dest="exclude_self", action="store_false")
            p.add_argument("--fields", choices=("full", "compact"), default="full")
        elif action == "verify":
            p.add_argument("target", help="event seq or semantic_id")
            p.add_argument("--root", type=Path)
        elif action == "propose-decision":
            p.description = owner_relay.PROPOSE_HELP
            p.add_argument("--to-session", required=True, help="the worker's session id")
            p.add_argument("--text", required=True, help="the decision, verbatim (1-4000 characters)")
            p.add_argument("--answers", type=int, metavar="SEQ", help="the worker event it answers")
            p.add_argument("--ref", action="append", default=[], help="a reference (repeatable, at most 20)")
        elif action == "supervising":
            p.description = owner_relay.SUPERVISING_HELP
            p.add_argument("--to-session", help="the worker's session id")
            p.add_argument("--name", help="a short name for `decide <name>: …` (a-z, 0-9, -; at most 32)")
            p.add_argument("--remove", action="store_true", help="drop the target (by --name, or by id and session)")
            p.add_argument("--replace", action="store_true",
                           help="move --name here from a target of another investigation")
        elif action == "withdraw-proposal":
            p.add_argument("proposal_id", help="dp_...")
        elif action == "budget":
            p.description = BUDGET_HELP
            p.add_argument("--budget-file", type=Path, required=True, help="private 0600 cache, e.g. "
                           "~/.cardinal/supervise/<inv>/budget.json")
            mode = p.add_mutually_exclusive_group(required=True)
            mode.add_argument("--set", action="store_true", help="record a new direction from --plan (closes the "
                              "open one as replaced)")
            mode.add_argument("--show", action="store_true", help="the direction, its milestones, firings, history")
            mode.add_argument("--close", action="store_true", help="end the direction (--outcome)")
            mode.add_argument("--pass", dest="pass_", choices=("estimator", "stepback"), help=argparse.SUPPRESS)
            mode.add_argument("--calibrate", action="store_true", help=argparse.SUPPRESS)
            mode.add_argument("--verdict", nargs=2, metavar=("SCOPE", "VERDICT"),
                              help="SCOPE: milestone, opening or all; VERDICT: on_track | worker_choice | owner_constraint")
            mode.add_argument("--stepback-outcome", nargs=2, metavar=("SCOPE", "OUTCOME"), help=argparse.SUPPRESS)
            p.add_argument("--amend", action="store_true", help="--set: re-plan the open direction's milestones")
            p.add_argument("--plan", type=Path, help="--set: the estimator's JSON")
            p.add_argument("--question", help="--set: the owner's question for this direction, one line")
            p.add_argument("--worker-session", help="--set, pass estimator: the worker's session id")
            p.add_argument("--worker-estimate", help="--set: what the worker itself estimated, if anything")
            p.add_argument("--since-seq", type=int, metavar="SEQ",
                           help="--set: count from this event (the new direction's start); default: the head")
            p.add_argument("--threshold", action="append", default=[], metavar="K=V",
                           help="--set: pin a threshold the owner approved (T, Z, rearm, backstop, idle_min, "
                           "min_spent_h)")
            p.add_argument("--brief", type=Path, help="pass: the facts brief")
            p.add_argument("--milestone", help="pass stepback: the milestone that fired, or all")
            p.add_argument("--routed-to", choices=("none", "worker", "owner"), help="verdict")
            p.add_argument("--post-ref", help="verdict: your post's \"#<seq>\"")
            p.add_argument("--ref", action="append", default=[], help="outcome: \"#<seq>\" or ev_ (repeat)")
            p.add_argument("--outcome", choices=("done", "stopped", "replaced"), help="--close")
            p.add_argument("--missed", help="--close: milestones that needed rework or a pivot with no step back")
    return parser


def normalize_argv(argv):
    """Accept Claude's inv positional/flags and Codex's compact ack/post forms."""
    argv = list(argv)
    if not argv:
        return argv
    action = argv[0]
    # Options take one value except the explicitly boolean options.
    boolean = {
        "--json",
        "--uphold",
        "--withdraw",
        "--follow",
        "--signal-only",
        "--auto-review",
        "--exclude-self",
        "--include-self",
        "--owner-input",
        "--remove",
        "--replace",
        "--set",
        "--show",
        "--close",
        "--amend",
        "--calibrate",
        "--help",
        "-h",
    }
    positional = []
    i = 1
    while i < len(argv):
        token = argv[i]
        if token.startswith("-"):
            i += 1 if token in boolean or "=" in token else 2
        else:
            positional.append(i)
            i += 1
    if action == "budget":
        # `budget [<inv>] … pass estimator`, `calibrate`, `verdict <scope> <v>`, `outcome <scope> <o>`.
        verbs = {"pass": "--pass", "calibrate": "--calibrate", "verdict": "--verdict", "outcome": "--stepback-outcome"}
        for idx in positional:
            if argv[idx] in verbs:
                argv[idx] = verbs[argv[idx]]
                break
    if action in ("ack", "post") and positional and ie.valid_investigation(argv[positional[0]]):
        idx = positional.pop(0)
        inv = argv[idx]
        argv[idx : idx + 1] = ["--investigation", inv]
        positional = [n + 1 for n in positional]
    if action == "ack" and len(positional) == 2 and "--disposition" not in argv:
        idx = positional[1]
        argv[idx : idx + 1] = ["--disposition", argv[idx]]
    if action == "post" and len(positional) == 2 and "--type" not in argv:
        a, b = positional
        text, typ = argv[b], argv[a]
        del argv[b]
        del argv[a]
        argv.extend(["--type", typ, "--text", text])
    return argv


def cli_main(argv, wiring, session_env=(), *, legacy=False):
    args = build_parser(wiring, session_env).parse_args(normalize_argv(argv))
    cli = CLI(wiring, args, legacy)
    try:
        if cli.scoped and args.action in ("propose-decision", "supervising"):
            print(f"not {'proposed' if args.action == 'propose-decision' else 'registered'}: an owner decision is "
                  "relayed with this machine's own key, never with an access grant; run "
                  f"{args.action} without --credential-file and without CARDINAL_INVESTIGATION_TOKEN", file=sys.stderr)
            return EXIT_USAGE
        if cli.scoped and args.action not in ("events", "post", "show", "reviewed", "verify", "rule", "proposals",
                                              "withdraw-proposal", "budget", "renew-cursor"):
            return EXIT_USAGE if not cli._author_only("not recorded") else 0
        if args.action == "post" and (args.type is None or args.text is None):
            raise ValueError("post requires type and text (positional or --type/--text)")
        if args.action == "reviewed":
            if not cli.scoped or not args.cursor_file:
                raise ValueError("reviewed requires a scoped credential and --cursor-file")
            from . import investigation_budget as budget

            conn, _ = cli._grantee()
            return budget.reviewed(args, grant=conn, own_conn=cli.connection, home=cli._home())
        if args.action == "verify":
            from . import investigation_artifacts

            got = cli._target(args)
            if got is None:
                return EXIT_USAGE
            conn, client, inv = got
            return investigation_artifacts.verify(conn, inv, args.target, root=args.root, client=client)
        return getattr(cli, "cmd_" + args.action.replace("-", "_"))(args)
    except (ValueError, ie.CheckpointInputError) as e:
        if type(e).__name__ == "BudgetError":   # at most 2 lines, each naming the fix
            print("\n".join(ie._one_line(x, 1000) for x in str(e).splitlines()[:2]), file=sys.stderr)
        else:
            print(ie._one_line(e, 1000), file=sys.stderr)
        if (
            args.action == "grant"
            and not args.out
            and not sys.stdout.isatty()
            and not getattr(args, "grant_failure_notice", False)
        ):
            print("no file written: the grant failed (see above)", file=sys.stderr)
        return EXIT_USAGE
    except sync.ServerError as e:
        print(ie.plain(e), file=sys.stderr)
        return 1
    except ist.FetchError as e:
        if args.action in ("checkpoint", "question", "post", "ack"):
            return not_recorded(e, outbox=getattr(args, "outbox_file", None))
        print(ie._one_line(e, 1000), file=sys.stderr)
        return 1
    except OSError as e:
        print(ie._one_line(e, 1000), file=sys.stderr)
        return 1
