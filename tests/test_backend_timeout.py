"""Regression tests: a hung backend must never burn the whole action budget.

The user hit "data source discovery timed out after 60s" repeatedly. The tool
had a single 60s budget for the whole action, so ONE blocked connector
(DNS/connect/TLS) consumed everything and produced an uninformative error.
Now every connector has its own budget, and local hits never touch a slow
backend at all.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from geoharness.config.settings import GeoConfig
from geoharness.data.catalog import ConnectorTimeout, DataCatalog
from geoharness.data.file_loader import FileLoader

# ── fake connectors ───────────────────────────────────────────────────────


class HungConnector:
    """Blocks far longer than any budget (simulates a network stall)."""

    def __init__(self, delay: float = 30.0) -> None:
        self.delay = delay
        self.calls = 0

    def get_source_detail(self, name: str) -> dict[str, Any]:
        self.calls += 1
        time.sleep(self.delay)
        return {}

    def list_sources(self) -> list[Any]:
        return []


class InstantConnector:
    def __init__(self, detail: dict[str, Any] | None = None) -> None:
        self.detail = detail or {}
        self.calls = 0

    def get_source_detail(self, name: str) -> dict[str, Any]:
        self.calls += 1
        return self.detail

    def list_sources(self) -> list[Any]:
        return []


def _catalog(**connectors: Any) -> DataCatalog:
    catalog = DataCatalog(GeoConfig(), workspace_dir=".")
    for name, conn in connectors.items():
        catalog.register_connector(name, conn)
    return catalog


# ── per-connector budget ──────────────────────────────────────────────────


class TestPerConnectorBudget:
    def test_local_hit_does_not_touch_hung_backend(self) -> None:
        """A file hit must be returned without calling slow connectors."""
        file_conn = InstantConnector({"source_type": "file", "path": "a.shp"})
        hung = HungConnector()
        catalog = _catalog(file=file_conn, postgis=hung, admin_kg=hung)

        t = time.time()
        detail = catalog.get_source_detail("a")
        assert detail == {"source_type": "file", "path": "a.shp"}
        assert time.time() - t < 5
        assert hung.calls == 0, "hung backend must not be consulted"

    def test_hung_connector_is_abandoned_within_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.5)
        catalog = _catalog(file=InstantConnector(), postgis=HungConnector())
        t = time.time()
        with pytest.raises(ConnectorTimeout):
            catalog.get_source_detail("ghost")
        elapsed = time.time() - t
        assert elapsed < 5, f"budget not enforced ({elapsed:.1f}s)"

    def test_timeout_message_names_the_connector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        catalog = _catalog(file=InstantConnector(), postgis=HungConnector())
        with pytest.raises(ConnectorTimeout) as exc:
            catalog.get_source_detail("ghost")
        assert "postgis" in str(exc.value)

    def test_filtered_lookup_propagates_backend_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow filtered backend must surface as a timeout, not 'not found'."""
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        catalog = _catalog(postgis=HungConnector())
        t = time.time()
        with pytest.raises(ConnectorTimeout):
            catalog.get_source_detail("x", "postgis")
        assert time.time() - t < 5

    def test_total_stays_under_action_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with three hung backends, we stay well under 60s."""
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.5)
        catalog = _catalog(
            file=HungConnector(), postgis=HungConnector(), admin_kg=HungConnector()
        )
        t = time.time()
        with pytest.raises(ConnectorTimeout):
            catalog.get_source_detail("ghost")
        assert time.time() - t < 10


# ── tool level ────────────────────────────────────────────────────────────


class TestToolDegradesGracefully:
    @pytest.mark.asyncio
    async def test_inspect_reports_backend_timeout_not_generic_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from openharness.tools.base import ToolExecutionContext

        from geoharness.data import catalog as mod
        from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        catalog = _catalog(file=InstantConnector(), postgis=HungConnector())
        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": GeoConfig(), "data_catalog": catalog},
            hook_executor=None,
        )
        t = time.time()
        res = await GeoDataTool().execute(
            GeoDataInput(action="inspect", name="ghost"), ctx
        )
        elapsed = time.time() - t
        assert res.is_error
        assert "timed out" in res.output.lower()
        assert "not found" not in res.output.lower()
        assert elapsed < 10, f"took {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_load_reports_backend_timeout(self, tmp_path: Path) -> None:
        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

        # Connector raises ConnectorTimeout directly.
        class TimeoutConnector(InstantConnector):
            def get_source_detail(self, name: str) -> dict[str, Any]:
                raise ConnectorTimeout("postgres is unresponsive")

        catalog = _catalog(postgis=TimeoutConnector())
        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": GeoConfig(), "data_catalog": catalog},
            hook_executor=None,
        )
        res = await GeoDataTool().execute(
            GeoDataInput(action="load", name="province", source="postgis"), ctx
        )
        assert res.is_error
        assert "timed out" in res.output.lower()

    @pytest.mark.asyncio
    async def test_local_flow_unaffected_by_hung_backend(
        self, tmp_path: Path
    ) -> None:
        """The normal local path must stay fast and successful."""
        import json

        from openharness.tools.base import ToolExecutionContext

        from geoharness.data.catalog import DataCatalog
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
        catalog.register_connector("postgis", HungConnector())  # type: ignore[arg-type]
        catalog.register_connector(  # type: ignore[arg-type]
            "file", FileLoader(file_dir=str(tmp_path))
        )

        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": GeoConfig(), "data_catalog": catalog},
            hook_executor=None,
        )
        tool = GeoDataTool()

        for action in ("inspect", "load"):
            t = time.time()
            res = await tool.execute(
                GeoDataInput(action=action, name="roads", source="file"), ctx
            )
            assert not res.is_error, res.output
            assert time.time() - t < 5

    @pytest.mark.asyncio
    async def test_timeout_message_includes_diagnostic(
        self, tmp_path: Path
    ) -> None:
        from openharness.tools.base import ToolExecutionContext

        from geoharness.tools import geo_data as mod
        from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

        class AlwaysTimeoutCatalog:
            def list_sources(self, source_type: str = "all") -> list[Any]:
                time.sleep(5)
                return []

        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": GeoConfig(), "data_catalog": AlwaysTimeoutCatalog()},
            hook_executor=None,
        )
        original = mod._CATALOG_TIMEOUT_SECONDS
        mod._CATALOG_TIMEOUT_SECONDS = 0.3
        try:
            res = await GeoDataTool().execute(GeoDataInput(action="list"), ctx)
        finally:
            mod._CATALOG_TIMEOUT_SECONDS = original
        assert res.is_error
        assert "Where time went" in res.output
        assert "source='file'" in res.output


# ── local fallback when a network backend times out ───────────────────────


class ResettableConnector(HungConnector):
    """Hung backend that records whether the catalog dropped its connection."""

    def __init__(self, delay: float = 30.0) -> None:
        super().__init__(delay)
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1


class TestLocalFallbackOnBackendTimeout:
    def test_local_file_wins_and_backend_never_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """source='postgis' + local file present => file wins, fast, no timeout."""
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        file_conn = InstantConnector({"source_type": "file", "path": "x.shp"})
        hung = HungConnector()
        catalog = _catalog(file=file_conn, postgis=hung)

        t = time.time()
        detail, warning = catalog.resolve_source_detail("x", "postgis")
        assert detail == {"source_type": "file", "path": "x.shp"}
        assert warning == ""
        assert hung.calls == 0, "a local hit must skip the slow backend entirely"
        assert time.time() - t < 1

    def test_specific_backend_timeout_without_local_match_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        catalog = _catalog(file=InstantConnector(), postgis=HungConnector())
        with pytest.raises(ConnectorTimeout):
            catalog.resolve_source_detail("ghost", "postgis")

    def test_file_backend_timeout_is_not_recovered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow *local* connector has nothing to fall back to."""
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        catalog = _catalog(file=HungConnector())
        with pytest.raises(ConnectorTimeout):
            catalog.resolve_source_detail("x", "file")

    def test_broad_lookup_is_not_double_queried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With source='all' the file connector is only consulted once."""
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        file_conn = InstantConnector()
        admin = HungConnector()
        catalog = _catalog(file=file_conn, admin_kg=admin)
        with pytest.raises(ConnectorTimeout):
            catalog.resolve_source_detail("ghost", "all")
        assert file_conn.calls == 1

    def test_timed_out_connector_connection_is_reset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged connector must be dropped so the next call reconnects."""
        from geoharness.data import catalog as mod

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)
        hung = ResettableConnector()
        catalog = _catalog(file=InstantConnector(), postgis=hung)
        with pytest.raises(ConnectorTimeout):
            catalog.get_source_detail("ghost")
        assert hung.resets >= 1, "catalog must reset the wedged connector"

    @pytest.mark.asyncio
    async def test_inspect_local_file_even_when_postgis_requested(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """End-to-end: LLM asks source='postgis', local file is inspected fast."""
        import json

        from openharness.tools.base import ToolExecutionContext

        from geoharness.data import catalog as mod
        from geoharness.data.catalog import DataCatalog
        from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

        monkeypatch.setattr(mod, "_PER_CONNECTOR_SECONDS", 0.4)

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

        hung = HungConnector()
        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        catalog.register_connector("postgis", hung)  # type: ignore[arg-type]
        catalog.register_connector(  # type: ignore[arg-type]
            "file", FileLoader(file_dir=str(tmp_path))
        )

        ctx = ToolExecutionContext(
            cwd=tmp_path,
            metadata={"data_catalog": catalog},
            hook_executor=None,
        )
        t = time.time()
        res = await GeoDataTool().execute(
            GeoDataInput(action="inspect", name="roads", source="postgis"), ctx
        )
        assert not res.is_error, res.output
        assert "Source: roads" in res.output
        assert "timed out" not in res.output
        assert time.time() - t < 2
        assert hung.calls == 0, "local file must be found without touching PostGIS"
