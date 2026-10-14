"""Entry point executed only by QGIS's own interpreter, with a JSON request file."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path


def _bootstrap_package() -> None:
    """Locate only GeoHarness, without exposing the agent's site-packages to QGIS.

    A wheel's parent directory contains the agent's NumPy/GDAL dependencies.
    Adding that parent to sys.path can break QGIS's independent native ABI.
    Package search locations supply our modules without changing dependency order.
    """
    package_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "geoharness",
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not locate GeoHarness worker package")
    package = importlib.util.module_from_spec(spec)
    sys.modules["geoharness"] = package
    spec.loader.exec_module(package)


if not __package__:
    _bootstrap_package()
from geoharness.qgis.worker_context import WorkerContext  # noqa: E402


def main() -> int:
    request_path = Path(sys.argv[1]).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    context = WorkerContext(
        Path(request["cwd"]),
        Path(request["run_dir"]),
        [Path(value) for value in request["read_roots"]],
    )
    # Windows job containment must be attached before native provider code executes.
    deadline = time.monotonic() + 30
    while not (context.run_dir / ".start").is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("Parent did not authorize QGIS worker startup")
        time.sleep(0.02)
    app = None
    try:
        from qgis.core import QgsApplication, QgsProject
        from qgis.PyQt.QtGui import QFontDatabase

        QgsApplication.setPrefixPath(os.environ["QGIS_PREFIX_PATH"], True)
        app = QgsApplication([], False, str(context.run_dir / ".profile"))
        # Offscreen Qt on Windows may have no font database, including CJK fonts.
        fonts = QFontDatabase()
        if not fonts.families() and os.name == "nt":
            font_dir = Path(os.environ.get("WINDIR", os.environ.get("SystemRoot", ""))) / "Fonts"
            if font_dir.is_dir():
                for font in font_dir.iterdir():
                    if font.suffix.lower() in {".ttf", ".ttc", ".otf"}:
                        fonts.addApplicationFont(str(font))
        app.initQgis()
        action, payload = request["action"], request["payload"]
        if action.startswith("project_"):
            from geoharness.qgis.project_worker import dispatch
        else:
            from geoharness.qgis.processing_worker import dispatch
        response = {
            "status": "success",
            "result": dispatch(action, payload, context),
            "error_summary": None,
        }
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        response = {
            "status": "error",
            "result": None,
            "error_summary": f"{type(exc).__name__}: {exc}",
            "diagnostics": getattr(exc, "diagnostics", None),
        }
    finally:
        if app is not None:
            QgsProject.instance().clear()
            app.exitQgis()
    (context.run_dir / "result.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if response["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
