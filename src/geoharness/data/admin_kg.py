"""AdminKGConnector — Chinese administrative boundaries via DataV API.

Fetches province/city/county three-level hierarchy from Aliyun DataV API.
Includes file cache to avoid repeated API calls.

API: https://geo.datav.aliyun.com/v2/district
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


class AdminKGConnector:
    """Chinese administrative boundary connector via DataV API.

    Uses file-based cache to avoid repeated API calls.
    No API key required — DataV public API.
    """

    def __init__(
        self,
        api_url: str = "https://geo.datav.aliyun.com/v2/district",
        cache_dir: str = "",
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".geoharness" / "cache"

        if not self._cache_dir.exists():
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("Failed to create cache dir: %s", e)

    def _fetch_boundary(self, adcode: str, level: str = "full") -> dict[str, Any]:
        """Fetch boundary GeoJSON from DataV API with file cache.

        Args:
            adcode: Administrative code (e.g., "420000" for Hubei).
            level: Boundary detail level
                ("full"=complete, "parent"=parent only, "children"=children only).

        Returns:
            GeoJSON FeatureCollection dict.
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
            with urlopen(url, timeout=30) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))

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

        Args:
            name: Region name (e.g., "湖北省", "武汉市", "武昌区").

        Returns:
            Dictionary with region details, or empty dict if not found.
        """
        # Search provinces first
        try:
            provinces = self.list_provinces()
            for p in provinces:
                if p["name"] == name or p["name"].replace("省", "") == name:
                    return {
                        "name": p["name"],
                        "source_type": "admin_kg",
                        "format": "GeoJSON",
                        "adcode": p["adcode"],
                        "level": "province",
                        "children_count": len(self.list_cities(p["adcode"])),
                    }
        except Exception:
            pass

        # If not found at province level, try city/district
        # (This requires knowing the parent adcode — in production,
        # we'd maintain a lookup table. For now, return empty.)
        return {}
