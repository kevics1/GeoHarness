"""Regression tests: a slow backend must never hang a tool call.

The old unified ``geo_data`` tool mixed three data sources behind one call and
fanned out across every connector for a single name. One blocked connector
(DNS/connect/TLS) therefore delayed — and eventually failed — lookups that had
nothing to do with it.

The design was abandoned: each data type now has a dedicated tool
(``geo_db_data`` / ``geo_vector_data`` / ``geo_raster_data``) that talks to
exactly one connector. These tests lock that isolation in:

- A tool only ever calls its own connector.
- A hung connector surfaces as a bounded timeout error, not a hang.
- Local tools are unaffected by an unreachable database/network backend.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import GeoConfig
from geoharness.data.catalog import DataCatalog
from geoharness.tools.geo_db_data import GeoDBDataInput, GeoDBDataTool
from geoharness.tools.geo_vector_data import GeoVectorDataInput, GeoVectorDataTool

# ── fake connectors ───────────────────────────────────────────────────────


class HungConnector:
    """Blocks far longer than any tool budget (simulates a network stall)."""

    def __init__(self, delay: float = 30.0) -> None:
        self.delay = delay
        self.calls = 0
        self.resets = 0

    def list_sources(self) -> list[Any]:
        time.sleep(self.delay)
        return []

    def get_source_detail(self, name: str) -> dict[str, Any]:
        self.calls += 1
        time.sleep(self.delay)
        return {}

    def query_geodataframe(self, name: str, limit: int = 5000) -> Any:
        self.calls += 1
        time.sleep(self.delay)
        raise RuntimeError("unreachable")

    def reset(self) -> None:
        self.resets += 1


class InstantConnector:
    def __init__(self, detail: dict[str, Any] | None = None) -> None:
        self.detail = detail or {}
        self.calls = 0

    def list_sources(self) -> list[Any]:
        return []

    def get_source_detail(self, name: str) -> dict[str, Any]:
        self.calls += 1
        return self.detail


def _catalog(**connectors: Any) -> DataCatalog:
    catalog = DataCatalog(GeoConfig(), workspace_dir=".")
    for name, conn in connectors.items():
        catalog.register_connector(name, conn)
    return catalog


def _ctx(tmp_path: Path, catalog: Any) -> ToolExecutionContext:
    return ToolExecutionContext(
        cwd=tmp_path, metadata={"data_catalog": catalog}, hook_executor=None
    )


# ── isolation: a tool touches only its own connector ──────────────────────


class TestToolIsolation:
    @pytest.mark.asyncio
    async def test_vector_tool_never_calls_the_database(self, tmp_path: Path) -> None:
        hung_db = HungConnector()
        file_conn = InstantConnector({"source_type": "file", "path": "x.shp"})
        catalog = _catalog(postgis=hung_db, file=file_conn)

        res = await GeoVectorDataTool().execute(
            GeoVectorDataInput(action="inspect", name="x"), _ctx(tmp_path, catalog)
        )
        assert not res.is_error, res.output
        assert hung_db.calls == 0, "vector tool must not touch the database"

    @pytest.mark.asyncio
    async def test_db_tool_never_touches_local_files(self, tmp_path: Path) -> None:
        hung_file = HungConnector()
        pg = InstantConnector({"source_type": "postgis"})
        catalog = _catalog(postgis=pg, file=hung_file)

        res = await GeoDBDataTool().execute(
            GeoDBDataInput(action="inspect", name="province"), _ctx(tmp_path, catalog)
        )
        assert not res.is_error, res.output
        assert hung_file.calls == 0, "db tool must not touch local files"


# ── a hung backend is bounded, never a hang ───────────────────────────────


class TestHungBackendIsBounded:
    @pytest.mark.asyncio
    async def test_db_tool_times_out_within_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from geoharness.tools import geo_db_data as mod

        monkeypatch.setattr(mod, "_DB_TIMEOUT_SECONDS", 0.5)
        catalog = _catalog(postgis=HungConnector())

        t = time.time()
        res = await GeoDBDataTool().execute(
            GeoDBDataInput(action="inspect", name="province"), _ctx(tmp_path, catalog)
        )
        elapsed = time.time() - t
        assert res.is_error
        assert "did not respond" in res.output
        assert elapsed < 5, f"budget not enforced ({elapsed:.1f}s)"

    @pytest.mark.asyncio
    async def test_db_tool_list_times_out_within_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from geoharness.tools import geo_db_data as mod

        monkeypatch.setattr(mod, "_DB_TIMEOUT_SECONDS", 0.5)
        catalog = _catalog(postgis=HungConnector())

        t = time.time()
        res = await GeoDBDataTool().execute(
            GeoDBDataInput(action="list"), _ctx(tmp_path, catalog)
        )
        assert res.is_error
        assert time.time() - t < 5

    @pytest.mark.asyncio
    async def test_vector_tool_times_out_within_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from geoharness.tools import geo_vector_data as mod

        monkeypatch.setattr(mod, "_VECTOR_TIMEOUT_SECONDS", 0.5)
        catalog = _catalog(file=HungConnector())

        t = time.time()
        res = await GeoVectorDataTool().execute(
            GeoVectorDataInput(action="list"), _ctx(tmp_path, catalog)
        )
        assert res.is_error
        assert time.time() - t < 5


# ── local tools are unaffected by an unreachable backend ──────────────────


class TestLocalToolUnaffectedByBackend:
    @pytest.mark.asyncio
    async def test_vector_inspect_fast_with_hung_postgis(
        self, tmp_path: Path
    ) -> None:
        import json

        data_dir = tmp_path / "数据"
        data_dir.mkdir()
        (data_dir / "roads.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"n": 1},
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                }],
            }),
            encoding="utf-8",
        )

        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        catalog.register_connector("postgis", HungConnector())  # type: ignore[arg-type]
        catalog.register_connector(  # type: ignore[arg-type]
            "file", InstantConnector({"source_type": "file", "path": "roads"})
        )

        t = time.time()
        res = await GeoVectorDataTool().execute(
            GeoVectorDataInput(action="inspect", name="roads"), _ctx(tmp_path, catalog)
        )
        assert not res.is_error, res.output
        assert time.time() - t < 2, "local inspect must not wait on the database"
