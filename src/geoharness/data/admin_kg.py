"""AdminKGConnector — Chinese administrative boundaries via DataV API.

Fetches province/city/county three-level hierarchy from Aliyun DataV API.
Includes file cache to avoid repeated API calls.

API: https://geo.datav.aliyun.com/areas_v3/bound
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from geoharness.data.catalog import DataSource

logger = logging.getLogger(__name__)

# Administrative levels
_LEVELS = ["province", "city", "district"]

# Suffixes that mark a name as a Chinese administrative region. Used to skip
# needless network lookups for table/file identifiers.
_REGION_SUFFIXES = (
    "省", "市", "区", "县", "州", "盟", "旗",
    "自治区", "特别行政区", "自治县", "自治州",
)


def _looks_like_region_name(name: str) -> bool:
    """Heuristic: does ``name`` look like a Chinese administrative region?

    Local file names (``城际公路``), PostGIS tables (``province``) and typo'd
    identifiers must not trigger a network request.
    """
    candidate = (name or "").strip()
    if not candidate:
        return False
    if candidate.isdigit():
        return True
    return any(candidate.endswith(suffix) for suffix in _REGION_SUFFIXES)


class AdminKGConnector:
    """Chinese administrative boundary connector via DataV API.

    Uses file-based cache to avoid repeated API calls.
    No API key required — DataV public API.
    """

    def __init__(
        self,
        api_url: str = "https://geo.datav.aliyun.com/areas_v3/bound",
        cache_dir: str = "",
    ) -> None:
        self._api_url = api_url.rstrip("/")
        if cache_dir:
            self._cache_dir = Path(cache_dir).expanduser()
        else:
            self._cache_dir = Path.home() / ".geoharness" / "cache"

        if not self._cache_dir.exists():
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("Failed to create cache dir: %s", e)

    @staticmethod
    def _ssl_contexts() -> list[Any]:
        """Return candidate SSL contexts, most capable first.

        Some servers (the DataV API included) close the connection during a
        TLS 1.3 handshake. Pinning TLS 1.2 makes the request succeed where the
        default context fails with ``UNEXPECTED_EOF_WHILE_READING``. The
        default context stays first so healthy servers are unaffected.
        """
        import ssl

        contexts: list[Any] = [ssl.create_default_context()]
        try:
            legacy = ssl.create_default_context()
            legacy.maximum_version = ssl.TLSVersion.TLSv1_2
            contexts.append(legacy)
        except Exception as exc:  # pragma: no cover - very old Python
            logger.debug("Could not build a TLS1.2 context: %s", exc)
        return contexts

    # The DataV endpoint intermittently resets connections mid-handshake
    # ("UNEXPECTED_EOF_WHILE_READING"). A few short retries plus a TLS1.2
    # fallback make the fetch reliable — but the whole operation is capped by
    # a total deadline, so an unreachable network fails fast instead of
    # multiplying retries x contexts x timeout into minutes.
    _FETCH_ATTEMPTS = 2
    _FETCH_DEADLINE_SECONDS = 8.0
    _FETCH_TIMEOUT_SECONDS = 4

    def _http_get_json(
        self, url: str, timeout: int | None = None
    ) -> dict[str, Any]:
        """GET ``url`` and decode JSON within a hard total deadline.

        Args:
            url: Absolute URL to fetch.
            timeout: Per-attempt socket timeout. Defaults to a small value so
                attempts stay cheap; the overall budget is
                ``_FETCH_DEADLINE_SECONDS`` regardless.

        Raises:
            TimeoutError: if the deadline elapses before a successful read.
            Exception: the last transport error, if every attempt fails.
        """
        import time as _time

        per_attempt = timeout or self._FETCH_TIMEOUT_SECONDS
        deadline = _time.monotonic() + self._FETCH_DEADLINE_SECONDS
        last_error: Exception | None = None

        for attempt in range(self._FETCH_ATTEMPTS):
            for context in self._ssl_contexts():
                if _time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"DataV fetch exceeded {self._FETCH_DEADLINE_SECONDS:.0f}s "
                        f"budget for {url}"
                    ) from last_error
                try:
                    with urlopen(  # noqa: S310
                        url, timeout=per_attempt, context=context
                    ) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except Exception as exc:
                    last_error = exc
                    logger.debug(
                        "Fetch attempt %d failed for %s (%s)",
                        attempt + 1,
                        url,
                        exc,
                    )
        raise last_error if last_error else RuntimeError("No SSL context available")

    def _fetch_boundary(self, adcode: str, level: str = "full") -> dict[str, Any]:
        """Fetch boundary GeoJSON from DataV API with file cache.

        Args:
            adcode: Administrative code (e.g., "420000" for Hubei).
            level: Boundary detail level
                ("full"=complete, "parent"=parent only, "children"=children only).

        Returns:
            GeoJSON FeatureCollection dict (empty FeatureCollection on failure).
        """
        cache_file = self._cache_dir / f"{adcode}_{level}.geojson"

        # Try cache first
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Cache read failed for %s: %s", cache_file, e)

        # Fetch from API
        url = f"{self._api_url}/{adcode}_{level}.json"
        try:
            data = self._http_get_json(url)

            # Cache the result
            try:
                cache_file.write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as e:
                logger.warning("Cache write failed: %s", e)

            return data
        except Exception as e:
            logger.error("API fetch failed for adcode %s: %s", adcode, e)
            # Return empty feature collection
            return {"type": "FeatureCollection", "features": []}

    def list_provinces(self) -> list[dict[str, str]]:
        """List all provinces with adcodes."""
        data = self._fetch_boundary("100000", "full")
        provinces: list[dict[str, str]] = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            provinces.append({
                "name": props.get("name", ""),
                "adcode": str(props.get("adcode", "")),
                "level": "province",
            })
        return provinces

    def list_cities(self, province_adcode: str) -> list[dict[str, str]]:
        """List cities within a province."""
        data = self._fetch_boundary(province_adcode, "children")
        cities: list[dict[str, str]] = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            if props.get("level") == "city":
                cities.append({
                    "name": props.get("name", ""),
                    "adcode": str(props.get("adcode", "")),
                    "level": "city",
                    "parent": province_adcode,
                })
        return cities

    def list_districts(self, city_adcode: str) -> list[dict[str, str]]:
        """List districts within a city."""
        data = self._fetch_boundary(city_adcode, "children")
        districts: list[dict[str, str]] = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            if props.get("level") == "district":
                districts.append({
                    "name": props.get("name", ""),
                    "adcode": str(props.get("adcode", "")),
                    "level": "district",
                    "parent": city_adcode,
                })
        return districts

    def get_boundary(self, adcode: str) -> dict[str, Any]:
        """Get boundary GeoJSON for an adcode."""
        return self._fetch_boundary(adcode, "full")

    def load_geodataframe(self, region: str) -> Any:
        """Load an administrative boundary as a GeoDataFrame.

        Args:
            region: Region name (e.g., "湖北省") or a 6-digit adcode.

        Returns:
            GeoDataFrame in EPSG:4326, one row per returned boundary feature.

        Raises:
            ValueError: If the region cannot be resolved to a boundary.
        """
        import geopandas as gpd

        name = (region or "").strip()
        if not name:
            raise ValueError("Region name or adcode is required.")

        # Direct adcode (all digits) → fetch straight away.
        if name.isdigit():
            data = self._fetch_boundary(name, "full")
        else:
            detail = self.get_source_detail(name)
            adcode = str(detail.get("adcode", "")) if detail else ""
            if not adcode:
                raise ValueError(
                    f"Administrative region not found: {region!r}. "
                    "Only province-level names are resolvable offline; "
                    "pass an adcode for city/district boundaries."
                )
            data = self._fetch_boundary(adcode, "full")

        features = data.get("features", [])
        if not features:
            raise ValueError(
                f"No boundary features returned for {region!r} "
                "(network failure or unknown adcode)."
            )
        return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")

    def list_sources(self) -> list[DataSource]:
        """List all province-level boundaries as data sources."""
        try:
            provinces = self.list_provinces()
        except Exception as e:
            logger.warning("Failed to list provinces: %s", e)
            return []

        return [
            DataSource(
                name=p["name"],
                source_type="admin_kg",
                metadata={
                    "format": "GeoJSON",
                    "adcode": p["adcode"],
                    "level": "province",
                    "api_url": self._api_url,
                },
            )
            for p in provinces
        ]

    def get_source_detail(self, name: str) -> dict[str, Any]:
        """Get details for a specific administrative region by name.

        Offline-first: local file names and PostGIS table names never carry a
        Chinese region suffix, so a cheap shape check avoids a pointless
        network round-trip (and the multi-second stall that follows when the
        network is unreachable).

        Args:
            name: Region name (e.g., "湖北省", "武汉市") or a 6-digit adcode.

        Returns:
            Dictionary with region details, or empty dict if not found.
        """
        candidate = (name or "").strip()
        if not candidate:
            return {}

        # A bare adcode: resolve it as a region without a province lookup.
        if candidate.isdigit():
            data = self._fetch_boundary(candidate, "full")
            features = data.get("features", [])
            if not features:
                return {}
            props = features[0].get("properties", {})
            return {
                "name": props.get("name", candidate),
                "source_type": "admin_kg",
                "format": "GeoJSON",
                "adcode": str(props.get("adcode", candidate)),
                "level": props.get("level", ""),
            }

        # Only names that look like Chinese administrative regions are worth a
        # network request. Table/file identifiers ("province", "城际公路") are
        # not regions, so skip the network entirely.
        if not _looks_like_region_name(candidate):
            return {}

        try:
            provinces = self.list_provinces()
            for p in provinces:
                if p["name"] == candidate or p["name"].replace("省", "") == candidate:
                    return {
                        "name": p["name"],
                        "source_type": "admin_kg",
                        "format": "GeoJSON",
                        "adcode": p["adcode"],
                        "level": "province",
                        "children_count": len(self.list_cities(p["adcode"])),
                    }
        except Exception as exc:
            logger.debug("Province lookup failed for %r: %s", candidate, exc)

        # If not found at province level, try city/district
        # (This requires knowing the parent adcode — in production,
        # we'd maintain a lookup table. For now, return empty.)
        return {}
