"""Shared compact cautions, available before tool package initialization."""

from __future__ import annotations

PYTHON_CAUTIONS = (
    "Fresh process and RUN_DIR each call: import/define all names; bind prior files via "
    "input_refs/INPUTS or exact returned paths.",
    "Inspect actual intermediate columns/CRS and library signatures before use; "
    "input_requirements does not type-check arbitrary code.",
    "Rasterio read(1) is 2D: do not blindly append [0]. mask_raster returns "
    "(array, transform), normally band-first (indexes=int gives 2D); inspect shape before "
    "stacking/writing. Prefer read_raster/write_raster for an always-3D array contract.",
    "Keep verified CRS/transform/nodata with raster arrays; use nodata, not fill_value, "
    "for rasterio.mask.mask. Write only RUN_DIR; preserve source data.",
    "After failure inspect the measured cause, change that input/code, and rerun only "
    "the failed stage; unchanged retry is reserved for confirmed transient failures.",
)

QGIS_CAUTIONS = (
    "Prefer spec -> edits(spec_ref, layer_key) -> preflight -> create_call unchanged. "
    "Frozen create accepts only action+plan_ref; changes need new preflight.",
    "Each frame: FIT focus_layers OR explicit extent, never both. "
    "render_layers must include focus_layers/label_layers; a detail needs a range source.",
    "Keep stable layer_key and declared subject requirements. Extent tightening must "
    "preserve coverage; actual zoom is main_scale/detail_scale after native fitting.",
    "Diagnostic detail_path/offset/limit are read-only help/inspect selectors. "
    "Do not copy them into create/preflight/spec.",
    "Roster key_field (or first field) is unique/non-null; top_n requires sort_by and "
    "excludes keys. Measure table/legend space; create does not imply visual acceptance.",
)


def invocation_guide(tool_name: str) -> str:
    rules = PYTHON_CAUTIONS if tool_name == "geo_python" else QGIS_CAUTIONS
    return "CALL-TIME CAUTIONS: " + " ".join(rules)


def section_cautions(section: str) -> list[str]:
    by_section = {
        "frame": [QGIS_CAUTIONS[1], QGIS_CAUTIONS[2]],
        "layout": [QGIS_CAUTIONS[1], QGIS_CAUTIONS[2], QGIS_CAUTIONS[4]],
        "roster": [QGIS_CAUTIONS[4]],
        "layer": ["layers draw bottom first/top last; table_only is never rendered. "
                  "Put renderer/field/categories inside style, using actual source fields."],
        "style": ["Use style.renderer/field/categories/ranges; preserve actual category values. "
                  "QGIS mm/pt sizes differ from Matplotlib point areas."],
        "labels": ["Check actual geometry/label field. label_layers must be rendered; "
                   "purpose is metadata, detail_title is visible text."],
        "edits": [QGIS_CAUTIONS[0], QGIS_CAUTIONS[2]],
        "workflow": list(QGIS_CAUTIONS),
    }
    return list(by_section.get(section, QGIS_CAUTIONS))


def action_forms() -> dict:
    """Recommended short forms; legacy full-input forms remain supported."""
    return {
        "help": ["action", "section", "detail_path?", "offset?", "limit?"],
        "spec": ["action", "spec_ref", "edits or patches"],
        "preflight": ["action", "spec_ref"],
        "create": ["action", "plan_ref"],
        "inspect": ["action", "spec_ref or plan_ref or project_path or map_id"],
        "export": ["action", "project_path", "layout_name?", "output_format?"],
        "recover": ["action", "map_id"],
    }
