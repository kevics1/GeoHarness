"""Verify data/context/masking/image seams on an existing workspace, without redraw.

Usage: python diagnostics/verify_test02_prevention.py WORKSPACE DATA_DIR SPREAD_FILE QGIS_JOB_DIR
All validation outputs go to a new temporary directory. Original data is read-only.
"""

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import DataConfig, ExecutionConfig, GeoConfig
from geoharness.data.catalog import build_data_catalog
from geoharness.data.profiles import current_profiles
from geoharness.data.task_context import model_data_context
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool
from geoharness.tools.geo_raster_data import GeoRasterDataInput, GeoRasterDataTool
from geoharness.tools.geo_vector_data import GeoVectorDataInput, GeoVectorDataTool


async def verify(workspace: Path, data_dir: Path, spread: Path, qgis_job: Path) -> None:
    output = Path(tempfile.mkdtemp(prefix="geoharness-test02-prevention-"))
    config = GeoConfig(data=DataConfig(file_dir=str(data_dir)),
                       execution=ExecutionConfig(outputs_dir=str(output / "runs")))
    context = ToolExecutionContext(cwd=workspace, metadata={
        "geoharness_config": config,
        "data_catalog": build_data_catalog(config, workspace_dir=str(workspace)),
    })
    listed = await asyncio.gather(
        GeoVectorDataTool().execute(GeoVectorDataInput(action="list"), context),
        GeoRasterDataTool().execute(GeoRasterDataInput(action="list"), context),
    )
    assert all(not item.is_error for item in listed), [item.output for item in listed]
    profiles = current_profiles(context.metadata)
    rasters = [item for item in profiles.values() if item["kind"] == "raster"]
    assert rasters and all(item.get("crs") for item in rasters)
    inputs = [spread, *(Path(item["path"]) for item in rasters), qgis_job / "preview.png"]
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    code = f"""
import json
import geopandas as gpd
from PIL import Image
source = gpd.read_file({str(spread)!r}).to_crs('EPSG:32648')
clip = gpd.GeoSeries([source.geometry.union_all().buffer(2000)], crs=source.crs)
results = []
for raster in {rasters!r}:
    pixels, transform = mask_raster(raster['path'], clip, crop=True)
    assert pixels.size > 0
    assert GEO_DATA
    results.append({{'raster': raster['name'], 'raster_crs': raster['crs'],
                    'geometry_crs': str(clip.crs), 'shape': list(pixels.shape)}})
assert Image.open({str(qgis_job / 'preview.png')!r}).size[0] > 100
project = json.load(open({str(qgis_job / 'project.json')!r}, encoding='utf-8'))
assert project['backend'] == 'qgis'
print(json.dumps({{'masks': results, 'qgis_image_and_metadata_read': True,
                  'previous_output_count': len(PREVIOUS_OUTPUTS)}}, ensure_ascii=False))
"""
    execution = await GeoPythonTool().execute(GeoPythonInput(code=code, name="test02_prevention"),
                                              context)
    assert not execution.is_error, execution.output
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
               for path, digest in hashes.items())
    prompt = model_data_context(context.metadata, cwd=workspace, config=config)
    assert all(item["crs"] in prompt for item in rasters)
    assert "qgis_jobs" in prompt and "NEW RUN_DIR" in prompt
    verification = {"workspace": str(workspace), "metadata_count": len(profiles),
                    "raster_profiles": rasters, "execution": execution.metadata,
                    "original_hashes_unchanged": hashes,
                    "context_chars": len(prompt), "context": prompt,
                    "full_business_map_redrawn": False}
    target = output / "verification.json"
    target.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    asyncio.run(verify(*(Path(value).resolve() for value in sys.argv[1:])))
