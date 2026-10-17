"""Local presentation extensions without modifying the installed OpenHarness frontend."""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def frontend_overlay(upstream: Path):
    """Reuse upstream source/dependencies with our tool-row component for one launch.

    Only the small frontend source is copied. On Windows a junction avoids requiring
    symlink privileges; TemporaryDirectory/rmtree removes the junction, not its target.
    No backend events or conversation messages are transformed here.
    """
    upstream = upstream.resolve()
    with TemporaryDirectory(prefix="geoh-frontend-") as directory:
        root = Path(directory).resolve()
        shutil.copytree(upstream / "src", root / "src")
        for name in ("package.json", "package-lock.json", "tsconfig.json"):
            if (upstream / name).is_file():
                shutil.copy2(upstream / name, root / name)
        dependencies = upstream / "node_modules"
        if dependencies.is_dir():
            if os.name == "nt":
                import _winapi

                _winapi.CreateJunction(str(dependencies), str(root / "node_modules"))
            else:
                (root / "node_modules").symlink_to(dependencies, target_is_directory=True)
        component = Path(__file__).parent / "_frontend" / "ToolCallDisplay.tsx"
        shutil.copy2(component, root / "src" / "components" / component.name)
        yield root


def tool_result_summary(output: str, *, is_error: bool) -> str:
    """Match the React nonempty-line count in the plain terminal renderer."""
    count = sum(bool(line.strip()) for line in output.split("\n"))
    return f"{'× ' if is_error else ''}→ {count}L"
