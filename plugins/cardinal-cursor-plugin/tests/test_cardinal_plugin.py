"""Unit tests for cardinal-cursor-plugin.

Contract parity with cardinal-codex-plugin's fixture suite:
  * Initiative resolution + worktree stripping fixtures MUST match the
    Codex plugin (docs/specs/cursor-parity.md §Keeping the repos in
    lockstep).
  * Bash-class classifier fixtures MUST match Codex.
  * detect_command MUST recognize both raw `/cmd` and `<command-name>` forms.

Cursor-specific tests cover:
  * JSON managed-block round-trip for mcp.json / hooks.json.
  * Three-tier notify/warn resolution (block, degraded-notify, strict-warn
    escalation).
  * postToolUse handler consumes staged notify once per turn.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PLUGIN_ROOT / "hooks"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def _load_module(name: str, path: Path):
    """Load a Python file whether or not it has a .py extension. Cursor
    scripts (`cardinal-connect`, etc.) have no extension by design, so
    the default spec_from_file_location loader guessing fails on them."""
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(HOOKS_DIR))
        except ValueError:
            pass
    return module


class _CursorHome:
    """Scoped ~/.cursor override so tests never touch the real dotdir."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cardinal-cursor-test-"))
        self.cursor = self.tmp / ".cursor"
        self.cursor.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.tmp)})
        self._env.start()

    def close(self) -> None:
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ContractParityTests(unittest.TestCase):
    """These fixtures MUST mirror cardinal-codex-plugin's suite verbatim.
    Any diff here is a spec violation — fix the plugin, not the test."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry",
                                 HOOKS_DIR / "cardinal-cursor-telemetry.py")

    def test_resolve_initiative_branch_fixtures(self) -> None:
        cases = [
            ("main", (None, "research")),
            ("master", (None, "research")),
            ("develop", (None, "research")),
            ("HEAD", (None, "research")),
            ("feat/outcomes-observability", ("outcomes-observability", "feature")),
            ("feature/outcomes", ("outcomes", "feature")),
            ("perf/render-fast", ("render-fast", "feature")),
            ("fix/login-crash", ("login-crash", "bugfix")),
            ("bugfix/login", ("login", "bugfix")),
            ("refactor/auth-token-rotation", ("auth-token-rotation", "refactor")),
            ("cleanup/dead-code", ("dead-code", "refactor")),
            ("infra/observability", ("observability", "infra")),
            ("chore/upgrade-deps", ("upgrade-deps", "infra")),
            ("test/gate-suite", ("gate-suite", "infra")),
            ("ci/pin-actions", ("pin-actions", "infra")),
            ("build/docker", ("docker", "infra")),
            ("docs/telemetry", ("telemetry", "infra")),
            ("research/data-pipeline-spike", ("data-pipeline-spike", "research")),
            ("spike/prototype", ("prototype", "research")),
            ("weird-branch-name", ("weird-branch-name", "feature")),
            # Unknown prefix + slash: the branch is NOT partitioned, since
            # only a mapped prefix triggers the split. Full name survives.
            ("someone/foo/bar", ("someone/foo/bar", "feature")),
        ]
        for branch, expected in cases:
            with self.subTest(branch=branch):
                self.assertEqual(self.hook.resolve_initiative(branch), expected)

    def test_worktree_noise_stripping(self) -> None:
        cases = [
            ("worktree-fix-1018-github-app-repo-picker", "github-app-repo-picker"),
            ("worktree-feat-42-outcomes-observability", "outcomes-observability"),
            ("worktree-bug-100-login", "login"),
            ("worktree-1234-simple", "simple"),
            ("worktree-pr-77-review-comments", "review-comments"),
            ("worktree-fix-only", "only"),
            # Non-worktree passes through verbatim.
            ("regular-branch", "regular-branch"),
            ("feat/scope", "feat/scope"),
            # All-noise leaves the original name (conservative).
            ("worktree-fix-1234", "worktree-fix-1234"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.hook.strip_worktree_noise(raw), expected)

    def test_detect_command_forms(self) -> None:
        # Raw form (anchored at start).
        self.assertEqual(self.hook.detect_command("/code-review --fix"), "code-review")
        self.assertEqual(self.hook.detect_command("  /docs help"), "docs")
        self.assertIsNone(self.hook.detect_command("please run /docs later"))
        # Tag form (anywhere).
        self.assertEqual(self.hook.detect_command("<command-name>/simplify</command-name>"), "simplify")
        self.assertEqual(self.hook.detect_command("<command-name>verify</command-name>"), "verify")
        # Non-strings.
        self.assertIsNone(self.hook.detect_command(None))
        self.assertIsNone(self.hook.detect_command(42))

    def test_bash_class_fixtures(self) -> None:
        cases = [
            ("ls -la", ("file-read", False)),
            ("rm -rf build/", ("file-write", False)),
            ("git status", ("git-read", False)),
            ("git commit -m foo", ("git-write", False)),
            ("git status && git commit", ("git-write", True)),  # git-write wins
            ("pytest -k thing", ("test", False)),
            # `make` is unconditionally "build" (arguments never consulted),
            # so both segments collapse to one class and multi=False.
            ("make build && make test", ("build", False)),
            # Real cross-class multi: build then test binaries.
            ("tsc && pytest -k thing", ("build", True)),
            ("curl https://example.com", ("network", False)),
            ("sudo apt-get install foo", ("pkg", False)),
            ("cd /tmp", ("other", False)),  # unknown word
            ("FOO=bar rm file", ("file-write", False)),
            ("go test ./...", ("test", False)),
            ("go build ./...", ("build", False)),
            ("npm install", ("pkg", False)),
            ("cargo add serde", ("pkg", False)),
            ("cat foo | grep bar", ("file-read", False)),  # both file-read
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(self.hook.classify_bash_command(command), expected)


class ToolNormalizationTests(unittest.TestCase):
    """Cursor-shaped tool inputs → normalized (display_name, params, target)."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_norm",
                                 HOOKS_DIR / "cardinal-cursor-telemetry.py")

    def test_shell_tool_names_route_to_bash(self) -> None:
        for raw in ("run_terminal_cmd", "run_shell_command", "shell", "terminal"):
            display, extra, _ = self.hook.normalize_tool_name(raw, {"command": "ls -la"})
            self.assertEqual(display, "Bash")
            self.assertEqual(extra["full_command"], "ls -la")
            self.assertEqual(extra["bash_command"], "ls")

    def test_mcp_prefixed_names_split(self) -> None:
        display, extra, _ = self.hook.normalize_tool_name("mcp__cardinal__lakerunner__list_services", {})
        self.assertEqual(display, "mcp_tool")
        self.assertEqual(extra["mcp_server_name"], "cardinal")
        # Multi-underscore tool paths preserve the tail.
        self.assertEqual(extra["mcp_tool_name"], "lakerunner__list_services")

    def test_unknown_tool_passes_through(self) -> None:
        display, extra, _ = self.hook.normalize_tool_name("my_custom_tool", {"path": "/foo"})
        self.assertEqual(display, "my_custom_tool")
        self.assertEqual(extra, {})


class LimitsGateTests(unittest.TestCase):
    """Three-tier resolution from docs/specs/cursor-parity.md Divergence E."""

    def setUp(self) -> None:
        self.home = _CursorHome()
        # Re-import so module-level path constants pick up the new HOME.
        for mod in list(sys.modules):
            if mod in {"_limits_common", "cursor_telemetry_gate"}:
                del sys.modules[mod]
        self.hook = _load_module("cursor_telemetry_gate",
                                 HOOKS_DIR / "cardinal-cursor-telemetry.py")
        self.lc = _load_module("_limits_common", HOOKS_DIR / "_limits_common.py")

        # Minimal state so limits_config() and ingest_api_key() succeed.
        state = {
            "ingest_endpoint": "https://ingest.example",
            "limits": {"status_url": "https://limits.example/status", "enabled": True},
        }
        (self.home.cursor / "cardinal.json").write_text(json.dumps(state))
        (self.home.cursor / "cardinal-secrets.json").write_text(json.dumps({"ingest_api_key": "abc"}))

        self.conv = "conv-1"

    def tearDown(self) -> None:
        self.home.close()

    def _write_verdict(self, verdict: dict) -> None:
        import time as _t
        v = dict(verdict)
        v.setdefault("fetched_at", _t.time())
        self.lc.atomic_write_json(self.lc.verdict_path(self.conv), v)

    def test_block_verdict_emits_continue_false_and_user_message(self) -> None:
        self._write_verdict({"decision": "block", "band": 3,
                             "user_message": "You've hit the session cap.",
                             "block_reason": "Session cap reached."})
        out = self.hook.limits_gate_output(self.conv)
        self.assertEqual(out, {"continue": False, "user_message": "Session cap reached."})

    def test_block_override_downgrades_to_notify_staging(self) -> None:
        self._write_verdict({"decision": "block", "band": 3,
                             "block_reason": "Session cap.",
                             "user_message": "You've hit the cap.",
                             "agent_context": "Budget context here."})
        self.lc.override_path(self.conv).parent.mkdir(parents=True, exist_ok=True)
        self.lc.override_path(self.conv).write_text("{}")

        out = self.hook.limits_gate_output(self.conv)
        self.assertIsNone(out)  # No block emitted; staged for postToolUse.
        staged = json.loads(self.lc.notify_path(self.conv).read_text())
        self.assertIn("Budget context here.", staged["message"])
        self.assertIn("You've hit the cap.", staged["message"])

    def test_notify_band_stages_agent_context_only(self) -> None:
        self._write_verdict({"decision": "allow", "band": 1,
                             "agent_context": "You are at 60% of session budget."})
        out = self.hook.limits_gate_output(self.conv)
        self.assertIsNone(out)
        staged = json.loads(self.lc.notify_path(self.conv).read_text())
        self.assertEqual(staged["message"], "You are at 60% of session budget.")

    def test_strict_warn_escalates_to_block(self) -> None:
        self._write_verdict({"decision": "warn", "band": 2,
                             "user_message": "Careful — approaching cap."})
        with mock.patch.dict(os.environ, {"CARDINAL_CURSOR_STRICT_WARN": "1"}):
            out = self.hook.limits_gate_output(self.conv)
        self.assertIsNotNone(out)
        self.assertFalse(out["continue"])
        self.assertIn("Careful — approaching cap.", out["user_message"])

    def test_warn_hysteresis_only_stages_once_per_band(self) -> None:
        self._write_verdict({"decision": "warn", "band": 2,
                             "user_message": "Slow down.", "agent_context": "ctx"})
        self.assertIsNone(self.hook.limits_gate_output(self.conv))
        self.assertTrue(self.lc.notify_path(self.conv).exists())
        # Simulate the notify being consumed by postToolUse.
        self.lc.notify_path(self.conv).unlink()
        # Same band, second turn: ack already recorded → no re-stage.
        self.assertIsNone(self.hook.limits_gate_output(self.conv))
        self.assertFalse(self.lc.notify_path(self.conv).exists())


class NotifyConsumeTests(unittest.TestCase):
    """postToolUse picks up the staged notify once, then removes it."""

    def setUp(self) -> None:
        self.home = _CursorHome()
        for mod in list(sys.modules):
            if mod in {"_limits_common"}:
                del sys.modules[mod]
        self.lc = _load_module("_limits_common", HOOKS_DIR / "_limits_common.py")

    def tearDown(self) -> None:
        self.home.close()

    def test_consume_notify_reads_once(self) -> None:
        conv = "conv-2"
        self.lc.atomic_write_json(self.lc.notify_path(conv), {"message": "hello"})
        self.assertEqual(self.lc.consume_notify(conv), "hello")
        # File is gone; second consume returns None.
        self.assertIsNone(self.lc.consume_notify(conv))

    def test_consume_notify_missing_is_none(self) -> None:
        self.assertIsNone(self.lc.consume_notify("nonexistent"))


class JsonManagedBlockTests(unittest.TestCase):
    """Round-trip: connect writes → disconnect strips → foreign content
    preserved."""

    def setUp(self) -> None:
        self.home = _CursorHome()
        self.connect = _load_module("cardinal_connect", SCRIPTS_DIR / "cardinal-connect")
        self.disconnect = _load_module("cardinal_disconnect", SCRIPTS_DIR / "cardinal-disconnect")

    def tearDown(self) -> None:
        self.home.close()

    def test_mcp_write_then_strip_preserves_foreign_entries(self) -> None:
        path = self.home.cursor / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"other": {"url": "https://foreign"}}}))

        self.connect.write_mcp_config(path, "https://mcp.example", "key")
        data = json.loads(path.read_text())
        self.assertIn("other", data["mcpServers"])
        self.assertTrue(data["mcpServers"]["cardinal"]["cardinalManaged"])

        self.disconnect.remove_mcp_entry(path)
        data = json.loads(path.read_text())
        self.assertIn("other", data["mcpServers"])
        self.assertNotIn("cardinal", data["mcpServers"])

    def test_unmanaged_cardinal_entry_is_refused(self) -> None:
        path = self.home.cursor / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"cardinal": {"url": "https://user-wrote-this"}}
        }))
        with self.assertRaises(SystemExit):
            self.connect.write_mcp_config(path, "https://mcp.example", "key")

    def test_hooks_write_then_strip_preserves_foreign_hooks(self) -> None:
        path = self.home.cursor / "hooks.json"
        path.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": "echo foreign", "type": "command"}]
            }
        }))
        self.connect.write_hooks_config(path)
        data = json.loads(path.read_text())
        session_hooks = data["hooks"]["sessionStart"]
        cmds = [h["command"] for h in session_hooks]
        self.assertTrue(any("echo foreign" in c for c in cmds))
        self.assertTrue(any("cardinal-cursor-plugin" in c for c in cmds))

        self.disconnect.remove_hooks_config(path)
        data = json.loads(path.read_text())
        cmds = [h["command"] for h in data["hooks"].get("sessionStart", [])]
        self.assertTrue(any("echo foreign" in c for c in cmds))
        self.assertFalse(any("cardinal-cursor-plugin" in c for c in cmds))

    def test_hooks_registers_all_managed_events(self) -> None:
        path = self.home.cursor / "hooks.json"
        self.connect.write_hooks_config(path)
        data = json.loads(path.read_text())
        events = set(data["hooks"].keys())
        self.assertEqual(events, {
            "sessionStart", "beforeSubmitPrompt", "postToolUse",
            "preCompact", "stop", "subagentStop",
            # v0.2.0: emit cardinal.turn_response / cardinal.turn_thought.
            "afterAgentResponse", "afterAgentThought",
        })


class ManifestTests(unittest.TestCase):
    def test_plugin_json_shape(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / ".cursor-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "cardinal-cursor-plugin")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+")
        self.assertEqual(manifest["license"], "Apache-2.0")

    def test_plugin_version_loads_from_manifest(self) -> None:
        for mod in list(sys.modules):
            if mod == "_plugin_version":
                del sys.modules[mod]
        pv = _load_module("_plugin_version", HOOKS_DIR / "_plugin_version.py")
        pv.plugin_version.cache_clear()
        version = pv.plugin_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+")


class ResourceAttrsTests(unittest.TestCase):
    """Base OTel resource attributes stamp Cursor identity from the
    hook payload's base fields (model, model_id, model_params,
    cursor_version)."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_ra",
                                 HOOKS_DIR / "cardinal-cursor-telemetry.py")

    def test_base_attrs_without_payload(self) -> None:
        attrs = self.hook.resource_attrs({"deployment_environment": "prod",
                                          "user_email": "a@b.com",
                                          "org_slug": "acme"})
        self.assertEqual(attrs["service.name"], "cursor")
        self.assertEqual(attrs["deployment.environment"], "prod")
        self.assertEqual(attrs["user.email"], "a@b.com")
        self.assertEqual(attrs["cardinal.org"], "acme")
        self.assertNotIn("cursor.model", attrs)
        self.assertNotIn("cursor.version", attrs)

    def test_payload_stamps_model_and_version(self) -> None:
        payload = {
            "model": "claude-3.5-sonnet",
            "model_id": "anthropic/claude-3.5-sonnet",
            "model_params": {"temperature": 0.2, "max_tokens": 4096},
            "cursor_version": "0.44.11",
        }
        attrs = self.hook.resource_attrs({}, payload)
        self.assertEqual(attrs["cursor.model"], "claude-3.5-sonnet")
        self.assertEqual(attrs["cursor.model_id"], "anthropic/claude-3.5-sonnet")
        self.assertEqual(attrs["cursor.version"], "0.44.11")
        # model_params serialized to JSON string.
        self.assertEqual(json.loads(attrs["cursor.model_params"]),
                         {"temperature": 0.2, "max_tokens": 4096})

    def test_payload_string_model_params_passthrough(self) -> None:
        attrs = self.hook.resource_attrs({}, {"model_params": "opaque"})
        self.assertEqual(attrs["cursor.model_params"], "opaque")

    def test_payload_missing_fields_skipped(self) -> None:
        attrs = self.hook.resource_attrs({}, {"model": ""})
        self.assertNotIn("cursor.model", attrs)


class PreCompactEmitTests(unittest.TestCase):
    """preCompact payload → cardinal.plan_usage (context-window slice)."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_pc",
                                 HOOKS_DIR / "cardinal-cursor-telemetry.py")

    def test_pre_compact_emits_plan_usage(self) -> None:
        payload = {
            "conversation_id": "conv-1",
            "trigger": "auto",
            "context_usage_percent": 87,
            "context_tokens": 174_000,
            "context_window_size": 200_000,
            "message_count": 42,
            "messages_to_compact": 30,
            "is_first_compaction": True,
            "model": "claude-3.5-sonnet",
            "cursor_version": "0.44.11",
        }
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda records, payload=None: captured.append((records, payload))):
            self.hook.handle_pre_compact(payload)
        self.assertEqual(len(captured), 1)
        records, forwarded_payload = captured[0]
        self.assertEqual(len(records), 1)
        rec = records[0]
        # Body carries event name.
        self.assertEqual(rec["body"]["stringValue"], "cardinal.plan_usage")
        # Attributes contain the plan.* keys.
        attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
        self.assertEqual(attrs["plan.compact_trigger"], "auto")
        self.assertEqual(attrs["plan.context_tokens"], "174000")
        self.assertEqual(attrs["plan.context_window"], "200000")
        self.assertEqual(attrs["plan.messages_to_compact"], "30")
        self.assertTrue(attrs["plan.is_first_compaction"])
        # Payload forwarded so resource_attrs can stamp cursor.model etc.
        self.assertIs(forwarded_payload, payload)

    def test_pre_compact_no_conv_id_no_emit(self) -> None:
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda *a, **kw: captured.append(a)):
            self.hook.handle_pre_compact({"trigger": "auto"})
        self.assertEqual(captured, [])


class ThoughtResponseEmitTests(unittest.TestCase):
    """afterAgentThought / afterAgentResponse emit length-only events."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_tr",
                                 HOOKS_DIR / "cardinal-cursor-telemetry.py")

    def test_thought_emits_duration_and_len_never_text(self) -> None:
        payload = {
            "conversation_id": "conv-1",
            "duration_ms": 1234,
            "text": "internal chain-of-thought, do not emit",
        }
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda records, payload=None: captured.append(records)):
            self.hook.handle_after_agent_thought(payload)
        self.assertEqual(len(captured), 1)
        rec = captured[0][0]
        self.assertEqual(rec["body"]["stringValue"], "cardinal.turn_thought")
        attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
        self.assertEqual(attrs["thought.duration_ms"], "1234")
        self.assertEqual(attrs["thought.text_len"], str(len(payload["text"])))
        # The raw text MUST NOT appear in any attribute value.
        for a in rec["attributes"]:
            for v in a["value"].values():
                self.assertNotIn("chain-of-thought", str(v))

    def test_response_emits_text_len_never_text(self) -> None:
        payload = {
            "conversation_id": "conv-1",
            "text": "final response body containing user code",
        }
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda records, payload=None: captured.append(records)):
            self.hook.handle_after_agent_response(payload)
        self.assertEqual(len(captured), 1)
        rec = captured[0][0]
        self.assertEqual(rec["body"]["stringValue"], "cardinal.turn_response")
        attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
        self.assertEqual(attrs["response.text_len"], str(len(payload["text"])))
        for a in rec["attributes"]:
            for v in a["value"].values():
                self.assertNotIn("user code", str(v))

    def test_response_no_conv_id_no_emit(self) -> None:
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda *a, **kw: captured.append(a)):
            self.hook.handle_after_agent_response({"text": "x"})
        self.assertEqual(captured, [])


class SubagentTests(unittest.TestCase):
    """Cursor's documented subagentStop payload keys become subagent_usage."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_sa",
                                 HOOKS_DIR / "cardinal-cursor-telemetry.py")

    def test_description_prefers_description_then_task_then_summary(self) -> None:
        f = self.hook.subagent_description_from_payload
        self.assertEqual(f({"description": "primary", "task": "t", "summary": "s"}), "primary")
        self.assertEqual(f({"task": "t", "summary": "s"}), "t")
        self.assertEqual(f({"summary": "s"}), "s")
        self.assertIsNone(f({}))

    def test_description_truncated_to_160_chars(self) -> None:
        long = "x" * 500
        self.assertEqual(len(self.hook.subagent_description_from_payload({"description": long})), 160)


if __name__ == "__main__":
    unittest.main()
