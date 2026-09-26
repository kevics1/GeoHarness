"""Regression tests for the data-source lookup defects.

History: the old unified ``geo_data`` tool consulted EVERY connector for a
single name, so inspecting a purely-local source could trigger a DataV fetch
or a PostGIS query — and one blocked connector produced "connector did not
respond within 15s". The fix abandoned that design entirely: each data type now
has its own connector *and* its own tool.

Verified here:
1. `DataCatalog` does NOT fan out across connectors (no cross-source lookup).
2. `admin_kg.get_source_detail` is offline-first (region-name heuristic).
3. `admin_kg._http_get_json` obeys a hard total deadline.
4. A local vector inspect never touches an unreachable network backend.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from geoharness.config.settings import GeoConfig
from geoharness.data.admin_kg import AdminKGConnector, _looks_like_region_name
from geoharness.data.catalog import DataCatalog

# ── region-name heuristic ─────────────────────────────────────────────────


class TestRegionNameHeuristic:
    @pytest.mark.parametrize(
        "name",
        ["湖北省", "武汉市", "武昌区", "西昌市", "巴音郭楞蒙古自治州",
         "内蒙古自治区", "香港特别行政区", "420000", "110000"],
    )
    def test_accepts_region_like_names(self, name: str) -> None:
        assert _looks_like_region_name(name) is True

    @pytest.mark.parametrize(
        "name",
        ["province", "city", "城际公路", "受威胁居民点", "roads", "nope_layer",
         "西昌市行政区划", "", "   "],
    )
    def test_rejects_non_region_names(self, name: str) -> None:
        assert _looks_like_region_name(name) is False


# ── admin_kg must not hit the network for non-regions ─────────────────────


class TestAdminKGOfflineFirst:
    def test_non_region_name_makes_no_network_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A local-looking identifier must return {} without any fetch."""
        calls = {"n": 0}

        def boom(*a: Any, **k: Any) -> Any:
            calls["n"] += 1
            raise AssertionError("network must not be touched")

        conn = AdminKGConnector(api_url="https://x/bound")
        monkeypatch.setattr(conn, "_fetch_boundary", boom)
        assert conn.get_source_detail("城际公路") == {}
        assert conn.get_source_detail("province") == {}
        assert calls["n"] == 0

    def test_region_name_does_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real region name is allowed to consult the backend."""
        conn = AdminKGConnector(api_url="https://x/bound")
        monkeypatch.setattr(
            conn, "list_provinces",
            lambda: [{"name": "湖北省", "adcode": "420000", "level": "province"}],
        )
        monkeypatch.setattr(conn, "list_cities", lambda adcode: [])
        detail = conn.get_source_detail("湖北省")
        assert detail["adcode"] == "420000"


# ── hard fetch deadline ───────────────────────────────────────────────────


class TestFetchDeadline:
    def test_deadline_is_small(self) -> None:
        assert 0 < AdminKGConnector._FETCH_DEADLINE_SECONDS <= 15

    def test_total_time_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even when every attempt hangs, the call must return promptly."""
        from geoharness.data import admin_kg as mod

        class HangingResp:
            def __enter__(self) -> "HangingResp":
                time.sleep(3)
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

            def read(self) -> bytes:
                # Invalid JSON -> treated as a failure, so retries continue.
                return b"<not json>"

        monkeypatch.setattr(mod, "urlopen", lambda *a, **k: HangingResp())
        monkeypatch.setattr(AdminKGConnector, "_FETCH_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(AdminKGConnector, "_FETCH_DEADLINE_SECONDS", 2.0)

        conn = AdminKGConnector(api_url="https://x/bound")
        t = time.time()
        with pytest.raises(Exception):
            conn._http_get_json("https://x/bound/1_full.json")
        elapsed = time.time() - t
        # One 3s attempt is allowed to finish; the deadline then stops retries.
        assert elapsed < 10, f"deadline not enforced ({elapsed:.1f}s)"

    def test_success_within_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from geoharness.data import admin_kg as mod

        class OkResp:
            def __enter__(self) -> "OkResp":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"type":"FeatureCollection","features":[]}'

        monkeypatch.setattr(mod, "urlopen", lambda *a, **k: OkResp())
        conn = AdminKGConnector(api_url="https://x/bound")
        assert conn._http_get_json("https://x/bound/1_full.json")["type"] == (
            "FeatureCollection"
        )


# ── type isolation: each tool uses exactly one connector ──────────────────


class _RecordingConnector:
    def __init__(self, detail: dict[str, Any]) -> None:
        self._detail = detail
        self.calls = 0

    def get_source_detail(self, name: str) -> dict[str, Any]:
        self.calls += 1
        return self._detail

    def list_sources(self) -> list[Any]:
        return []


class TestConnectorIsolation:
    """The catalog no longer fans out across connectors — that coupling was
    the old design's root defect."""

    def test_catalog_has_no_cross_source_lookup(self) -> None:
        catalog = DataCatalog(GeoConfig(), workspace_dir=".")
        assert not hasattr(catalog, "get_source_detail")
        assert not hasattr(catalog, "resolve_source_detail")

    def test_get_connector_returns_only_the_named_one(self) -> None:
        catalog = DataCatalog(GeoConfig(), workspace_dir=".")
        pg = _RecordingConnector({"source_type": "postgis"})
        file_conn = _RecordingConnector({"source_type": "file"})
        catalog.register_connector("postgis", pg)  # type: ignore[arg-type]
        catalog.register_connector("file", file_conn)  # type: ignore[arg-type]

        assert catalog.get_connector("postgis") is pg
        detail = catalog.get_connector("postgis").get_source_detail("province")
        assert detail == {"source_type": "postgis"}
        assert pg.calls == 1
        assert file_conn.calls == 0, "the file connector must not be touched"

    def test_unknown_connector_returns_none(self) -> None:
        catalog = DataCatalog(GeoConfig(), workspace_dir=".")
        assert catalog.get_connector("oracle") is None


# ── end-to-end: each data tool is fast and isolated ───────────────────────


class TestDataToolsAreIsolated:
    @pytest.mark.asyncio
    async def test_vector_inspect_ignores_black_hole_admin_kg(
        self, tmp_path
    ) -> None:
        """A local vector inspect must be fast even if admin_kg is unreachable."""
        import json

        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools.geo_vector_data import (
            GeoVectorDataInput,
            GeoVectorDataTool,
        )

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
        catalog._ensure_initialized()
        admin = catalog.get_connector("admin_kg")
        # Black-hole endpoint: any network attempt would stall.
        admin._api_url = "https://10.255.255.1/bound"  # type: ignore[union-attr]

        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"data_catalog": catalog},
            hook_executor=None,
        )

        t = time.time()
        res = await GeoVectorDataTool().execute(
            GeoVectorDataInput(action="inspect", name="roads"), ctx
        )
        elapsed = time.time() - t

        assert not res.is_error, res.output
        assert "timed out" not in res.output
        assert elapsed < 10, f"inspect took {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_vector_missing_source_returns_not_found(self, tmp_path) -> None:
        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools.geo_vector_data import (
            GeoVectorDataInput,
            GeoVectorDataTool,
        )

        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"data_catalog": catalog},
            hook_executor=None,
        )
        t = time.time()
        res = await GeoVectorDataTool().execute(
            GeoVectorDataInput(action="inspect", name="nope"), ctx
        )
        assert res.is_error
        assert "not found" in res.output.lower()
        assert time.time() - t < 10
