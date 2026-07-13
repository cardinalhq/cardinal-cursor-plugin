"""Bash verb classification — a closed enum derived from the command WORD
only; the command string is never emitted on cardinal.turn_tool, in whole
or in part. Ambiguity resolves toward the write-risky side (the harvester
discounts write work, so misclassifying read-as-write only costs savings
estimate, never privacy or correctness).

This is the single copy; per-plugin fixture parity is enforced by the
cross-adapter contract test (spec §Test strategy).

Write-risk ordering: when a compound command spans classes, the
lowest-index class wins and bash_multi=true is emitted.
"""

from __future__ import annotations

BASH_CLASS_RANK = (
    "file-write",
    "git-write",
    "pkg",
    "network",
    "build",
    "test",
    "git-read",
    "file-read",
    "other",
)

# Single-word command → class. Unknown words → "other".
BASH_CMD_CLASS = {
    # test
    "pytest": "test", "tox": "test", "jest": "test", "vitest": "test",
    "rspec": "test", "phpunit": "test",
    # build
    "make": "build", "cmake": "build", "tsc": "build", "gradle": "build",
    "mvn": "build", "gcc": "build", "clang": "build", "webpack": "build",
    # pkg
    "pip": "pkg", "pip3": "pkg", "brew": "pkg", "gem": "pkg",
    "apt": "pkg", "apt-get": "pkg", "yum": "pkg", "dnf": "pkg",
    "apk": "pkg", "poetry": "pkg", "uv": "pkg",
    # file-read
    "ls": "file-read", "cat": "file-read", "find": "file-read",
    "grep": "file-read", "rg": "file-read", "head": "file-read",
    "tail": "file-read", "wc": "file-read", "du": "file-read",
    "df": "file-read", "stat": "file-read", "file": "file-read",
    "tree": "file-read", "which": "file-read", "pwd": "file-read",
    "less": "file-read", "more": "file-read", "diff": "file-read",
    "awk": "file-read", "echo": "file-read", "sort": "file-read",
    "uniq": "file-read", "cut": "file-read", "jq": "file-read",
    # file-write (sed classifies here: -i vs not is an argument, and
    # arguments are never consulted — write-risky wins)
    "rm": "file-write", "mv": "file-write", "cp": "file-write",
    "mkdir": "file-write", "rmdir": "file-write", "chmod": "file-write",
    "chown": "file-write", "touch": "file-write", "ln": "file-write",
    "sed": "file-write", "tee": "file-write", "truncate": "file-write",
    "dd": "file-write", "tar": "file-write", "unzip": "file-write",
    "zip": "file-write",
    # network
    "curl": "network", "wget": "network", "gh": "network",
    "ssh": "network", "scp": "network", "rsync": "network",
    "nc": "network", "ping": "network", "dig": "network",
    "host": "network", "nslookup": "network",
}

# Multiplexer commands whose class hangs on the SUBcommand word (still
# never an argument): {cmd: (subcommand → class, default class)}.
GIT_READ_SUBS = {
    "status", "log", "diff", "show", "blame", "shortlog", "reflog",
    "describe", "rev-parse", "ls-files", "ls-remote", "ls-tree",
    "cat-file", "grep",
}
BASH_MULTIPLEX_CLASS = {
    # git subcommands outside the read set default to git-write
    # (write-risky wins for branch/tag/stash-style ambiguity).
    "git": ({s: "git-read" for s in GIT_READ_SUBS}, "git-write"),
    "go": (
        {"test": "test", "vet": "test",
         "build": "build", "run": "build", "generate": "build",
         "get": "pkg", "install": "pkg", "mod": "pkg"},
        "other",
    ),
    "cargo": (
        {"test": "test", "bench": "test",
         "build": "build", "check": "build", "run": "build",
         "clippy": "build",
         "add": "pkg", "install": "pkg", "update": "pkg",
         "remove": "pkg"},
        "other",
    ),
    "npm": (
        {"test": "test", "run": "build", "exec": "build"},
        "pkg",  # install/i/ci/add/uninstall/update/…
    ),
    "pnpm": (
        {"test": "test", "run": "build", "exec": "build"},
        "pkg",
    ),
    "yarn": (
        {"test": "test", "run": "build"},
        "pkg",
    ),
    "bun": (
        {"test": "test", "run": "build", "build": "build"},
        "pkg",
    ),
}


def classify_bash_command(command: str) -> tuple[str, bool] | None:
    """Map a Bash command string to (bash_class, bash_multi).

    Tokenizes on shell separators (&&, ||, ;, |, newline); classifies
    each segment by its leading command word after stripping env-var
    prefixes and sudo; the most write-risky class present wins
    (BASH_CLASS_RANK order). bash_multi is True when segments span more
    than one class. Only the command/subcommand WORD feeds the lookup —
    no argument ever does, and nothing from the string is returned
    beyond the closed enum. Returns None when no command word is found.
    """
    for sep in ("&&", "||", ";", "|", "\n"):
        command = command.replace(sep, "\x00")
    classes: set[str] = set()
    for segment in command.split("\x00"):
        words = segment.split()
        # Strip env-var prefixes (FOO=bar) and sudo from the front.
        while words and ("=" in words[0] or words[0] == "sudo"):
            words.pop(0)
        if not words:
            continue
        cmd = words[0].rsplit("/", 1)[-1]  # /usr/bin/git → git
        mux = BASH_MULTIPLEX_CLASS.get(cmd)
        if mux is not None:
            sub_map, default = mux
            sub = words[1] if len(words) > 1 else ""
            classes.add(sub_map.get(sub, default))
        else:
            classes.add(BASH_CMD_CLASS.get(cmd, "other"))
    if not classes:
        return None
    winner = min(classes, key=BASH_CLASS_RANK.index)
    return winner, len(classes) > 1
