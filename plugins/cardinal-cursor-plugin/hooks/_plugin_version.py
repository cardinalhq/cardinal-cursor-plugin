"""Resolve this plugin's canonical version at hook-execution time.

The version stamped on emitted telemetry (`cardinal.plugin_version`
resource attribute + OTel scope version) must reflect the plugin
package that is CURRENTLY installed, not a constant hardcoded in
source that developers have to remember to bump on every release.

Reading from the sibling `../.cursor-plugin/plugin.json` at every
hook execution keeps the stamp in lockstep with `plugin.json` — the
same file the release tooling already owns.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache


_FALLBACK = "unknown"


@lru_cache(maxsize=1)
def plugin_version() -> str:
    """Read the plugin's canonical semver from ../.cursor-plugin/plugin.json.

    Returns "unknown" if the file is missing or unparseable — never
    raises, since a version-stamp bug must not break telemetry
    emission itself.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    manifest = os.path.join(here, "..", ".cursor-plugin", "plugin.json")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _FALLBACK
    v = data.get("version")
    if isinstance(v, str) and v:
        return v
    return _FALLBACK
