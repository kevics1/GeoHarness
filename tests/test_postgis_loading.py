"""PostGIS CRS/completeness regression tests; no network connections."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from pyproj import CRS
from shapely.geometry import Point

from geoharness.cartography.sources import resolve_geodataframe
from geoharness.data.postgis import PostGISConnector


class _Cursor:
    def __init__(self, srids=(32650, 32650), definition=("EPSG", 32650, ""), column="geom"):
        self.srids = srids
        self.definition = definition
        self.column = column
        self.calls = []
        self.phase = ""
        self.description = [("id",), (column,), ("__geoh_wkb",), ("__geoh_srid",)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters=None):
        text = query if isinstance(query, str) else query.as_string()
        self.calls.append((text, parameters))
        if "pg_attribute" in text:
            self.phase = "column"
        elif "spatial_ref_sys" in text:
            self.phase = "crs"
        else:
            self.phase = "features"

    def fetchall(self):
        if self.phase == "column":
            return [(self.column,)]
        assert self.phase == "features"
        return [(i, "raw geometry", Point(500000 + i * 25, 3400000).wkb, srid)
                for i, srid in enumerate(self.srids)]

    def fetchone(self):
        assert self.phase == "crs"
        return self.definition


def _connector(cursor, monkeypatch):
    connector = PostGISConnector(dsn="unused")

    class _Connection:
        def cursor(self):
            return cursor

    @contextmanager
    def connection():
        yield _Connection()

    monkeypatch.setattr(connector, "_use_connection", connection)
    return connector


def test_postgis_preserves_real_projected_crs_and_geometry_name(monkeypatch):
    cursor = _Cursor()
    frame = _connector(cursor, monkeypatch).query_geodataframe("assets")
    assert frame.crs.to_epsg() == 32650
    assert frame.geometry.name == "geom"
    assert frame.geometry.x.tolist() == [500000, 500025]
    assert frame.geometry.distance(Point(500000, 3400000)).tolist() == [0, 25]
    assert cursor.calls[1][0].endswith("LIMIT %s")
    assert cursor.calls[1][1] == (5000,)
    assert cursor.calls[2][1] == (32650,)


def test_source_resolution_reads_complete_real_postgis_layer(monkeypatch):
    cursor = _Cursor(srids=(32650,) * 5001)
    connector = _connector(cursor, monkeypatch)

    class _Catalog:
        def get_connector(self, kind):
            assert kind == "postgis"
            return connector

    frame = resolve_geodataframe(source_type="postgis", source_name="assets", catalog=_Catalog())
    assert len(frame) == 5001
    assert "LIMIT" not in cursor.calls[1][0]
    assert cursor.calls[1][1] is None


@pytest.mark.parametrize("srids", [(0, 0), (None, 32650), (-1, 32650)])
def test_unknown_srid_rejected(monkeypatch, srids):
    cursor = _Cursor(srids=srids)
    with pytest.raises(ValueError, match="unknown geometry SRID"):
        _connector(cursor, monkeypatch).query_geodataframe("assets")


def test_mixed_srids_rejected(monkeypatch):
    cursor = _Cursor(srids=(4326, 32650))
    with pytest.raises(ValueError, match="mixed geometry SRIDs"):
        _connector(cursor, monkeypatch).query_geodataframe("assets")


def test_missing_crs_definition_rejected(monkeypatch):
    cursor = _Cursor(definition=None)
    with pytest.raises(ValueError, match="Unknown CRS"):
        _connector(cursor, monkeypatch).query_geodataframe("assets")


def test_custom_postgis_srid_uses_registered_wkt(monkeypatch):
    wkt = CRS.from_epsg(32650).to_wkt()
    cursor = _Cursor(srids=(910001,), definition=(None, None, wkt))
    frame = _connector(cursor, monkeypatch).query_geodataframe("assets", limit=None)
    assert frame.crs.to_epsg() == 32650
    assert "LIMIT" not in cursor.calls[1][0]


def test_identifiers_are_quoted_and_limit_is_parameterized(monkeypatch):
    cursor = _Cursor(column='geom";DROP TABLE x;--')
    _connector(cursor, monkeypatch).query_geodataframe('assets";DROP TABLE x;--', limit=12)
    assert cursor.calls[0][1] == ('"assets"";DROP TABLE x;--"',)
    assert 'FROM "assets"";DROP TABLE x;--"' in cursor.calls[1][0]
    assert 'ST_SRID("geom"";DROP TABLE x;--")' in cursor.calls[1][0]
    assert cursor.calls[1][1] == (12,)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limit_rejected_before_database_access(monkeypatch, limit):
    cursor = _Cursor()
    with pytest.raises(ValueError, match="positive integer"):
        _connector(cursor, monkeypatch).query_geodataframe("assets", limit=limit)
    assert not cursor.calls
