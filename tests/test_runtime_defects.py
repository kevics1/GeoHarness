"""Regression tests for the two reported runtime defects.

1. ``geo_data`` could not see files in the working directory (the file
   connector was only registered when ``data.file_dir`` was set, and scanning
   was non-recursive).
2. ``geo_cartography`` / ``geo_data`` could hang the session: blocking
   DB/render calls ran directly on the async event loop, and ``localhost``
   DSNs stalled on an IPv6-first resolution before falling back to IPv4.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from geoharness.config.settings import DataConfig, GeoConfig
from geoharness.data.catalog import DataCatalog, build_data_catalog
from geoharness.data.file_loader import FileLoader
from geoharness.data.postgis import PostGISConnector

# ── Problem 1: file sources ───────────────────────────────────────────────


def _write_geojson(path: Path, name: str = "z") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{"n":1},"geometry":{"type":"Point","coordinates":[0,0]}}]}',
        encoding="utf-8",
    )
    return path


class TestFileSourceDiscovery:
    def test_file_connector_registered_without_file_dir(self, tmp_path: Path) -> None:
        """A file connector must exist even when data.file_dir is unset."""
        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        assert "file" in catalog.connector_names

    def test_file_dir_wins_over_workspace(self, tmp_path: Path) -> None:
        """An explicit data.file_dir takes precedence over the workspace."""
        configured = tmp_path / "configured"
        configured.mkdir()
        catalog = DataCatalog(
            GeoConfig(data=DataConfig(file_dir=str(configured))),
            workspace_dir=str(tmp_path),
        )
        assert "file" in catalog.connector_names

    def test_discovers_files_in_subdirectories(self, tmp_path: Path) -> None:
        """Files nested in a subfolder (e.g. 数据/) must be discoverable."""
        _write_geojson(tmp_path / "数据" / "roads.geojson")
        loader = FileLoader(file_dir=str(tmp_path))
        names = {p.name for p in loader._scan_files()}
        assert names == {"roads.geojson"}

    def test_non_recursive_mode_ignores_subdirs(self, tmp_path: Path) -> None:
        _write_geojson(tmp_path / "sub" / "roads.geojson")
        loader = FileLoader(file_dir=str(tmp_path), recursive=False)
        assert loader._scan_files() == []

    def test_workspace_files_visible_through_catalog(self, tmp_path: Path) -> None:
        """End-to-end: workspace files show up in the catalog listing."""
        _write_geojson(tmp_path / "数据" / "roads.geojson")
        _write_geojson(tmp_path / "数据" / "rivers.geojson")
        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        sources = catalog.list_sources("file")
        names = {s.name for s in sources}
        assert {"roads", "rivers"} <= names

    def test_file_dir_tilde_is_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = FileLoader(file_dir="~/some/where")
        assert "~" not in str(loader._file_dir)

    def test_build_data_catalog_passes_workspace(self, tmp_path: Path) -> None:
        catalog = build_data_catalog(GeoConfig(), workspace_dir=str(tmp_path))
        assert "file" in catalog.connector_names

    def test_find_file_resolves_nested_bare_name(self, tmp_path: Path) -> None:
        """A bare name must resolve to a file in a subfolder."""
        _write_geojson(tmp_path / "数据" / "城际公路.geojson")
        loader = FileLoader(file_dir=str(tmp_path))
        found = loader._find_file("城际公路")
        assert found is not None and found.name == "城际公路.geojson"

    def test_find_file_resolves_nested_with_extension(self, tmp_path: Path) -> None:
        _write_geojson(tmp_path / "数据" / "roads.geojson")
        loader = FileLoader(file_dir=str(tmp_path))
        found = loader._find_file("roads.geojson")
        assert found is not None and found.parent.name == "数据"

    def test_find_file_returns_none_for_missing(self, tmp_path: Path) -> None:
        loader = FileLoader(file_dir=str(tmp_path))
        assert loader._find_file("nope") is None

    def test_inspect_resolves_nested_file_detail(self, tmp_path: Path) -> None:
        """get_source_detail must work for nested files via the catalog."""
        _write_geojson(tmp_path / "数据" / "roads.geojson")
        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        detail = catalog.get_source_detail("roads")
        assert detail, "nested file detail should resolve"
        assert detail.get("format") == "geojson"


# ── Problem 2a: DSN localhost stall ───────────────────────────────────────


class TestPostGISDsnResolution:
    def test_localhost_rewritten_when_ipv4_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reachable IPv4 loopback -> localhost is rewritten to 127.0.0.1."""
        import socket

        class FakeConn:
            def __enter__(self) -> "FakeConn":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: FakeConn())
        conn = PostGISConnector("postgresql://u:p@localhost:5432/db")
        assert conn._resolve_dsn() == "postgresql://u:p@127.0.0.1:5432/db"

    def test_localhost_kept_when_ipv4_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import socket

        def boom(*a: Any, **k: Any) -> Any:
            raise OSError("refused")

        monkeypatch.setattr(socket, "create_connection", boom)
        dsn = "postgresql://u:p@localhost:5432/db"
        conn = PostGISConnector(dsn)
        assert conn._resolve_dsn() == dsn

    def test_non_localhost_dsn_untouched(self) -> None:
        dsn = "postgresql://u:p@db.internal:5432/db"
        assert PostGISConnector(dsn)._resolve_dsn() == dsn

    def test_resolution_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        calls = {"n": 0}

        class FakeConn:
            def __enter__(self) -> "FakeConn":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

        def counting(*a: Any, **k: Any) -> Any:
            calls["n"] += 1
            return FakeConn()

        monkeypatch.setattr(socket, "create_connection", counting)
        conn = PostGISConnector("postgresql://u:p@localhost:5432/db")
        conn._resolve_dsn()
        conn._resolve_dsn()
        assert calls["n"] == 1

    def test_connect_timeout_is_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """psycopg.connect must receive a bounded connect_timeout."""
        import psycopg

        captured: dict[str, Any] = {}

        class FakePsycopgConn:
            pass

        def fake_connect(dsn: str, **kwargs: Any) -> Any:
            captured["dsn"] = dsn
            captured.update(kwargs)
            return FakePsycopgConn()

        monkeypatch.setattr(psycopg, "connect", fake_connect)
        conn = PostGISConnector("postgresql://u:p@db.internal:5432/db")
        conn._ensure_connection()
        assert captured["connect_timeout"] == 10
        assert captured["autocommit"] is True


# ── Problem 2b: system tables excluded ────────────────────────────────────


class TestSystemTableFiltering:
    def test_spatial_ref_sys_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = PostGISConnector("postgresql://u:p@db.internal/db")

        class FakeCursor:
            def execute(self, *a: Any) -> None: ...

            def fetchall(self) -> list[tuple[str]]:
                return [("province",), ("spatial_ref_sys",), ("roads",)]

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

        class FakeConn:
            def cursor(self) -> FakeCursor:
                return FakeCursor()

        monkeypatch.setattr(conn, "_ensure_connection", lambda: FakeConn())
        assert conn.list_tables() == ["province", "roads"]


# ── Problem 2c: event loop must not be blocked ────────────────────────────


class TestNonBlockingTools:
    @pytest.mark.asyncio
    async def test_geo_data_does_not_block_event_loop(self) -> None:
        """A slow catalog call must not freeze the loop (it runs in a thread)."""
        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

        class SlowCatalog:
            def list_sources(self, source_type: str = "all") -> list[Any]:
                time.sleep(2.0)
                return []

        ctx = ToolExecutionContext(
            cwd=Path.cwd(),
            metadata={"data_catalog": SlowCatalog()},
            hook_executor=None,
        )

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.1)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            await GeoDataTool().execute(GeoDataInput(action="list"), ctx)
        finally:
            beat.cancel()
        # Loop stayed alive during the 2s blocking call.
        assert ticks >= 5, f"event loop was blocked (ticks={ticks})"

    @pytest.mark.asyncio
    async def test_geo_data_timeout_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An over-long catalog call yields an error, not a hang."""
        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools import geo_data as mod

        monkeypatch.setattr(mod, "_CATALOG_TIMEOUT_SECONDS", 0.3)

        class HangingCatalog:
            def list_sources(self, source_type: str = "all") -> list[Any]:
                time.sleep(5.0)
                return []

        ctx = ToolExecutionContext(
            cwd=Path.cwd(),
            metadata={"data_catalog": HangingCatalog()},
            hook_executor=None,
        )
        res = await mod.GeoDataTool().execute(
            mod.GeoDataInput(action="list"), ctx
        )
        assert res.is_error
        assert "timed out" in res.output

    @pytest.mark.asyncio
    async def test_cartography_render_runs_off_loop(self, tmp_path: Path) -> None:
        """geo_cartography must render without blocking the event loop."""
        import json

        from openharness.tools.base import ToolExecutionContext

        from geoharness.config.settings import CartographyConfig
        from geoharness.tools.geo_cartography import (
            GeoCartographyInput,
            GeoCartographyTool,
        )

        fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"v": i},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[i, 0], [i + 1, 0], [i + 1, 1], [i, 1], [i, 0]]
                        ],
                    },
                }
                for i in range(4)
            ],
        }
        cfg = GeoConfig(
            cartography=CartographyConfig(outputs_dir=str(tmp_path))
        )
        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": cfg, "data_catalog": None},
            hook_executor=None,
        )

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            res = await GeoCartographyTool().execute(
                GeoCartographyInput(
                    action="export",
                    layer_name="loop_check",
                    analysis_type="moran_local",
                    field_name="v",
                    geojson=json.dumps(fc),
                    output_format="png",
                ),
                ctx,
            )
        finally:
            beat.cancel()

        assert not res.is_error, res.output
        assert ticks >= 1, "event loop was blocked during render"

    def test_render_timeout_constant_is_bounded(self) -> None:
        from geoharness.tools.geo_cartography import _RENDER_TIMEOUT_SECONDS

        assert 0 < _RENDER_TIMEOUT_SECONDS <= 600


# ── Problem 3: admin_kg TLS / endpoint reliability ────────────────────────


class TestAdminKGFetch:
    def test_ssl_contexts_include_tls12_fallback(self) -> None:
        """A TLS1.2-pinned context must be offered as a fallback."""
        import ssl

        from geoharness.data.admin_kg import AdminKGConnector

        contexts = AdminKGConnector._ssl_contexts()
        assert len(contexts) >= 2
        assert contexts[0].maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED
        assert contexts[1].maximum_version == ssl.TLSVersion.TLSv1_2

    def test_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A transient TLS reset must not fail the fetch outright."""
        from geoharness.data import admin_kg as mod

        attempts = {"n": 0}

        class FakeResp:
            def __enter__(self) -> "FakeResp":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"type":"FeatureCollection","features":[]}'

        def flaky(*a: Any, **k: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("UNEXPECTED_EOF_WHILE_READING")
            return FakeResp()

        monkeypatch.setattr(mod, "urlopen", flaky)
        conn = mod.AdminKGConnector(api_url="https://x/bound")
        data = conn._http_get_json("https://x/bound/1_full.json")
        assert data == {"type": "FeatureCollection", "features": []}
        assert attempts["n"] == 2

    def test_raises_after_all_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from geoharness.data import admin_kg as mod

        def always_fail(*a: Any, **k: Any) -> Any:
            raise OSError("boom")

        monkeypatch.setattr(mod, "urlopen", always_fail)
        conn = mod.AdminKGConnector(api_url="https://x/bound")
        with pytest.raises(OSError, match="boom"):
            conn._http_get_json("https://x/bound/1_full.json")

    def test_fetch_boundary_uses_cache(self, tmp_path: Path) -> None:
        """A cached file must be served without any HTTP call."""
        from geoharness.data import admin_kg as mod

        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "420000_full.geojson").write_text(
            '{"type":"FeatureCollection","features":[{"type":"Feature",'
            '"properties":{},"geometry":null}]}',
            encoding="utf-8",
        )

        def boom(*a: Any, **k: Any) -> Any:
            raise AssertionError("HTTP should not be called when cache exists")

        conn = mod.AdminKGConnector(api_url="https://x/bound", cache_dir=str(cache))
        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(mod, "urlopen", boom)
            data = conn._fetch_boundary("420000")
        assert len(data["features"]) == 1

    def test_fetch_boundary_returns_empty_on_total_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from geoharness.data import admin_kg as mod

        def always_fail(*a: Any, **k: Any) -> Any:
            raise OSError("network down")

        monkeypatch.setattr(mod, "urlopen", always_fail)
        conn = mod.AdminKGConnector(
            api_url="https://x/bound", cache_dir=str(tmp_path / "c")
        )
        data = conn._fetch_boundary("420000")
        assert data == {"type": "FeatureCollection", "features": []}

    def test_fetch_boundary_caches_success(self, tmp_path: Path) -> None:
        from geoharness.data import admin_kg as mod

        class FakeResp:
            def __enter__(self) -> "FakeResp":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"type":"FeatureCollection","features":[]}'

        cache = tmp_path / "c"
        cache.mkdir()
        conn = mod.AdminKGConnector(api_url="https://x/bound", cache_dir=str(cache))
        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(mod, "urlopen", lambda *a, **k: FakeResp())
            conn._fetch_boundary("420000")
        assert (cache / "420000_full.geojson").exists()

    def test_api_url_default_is_reachable_form(self) -> None:
        """The default endpoint must be the working areas_v3 form."""
        from geoharness.data.admin_kg import AdminKGConnector

        assert "areas_v3/bound" in AdminKGConnector()._api_url
        assert "v2/district" not in AdminKGConnector()._api_url

    def test_cache_dir_expands_user(self) -> None:
        from geoharness.data.admin_kg import AdminKGConnector

        conn = AdminKGConnector(cache_dir="~/geoh_cache")
        assert "~" not in str(conn._cache_dir)
