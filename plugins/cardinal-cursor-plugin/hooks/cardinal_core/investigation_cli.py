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
            payload = ie.ack_payload(args.seq, args.disposition, args.note)
        except ist.FetchError as err:
            print("not acknowledged: " + str(err), file=sys.stderr)
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
        if f["view_url"]:
            sys.stdout.write(f"Storyboard (live, private to its author while draft): {f['view_url']}\n")
        if f["investigation_url"]:
            sys.stdout.write(f"Investigation (control log): {f['investigation_url']}\n")
        sys.stdout.write(
            f"investigation {f['investigation_id']}"
            + (f" · storyboard {f['storyboard_id']}" if f["storyboard_id"] else " · no storyboard attached")
            + ("" if f["is_author"] else " · joined: someone else's investigation (not the author)")
            + "\n"
        )

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
        own = args.session
        sid = getattr(args, "session", None) or own
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
            if isinstance(payload.get("note"), str):
                body += f" {ie.json_text(payload['note'])}"
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
        if args.out:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        remote_ok = False
        try:
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
            if isinstance(e, sync.ServerError):
                said = gr.refusal(e)
                print(
                    "no access granted: " + said if said else self.refused(e, "no access granted"),
                    file=sys.stderr,
                )
                return 1
            raise
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
        if args.json:
            sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(ie.checkpoint_line(out) + "\n")
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


def build_parser(wiring, session_env=()):
    parser = argparse.ArgumentParser(
        prog="cardinal-storyboard investigation",
        description=(
            "Owner authority comes from the owner's own words: in the session transcript, or in an owner decision "
            "posted from the owner's signed-in Cardinal web session. Advisory input carries no owner authority."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    default_sid = next((wiring.environ.get(k) for k in session_env if wiring.environ.get(k)), None)
    for action in (
        "link",
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
        "reviewed",
        "show",
        "verify",
    ):
        p = sub.add_parser(
            action,
            description=CHECKPOINT_HELP if action == "checkpoint" else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        if action == "checkpoint":
            p.usage = "%(prog)s [--session SID] [--key K] [--json]"
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
        if action in ("events", "show", "post", "bind", "attach"):
            p.add_argument("investigation_id", nargs=None if action in ("bind", "attach") else "?")
        if action in ("events", "reviewed"):
            p.add_argument(
                "--cursor-file", type=Path, help="#190 durable pending-page review state (requires grant)"
            )
        if action == "reviewed":
            p.add_argument("--batch", required=True)
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
                "--scope", help="read,advise,owner_input:read,evidence:read (latter scopes require read)"
            )
            p.add_argument("--owner-input", action="store_true")
        elif action == "revoke":
            p.add_argument("grant_id")
        elif action == "post":
            p.add_argument("--type", choices=tuple(ie.POST_TYPES))
            p.add_argument("--text")
            p.add_argument("--to-session")
            p.add_argument("--ref", action="append", default=[])
            p.add_argument("--outbox-file", type=Path, help="durable intent; required with a scoped grant")
            p.add_argument("--idempotency-key")
        elif action == "ack":
            p.add_argument("seq", type=int)
            p.add_argument("--disposition", choices=ie.DISPOSITIONS, required=True)
            p.add_argument("--note")
        elif action == "events":
            p.add_argument("--after", type=int, default=0)
            p.add_argument(
                "--class", dest="event_class", choices=ie.READ_CLASSES, action="append", default=[]
            )
            p.add_argument("--type", dest="event_type", action="append", default=[])
            p.add_argument("--follow", action="store_true")
            p.add_argument(
                "--auto-review", action="store_true", help="print then commit each full pending batch"
            )
            p.add_argument("--interval", type=float, default=5)
            p.add_argument("--timeout", type=float)
            p.add_argument("--max", type=int, help="stop after N displayed events")
            p.add_argument("--exclude-self", action="store_true", default=None)
            p.add_argument("--include-self", dest="exclude_self", action="store_false")
            p.add_argument("--fields", choices=("full", "compact"), default="full")
        elif action == "verify":
            p.add_argument("target", help="event seq or semantic_id")
            p.add_argument("--root", type=Path)
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
        "--follow",
        "--auto-review",
        "--exclude-self",
        "--include-self",
        "--owner-input",
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
        if cli.scoped and args.action not in ("events", "post", "show", "reviewed", "verify"):
            return EXIT_USAGE if not cli._author_only("not recorded") else 0
        if args.action == "post" and (args.type is None or args.text is None):
            raise ValueError("post requires type and text (positional or --type/--text)")
        if args.action == "reviewed":
            if not cli.scoped or not args.cursor_file:
                raise ValueError("reviewed requires a scoped credential and --cursor-file")
            conn, _ = cli._grantee()
            print(json.dumps(consumer.reviewed(conn, args.cursor_file, args.batch), indent=2))
            return 0
        if args.action == "verify":
            from . import investigation_artifacts

            got = cli._target(args)
            if got is None:
                return EXIT_USAGE
            conn, client, inv = got
            return investigation_artifacts.verify(conn, inv, args.target, root=args.root, client=client)
        return getattr(cli, "cmd_" + args.action)(args)
    except (ValueError, ie.CheckpointInputError) as e:
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
