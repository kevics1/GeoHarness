"""Validate a portable map with QGIS's Python runtime; no desktop window opens."""

import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("project", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsLayoutExporter,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsProject,
)
from qgis.PyQt.QtGui import QFontDatabase  # noqa: E402

profile = args.output.resolve().parent / ".qgis-validation-profile"
profile.mkdir(parents=True, exist_ok=True)
app = QgsApplication([], False, str(profile))
# Qt's Windows offscreen platform has no system font database. Load local fonts
# for this verification process; ordinary QGIS desktop uses its system fonts.
fonts = QFontDatabase()
if not fonts.families() and os.name == "nt":
    font_dir = Path(os.environ["WINDIR"]) / "Fonts"
    for font_file in font_dir.iterdir():
        if font_file.suffix.lower() in {".ttf", ".ttc", ".otf"}:
            fonts.addApplicationFont(str(font_file))
assert fonts.families(), "QGIS validation needs at least one available font"
app.initQgis()
project = QgsProject.instance()
assert args.project.is_file(), "Validation project does not exist"
assert project.read(str(args.project.resolve())), (
    f"QGIS could not read the project: {project.error()}"
)
layers = list(project.mapLayers().values())
assert layers and all(layer.isValid() for layer in layers), "QGIS has invalid/missing data layers"
layouts = project.layoutManager().layouts()
assert len(layouts) == 1, "Expected an editable print layout"
layout = layouts[0]
page = layout.pageCollection().page(0).rect()
maps = [item for item in layout.items() if isinstance(item, QgsLayoutItemMap)]
assert maps, "The layout needs an editable map frame"
for item in maps:
    assert item.pos().x() + item.rect().width() <= page.width() + 0.01
    assert item.pos().y() + item.rect().height() <= page.height() + 0.01, "Map exceeds page"
scalebars = [item for item in layout.items() if isinstance(item, QgsLayoutItemScaleBar)]
assert scalebars and all(item.linkedMap() is not None for item in scalebars)
settings = QgsLayoutExporter.ImageExportSettings()
settings.dpi = 110
exporter = QgsLayoutExporter(layout)
code = exporter.exportToImage(str(args.output.resolve()), settings)
assert code == QgsLayoutExporter.Success, f"QGIS layout export failed: {code}"
print(
    json.dumps(
        {
            "project": str(args.project.resolve()),
            "layers": [layer.name() for layer in layers],
            "layout": layout.name(),
            "export": str(args.output.resolve()),
        },
        ensure_ascii=False,
    )
)
project.clear()
app.exitQgis()
