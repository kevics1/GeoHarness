"""Tests for geoharness.data layer — catalog, postgis, file_loader, admin_kg.

CRITICAL: All connector tests MUST mock network/DB calls.
Real TCP connections will hang the test suite indefinitely.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from geoharness.config.settings import DataConfig, GeoConfig
from geoharness.data.catalog import DataCatalog, DataSource

# ─── Mock Connectors ──────────────────────────────────────────────────────


class MockPostGISConnector:
    """Mock PostGIS connector — no real DB connection."""

    def __init__(self, **kwargs) -> None:
        self.tables = ["wuhan_air_quality", "china_rivers", "land_use_2024"]

    def list_tables(self) -> list[str]:
        return self.tables

    def get_table_schema(self, table_name: str) -> dict[str, str]:
        schemas = {
            "wuhan_air_quality": {"id": "integer", "geom": "geometry", "pm25": "double"},
            "china_rivers": {"id": "integer", "geom": "geometry", "name": "text"},
            "land_use_2024": {"id": "integer", "geom": "geometry", "type": "text"},
        }
        return schemas.get(table_name, {})

    def get_table_extent(self, table_name: str) -> list[float] | None:
        extents = {
            "wuhan_air_quality": [114.0, 30.0, 115.0, 31.0],
            "china_rivers": [100.0, 20.0, 120.0, 40.0],
            "land_use_2024": [73.0, 18.0, 135.0, 53.0],
        }
        return extents.get(table_name)

    def list_sources(self) -> list[DataSource]:
        return [
            DataSource(
                name=table,
                source_type="postgis",
                metadata={
                    "format": "PostGIS table",
                    "path": table,
                    "crs": "EPSG:4326",
                    "schema": self.get_table_schema(table),
                    "bbox": self.get_table_extent(table),
                },
            )
            for table in self.tables
        ]

    def get_source_detail(self, name: str) -> dict:
        if name not in self.tables:
            return {}
        return {
            "name": name,
            "source_type": "postgis",
            "format": "PostGIS table",
            "path": name,
            "crs": "EPSG:4326",
            "schema": self.get_table_schema(name),
            "bbox": self.get_table_extent(name),
        }


class MockFileLoader:
    """Mock file loader — no real file I/O."""

    def __init__(self, **kwargs) -> None:
        self.files = ["wuhan_pm25.geojson", "yangtze_basin.shp", "stations.csv"]

    def list_sources(self) -> list[DataSource]:
        return [
            DataSource(
                name=Path(f).stem,
                source_type="file",
                metadata={
                    "format": Path(f).suffix.lstrip("."),
                    "path": f"/data/{f}",
                },
            )
            for f in self.files
        ]

    def get_source_detail(self, name: str) -> dict:
        for f in self.files:
            if Path(f).stem == name or f == name:
                return {
                    "name": name,
                    "source_type": "file",
                    "format": Path(f).suffix.lstrip("."),
                    "path": f"/data/{f}",
                    "crs": "EPSG:4326",
                }
        return {}


class MockAdminKGConnector:
    """Mock admin KG connector — no real API calls."""

    def __init__(self, **kwargs) -> None:
        self.provinces = [
            {"name": "湖北省", "adcode": "420000"},
            {"name": "广东省", "adcode": "440000"},
            {"name": "四川省", "adcode": "510000"},
        ]

    def list_provinces(self) -> list[dict]:
        return self.provinces

    def list_sources(self) -> list[DataSource]:
        return [
            DataSource(
                name=p["name"],
                source_type="admin_kg",
                metadata={
                    "format": "GeoJSON",
                    "adcode": p["adcode"],
                    "level": "province",
                },
            )
            for p in self.provinces
        ]

    def get_source_detail(self, name: str) -> dict:
        for p in self.provinces:
            if p["name"] == name or p["name"].replace("省", "") == name:
                return {
                    "name": p["name"],
                    "source_type": "admin_kg",
                    "format": "GeoJSON",
                    "adcode": p["adcode"],
                    "level": "province",
                }
        return {}


# ─── DataCatalog Tests ────────────────────────────────────────────────────


class TestDataCatalog:
    """Test DataCatalog with mocked connectors."""

    @pytest.fixture
    def mock_config(self) -> GeoConfig:
        """Config with all data sources configured."""
        return GeoConfig(
            data=DataConfig(
                postgis_dsn="postgresql://mock@localhost/mock",
                file_dir="/mock/data",
                admin_kg_api_url="https://mock.api.example.com",
                admin_kg_cache_dir="/mock/cache",
            )
        )

    def test_catalog_creation_no_connection(self, mock_config: GeoConfig):
        """DataCatalog creation should NOT establish any connections."""
        catalog = DataCatalog(mock_config)
        # _initialized should be False — no connections made
        assert catalog._initialized is False

    def test_list_sources_with_mocked_connectors(
        self, mock_config: GeoConfig
    ):
        """List sources with all connectors mocked."""
        catalog = DataCatalog(mock_config)
        catalog.register_connector("postgis", MockPostGISConnector())
        catalog.register_connector("file", MockFileLoader())
        catalog.register_connector("admin_kg", MockAdminKGConnector())

        sources = catalog.list_sources()
        # 3 PostGIS + 3 files + 3 provinces = 9
        assert len(sources) == 9

    def test_list_sources_filtered_by_type(
        self, mock_config: GeoConfig
    ):
        """List sources filtered by source type."""
        catalog = DataCatalog(mock_config)
        catalog.register_connector("postgis", MockPostGISConnector())
        catalog.register_connector("file", MockFileLoader())
        catalog.register_connector("admin_kg", MockAdminKGConnector())

        pg_sources = catalog.list_sources("postgis")
        assert len(pg_sources) == 3
        assert all(s.source_type == "postgis" for s in pg_sources)

        file_sources = catalog.list_sources("file")
        assert len(file_sources) == 3
        assert all(s.source_type == "file" for s in file_sources)

        admin_sources = catalog.list_sources("admin_kg")
        assert len(admin_sources) == 3
        assert all(s.source_type == "admin_kg" for s in admin_sources)

    def test_get_source_detail(self, mock_config: GeoConfig):
        """Get detail for a specific source."""
        catalog = DataCatalog(mock_config)
        catalog.register_connector("postgis", MockPostGISConnector())

        detail = catalog.get_source_detail("wuhan_air_quality")
        assert detail is not None
        assert detail["name"] == "wuhan_air_quality"
        assert detail["source_type"] == "postgis"
        assert "schema" in detail

    def test_get_source_detail_not_found(self, mock_config: GeoConfig):
        """Get detail for non-existent source returns None."""
        catalog = DataCatalog(mock_config)
        catalog.register_connector("postgis", MockPostGISConnector())

        detail = catalog.get_source_detail("nonexistent_table")
        assert detail is None

    def test_connector_names(self, mock_config: GeoConfig):
        """Test connector_names property."""
        catalog = DataCatalog(mock_config)
        catalog.register_connector("postgis", MockPostGISConnector())
        catalog.register_connector("file", MockFileLoader())

        names = catalog.connector_names
        assert "postgis" in names
        assert "file" in names

    def test_empty_config_no_crash(self):
        """Catalog with empty config should not crash."""
        config = GeoConfig()  # All defaults
        catalog = DataCatalog(config)
        # Should be able to create without any data sources configured
        assert catalog._initialized is False


# ─── PostGISConnector Tests (mocked) ──────────────────────────────────────


class TestPostGISConnector:
    """Test PostGISConnector with mocked psycopg."""

    def test_list_sources_with_mock(self):
        """Test list_sources with mocked DB connection."""
        with patch("geoharness.data.postgis.PostGISConnector._ensure_connection") as mock_conn:
            mock_cursor = MagicMock()
            # list_sources now issues a single information_schema query that
            # returns (table_name, column_name) pairs for geometry columns.
            mock_cursor.fetchall.return_value = [
                ("wuhan_air", "geom"), ("china_rivers", "geom"),
            ]
            mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
            mock_cursor.__exit__ = MagicMock(return_value=False)
            mock_conn_obj = MagicMock()
            mock_conn_obj.cursor.return_value = mock_cursor
            mock_conn.return_value = mock_conn_obj

            from geoharness.data.postgis import PostGISConnector

            connector = PostGISConnector(dsn="postgresql://mock@localhost/mock")
            # Mock list_tables to avoid complex DB mocking
            connector.list_tables = MagicMock(return_value=["wuhan_air", "china_rivers"])
            connector.get_table_schema = MagicMock(
                return_value={"id": "integer", "geom": "geometry"}
            )
            connector.get_table_extent = MagicMock(return_value=[114.0, 30.0, 115.0, 31.0])

            sources = connector.list_sources()
            assert len(sources) == 2
            assert sources[0].source_type == "postgis"
            assert sources[0].name == "wuhan_air"

    def test_write_operation_blocked(self):
        """Test that write operations are blocked."""
        from geoharness.data.postgis import PostGISConnector

        connector = PostGISConnector(dsn="postgresql://mock@localhost/mock")
        with pytest.raises(ValueError, match="Write operation blocked"):
            connector._check_read_only("INSERT INTO test VALUES (1)")
        with pytest.raises(ValueError, match="Write operation blocked"):
            connector._check_read_only("DROP TABLE test")
        # Read queries should not raise
        connector._check_read_only("SELECT * FROM test")


# ─── FileLoader Tests ─────────────────────────────────────────────────────


class TestFileLoader:
    """Test FileLoader with mocked file system."""

    def test_list_sources_with_mocked_dir(self, tmp_path: Path):
        """Test listing files from a mocked directory."""
        # Create mock spatial files
        (tmp_path / "test1.geojson").write_text("{}", encoding="utf-8")
        (tmp_path / "test2.shp").write_text("mock", encoding="utf-8")
        (tmp_path / "data.csv").write_text("lat,lon\n30,114", encoding="utf-8")
        (tmp_path / "readme.txt").write_text("not spatial", encoding="utf-8")

        from geoharness.data.file_loader import FileLoader

        loader = FileLoader(file_dir=str(tmp_path))
        sources = loader.list_sources()

        # Should find 3 spatial files (not .txt)
        assert len(sources) == 3
        names = {s.name for s in sources}
        assert "test1" in names
        assert "test2" in names
        assert "data" in names

    def test_get_source_detail_not_found(self, tmp_path: Path):
        """Test get_source_detail for non-existent file."""
        from geoharness.data.file_loader import FileLoader

        loader = FileLoader(file_dir=str(tmp_path))
        detail = loader.get_source_detail("nonexistent")
        assert detail == {}


# ─── AdminKGConnector Tests (mocked) ──────────────────────────────────────


class TestAdminKGConnector:
    """Test AdminKGConnector with mocked API."""

    def test_list_sources_with_mocked_api(self):
        """Test list_sources with mocked API response."""
        mock_response = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "湖北省", "adcode": "420000"},
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {"name": "广东省", "adcode": "440000"},
                    "geometry": None,
                },
            ],
        }

        from geoharness.data.admin_kg import AdminKGConnector

        connector = AdminKGConnector(
            api_url="https://mock.api.example.com",
            cache_dir="",  # Use default but mock the fetch
        )

        # Mock _fetch_boundary to return our test data
        connector._fetch_boundary = MagicMock(return_value=mock_response)

        sources = connector.list_sources()
        assert len(sources) == 2
        assert sources[0].name == "湖北省"
        assert sources[0].source_type == "admin_kg"
        assert sources[0].metadata["adcode"] == "420000"

    def test_get_source_detail_found(self):
        """Test get_source_detail for a known province."""
        mock_response = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "湖北省", "adcode": "420000"},
                    "geometry": None,
                },
            ],
        }

        from geoharness.data.admin_kg import AdminKGConnector

        connector = AdminKGConnector(api_url="https://mock.api.example.com")
        connector._fetch_boundary = MagicMock(return_value=mock_response)

        detail = connector.get_source_detail("湖北省")
        assert detail["name"] == "湖北省"
        assert detail["adcode"] == "420000"
        assert detail["level"] == "province"

    def test_get_source_detail_not_found(self):
        """Test get_source_detail for unknown region."""
        mock_response = {
            "type": "FeatureCollection",
            "features": [],
        }

        from geoharness.data.admin_kg import AdminKGConnector

        connector = AdminKGConnector(api_url="https://mock.api.example.com")
        connector._fetch_boundary = MagicMock(return_value=mock_response)

        detail = connector.get_source_detail("不存在的省")
        assert detail == {}


# ─── Integration: DataCatalog + build_data_catalog ────────────────────────


class TestBuildDataCatalog:
    """Test the build_data_catalog factory function."""

    def test_build_returns_datacatalog(self):
        """build_data_catalog should return a DataCatalog instance."""
        from geoharness.data.catalog import build_data_catalog

        config = GeoConfig()
        catalog = build_data_catalog(config)
        assert isinstance(catalog, DataCatalog)

    def test_build_does_not_initialize(self):
        """build_data_catalog should NOT initialize connectors."""
        from geoharness.data.catalog import build_data_catalog

        config = GeoConfig()
        catalog = build_data_catalog(config)
        assert catalog._initialized is False
