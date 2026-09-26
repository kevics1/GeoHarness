"""Regression tests for the geo_data inspect timeout defect.

Root cause (regression from the previous fix round): `admin_kg` performs a
network request inside `get_source_detail`, and `DataCatalog.get_source_detail`
consulted EVERY connector, so inspecting a purely-local source still triggered
a DataV fetch. Combined with 3 attempts x 2 TLS contexts x 30s timeout, the
worst case far exceeded the tool's 60s budget -> every inspect timed out.

Fixes verified here:
1. `DataCatalog.get_source_detail(name, source_type)` honours a connector filter.
2. `geo_data` forwards `args.source` for inspect/load.
3. `admin_kg.get_source_detail` is offline-first (region-name heuristic).
4. `admin_kg._http_get_json` obeys a hard total deadline.
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


# ── catalog source filtering ──────────────────────────────────────────────


class _RecordingConnector:
    def __init__(self, detail: dict[str, Any]) -> None:
        self._detail = detail
        self.calls = 0

    def get_source_detail(self, name: str) -> dict[str, Any]:
        self.calls += 1
        return self._detail

    def list_sources(self) -> list[Any]:
        return []


class TestCatalogSourceFilter:
    def _catalog(self) -> tuple[DataCatalog, dict[str, _RecordingConnector]]:
        catalog = DataCatalog(GeoConfig(), workspace_dir=".")
        file_conn = _RecordingConnector({})
        pg_conn = _RecordingConnector({"source_type": "postgis"})
        admin_conn = _RecordingConnector({"source_type": "admin_kg"})
        catalog.register_connector("file", file_conn)  # type: ignore[arg-type]
        catalog.register_connector("postgis", pg_conn)  # type: ignore[arg-type]
        catalog.register_connector("admin_kg", admin_conn)  # type: ignore[arg-type]
        return catalog, {"file": file_conn, "postgis": pg_conn, "admin_kg": admin_conn}

    def test_filtered_lookup_peeks_local_first(self) -> None:
        """A local file is always checked first; the filter is a fallback."""
        catalog, conns = self._catalog()
        detail = catalog.get_source_detail("province", "postgis")
        assert detail == {"source_type": "postgis"}
        # The (empty) file connector is peeked first, then the filter applies.
        assert conns["file"].calls == 1
        assert conns["postgis"].calls == 1
        assert conns["admin_kg"].calls == 0, "network connector must not be queried"

    def test_local_file_short_circuits_requested_backend(self) -> None:
        """If a local file exists, the requested backend is never consulted."""
        catalog, conns = self._catalog()
        conns["file"]._detail = {"source_type": "file", "path": "x.shp"}
        detail = catalog.get_source_detail("西昌市行政区划", "postgis")
        assert detail == {"source_type": "file", "path": "x.shp"}
        assert conns["postgis"].calls == 0, "backend must be skipped on local hit"

    def test_filtered_lookup_unknown_connector(self) -> None:
        catalog, _ = self._catalog()
        assert catalog.get_source_detail("x", "oracle") is None

    def test_broad_lookup_prefers_local_connectors(self) -> None:
        """With no filter, a local hit must short-circuit before admin_kg."""
        catalog, conns = self._catalog()
        conns["postgis"]._detail = {"source_type": "postgis"}
        detail = catalog.get_source_detail("province")
        assert detail == {"source_type": "postgis"}
        assert conns["admin_kg"].calls == 0

    def test_broad_lookup_falls_through_when_local_miss(self) -> None:
        catalog, conns = self._catalog()
        # local connectors return nothing -> admin_kg is consulted
        conns["file"]._detail = {}
        conns["postgis"]._detail = {}
        detail = catalog.get_source_detail("湖北省")
        assert detail == {"source_type": "admin_kg"}
        assert conns["file"].calls == 1
        assert conns["postgis"].calls == 1
        assert conns["admin_kg"].calls == 1


# ── end-to-end: inspect must not return a timeout ─────────────────────────


class TestInspectIsFast:
    @pytest.mark.asyncio
    async def test_local_inspect_with_black_hole_network(self, tmp_path) -> None:
        """A local inspect must succeed fast even if admin_kg is unreachable."""
        import json

        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

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
            metadata={"geoharness_config": GeoConfig(), "data_catalog": catalog},
            hook_executor=None,
        )

        t = time.time()
        res = await GeoDataTool().execute(
            GeoDataInput(action="inspect", name="roads", source="file"), ctx
        )
        elapsed = time.time() - t

        assert not res.is_error, res.output
        assert "timed out" not in res.output
        assert elapsed < 15, f"inspect took {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_missing_local_source_returns_not_found(self, tmp_path) -> None:
        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": GeoConfig(), "data_catalog": catalog},
            hook_executor=None,
        )
        t = time.time()
        res = await GeoDataTool().execute(
            GeoDataInput(action="inspect", name="nope", source="file"), ctx
        )
        assert res.is_error
        assert "not found" in res.output.lower()
        assert time.time() - t < 10
