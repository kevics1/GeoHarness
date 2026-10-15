import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from openharness.tools.base import ToolExecutionContext
from rasterio.transform import from_origin
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.execution.contracts import sample_raster_points, score_risk
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


@pytest.mark.asyncio
async def test_cross_run_dependency_failure_repair_and_drift(tmp_path):
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    tool = GeoPythonTool()
    first = await tool.execute(GeoPythonInput(stage_id='extract',
        code="(RUN_DIR/'data.csv').write_text('id,value\\na,3\\n')",
        expected_outputs=['data.csv']), context)
    assert not first.is_error, first.output
    path = next(f['path'] for f in first.metadata['files'] if f['path'].endswith('data.csv'))
    args = GeoPythonInput(stage_id='score', depends_on=['extract'], input_refs={'data': path},
        code="import pandas as pd\ndf = pd.read_csv(INPUTS['data'])\n"
             "df['missing'].to_csv('scores.csv')", expected_outputs=['scores.csv'])
    bad = await tool.execute(args, context)
    assert bad.is_error and bad.metadata['failure']['category'] == 'schema'
    duplicate = await tool.execute(args, context)
    assert duplicate.is_error and duplicate.metadata['script_executed'] is False
    fixed = await tool.execute(args.model_copy(update={'code':
        "import pandas as pd\npd.read_csv(INPUTS['data']).to_csv('scores.csv', index=False)"}),
        context)
    assert not fixed.is_error, fixed.output
    Path(path).write_text('id,value\na,999\n')
    stale = await tool.execute(args, context)
    assert stale.is_error and 'fingerprint changed' in stale.output
    # Success is in the actual producer manifest, not inferred from stdout.
    assert json.loads(Path(fixed.metadata['manifest_path']).read_text())['status'] == 'succeeded'


def test_risk_missing_and_point_sampling_do_not_invent_values(tmp_path):
    frame = gpd.GeoDataFrame({'id': ['a', 'b'], 'value': [3, np.nan]},
                            geometry=[Point(.5, .5), Point(20, 20)], crs=4326)
    components = [{'field': 'value', 'weight': 1, 'minimum': 0, 'maximum': 10,
                   'direction': 'higher'}]
    with pytest.raises(ValueError, match='missing'):
        score_risk(frame, key='id', components=components)
    result = score_risk(frame, key='id', components=components, missing_policy='unknown')
    assert result.risk_status.tolist() == ['computed', 'unknown']
    assert result.geometry.equals(frame.geometry)
    with pytest.raises(ValueError, match='sum to 1'):
        score_risk(frame, key='id', components=[{**components[0], 'weight': .5}],
                   missing_policy='unknown')
    path = tmp_path / 'sample.tif'
    with rasterio.open(path, 'w', driver='GTiff', width=1, height=1, count=1,
                       dtype='int16', nodata=-999, crs=4326,
                       transform=from_origin(0, 1, 1, 1)) as ds:
        ds.write(np.array([[[7]]], dtype='int16'))
    values = sample_raster_points(path, frame)
    assert values[0] == 7 and np.isnan(values[1])
    with rasterio.open(path, 'r+') as ds:
        ds.nodata = None
    assert np.isnan(sample_raster_points(path, frame)[1])
