"""Generate two deterministic, disposable GIS acceptance workspaces (no model calls)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box

CRS = 'EPSG:32648'
ORIGIN = np.array([500000., 3000000.])


def generate(root: Path):
    if root.exists():
        raise FileExistsError('请使用新的输出目录，避免覆盖已有测试结果。')
    root.mkdir(parents=True)
    summaries = {}
    for name, complex_case in [('simple', False), ('complex', True)]:
        folder = root / name
        folder.mkdir()
        xy = (np.array([(400*i+100, 400*j+100) for j in range(10) for i in range(12)])
              if complex_case else np.array([(1000*i+100, 100) for i in range(20)]))
        shelters = (np.array([(0, 0), (4800, 0), (0, 4000), (4800, 4000)])
                    if complex_case else np.array([(0, 0), (10000, 0), (20000, 0)]))
        ids = [1007 + i*13 for i in range(len(xy))]  # Intentionally not row numbers.
        population = [None if complex_case and i in {4, 27, 98} else 100+i*7
                      for i in range(len(xy))]
        points = gpd.GeoDataFrame({'entity_id': ids,
            'name': [f'居民点{i+1}' for i in range(len(xy))], 'population': population},
            geometry=[Point(*(p+ORIGIN)) for p in xy], crs=CRS)
        points.to_file(folder/'residents.gpkg', layer='residents')
        facilities = gpd.GeoDataFrame({'shelter_id': list(range(1, len(shelters)+1)),
            'name': [f'避难所{i+1}' for i in range(len(shelters))]},
            geometry=[Point(*(p+ORIGIN)) for p in shelters], crs=CRS)
        # Complex case deliberately requires reprojection before distance analysis.
        (facilities.to_crs(4326) if complex_case else facilities).to_file(
            folder/'shelters.gpkg', layer='shelters')
        distances = np.linalg.norm(xy[:, None, :] - shelters[None, :, :], axis=2)
        nearest = distances.argmin(axis=1)
        actual = distances[np.arange(len(xy)), nearest]
        import csv

        with (folder/'expected.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['entity_id', 'nearest_shelter_id', 'distance_m', 'priority'])
            writer.writerows(zip(ids, nearest+1, actual, actual < 500, strict=True))
        if complex_case:
            gpd.GeoDataFrame({'hour': [1, 2, 3]}, geometry=[
                box(500000, 3000000, 500000+w, 3000000+h)
                for w, h in [(2000, 2000), (3200, 2800), (4800, 4000)]], crs=CRS).to_file(
                    folder/'fire.gpkg', layer='fire')
            gpd.GeoDataFrame({'road_id': [8, 19]}, geometry=[
                LineString([(500000, 3001000), (504800, 3001000)]),
                LineString([(502000, 3000000), (502000, 3004000)])], crs=CRS).to_file(
                    folder/'roads.gpkg', layer='roads')
            dem = np.full((50, 60), 200., dtype='float32')
            dem[47:50, 0:3] = -9999  # One resident falls in this genuine missing-data patch.
            with rasterio.open(folder/'dem.tif', 'w', driver='GTiff', width=60, height=50,
                    count=1, dtype='float32', crs=points.crs.to_wkt(),
                    transform=from_origin(500000, 3005000, 100, 100), nodata=-9999) as dst:
                dst.write(dem, 1)
        expected = {'resident_count': len(xy), 'shelter_count': len(shelters),
            'priority_distance_m': '<500', 'priority_count': int((actual < 500).sum()),
            'population_missing': 3 if complex_case else 0,
            'elevation_missing': 1 if complex_case else 'not_requested',
            'required_pages': 4 if complex_case else 1,
            'expected_initial_review_requests': 4 if complex_case else 1,
            'max_review_requests_per_page': 3, 'max_review_requests_per_task': 12}
        (folder/'expected.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2),
                                           encoding='utf-8')
        summaries[name] = expected
    return summaries


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    print(json.dumps(generate(parser.parse_args().output), ensure_ascii=False, indent=2))
