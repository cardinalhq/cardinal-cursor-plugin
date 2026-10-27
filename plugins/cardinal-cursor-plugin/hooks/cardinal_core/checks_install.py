"""Checks preview opt-in: the per-runtime setting and the checks skill's
install and removal, for every adapter.

Checks are reusable, evidence-backed decisions over telemetry, built on the
plugin's `cardinal-behavior` MCP tools. The preview is off unless the user
runs `cardinal-checks on` in their own terminal. Off means the agent cannot
use the skill, not just that nothing suggests it: the skill is not shipped in
the auto-loaded skills directory. It ships as a template in
`<plugin>/checks/skill/` and `on` copies it into the directory the runtime
loads skills from (`skills_dir`); `off` removes that copy. Each adapter's
SessionStart handler calls `session_start`, which reconciles the copy with
the setting (a plugin update installs a fresh plugin directory without it;
`off` in another terminal leaves a copy behind) and returns the routing rule
while the preview is on and Cardinal is connected.

Per runtime:
  claude   <plugin>/skills/checks/ (listed as cardinal:checks). Its gate hook
           also returns reloadSkills when the copy changed and denies the
           Skill tool for the skill while off (adapters/claude/hooks/
           checks-gate.py): the change applies to the running session.
  codex    <plugin>/skills/checks/ (plugin.json "skills": "./skills/").
  cursor   <plugin>/skills/checks/ (plugin.json "skills": "./skills/").
  copilot  <plugin>/skills/checks/ (.plugin/plugin.json "skills": "skills").
  gemini   ~/.gemini/extensions/cardinal/skills/checks/ (Gemini CLI loads an
           extension's skills/; cardinal-connect builds that extension), only
           when that extension is Cardinal's.
  On codex, cursor and copilot a change takes effect at the next session: no skill
  reload, no skill-tool deny. On gemini, BeforeTool --role checks-gate denies
  activate_skill for the skill while off; the user's /skills reload applies
  on / off to a running session (a hook cannot reload skills).

State, per runtime (AgentPaths(runtime).runtime_dir / "checks", removed with
the runtime's identity caches on disconnect, which turns the preview off):
  enabled.json             {"enabled": bool, "updated_at": ...}
  drafts/<draft>.json      the teaching-label ledger (lib/behavior/
                           checks_ledger.py; written by the MCP tools)

Read helpers never raise; install helpers raise OSError for the CLI to report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

from .paths import AgentPaths

RUNTIMES = ("claude", "codex", "cursor", "gemini", "copilot")
DISPLAY = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "gemini": "Gemini CLI",
           "copilot": "Copilot CLI"}
TEMPLATE_DIR = Path("checks") / "skill"
SKILL_DIR = "checks"
# Written into the installed copy so only a copy this plugin made is ever
# removed or replaced; holds the template digest it was copied from (and
# nothing time-dependent: a rebuilt Gemini extension must compare equal).
MARKER = ".cardinal-checks-install"
GEMINI_EXTENSION = Path(".gemini") / "extensions" / "cardinal"

ROUTING_RULE = (
    "Cardinal Checks preview is on. For telemetry questions, answer with the Cardinal query tools first. "
    "Use the checks skill only when the answer needs a judgment about every trace: relationships between "
    "events inside one trace, reading what was said, or a rule the user wants to reuse. Never build a "
    "check for something one aggregate query answers (counts, rates, top endpoints, latency); answer it."
)

# What an agent is told when it tries the checks skill while the preview is
# off: Claude Code's Skill-tool deny (adapters/claude/hooks/checks-gate.py)
# and Gemini CLI's activate_skill deny (BeforeTool --role checks-gate).
DENY_REASON = (
    "The Cardinal Checks preview is off on this machine, so the checks skill is unavailable. Answer with the "
    "ordinary Cardinal query tools, or tell the user that per-trace checks are not enabled "
    "(`cardinal-checks on` turns the preview on)."
)
# The checks skill's name in its SKILL.md frontmatter (Gemini CLI's
# activate_skill passes it as tool_input.name).
SKILL_NAME = "cardinal-checks"


def _runtime(runtime: str) -> str:
    if runtime not in RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}")
    return runtime


def home_dir(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    return Path(os.environ.get("HOME") or str(Path.home()))


def agent_paths(runtime: str, home: Optional[Path] = None) -> AgentPaths:
    return AgentPaths(home_dir(home) / f".{_runtime(runtime)}")


def state_dir(runtime: str, home: Optional[Path] = None) -> Path:
    return agent_paths(runtime, home).runtime_dir / "checks"


# --- the cardinal-behavior MCP server's registration --------------------------

BEHAVIOR_SERVER = "cardinal-behavior"
_CODEX_BEHAVIOR_TABLE_RE = re.compile(
    r'(?m)^\s*\[mcp_servers\.(?:"cardinal-behavior"|cardinal-behavior)\]\s*(?:#.*)?$')


def _json_servers(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def behavior_registered(runtime: str, home: Optional[Path] = None) -> bool:
    """Whether the runtime's own config registers the cardinal-behavior MCP
    server the checks skill uses (managed or the user's own entry):
      claude   always: the plugin's .mcp.json ships it
      codex    [mcp_servers.cardinal-behavior] in ~/.codex/config.toml
      cursor   mcpServers.cardinal-behavior in ~/.cursor/mcp.json
      copilot  mcpServers.cardinal-behavior in ~/.copilot/mcp-config.json
               ($COPILOT_HOME/mcp-config.json when COPILOT_HOME is set)
      gemini   mcpServers.cardinal-behavior in Cardinal's extension manifest
               or ~/.gemini/settings.json (a --no-extension install)
    A connection made before the Checks preview has none until its repair
    (repair_command) runs. Never raises."""
    runtime = _runtime(runtime)
    if runtime == "claude":
        return True
    agent = agent_paths(runtime, home).home
    if runtime == "codex":
        try:
            return bool(_CODEX_BEHAVIOR_TABLE_RE.search((agent / "config.toml").read_text()))
        except (OSError, UnicodeDecodeError):
            return False
    if runtime == "cursor":
        return BEHAVIOR_SERVER in _json_servers(agent / "mcp.json")
    if runtime == "copilot":
        return BEHAVIOR_SERVER in _json_servers(copilot_mcp_config(home))
    return any(BEHAVIOR_SERVER in _json_servers(path)
               for path in (gemini_extension_dir(home) / "gemini-extension.json", agent / "settings.json"))


def copilot_mcp_config(home: Optional[Path] = None) -> Path:
    """Copilot CLI's user MCP config: $COPILOT_HOME/mcp-config.json (an
    absolute COPILOT_HOME, and only without an explicit `home`), else
    ~/.copilot/mcp-config.json."""
    custom = os.environ.get("COPILOT_HOME") if home is None else None
    base = Path(custom) if custom and os.path.isabs(custom) else home_dir(home) / ".copilot"
    return base / "mcp-config.json"


def is_connected(runtime: str, home: Optional[Path] = None) -> bool:
    return agent_paths(runtime, home).state_path.is_file()


def repair_command(runtime: str, root: Path) -> Optional[str]:
    """The command that registers cardinal-behavior on an existing
    connection without new credentials (None for claude: nothing to repair).
    Codex: --repair-hooks; Cursor, Gemini CLI and Copilot CLI: a re-run without --rotate."""
    if _runtime(runtime) == "claude":
        return None
    script = shlex.quote(str(Path(root) / "scripts" / "cardinal-connect"))
    return f"python3 {script} --repair-hooks" if runtime == "codex" else f"python3 {script}"


# --- opt-in ---------------------------------------------------------------

def is_enabled(runtime: str, home: Optional[Path] = None) -> bool:
    try:
        data = json.loads((state_dir(runtime, home) / "enabled.json").read_text())
        return isinstance(data, dict) and data.get("enabled") is True
    except (OSError, ValueError):
        return False


def set_enabled(runtime: str, enabled: bool, home: Optional[Path] = None) -> None:
    """Write enabled.json atomically, 0600 in a 0700 checks state dir (the
    same discipline as the draft ledger, lib/behavior/checks_ledger.py)."""
    state = state_dir(runtime, home)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    path = state / "enabled.json"
    fd, tmp = tempfile.mkstemp(prefix=".enabled.json-", dir=str(state))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"enabled": bool(enabled), "updated_at": time.time()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- where the skill goes -----------------------------------------------------

def template_dir(root: Path) -> Path:
    return Path(root) / TEMPLATE_DIR


def gemini_extension_dir(home: Optional[Path] = None) -> Path:
    return home_dir(home) / GEMINI_EXTENSION


def skills_dir(runtime: str, root: Path, home: Optional[Path] = None) -> Optional[Path]:
    """The directory this runtime loads the plugin's skills from, or None
    when there is none to install into (Gemini without Cardinal's extension:
    `cardinal-connect --no-extension`, or not connected)."""
    if _runtime(runtime) != "gemini":
        return Path(root) / "skills"
    ext = gemini_extension_dir(home)
    try:
        manifest = json.loads((ext / "gemini-extension.json").read_text())
    except (OSError, ValueError):
        return None
    if not (isinstance(manifest, dict) and manifest.get("cardinalManaged")):
        return None
    return ext / "skills"


def install_dir(runtime: str, root: Path, home: Optional[Path] = None) -> Optional[Path]:
    skills = skills_dir(runtime, root, home)
    return skills / SKILL_DIR if skills is not None else None


def template_digest(root: Path) -> Optional[str]:
    """SHA-256 over the template's relative paths and bytes; None when absent."""
    src = template_dir(root)
    if not (src / "SKILL.md").is_file():
        return None
    h = hashlib.sha256()
    for path in sorted(p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(path.relative_to(src).as_posix().encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def installed_digest(dest: Optional[Path]) -> Optional[str]:
    """The template digest recorded in an installed copy this plugin made."""
    if dest is None:
        return None
    try:
        data = json.loads((Path(dest) / MARKER).read_text())
        return data.get("template_sha256") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def is_installed(dest: Optional[Path]) -> bool:
    return dest is not None and (Path(dest) / "SKILL.md").is_file()


def install(root: Path, dest: Path) -> bool:
    """Copy the template at root to dest. True when anything changed.

    Leaves an up-to-date copy alone; replaces one this plugin made from an
    older template. Raises FileNotFoundError when the template is missing,
    FileExistsError when dest exists but was not made here."""
    digest = template_digest(root)
    if digest is None:
        raise FileNotFoundError(f"checks skill template missing: {template_dir(root)}")
    dest = Path(dest)
    current = installed_digest(dest)
    if current == digest and is_installed(dest):
        return False
    if dest.exists() and current is None:
        raise FileExistsError(f"{dest} exists and was not installed by cardinal-checks")
    staging = dest.with_name(".checks-staging")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(template_dir(root), staging, ignore=shutil.ignore_patterns("__pycache__"))
    (staging / MARKER).write_text(json.dumps({"template_sha256": digest}) + "\n")
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)
    return True


def uninstall(dest: Optional[Path]) -> bool:
    """Remove dest if this plugin installed it. True when removed."""
    if dest is None or not Path(dest).exists():
        return False
    dest = Path(dest)
    if installed_digest(dest) is None and not (dest / MARKER).exists():
        raise FileExistsError(f"{dest} was not installed by cardinal-checks; not removing it")
    shutil.rmtree(dest)
    return True


def reconcile(runtime: str, root: Path, home: Optional[Path] = None) -> bool:
    """Make the installed copy match the opt-in. True when it changed."""
    dest = install_dir(runtime, root, home)
    if dest is None:
        return False
    if is_enabled(runtime, home):
        return install(root, dest)
    return uninstall(dest)


def session_start(runtime: str, root: Path, *, connected: bool,
                  home: Optional[Path] = None) -> Tuple[bool, Optional[str]]:
    """The SessionStart part every adapter shares: (changed, routing context).

    Reconciles the installed skill (an OSError counts as unchanged), then
    returns ROUTING_RULE only while the preview is on and `connected`. Off:
    no text at all. Never raises."""
    try:
        changed = reconcile(runtime, root, home)
    except (OSError, ValueError):
        changed = False
    try:
        context = ROUTING_RULE if connected and is_enabled(runtime, home) else None
    except ValueError:
        context = None
    return changed, context


def is_checks_skill(name, cwd: Optional[str] = None, home: Optional[Path] = None) -> bool:
    """True for Claude's plugin checks skill as its Skill tool names it.

    Claude Code passes a plugin skill as "cardinal:checks". A bare "checks"
    counts too, unless the user or project has a skill of their own by that
    name (which a bare name would then mean)."""
    if not isinstance(name, str):
        return False
    name = name.strip().lstrip("/")
    if name == "cardinal:checks":
        return True
    if name != "checks":
        return False
    own = [home_dir(home) / ".claude" / "skills" / "checks"]
    if cwd:
        own.append(Path(cwd) / ".claude" / "skills" / "checks")
    return not any((d / "SKILL.md").is_file() for d in own)
