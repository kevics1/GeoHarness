"""Regression tests for the three cartography-workflow frictions the TUI hit:

1. ``action=full`` / ``symbolize`` demanded ``analysis_type`` even for plain
   thematic maps (fire-situation maps have no spatial-analysis stage).
2. ``action=full`` with a ``layers`` payload still demanded ``layer_name``
   for the per-layer symbolize step it did not need.
3. Every map export required user confirmation in default permission mode
   because geo_cartography writes files (into the outputs dir only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import CartographyConfig, GeoConfig
from geoharness.runtime import _build_permission_checker
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool

plt = pytest.importorskip("matplotlib")


def _context(tmp_path: Path) -> ToolExecutionContext:
    cfg = GeoConfig(
        cartography=CartographyConfig(outputs_dir=str(tmp_path / "out"))
    )
    return ToolExecutionContext(
        cwd=tmp_path,
        metadata={"geoharness_config": cfg, "data_catalog": None},
        hook_executor=None,
    )


class TestAnalysisTypeOptional:
    @pytest.mark.asyncio
    async def test_symbolize_without_analysis_type_uses_thematic(self) -> None:
        tool = GeoCartographyTool()
        res = await tool.execute(
            GeoCartographyInput(action="symbolize", layer_name="行政区划"),
            _context(Path.cwd()),
        )
        assert not res.is_error, res.output
        assert "thematic" in res.output.lower() or "categorized" in res.output

    @pytest.mark.asyncio
    async def test_full_without_analysis_type_renders(
        self, tmp_path: Path
    ) -> None:
        fc = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "properties": {"n": 1},
                "geometry": {"type": "Polygon",
                             "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
            }],
        }
        res = await GeoCartographyTool().execute(
            GeoCartographyInput(
                action="full",
                layer_name="行政区划",
                output_format="png",
                geojson=json.dumps(fc),
                output_path=str(tmp_path / "full.png"),
            ),
            _context(tmp_path),
        )
        assert not res.is_error, res.output
        assert Path(res.metadata["output_path"]).exists()


class TestFullSkipsSymbolizeForLayers:
    @pytest.mark.asyncio
    async def test_full_with_layers_needs_no_layer_name(
        self, tmp_path: Path
    ) -> None:
        """The exact TUI failure: action=full + layers, no layer_name."""
        fc = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "properties": {"n": 1},
                "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
            }],
        }
        layers = [
            {"geojson": json.dumps(fc), "label": "面", "color": "#4c78a8"},
            {"geojson": json.dumps(fc), "label": "点", "color": "#e4572e"},
        ]
        res = await GeoCartographyTool().execute(
            GeoCartographyInput(
                action="full",
                output_format="png",
                title="态势图",
                layers=json.dumps(layers),
                output_path=str(tmp_path / "full_layers.png"),
            ),
            _context(tmp_path),
        )
        assert not res.is_error, res.output
        assert "multi-layer" in res.output.lower()


class TestCartographyPermission:
    def test_geo_cartography_is_explicitly_allowed(self) -> None:
        checker = _build_permission_checker(GeoConfig())
        decision = checker.evaluate(
            "geo_cartography",
            is_read_only=False,
            file_path=str(Path.home() / ".geoharness" / "exports" / "m.png"),
        )
        assert decision.allowed
        assert not decision.requires_confirmation

    def test_deny_tool_still_blocks_cartography(self) -> None:
        """Explicit tool deny (and sensitive paths) outrank allowed_tools."""
        config = GeoConfig()
        checker = _build_permission_checker(config)
        checker._settings.denied_tools.append("geo_cartography")
        decision = checker.evaluate("geo_cartography", is_read_only=False)
        assert not decision.allowed, "denied_tools must win over allowed_tools"

    def test_sensitive_paths_still_blocked(self) -> None:
        """Built-in credential protection is unaffected by the allow-list."""
        checker = _build_permission_checker(GeoConfig())
        decision = checker.evaluate(
            "geo_cartography",
            is_read_only=False,
            file_path=str(Path.home() / ".ssh" / "id_rsa"),
        )
        assert not decision.allowed

    def test_other_mutating_tools_still_need_confirmation(self) -> None:
        checker = _build_permission_checker(GeoConfig())
        decision = checker.evaluate("some_other_tool", is_read_only=False)
        assert decision.requires_confirmation
