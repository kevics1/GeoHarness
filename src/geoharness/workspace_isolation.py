"""Route reused OpenHarness storage helpers into GeoHarness's own state tree.

OpenHarness's project plugin and autopilot helpers create ``cwd/.openharness``
even when merely discovering disabled plugins or reading absent repo context.
Its environment overrides only cover user storage, so the two project helpers
need an in-process adapter as well. Installed dependency files stay untouched.
"""

from __future__ import annotations

import os
import sys
from hashlib import sha256
from pathlib import Path

from geoharness.config.settings import get_config_dir


def _project_config_dir(cwd: str | Path) -> Path:
    workspace = Path(cwd).resolve()
    identity = os.path.normcase(str(workspace))
    digest = sha256(identity.encode("utf-8")).hexdigest()[:16]
    path = get_config_dir() / "workspaces" / f"{workspace.name}-{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_plugins_dir(cwd: str | Path) -> Path:
    path = _project_config_dir(cwd) / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def install_workspace_isolation() -> None:
    """Install GeoHarness storage routing before any reused startup consumer.

    Helpers remain signature-compatible and use the original agent cwd to keep
    project state separate. Also update aliases already imported by upstream
    modules; future imports see the adapted helpers directly. This adapter is
    process-wide, like the existing GeoHarness backend factory replacement.
    """
    from openharness.config import paths
    from openharness.plugins import loader

    config_dir = get_config_dir()
    os.environ["OPENHARNESS_CONFIG_DIR"] = str(config_dir)
    os.environ["OPENHARNESS_DATA_DIR"] = str(config_dir / "data")
    os.environ["OPENHARNESS_LOGS_DIR"] = str(config_dir / "logs")

    replacements = {
        id(paths.get_project_config_dir): _project_config_dir,
        id(loader.get_project_plugins_dir): _project_plugins_dir,
    }
    # Upstream uses both module calls and `from ... import ...` aliases.
    # Replace only these exact function objects, never unrelated path helpers.
    for name, module in tuple(sys.modules.items()):
        if not name.startswith("openharness.") or module is None:
            continue
        for attribute, value in tuple(vars(module).items()):
            replacement = replacements.get(id(value))
            if replacement is not None:
                setattr(module, attribute, replacement)
