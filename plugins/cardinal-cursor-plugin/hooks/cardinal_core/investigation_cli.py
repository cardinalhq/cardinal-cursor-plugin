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
from . import supervision_status as sv

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

    def _gate(self, args):
        from .investigation_gate import Gate
        if args.gate_file is None:
            from . import investigation_gate_runtime as runtime
            selected = runtime.route(self._home(), args.session, investigation=args.investigation,
                batch=getattr(args, "batch", None), credential_file=args.credential_file)
            if not selected:
                raise ValueError("no gate route for this session; pass --gate-file explicitly")
            if args.investigation and args.investigation != selected["investigation_id"]:
                raise ValueError("explicit investigation differs from this session's gate route")
            if args.credential_file and str(args.credential_file.absolute()) != selected.get("credential_file"):
                raise ValueError("explicit credential differs from this session's gate route")
            args.gate_file = Path(selected["gate_file"])
            args.investigation = selected["investigation_id"]
            args.credential_file = Path(selected["credential_file"]) if selected.get("credential_file") else None
            self.scoped = args.credential_file is not None or gr.token_set(self.wiring.environ)
        got = self._target(args)
        if got is None:
            raise ValueError("gate requires an investigation and connection")
        conn, client, inv = got
        return Gate({**conn, "investigation_id": inv}, args.gate_file, session=args.session,
                    client=client, home=self._home(), credential_file=args.credential_file)

    def cmd_gate_enable(self, args):
        from . import investigation_gate_runtime as runtime
        gate = self._gate(args)
        value = json.loads(args.policy.read_text(encoding="utf-8"))
        print(json.dumps(runtime.activate(gate, value, runtime=getattr(self.wiring, "runtime", ""),
                                         cli=self.wiring.cli, environ=self.wiring.environ,
                                         new_epoch=args.new_epoch, budget_file=args.budget_file,
                                         argv=getattr(args, "gate_argv", None))))
        return 0

    def cmd_gate_status(self, args):
        print(json.dumps(self._gate(args).status()))
        return 0

    def cmd_request(self, args):
        return self._request_write(args, "request.created", {"text": args.text,
            "assignee": {"principal": args.assignee, "session": args.assignee_session},
            "intent": args.intent or args.text})

    def _intent_gates(self, args):
        from . import investigation_gate_runtime as runtime
        if not self._session_arg(args):
            raise ValueError("coordination requires this agent's session")
        got = self._target(args)
        if not got:
            raise ValueError("coordination requires an authenticated Investigation")
        conn, client, inv = got
        args.intent_connection = consumer.identity({**conn, "investigation_id": inv})
        return runtime.prepare_intent(session=args.session, runtime=self.wiring.runtime,
            home=self._home(), conn={**conn, "investigation_id": inv}, client=client,
            cli=self.wiring.cli, credential_file=args.credential_file)

    def _sync_intent(self, args, gates):
        pending = []
        for gate in gates:
            required = args.requires if consumer.identity(gate.conn) == args.intent_connection else ()
            result = gate.synchronize(args.intent, required)
            if result.status != "allow":
                status = "needs_feedback" if result.batch else result.reason or "retry"
                after = getattr(args, "after_response", None)
                if (status == "needs_attention" and after is not None and result.requests
                        and all(r["request"] in required and (r["status"] == "waiting" or
                            (r["status"] == "blocked" and r["response"] <= after)) for r in result.requests)):
                    status = "waiting"
                pending.append({"status": status, "intent": args.intent, "released": False,
                    "investigation_id": gate.conn["investigation_id"],
                    "batch": result.batch or None, "message": result.message,
                    "requests": list(result.requests)})
        if pending:
            priority = {"needs_feedback": 0, "needs_attention": 1, "retry": 2, "uncertain": 2, "waiting": 3}
            selected = min(pending, key=lambda item: priority.get(item["status"], 2))
            return {**selected, "pending_contexts": [
                {"status": item["status"], "batch": item["batch"],
                 "requests": [r["request"] for r in item["requests"]]} for item in pending]}
        return {"status": "ready", "intent": args.intent, "released": True}

    def cmd_sync(self, args):
        result = self._sync_intent(args, self._intent_gates(args))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["released"] else 3

    def cmd_wait(self, args):
        """One bounded invocation; new feedback interrupts the wait for handling."""
        if not 0 < args.timeout <= 300:
            raise ValueError("wait timeout must be >0 and <=300 seconds")
        if args.after_response is not None and (args.after_response < 0 or not args.requires):
            raise ValueError("--after-response needs --request and a nonnegative sequence")
        end = time.monotonic() + args.timeout
        gates = self._intent_gates(args)
        delay = 0.1
        while True:
            result = self._sync_intent(args, gates)
            if result["status"] != "waiting" and not (args.incoming and result["released"]):
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result["released"] else 3
            remaining = end - time.monotonic()
            if remaining <= 0:
                print(json.dumps({**result, "status": "timeout", "released": False,
                    "message": "Wait ended; this intent and its required results remain durable. Resume with the same intent."}, ensure_ascii=False))
                return 3
            time.sleep(min(delay, remaining))
            delay = min(2.0, delay * 2)

    def cmd_respond(self, args):
        return self._request_write(args, "request.responded", {"request": args.request,
            "outcome": args.outcome, "text": args.text})

    def cmd_resolve(self, args):
        payload = {"request": args.request, "outcome": args.outcome, "text": args.text}
        if args.response is not None:
            payload["response"] = args.response
        return self._request_write(args, "request.resolved", payload)

    def _request_write(self, args, typ, payload):
        import hashlib
        got = self._target(args)
        if not got:
            return EXIT_USAGE
        conn, client, inv = got
        if not conn.get("grant_id") and not self._session_arg(args):
            return EXIT_USAGE
        if typ == "request.created":
            if not self._session_arg(args):
                return EXIT_USAGE
            from . import investigation_requests as requests
            out = requests.create(conn, inv, args.session, payload, home=self._home(), client=client,
                key=args.key, credential_file=args.credential_file)
            print(json.dumps(out, ensure_ascii=False))
            return 0
        # Response/resolution retries refer to the same immutable request.
        key = args.key or "request:" + hashlib.sha256(json.dumps(
            [inv, args.session, typ, payload], sort_keys=True).encode()).hexdigest()
        out = ie.append_event(conn, inv, typ, payload, idempotency_key=key, client=client,
            session_id=None if conn.get("grant_id") else args.session)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    def cmd_requests(self, args):
        if args.request is not None and args.request < 1:
            raise ValueError("request sequence must be positive")
        if args.after_response is not None and (not args.request or args.after_response < 0):
            raise ValueError("--after-response needs --request and a nonnegative sequence")
        if not 0 < args.timeout <= 300:
            raise ValueError("request wait timeout must be >0 and <=300 seconds")
        from . import investigation_requests as requests
        got = self._target(args)
        if not got:
            return EXIT_USAGE
        conn, client, inv = got
        after, head, state = 0, None, {}
        for _ in range(100):
            page = ie.read_snapshot(conn, inv, after=after, through_seq=head, limit=200,
                types=list(requests.TYPES), client=client, request_protocol=2)
            requests.fold(state, page["events"])
            if page["scan_complete"]:
                values = list(state.get("requests", {}).values())
                who = consumer.principal_key(page["viewer"]["principal"])
                if who.startswith("grantee:") and page["viewer"].get("lineage_root_id"):
                    who = "grantee:" + page["viewer"]["lineage_root_id"]
                args.request_viewer = who
                target = next((r for r in values if r["request"] == args.request), None) if args.request else None
                incoming = [r for r in values if r["assignee"] == requests.party(who, args.session)
                            and not r["resolved"] and r["status"] == "waiting"]
                if args.request and not target:
                    raise ValueError("request not found in the authenticated snapshot")
                result_ready = target and target["status"] != "waiting" and (args.after_response is None or (target["response"] or 0) > args.after_response)
                if not args.wait or result_ready or (not args.request and incoming):
                    print(json.dumps({"investigation_id": inv, "snapshot_head_seq": page["snapshot_head_seq"],
                        "requests": [target] if args.request and target else values}, ensure_ascii=False))
                    return 0
                return self._wait_requests(args)
            after, head = page["scanned_through"], page["snapshot_head_seq"]
        raise ValueError("request snapshot incomplete; no dependency status inferred")

    def _wait_requests(self, args):
        import copy
        end = time.monotonic() + args.timeout
        probe = copy.copy(args)
        probe.wait = False
        import contextlib, io
        from . import investigation_requests as requests
        got = self._target(args)
        if not got:
            return EXIT_USAGE
        conn = got[0]
        while time.monotonic() < end:
            time.sleep(min(2.0, max(0, end - time.monotonic())))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.cmd_requests(probe)
            result = json.loads(output.getvalue())
            values = result["requests"]
            if args.request:
                ready = any(r["request"] == args.request and r["status"] != "waiting"
                    and (args.after_response is None or (r["response"] or 0) > args.after_response) for r in values)
            else:
                # User/key participant identity comes from the authenticated
                # snapshot viewer, captured by cmd_requests, never its text.
                who = getattr(probe, "request_viewer", None)
                ready = any(r["assignee"] == requests.party(who, args.session) and not r["resolved"]
                            and r["status"] == "waiting" for r in values) if who else False
            if ready:
                print(json.dumps(result, ensure_ascii=False))
                return 0
        print(json.dumps({"status": "timeout", "released": False,
            "message": "No required result arrived; dependencies remain unresolved."}))
        return 3

    def cmd_gate_disable(self, args):
        from . import investigation_gate_runtime as runtime
        print(json.dumps(runtime.deactivate(self._gate(args))))
        return 0

    def cmd_gate_read(self, args):
        gate = self._gate(args)
        current = gate.status()
        batch = args.batch or current["batch"]
        if not batch:
            raise ValueError("no pending gate batch")
        next_index = next((i for i in range(1, current["chunks"] + 1) if i not in current["delivered"]), 1)
        print(gate.read(batch, args.chunk if args.chunk is not None else next_index).message)
        return 0

    def cmd_gate_reviewed(self, args):
        from . import investigation_budget as budget
        gate = self._gate(args)
        budget_file = gate.status().get("budget_file")
        if args.budget_file and str(args.budget_file.absolute()) != budget_file:
            raise ValueError("--budget-file differs from the gate's enabled response guard; start a new epoch")
        def guarded_review():
            review_args = argparse.Namespace(**vars(args))
            review_args.cursor_file = args.gate_file
            review_args.page_file = None
            # The gate cursor is independent of the established supervisor
            # cache. Moving the cursor must never choose an empty new cache.
            review_args.budget_file = Path(budget_file)
            return budget.reviewed(review_args, grant=gate.conn, own_conn=self.connection,
                                   home=self._home(), out=lambda _: None)
        print(json.dumps(gate.reviewed(args.batch, args.chunk_hash, supervisor_review=guarded_review)))
        return 0

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
        elif e.get("type") == ie.VERIFICATION:
            body = f"{ie.verification_words(payload)}: {ie.json_text(str(payload.get('text', '')))}"
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
        if args.succeeds_credential_file is not None:
            return self._succeed(args)
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

    def _succeed(self, args) -> int:
        """grant --succeeds-credential-file OLD: renew a live grant by
        succession (capability grant_succession), so the new grant keeps
        ruling and verdict authority over the old one's challenges. Refuses
        while an outbox of the old grant is unresolved; reserves --out; mints
        with `succeeds` (a retry after an uncertain answer is idempotent);
        writes the new token, renews the review cursor, then deletes OLD."""
        old_path = args.succeeds_credential_file
        old = consumer.credential_connection(old_path)
        inv = old["investigation_id"]
        if not args.out:
            raise ValueError("--succeeds-credential-file needs --out <the new credential file>")
        if args.investigation is not None and args.investigation != inv:
            raise ValueError(f"{old_path} is a grant on {inv}, not {args.investigation}")
        if not gr.valid_grant_id(old.get("grant_id")):
            raise ValueError(f"{old_path} names no grant id")
        old_scopes = [x for x in gr.SCOPES if x in (old.get("scopes") or [])]
        scopes = gr.parse_scopes(args.scope, owner_input=args.owner_input) \
            if args.scope is not None or args.owner_input else old_scopes
        if set(scopes) != set(old_scopes):
            raise ValueError(f"a successor has exactly its predecessor's scopes ({','.join(old_scopes)}); drop --scope")
        ttl = gr.parse_ttl(args.ttl)
        if not gr.TTL_MIN <= ttl <= gr.TTL_MAX:
            raise ValueError("--ttl is 5m to 24h")
        if args.label is not None and not gr.valid_label(args.label):
            raise ValueError(f"--label is 1 to {gr.MAX_LABEL} printable characters")
        conn, client = self.connection()
        if str(conn.get("origin") or "").rstrip("/") != old["origin"]:
            raise ValueError(f"{old_path} is a grant of {old['origin']}, not of this machine's Cardinal")
        able = gr.supports(conn, inv, gr.SUCCESSION_CAPABILITY, client=client)
        if able is None:
            able = gr.supports(old, inv, gr.SUCCESSION_CAPABILITY, client=client)
        if able is not True:
            print("not renewed: this Cardinal does not support grant succession yet. A new grant (`investigation "
                  "grant`, then `renew-cursor`) cannot rule on or verify the old grant's challenges", file=sys.stderr)
            return 1
        unresolved = consumer.unresolved_outboxes(old_path, old)
        if unresolved:
            print("not renewed: resolve or retry these posts first (once the old grant is revoked an uncertain post "
                  "cannot be retried, and re-posting it under the new grant could duplicate it): "
                  + ", ".join(str(x) for x in unresolved) + ". Explicit --outbox-file outboxes are not checked.",
                  file=sys.stderr)
            return 1
        try:
            kept = consumer.private_read(args.out)
        except FileNotFoundError:
            kept = None
        except (OSError, ValueError):
            raise ValueError(f"{args.out} exists and is not this succession's reservation")
        if kept is not None and (kept.get("succession_of") != old["grant_id"] or not isinstance(kept.get("request"), dict)):
            raise ValueError(f"{args.out} exists and is not this succession's reservation")
        if kept is not None:
            # A retry resends the earlier request unchanged: the server answers the same successor.
            request = kept["request"]
            print(f"resending the succession reserved in {args.out} unchanged", file=sys.stderr)
        else:
            request = self._succession_request(args, conn, client, inv, old, scopes, ttl)
            if request is None:
                return 1
            consumer.private_write(args.out, {"succession_of": old["grant_id"], "request": request}, create=True)
        recover = (f"{args.out} stays reserved. If the successor exists but its token was lost, find it with "
                   f"`cardinal-storyboard investigation grants --investigation {inv}` and revoke it, or ask your owner "
                   "for a grant from the web")
        try:
            out = gr.grant(conn, inv, request["scopes"], ttl_seconds=request["ttl_seconds"], label=request.get("label"),
                           client=client, supervisor=request.get("supervisor"),
                           succeeds={"grant_id": old["grant_id"], "token": old["token"]})
        except sync.ServerError as e:
            said = gr.refusal(e) or self.refused(e, "no access granted")
            if e.status < 500 and kept is None and e.body.get("error") != "predecessor_succeeded":
                args.out.unlink()   # a first attempt refused: nothing was minted
                print("not renewed: " + said, file=sys.stderr)
                return 1
            print(f"not renewed: {said}. {recover}", file=sys.stderr)
            return 1
        except ist.FetchError as e:
            print(f"the succession may have happened ({ie._one_line(e)}): run the same command again (it is "
                  f"idempotent while the old grant lives); {args.out} stays reserved", file=sys.stderr)
            return 3
        consumer.private_write(args.out, {"token": out["token"], "origin": conn["origin"]})
        cursor = args.cursor_file or (old_path.parent / "cursor.json")
        renewed = None
        if args.cursor_file or cursor.exists():
            try:
                renewed = consumer.renew_cursor(old, consumer.credential_connection(args.out), cursor, client=client)
            except (ValueError, OSError, sync.ServerError, ist.FetchError) as e:
                print(f"the new grant is in {args.out}, but the review cursor was not renewed ({ie._one_line(e)}); "
                      f"{old_path} is kept: run `cardinal-storyboard investigation renew-cursor "
                      f"--previous-credential-file {old_path} --credential-file {args.out} --cursor-file {cursor}`, "
                      "then delete it", file=sys.stderr)
                return 1
        # A durable, explicit successor association lets automatic consumers
        # validate the new grant before retiring old routes. Never guess files.
        consumer.private_write(old_path.with_suffix(old_path.suffix + ".successor.json"), {
            "old_identity": consumer.identity(old), "credential_file": str(args.out.absolute()),
            "new_identity": consumer.identity(consumer.credential_connection(args.out)),
            "lineage_root_id": out.get("lineage_root_id")})
        # Persist both new credential and successor directory entries before
        # deleting the old credential. Never lose both sides on power failure.
        for directory in {args.out.parent, old_path.parent}:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        old_path.unlink()
        fd = os.open(old_path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        shown = {k: v for k, v in out.items() if k != "token"}
        shown.update(credential_file=str(args.out), succeeded=old["grant_id"])
        if renewed is not None:
            shown["cursor"] = renewed
        print(json.dumps(shown, indent=2, ensure_ascii=False))
        print(f"{old['grant_id']} is revoked (succeeded by {ie._tok(out.get('grant_id'))}); {old_path} was deleted. "
              f"Restart your watch on {args.out}: one on the old file now gets 401. Explicit --outbox-file outboxes "
              "were not checked: an uncertain one of the old grant can no longer be retried.", file=sys.stderr)
        return 0

    def _succession_request(self, args, conn, client, inv, old, scopes, ttl):
        """The successor's request: the old grant's label and supervisor link
        (list-investigation-grants, this machine's key) unless --label /
        --session --investigation give them. None (said why) when the old
        grant cannot be read."""
        try:
            row = next((g for g in gr.list_grants(conn, inv, client=client) if gr.grant_id_of(g) == old["grant_id"]),
                       None)
        except (sync.ServerError, ist.FetchError) as e:
            print(f"not renewed: could not read the old grant's label and supervisor link ({ie._one_line(e)})",
                  file=sys.stderr)
            return None
        if row is None:
            print(f"not renewed: {old['grant_id']} is not a grant of {inv} this machine's key can list", file=sys.stderr)
            return None
        label = args.label if args.label is not None else row.get("label") if gr.valid_label(row.get("label")) else None
        supervisor = self._grant_supervisor(args, inv, scopes, conn)
        sup_inv, sup_sid = row.get("supervisor_investigation_id"), row.get("supervisor_session_id")
        if supervisor is None and ie.valid_investigation(sup_inv) and ie.valid_session(sup_sid):
            supervisor = {"investigation_id": sup_inv, "session_id": sup_sid}
        return {"scopes": scopes, "ttl_seconds": ttl, "label": label, "supervisor": supervisor}

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
        key, outbox = args.key, None
        if ie.wants_auto_revision(data) and not ie.capability(b, "checkpoint_revision"):
            data = ie.with_revision(data, None)
            print("warning: this Cardinal server does not support checkpoint revision; field stripped", file=sys.stderr)
        elif ie.wants_auto_revision(data):
            # Resolve once; every retry of this input resends the same batch and key.
            try:
                data, key, outbox = self._resolved_checkpoint(sid, b, data, args.key)
            except ie.CheckpointInputError as e:
                sys.stderr.write(f"not checkpointed: {e}\n")
                return EXIT_USAGE
        t = time.monotonic()
        try:
            keep = outbox is not None
            try:
                out = ie.checkpoint(
                    conn,
                    b["investigation_id"],
                    sid,
                    data,
                    idempotency_key=key,
                    client=client,
                    producer_client=self.client(),
                    timing=timing,
                    home=self._home(),
                )
                keep = False
            except (ie.CheckpointInputError, ie.EvidenceRefused):
                keep = False
                raise
            except sync.ServerError as e:
                keep = e.status >= 500
                raise
            finally:
                if outbox is not None and not keep:
                    outbox.unlink(missing_ok=True)
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
            if outbox is not None:
                print("the resolved checkpoint is kept: run the same command again to resend it", file=sys.stderr)
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

    def _resolved_checkpoint(self, sid: str, b: dict, data, key):
        """(events, key, outbox) for an input with "revision": "auto": the
        kept outbox of this exact input when an earlier send was uncertain
        (said, with the commit it carries), else the input resolved against
        this checkout, written to a new outbox before anything is sent."""
        home, inv = self._home(), b["investigation_id"]
        outbox = ie.checkpoint_outbox(home, sid, inv, data, key)
        saved = ie.read_checkpoint_outbox(outbox)
        if saved is not None:
            sent, head = saved.get("commit"), ie.head_commit(os.getcwd())
            note = f"replayed an earlier send at {ie._tok(str(sent)[:7])}"
            if head and sent and head != sent:
                note += f"; HEAD is now {ie._tok(head[:7])}: checkpoint again for the new commit"
            print(note, file=sys.stderr)
            return saved["events"], saved["key"], outbox
        revision = ie.resolve_revision(os.getcwd())
        events = ie.checkpoint_capabilities(ie.checkpoint_events(ie.with_revision(data, revision)), b)
        key = key or ie.checkpoint_key(inv, sid, events)
        ie._ensure_dirs(home)
        ie.write_json(outbox, {"version": 1, "commit": revision["commit"], "key": key, "events": events})
        return events, key, outbox

    # -- delegated supervision verdicts and the challenge table (supervisor side)

    def _supervise_grant(self, args, what: str):
        """(conn, client, inv) of a supervise grant's credential, or None
        (the reason printed)."""
        got = self._target(args) if self.scoped else None
        conn = got[0] if got is not None else {}
        if not self.scoped or (got is not None and not conn.get("token")):
            print(f"{what}: this needs a supervise grant's credential (--credential-file); the investigation's "
                  "author mints one with `cardinal-storyboard investigation grant --scope supervise`", file=sys.stderr)
            return None
        if got is None:
            return None
        scopes = conn.get("scopes") if isinstance(conn.get("scopes"), list) else None
        if scopes and gr.SUPERVISE not in scopes:
            print(f"{what}: this grant's scopes are {','.join(ie._tok(x) for x in scopes)}, without supervise",
                  file=sys.stderr)
            return None
        return got

    def cmd_verdict(self, args: argparse.Namespace) -> int:
        """Supervisor: a scoped, revision-bound verdict on a challenge's
        current correction (--correction) or on a claim (record-verification)."""
        got = self._supervise_grant(args, "no verdict recorded")
        if got is None:
            return EXIT_USAGE
        conn, client, inv = got
        verdict = "verified" if args.verified else "rejected" if args.rejected else "unverifiable"
        if args.revision is not None and args.revision != "auto":
            raise ValueError("--revision takes only auto (this checkout's HEAD); else --repo/--commit")
        if args.revision and (args.repo or args.commit or args.pr is not None):
            raise ValueError("--revision auto or --repo/--commit/--pr, not both")
        if args.credential_file is None:
            raise ValueError("verdict needs --credential-file: its outbox lives beside it")
        # A verdict follows a review, never a fetch.
        cursor = args.cursor_file or (args.credential_file.parent / "cursor.json")
        if args.correction is not None and (args.cursor_file or cursor.exists()):
            try:
                watermark = consumer.committed(conn, cursor)
            except ValueError:
                raise ValueError(f"{cursor} belongs to another grant, origin or investigation: renew it with "
                                 "`renew-cursor`, or pass this grant's --cursor-file")
            if watermark is not None and args.correction > watermark:
                print(f"no verdict recorded: review the page containing #{args.correction} first (your committed "
                      f"review position is #{watermark})", file=sys.stderr)
                return EXIT_USAGE
        unresolved = {"seq": args.seq, "verdict": verdict, "scope": args.scope, "correction": args.correction,
                      "repo": args.repo, "commit": args.commit, "pr": args.pr, "revision": args.revision,
                      "artifact": args.artifact, "target": args.target, "observed_at": args.observed_at,
                      "text": args.text, "evidence": args.evidence, "ref": args.ref, "supersedes": args.supersedes}
        outbox = consumer.verdict_outbox_path(conn, args.credential_file, unresolved)
        if gr.supports(conn, inv, gr.VERIFICATIONS_CAPABILITY, client=client) is False:
            print(gr.VERIFICATIONS_UNSUPPORTED, file=sys.stderr)
            return 1

        def build():
            target = self._verdict_target(conn, client, inv, args.seq)
            if target == ie.CHALLENGE and args.correction is None:
                raise ValueError(f"#{args.seq} is a challenge: name the correction you verified with --correction "
                                 "<seq> (`investigation challenges` lists it)")
            if target not in (None, ie.CHALLENGE) and args.correction is not None:
                raise ValueError(f"--correction is only for a challenge; #{args.seq} is a {ie._tok(target)}")
            if target not in (None, ie.CHALLENGE) and verdict == "rejected":
                raise ValueError(gr.VERIFICATION_REFUSALS["refute_with_challenge"])
            subject = {"repo": args.repo, "commit": args.commit, "pr": args.pr, "target": args.target,
                       "observed_at": args.observed_at}
            if args.revision:
                subject.update(ie.resolve_revision(os.getcwd()))   # the supervisor's own checkout
            if args.artifact:
                subject["artifacts"] = [artifact_hash(a) for a in args.artifact]
            if args.supersedes is not None:
                verdicts = [e for e in self._read_stream(conn, client, inv)[0]
                            if e.get("type") == ie.VERIFICATION and isinstance(e.get("payload"), dict)]
                if any(v["seq"] == args.supersedes for v in verdicts) \
                        and len(sv.chain(verdicts, args.supersedes)) - 1 >= sv.MAX_REVISIONS:
                    raise ValueError(gr.VERIFICATION_REFUSALS["verdict_revision_limit"])
            payload = gr.verification_payload(args.seq, verdict, args.scope, args.text, correction=args.correction,
                                              subject=subject, evidence=args.evidence, refs=args.ref or None,
                                              supersedes=args.supersedes)
            return payload, gr.verification_key(conn, payload)

        try:
            out, replayed = consumer.verdict(conn, outbox, build, client=client)
        except ie.CheckpointInputError as e:
            raise ValueError(str(e))
        except sync.ServerError as e:
            return self._verdict_refused(e, conn, client, inv, outbox)
        except ist.FetchError as e:
            if getattr(e, "network", False) or getattr(e, "ambiguous", False):
                if outbox.exists():
                    print("the resolved verdict is kept beside the credential: run the same command again to resend "
                          "it", file=sys.stderr)
                return not_recorded(e)
            print("no verdict recorded: " + ie._one_line(e, 1000), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
        ev = out["event"]
        said = "already recorded" if out.get("duplicate") or replayed else "recorded"
        print(f"{said} verdict #{ev['seq']}: {ie.verification_words(ev.get('payload') or {})}")
        return 0

    def _verdict_target(self, conn, client, inv, seq):
        """The type of event `seq` (None when it cannot be read: the server judges)."""
        try:
            page = ie.read_events(conn, inv, after=seq - 1, limit=1, client=client, timeout=10.0)
        except sync.ServerError:
            return None
        e = next((e for e in page["events"] if e.get("seq") == seq), None)
        if e is None:
            raise ValueError(f"there is no event #{seq} this grant can read in {inv}")
        return e.get("type")

    def _verdict_refused(self, e, conn, client, inv, outbox) -> int:
        code = e.body.get("error") if isinstance(e.body, dict) else None
        if gr.verification_unsupported(e) or (
                e.status in (401, 403) and code in (None, "token_scope_mismatch", "Invalid token")
                and gr.supports(conn, inv, gr.VERIFICATIONS_CAPABILITY, client=client) is False):
            print(gr.VERIFICATIONS_UNSUPPORTED, file=sys.stderr)
            return 1
        if code == "verification_exists" and isinstance(e.body.get("seq"), int):
            given = e.body.get("verification")
            stored = self._stored_event(conn, client, inv, e.body["seq"])   # with its grant's label
            if stored is None and isinstance(given, dict) and isinstance(given.get("payload"), dict) \
                    and given.get("seq") == e.body["seq"]:
                stored = given
            intent = consumer.private_read(outbox)["intent"]
            if stored is not None and stored.get("payload") == intent["payload"]:
                consumer.resolve_verdict(outbox, {"event": stored, "duplicate": True})
                print(f"already recorded as verdict #{stored['seq']} by {stored_grant(stored)}: "
                      f"{ie.verification_words(stored['payload'])}")
                return 0
            who = f" by {stored_grant(stored)}" if stored is not None else ""
            own = e.body["same_lineage"] if isinstance(e.body.get("same_lineage"), bool) else \
                stored is not None and sv.lineage(stored) == conn.get("grant_id")
            fix = (f"to revise your own verdict, run the same command with --supersedes {e.body['seq']}" if own else
                   "another lineage's verdict stays; a changed judgment needs a new correction")
            print(f"no verdict recorded: verdict #{e.body['seq']}{who} already judges that correction (or claim) in "
                  f"that scope with other content; {fix}", file=sys.stderr)
            return 1
        if code == "verdict_already_superseded":
            # A retried supersede (a renewed grant is a new principal): success when the
            # verdict that superseded it is this one.
            intent = consumer.private_read(outbox)["intent"]
            old = intent["payload"].get("supersedes_verdict")
            events = self._read_stream(conn, client, inv)[0]
            verdicts = [e for e in events if e.get("type") == ie.VERIFICATION and isinstance(e.get("payload"), dict)]
            by = sv.revised_by(verdicts).get(old)
            stored = next((v for v in verdicts if v["seq"] == by), None)
            if stored is not None and stored["payload"] == intent["payload"]:
                consumer.resolve_verdict(outbox, {"event": stored, "duplicate": True})
                print(f"already recorded as verdict #{stored['seq']} by {stored_grant(stored)}: "
                      f"{ie.verification_words(stored['payload'])}")
                return 0
        said = gr.VERIFICATION_REFUSALS.get(code)
        if code == "correction_not_current":
            current = next((e.body[k] for k in ("current_correction", "correction", "current")
                            if isinstance(e.body.get(k), int)), None)
            if current is not None:
                said += f": the current correction is #{current} (review it, then verify that one)"
        print("no verdict recorded: " + (f"Cardinal refused ({said})" if said else ie.plain(e)), file=sys.stderr)
        return 1

    def _stored_event(self, conn, client, inv, seq):
        try:
            page = ie.read_events(conn, inv, after=seq - 1, limit=1, client=client, timeout=10.0)
        except (sync.ServerError, ist.FetchError):
            return None
        return next((x for x in page["events"] if x.get("seq") == seq and isinstance(x.get("payload"), dict)), None)

    def _read_stream(self, conn, client, inv):
        """(every event this connection can read, the principals map)."""
        events, principals, after = [], {}, 0
        while True:
            page = ie.read_events(conn, inv, after=after, limit=ie.PAGE_LIMIT, client=client)
            events += page["events"]
            principals.update(page.get("principals") or {})
            if page["last_seq"] <= after or page["last_seq"] >= page["head_seq"]:
                return events, principals
            after = page["last_seq"]

    def cmd_challenges(self, args: argparse.Namespace) -> int:
        """One row per challenge: status, position, current correction,
        verdicts (superseded / revision mismatch / revoked), next action;
        --check-heads asks `gh` (read-only) where each named PR's head is."""
        got = self._target(args)
        if got is None:
            return EXIT_USAGE
        conn, client, inv = got
        rows = challenge_rows(*self._read_stream(conn, client, inv))
        heads = check_heads(rows) if args.check_heads else None
        if args.json:
            print(json.dumps({"investigation_id": inv, "heads": heads or "not requested", "challenges": rows},
                             indent=2, ensure_ascii=False))
            return 0
        for row in rows:
            print(challenge_line(row))
        if args.check_heads and heads == "unchecked":
            print("heads unchecked: gh is not available here", file=sys.stderr)
        print(f"{len(rows)} challenge{'s' if len(rows) != 1 else ''} in {inv}", file=sys.stderr)
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


def stored_grant(e: dict) -> str:
    """The grant a stored verdict was recorded with: its principal and label."""
    producer = e.get("producer") if isinstance(e.get("producer"), dict) else {}
    pr = producer.get("principal") if isinstance(producer.get("principal"), dict) else {}
    return f"{ie._tok(pr.get('kind'))}:{ie._tok(pr.get('id'))} ({ie.grantee_label(producer)})"


def artifact_hash(path: str) -> dict:
    """{path, sha256} of a repo-relative file (the verdict's subject)."""
    import hashlib
    ie.validate_structured("artifacts", [{"path": path, "sha256": "0" * 64}], "--artifact")
    with open(path, "rb") as f:
        return {"path": path, "sha256": hashlib.sha256(f.read()).hexdigest()}


def challenge_rows(events: list, principals: dict) -> list:
    """The `investigation challenges` rows, from structured state only."""
    rows = []
    for c, info in sorted(sv.challenge_report(events).items()):
        k = info["correction"]
        rev = (k.get("payload") or {}).get("revision") if k is not None else None
        rev = rev if isinstance(rev, dict) else None
        revised = sv.revised_by(info["verdicts"])
        verdicts = []
        for v in info["verdicts"]:
            p, producer = v.get("payload") or {}, v.get("producer") if isinstance(v.get("producer"), dict) else {}
            subject = p.get("subject") if isinstance(p.get("subject"), dict) else {}
            gid = (producer.get("principal") or {}).get("id") if isinstance(producer.get("principal"), dict) else None
            grant = principals.get(f"grantee:{gid}") if isinstance(principals.get(f"grantee:{gid}"), dict) else {}
            verdicts.append({
                "seq": v["seq"], "verdict": p.get("verdict"), "scope": p.get("scope"), "correction": p.get("correction"),
                **{f: subject[f] for f in ("repo", "commit", "pr", "dirty", "tree", "target", "observed_at")
                   if f in subject},
                "superseded": k is None or p.get("correction") != k["seq"],
                "revised_by": revised.get(v["seq"]),
                "revision_mismatch": sv.revision_mismatch(rev, subject),
                "snapshot_unbound": sv.snapshot_unbound(rev) or sv.snapshot_unbound(subject),
                "grant": gid, "grant_label": grant.get("label"),
                "revoked": bool(grant.get("revoked") is True or grant.get("revoked_at"))
                and grant.get("revoked_reason") != "succeeded",
            })
        pos = info["position"]
        nxt = sv.next_action(info, sv.deciding_staleness(events, info), c)
        rows.append({
            "seq": c, "status": info["status"], "ruling_seq": info["ruling_seq"], "owner_seq": info["owner_seq"],
            "position": {"seq": pos["seq"], "of": (pos.get("payload") or {}).get("ack_of"),
                         "disposition": (pos.get("payload") or {}).get("disposition")} if pos else None,
            "correction": {"seq": k["seq"], "revision": rev, "snapshot_unbound": sv.snapshot_unbound(rev),
                           "artifacts": len((k.get("payload") or {}).get("artifacts") or [])} if k else None,
            "verification_seq": info["verification_seq"], "verdicts": verdicts,
            "next": nxt,
        })
    return rows


def _rollup(checks) -> str:
    states = []
    for x in checks if isinstance(checks, list) else ():
        if not isinstance(x, dict):
            continue
        if "state" in x:
            states.append(str(x.get("state")).upper())
        else:
            states.append(str(x.get("conclusion")).upper() if x.get("status") == "COMPLETED" else "PENDING")
    if not states:
        return "none"
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE") for s in states):
        return "failing"
    if any(s in ("PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "NONE") for s in states):
        return "pending"
    return "passing"


def check_heads(rows: list) -> str:
    """Mark every PR a correction or verdict names with its live head
    (`gh pr view`, read-only): head, head_moved, ci. "checked", or
    "unchecked" without gh. Nothing is written anywhere."""
    import shutil
    import subprocess
    if shutil.which("gh") is None:
        return "unchecked"
    seen: dict = {}
    hosts = {"github.com": True}

    def trusted(host):
        # gh sends GH_ENTERPRISE_TOKEN / GITHUB_TOKEN to the host it is asked
        # about, and the repo is the worker's claim: only github.com and hosts
        # gh is authenticated for.
        if host not in hosts:
            try:
                hosts[host] = subprocess.run(["gh", "auth", "status", "--hostname", host], capture_output=True,
                                             timeout=15, check=False).returncode == 0
            except (OSError, subprocess.SubprocessError):
                hosts[host] = False
        return hosts[host]

    def head(repo, pr):
        if not trusted(repo.split("/", 1)[0]):
            return "untrusted"
        if (repo, pr) not in seen:
            seen[(repo, pr)] = None
            try:
                out = subprocess.run(["gh", "pr", "view", str(pr), f"--repo={repo}", "--json",
                                      "headRefOid,statusCheckRollup"], capture_output=True, text=True, timeout=15,
                                     check=False)
                data = json.loads(out.stdout) if out.returncode == 0 else None
            except (OSError, ValueError, subprocess.SubprocessError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("headRefOid"), str):
                seen[(repo, pr)] = (data["headRefOid"], _rollup(data.get("statusCheckRollup")))
        return seen[(repo, pr)]

    for row in rows:
        k = row["correction"]
        named = ([(k["revision"], k)] if k and k["revision"] else []) + [(v, v) for v in row["verdicts"]]
        for item, mark in named:
            if isinstance(item.get("pr"), int) and isinstance(item.get("repo"), str) and item.get("commit"):
                got = head(item["repo"], item["pr"])
                mark["head"] = {"unchecked": "host not trusted"} if got == "untrusted" else \
                    {"sha": got[0], "moved": got[0] != item["commit"], "ci": got[1]} if got else {"unreadable": True}
    return "checked"


def challenge_line(row: dict) -> str:
    def head(item):
        h = item.get("head")
        if not h:
            return ""
        if h.get("unreadable"):
            return " [head unreadable]"
        if h.get("unchecked"):
            return f" [head unchecked ({h['unchecked']})]"
        return (" [head moved]" if h["moved"] else "") + f" [CI {h['ci']}]"

    parts = [f"#{row['seq']} {row['status']}"]
    pos = row["position"]
    if pos:
        parts.append(f"position #{pos['seq']} {ie._tok(pos['disposition'])}"
                     + (f" of directive #{pos['of']}" if pos["of"] != row["seq"] else ""))
    k = row["correction"]
    if k:
        rev = k["revision"]
        what = f"correction #{k['seq']}" + (f" {ie.subject_words(rev)}" if rev else "") \
            + (f" {k['artifacts']} artifacts" if k["artifacts"] else "")
        parts.append(what + head(k))
    for v in row["verdicts"]:
        at = ie.subject_words(v)
        parts.append(f"verdict #{v['seq']} {ie._tok(v['verdict'])} {ie._tok(v['scope'])}" + (f" {at}" if at else "")
                     + (f" [superseded by #{k['seq']}]" if v["superseded"] and k else "")
                     + (f" [revised by #{v['revised_by']}]" if v["revised_by"] else "")
                     + (" [revision mismatch]" if v["revision_mismatch"] else "")
                     + (" [snapshot unbound]" if v["snapshot_unbound"] else "")
                     + (" [grant revoked]" if v["revoked"] else "") + head(v))
    nxt = row["next"]
    parts.append(f"next: {nxt['actor']} {nxt['action_kind']}"
                 + (f" ({nxt['action']})" if nxt["action_kind"] != "none" else ""))
    return " · ".join(parts)


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
        "verdict",
        "challenges",
        "reviewed",
        "renew-cursor",
        "show",
        "verify",
        "propose-decision",
        "supervising",
        "proposals",
        "withdraw-proposal",
        "budget",
        "request", "respond", "resolve", "requests", "sync", "wait",
        "gate-enable", "gate-status", "gate-read", "gate-reviewed", "gate-disable",
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
        if action in ("request", "respond", "resolve"):
            p.add_argument("--text", required=True, help="request, result, blocker, abstention/rejection or resolution reason")
            p.add_argument("--key", help="retry identity; omit for a deterministic key")
        if action == "request":
            p.add_argument("--assignee", required=True, help="authenticated kind:id principal")
            p.add_argument("--assignee-session", help="required for user/key; omit for a grantee")
            p.add_argument("--intent", help="free-form dependent work; defaults to the request text")
        if action in ("sync", "wait"):
            p.add_argument("--intent", required=True, help="purpose of the work or decision being synchronized")
            p.add_argument("--request", dest="requires", action="append", type=int, default=[],
                           help="required request sequence; repeat for multiple results; retained across restarts")
            if action == "wait":
                p.add_argument("--timeout", type=float, default=120, help="bounded wait, at most 300 seconds; timeout never releases work")
                p.add_argument("--incoming", action="store_true", help="wait for new information or an assignment even with no outgoing dependency")
                p.add_argument("--after-response", type=int, help="await progress after this blocked response")
        if action in ("respond", "resolve"):
            p.add_argument("request", type=int, help="request.created event sequence")
            p.add_argument("--outcome", required=True, choices=("complete", "blocked", "abstain", "reject") if action == "respond" else ("accepted", "cancelled"))
        if action == "requests":
            p.add_argument("--request", type=int, help="one request sequence")
            p.add_argument("--wait", action="store_true", help="wait for a response, or an incoming request when --request is omitted")
            p.add_argument("--after-response", type=int, help="with --request --wait, await a newer response after a blocked update")
            p.add_argument("--timeout", type=float, default=120, help="bounded wait seconds; a timeout never releases dependent actions")
        if action == "resolve":
            p.add_argument("--response", type=int, help="exact complete response sequence, required for accepted")
        if action.startswith("gate-"):
            p.add_argument("--gate-file", type=Path, required=action == "gate-enable",
                           help="separate private gate state; never a delivery cursor")
            if action == "gate-enable":
                p.add_argument("--policy", type=Path, required=True)
                p.add_argument("--new-epoch", action="store_true", help="archive old policy and rescan from zero")
                p.add_argument("--budget-file", type=Path, help="pin an existing supervisor response-guard cache")
            elif action == "gate-read":
                p.add_argument("--batch", help="default: this session's pending gate batch")
                p.add_argument("--chunk", type=int, help="default: next undelivered chunk, or replay chunk 1")
            elif action == "gate-reviewed":
                p.add_argument("--batch", required=True)
                p.add_argument("--chunk-hash", action="append", required=True)
                p.add_argument("--budget-file", type=Path)
                for flag in ("tag", "done", "reopen", "redone"):
                    p.add_argument("--" + flag, action="append", default=[])
        if action in ("events", "show", "post", "bind", "attach", "propose-decision", "supervising", "budget",
                      "challenges"):
            p.add_argument("investigation_id", nargs=None if action in ("bind", "attach") else "?")
        if action in ("events", "reviewed", "renew-cursor", "verdict", "grant"):
            p.add_argument(
                "--cursor-file", type=Path,
                help="#190 durable pending-page review state (requires grant)"
                     + ("; with --follow and no --auto-review, a watch from the committed position"
                        if action == "events" else
                        "; --correction must be at or below its committed position (default: cursor.json beside "
                        "the credential)" if action == "verdict" else
                        "; with --succeeds-credential-file, renewed for the new grant (default: cursor.json beside "
                        "the old credential)" if action == "grant" else ""),
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
            p.add_argument("--succeeds-credential-file", type=Path, metavar="OLD",
                           help="renew the live grant in OLD by succession (keeps its ruling and verdict authority): "
                                "needs --out; renews the cursor, then deletes OLD")
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
        elif action == "verdict":
            p.description = ("A scoped, revision-bound verdict on a challenge's current correction (--correction) "
                             "or on a claim. The scope is what you actually checked; never merge_readiness or "
                             "deployment from a fixture run.")
            p.add_argument("seq", type=int, help="the challenge (with --correction) or the claim you verified")
            verdict = p.add_mutually_exclusive_group(required=True)
            for flag in gr.VERDICTS:
                verdict.add_argument("--" + flag, action="store_true")
            p.add_argument("--scope", required=True, choices=gr.VERIFICATION_SCOPES)
            p.add_argument("--correction", type=int, metavar="SEQ", help="the challenge's current correction "
                           "(required for a challenge)")
            p.add_argument("--supersedes", type=int, metavar="SEQ", help="revise your own lineage's active verdict on "
                           "the same correction and scope")
            p.add_argument("--repo", help="<host>/<owner>/<name>")
            p.add_argument("--commit", help="40 lowercase hex")
            p.add_argument("--pr", type=int)
            p.add_argument("--revision", metavar="auto", help="auto: your own checkout's HEAD, origin, open PR")
            p.add_argument("--artifact", action="append", default=[], help="a repo-relative file you checked (repeat)")
            p.add_argument("--target", help="deployment / rollout_observed: what was observed (1-200 chars)")
            p.add_argument("--observed-at", help="deployment / rollout_observed: RFC 3339 time")
            p.add_argument("--text", required=True, help="what was checked and the deciding result (1-2000 chars)")
            p.add_argument("--evidence", action="extend", nargs="+", default=[], help="ev_… / rcpt_… (>=1 unless "
                           "--unverifiable)")
            p.add_argument("--ref", action="append", default=[])
        elif action == "challenges":
            p.add_argument("--check-heads", action="store_true", help="read-only `gh pr view` of every PR named in a "
                           "correction or verdict: head moved, CI rollup")
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
        "--verified",
        "--rejected",
        "--unverifiable",
        "--check-heads",
        "--help",
        "--new-epoch",
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
    args.gate_argv = normalize_argv(argv)
    cli = CLI(wiring, args, legacy)
    try:
        if cli.scoped and args.action in ("propose-decision", "supervising"):
            print(f"not {'proposed' if args.action == 'propose-decision' else 'registered'}: an owner decision is "
                  "relayed with this machine's own key, never with an access grant; run "
                  f"{args.action} without --credential-file and without CARDINAL_INVESTIGATION_TOKEN", file=sys.stderr)
            return EXIT_USAGE
        if cli.scoped and args.action not in ("events", "post", "show", "reviewed", "verify", "rule", "proposals",
                                              "withdraw-proposal", "budget", "renew-cursor", "verdict", "challenges",
                                              "request", "respond", "resolve", "requests", "sync", "wait",
                                              "gate-enable", "gate-status", "gate-read", "gate-reviewed", "gate-disable"):
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
