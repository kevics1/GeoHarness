"""Exercise real child processes, GIS output and analysis guard boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError
from shapely.geometry import Point

from geoharness.config.settings import ExecutionConfig, GeoConfig, MapReviewConfig
from geoharness.execution import execute_python
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


async def run_script(tmp_path: Path, code: str, **kwargs):
    return await execute_python(
        code=code,
        name="test-analysis",
        cwd=tmp_path,
        data_dir=kwargs.pop("data_dir", None),
        outputs_dir=tmp_path / "runs",
        timeout_seconds=kwargs.pop("timeout_seconds", 30),
        max_output_chars=kwargs.pop("max_output_chars", 12000),
        **kwargs,
    )


async def test_qgis_diagnostics_are_exact_read_only_files_not_a_directory_grant(tmp_path):
    job = tmp_path / ".geoharness/qgis/doctor-test"
    job.mkdir(parents=True)
    for name in ("request.json", "result.json", "stdout.txt", "stderr.txt"):
        (job / name).write_text("diagnostic", encoding="utf-8")
    (job / "manifest.json").write_text(json.dumps({"run_dir": str(job)}), encoding="utf-8")
    (job / "other.txt").write_text("private", encoding="utf-8")
    (job.parent / "config.yaml").write_text("private", encoding="utf-8")
    (job / ".profile").mkdir()
    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    code = f"""
from pathlib import Path
p = Path({str(job)!r})
for name in ('request.json','result.json','manifest.json','stdout.txt','stderr.txt'):
    assert (p/name).read_text()
for target, operation in [(p/'request.json','write'),(p/'other.txt','read'),
                          (p.parent/'config.yaml','read'),(p/'.profile','list'),(p,'list')]:
    try:
        if operation=='write':target.write_text('changed')
        elif operation=='list':list(target.iterdir())
        else:target.read_text()
    except PermissionError:pass
    else:raise AssertionError(f'Unexpected grant: {{target}}')
print('five diagnostics read; other paths and writes denied')
"""
    result = await GeoPythonTool().execute(GeoPythonInput(code=code), context)
    assert not result.is_error, result.output
    assert result.metadata["execution_contract"]["DATA_DIRS"] == "list[Path]"
    assert (job / "request.json").read_text(encoding="utf-8") == "diagnostic"


@pytest.mark.parametrize(("code", "expected"), [
    ("DATA_DIRS.split(':')", "DATA_DIRS is list[Path]"),
    ("from shapely.geometry import Point\nPoint(0,0).to_crs(3857)", "GeoSeries"),
    ("(SOURCE_CWD/'outside.txt').write_text('x')", "RUN_DIR"),
    ("print_stats('fuel', band=1)", "geo_raster_data action=stats"),
])
async def test_python_failures_return_specific_contract_hints(tmp_path, code, expected):
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoPythonTool().execute(GeoPythonInput(code=code), context)
    assert result.is_error
    assert expected in json.dumps(result.metadata["recovery_hints"])
    assert result.metadata["error_summary"]
    assert list(json.loads(result.output))[:3] == ["status", "error_summary", "recovery_hints"]


def test_qgis_diagnostic_discovery_rejects_redirected_files_and_mismatched_runs(tmp_path):
    from geoharness.tools.geo_python import qgis_diagnostic_files

    root = tmp_path / "qgis"
    job = root / "job"
    job.mkdir(parents=True)
    manifest = job / "manifest.json"
    manifest.write_text(json.dumps({"run_dir": str(tmp_path)}), encoding="utf-8")
    assert qgis_diagnostic_files(root) == ()
    manifest.write_text(json.dumps({"run_dir": str(job)}), encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (job / "request.json").symlink_to(outside)
        (root / "linked-job").symlink_to(job, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Filesystem link permission unavailable: {exc}")
    assert qgis_diagnostic_files(root) == (manifest,)


async def test_explicit_read_files_never_grant_credentials(tmp_path):
    credential = tmp_path / ".env"
    credential.write_text("synthetic", encoding="utf-8")
    result = await run_script(tmp_path, f"open({str(credential)!r}).read()",
                              read_files=(credential,))
    assert result["exit_code"] != 0
    assert "PermissionError" in result["stderr"]


async def test_real_gis_analysis_writes_vector_image_and_raster(tmp_path: Path):
    data = tmp_path / "输入数据"
    data.mkdir()
    source = data / "居民点.geojson"
    gpd.GeoDataFrame(
        {"name": ["甲", "乙"]}, geometry=[Point(0, 0), Point(100, 0)], crs="EPSG:3857"
    ).to_file(source, driver="GeoJSON")
    result = await run_script(
        tmp_path,
        """
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.transform import from_origin
frame = gpd.read_file(DATA_DIRS[1] / "居民点.geojson")
buffered = frame.copy()
buffered.geometry = frame.buffer(20)
buffered.to_file("影响范围.geojson", driver="GeoJSON")
assert buffered.geometry.contains(frame.geometry).all()
assert 2500 < buffered.geometry.area.sum() < 2520
frame.plot()
plt.savefig("地图.png")
with rasterio.open("栅格.tif", "w", driver="GTiff", height=2, width=2,
                   count=1, dtype="uint8", crs="EPSG:3857",
                   transform=from_origin(0, 2, 1, 1)) as dst:
    dst.write(np.ones((1, 2, 2), dtype="uint8"))
with rasterio.open("栅格.tif") as src:
    assert src.read(1).sum() == 4
print("完成：2 个缓冲区")
""",
        data_dir=data,
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert "完成" in result["stdout"]
    paths = {Path(value["path"]).name for value in result["files"]}
    assert paths == {"影响范围.geojson", "地图.png", "栅格.tif"}
    assert Path(result["script_path"]).is_file()
    persisted = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert persisted == result
    assert gpd.read_file(source).geometry.type.tolist() == ["Point", "Point"]


@pytest.mark.parametrize(
    "operation",
    [
        'source.write_text("changed")',
        "source.unlink()",
        'source.rename(RUN_DIR / "moved.txt")',
        '(SOURCE_CWD / "outside.txt").write_text("outside")',
        'open("../escaped.txt", "w").write("outside")',
    ],
)
async def test_source_mutation_and_path_escape_are_denied(tmp_path: Path, operation: str):
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    result = await run_script(tmp_path, f"source = SOURCE_CWD / 'source.txt'\n{operation}")
    assert result["exit_code"] != 0
    assert "PermissionError" in result["stderr"]
    assert source.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "outside.txt").exists()
    assert not (tmp_path / "runs" / "escaped.txt").exists()


@pytest.mark.parametrize(
    "code",
    [
        'import socket; socket.create_connection(("localhost", 1))',
        'import subprocess, sys; subprocess.run([sys.executable, "-c", "print(1)"])',
        'import os; os.system("echo forbidden")',
    ],
)
async def test_network_and_subprocess_are_denied(tmp_path: Path, code: str):
    result = await run_script(tmp_path, code)
    assert result["exit_code"] != 0
    assert "PermissionError" in result["stderr"]


@pytest.mark.parametrize("filename", [".env", ".geoharness/config.yaml", ".ssh/id_ed25519"])
async def test_credentials_cannot_be_read(tmp_path: Path, filename: str):
    secret = tmp_path / filename
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("secret-value", encoding="utf-8")
    result = await run_script(tmp_path, f"print((SOURCE_CWD / {filename!r}).read_text())")
    assert result["exit_code"] != 0
    assert "PermissionError" in result["stderr"]
    assert "secret-value" not in result["stdout"]


async def test_read_outside_data_directories_is_denied(tmp_path: Path):
    source = tmp_path / "outside.txt"
    source.write_text("outside", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    result = await run_script(work, f"print(open({str(source)!r}).read())")
    assert result["exit_code"] != 0
    assert "PermissionError" in result["stderr"]


async def test_native_gis_writes_and_network_paths_are_checked(tmp_path: Path):
    result = await run_script(
        tmp_path,
        """
import geopandas as gpd
import pyogrio
import rasterio
from shapely.geometry import Point
frame = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs="EPSG:3857")
attempts = [
    lambda: frame.to_file(SOURCE_CWD / "outside.geojson", driver="GeoJSON"),
    lambda: pyogrio.write_dataframe(frame, SOURCE_CWD / "outside.gpkg"),
    lambda: rasterio.open(SOURCE_CWD / "outside.tif", "w", driver="GTiff",
                         width=1, height=1, count=1, dtype="uint8"),
    lambda: gpd.read_file("https://example.invalid/data.geojson"),
    lambda: rasterio.open("/vsicurl/https://example.invalid/data.tif"),
]
for attempt in attempts:
    try:
        attempt()
    except PermissionError:
        continue
    raise AssertionError("unguarded native I/O")
print("guarded")
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "guarded"
    assert not list(tmp_path.glob("outside.*"))


def write_source_raster(path: Path, driver: str = "GTiff"):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
        path,
        "w",
        driver=driver,
        width=2,
        height=2,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 2, 1, 1),
    ) as dst:
        dst.write(np.ones((1, 2, 2), dtype="uint8"))


async def test_raster_public_copy_and_delete_cannot_mutate_sources(tmp_path: Path):
    source = tmp_path / "source.tif"
    write_source_raster(source)
    original = source.read_bytes()
    result = await run_script(
        tmp_path,
        """
import rasterio
from rasterio.shutil import copy, copyfiles, delete
source = SOURCE_CWD / "source.tif"
outside = SOURCE_CWD / "outside.tif"
with rasterio.open(source) as dataset:
    attempts = [
        lambda: copy(source, outside),
        lambda: copy(src=dataset, dst=outside, driver="GTiff"),
        lambda: copyfiles(src=source, dst=outside),
        lambda: delete(path=source),
    ]
    for attempt in attempts:
        try:
            attempt()
        except PermissionError:
            continue
        raise AssertionError("source raster was not protected")
print("protected")
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert source.read_bytes() == original
    assert not (tmp_path / "outside.tif").exists()


async def test_raster_vrt_sensitive_references_are_denied_before_pixel_read(tmp_path: Path):
    import rasterio.shutil

    sensitive = tmp_path / ".env"
    write_source_raster(sensitive)
    rasterio.shutil.copy(sensitive, tmp_path / "proxy.vrt", driver="VRT")
    original = sensitive.read_bytes()
    result = await run_script(
        tmp_path,
        """
import rasterio
from rasterio.shutil import copy, copyfiles
source = SOURCE_CWD / "proxy.vrt"
attempts = [
    lambda: rasterio.open(source).read(1),
    lambda: copy(src=source, dst=RUN_DIR / "copy.tif", driver="GTiff"),
    lambda: copyfiles(src=source, dst=RUN_DIR / "copy.vrt"),
]
for attempt in attempts:
    try:
        attempt()
    except PermissionError:
        continue
    raise AssertionError("VRT source referenced a sensitive file")
print("VRT sensitive reference blocked")
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "VRT sensitive reference blocked"
    assert sensitive.read_bytes() == original
    assert not result["files"]


async def test_vrt_valid_source_reads_and_update_requires_all_file_write_access(tmp_path: Path):
    import rasterio.shutil

    source = tmp_path / "source.tif"
    write_source_raster(source)
    rasterio.shutil.copy(source, tmp_path / "proxy.vrt", driver="VRT")
    original = source.read_bytes()
    result = await run_script(
        tmp_path,
        """
import rasterio
from rasterio.shutil import copy
with rasterio.open(SOURCE_CWD / "proxy.vrt") as dataset:
    assert dataset.read(1).sum() == 4
copy(src=SOURCE_CWD / "source.tif", dst=RUN_DIR / "local.vrt", driver="VRT")
with rasterio.open(RUN_DIR / "local.vrt") as dataset:
    assert dataset.read(1).sum() == 4
try:
    rasterio.open(RUN_DIR / "local.vrt", mode="r+")
except PermissionError:
    pass
else:
    raise AssertionError("Update mode accepted external associated source files")
print("valid VRT read; external update blocked")
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert source.read_bytes() == original


async def test_vector_vrt_sensitive_and_nested_sources_are_checked(tmp_path: Path):
    import xml.etree.ElementTree as ET

    frame = gpd.GeoDataFrame({"value": [123]}, geometry=[Point(0, 0)], crs="EPSG:3857")
    frame.to_file(tmp_path / "source.geojson", driver="GeoJSON")
    frame.to_file(tmp_path / ".env.geojson", driver="GeoJSON")

    def write_vrt(path: Path, reference: str):
        root = ET.Element("OGRVRTDataSource")
        layer = ET.SubElement(root, "OGRVRTLayer", name="layer")
        ET.SubElement(layer, "SrcDataSource", relativeToVRT="1").text = reference
        ET.SubElement(layer, "SrcLayer").text = "source"
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

    write_vrt(tmp_path / "sensitive.vrt", ".env.geojson")
    write_vrt(tmp_path / "nested.vrt", "sensitive.vrt")
    write_vrt(tmp_path / "valid.vrt", "source.geojson")
    result = await run_script(
        tmp_path,
        """
import geopandas as gpd
import pyogrio
import pyogrio.raw
for filename in ("sensitive.vrt", "nested.vrt"):
    for reader in (gpd.read_file, pyogrio.read_dataframe, pyogrio.raw.read, pyogrio.open_arrow):
        try:
            reader(SOURCE_CWD / filename)
        except PermissionError:
            continue
        raise AssertionError("OGR VRT leaked an indirect sensitive source")
valid = gpd.read_file(SOURCE_CWD / "valid.vrt")
assert valid["value"].tolist() == [123]
print("valid vector VRT read; sensitive references blocked")
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "valid vector VRT read; sensitive references blocked"


async def test_raster_local_copy_supports_handles_keywords_and_sidecars(tmp_path: Path):
    write_source_raster(tmp_path / "source.tif")
    write_source_raster(tmp_path / "multi.dat", driver="ENVI")
    assert (tmp_path / "multi.hdr").exists()
    result = await run_script(
        tmp_path,
        """
import rasterio
from rasterio.shutil import copy, copyfiles, delete
from rasterio.transform import from_origin
import numpy as np
source = SOURCE_CWD / "source.tif"
copy(src=source, dst=RUN_DIR / "path-copy.tif", driver="GTiff")
with rasterio.open(fp=source) as dataset:
    copy(src=dataset, dst=RUN_DIR / "handle-copy.tif", driver="GTiff")
copyfiles(src=SOURCE_CWD / "multi.dat", dst=RUN_DIR / "multi-copy.dat")
with rasterio.open(fp=RUN_DIR / "keyword.tif", mode="w", driver="GTiff",
                   width=2, height=2, count=1, dtype="uint8", crs="EPSG:3857",
                   transform=from_origin(0, 2, 1, 1)) as dst:
    dst.write(np.ones((1, 2, 2), dtype="uint8"))
with rasterio.open("handle-copy.tif") as src:
    assert src.read(1).sum() == 4
delete(path=RUN_DIR / "path-copy.tif", driver="GTiff")
assert not (RUN_DIR / "path-copy.tif").exists()
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    paths = {Path(value["path"]).name for value in result["files"]}
    assert {"handle-copy.tif", "multi-copy.dat", "multi-copy.hdr", "keyword.tif"} <= paths
    assert (tmp_path / "source.tif").exists()
    assert (tmp_path / "multi.dat").exists()
    assert (tmp_path / "multi.hdr").exists()


async def test_pyogrio_public_and_raw_apis_enforce_paths(tmp_path: Path):
    secret = tmp_path / ".env.geojson"
    secret.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{"secret":"synthetic-secret"},'
        '"geometry":{"type":"Point","coordinates":[0,0]}}]}',
        encoding="utf-8",
    )
    result = await run_script(
        tmp_path,
        """
import geopandas as gpd
import pyogrio
import pyogrio.core
import pyogrio.raw
from shapely.geometry import Point
secret = SOURCE_CWD / ".env.geojson"
outside = SOURCE_CWD / "outside.geojson"
frame = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs="EPSG:3857")
attempts = []
for module in (pyogrio, pyogrio.core, pyogrio.raw, pyogrio.geopandas):
    for name in ("read", "read_dataframe", "read_info", "read_bounds", "list_layers",
                 "open_arrow", "read_arrow"):
        if hasattr(module, name):
            attempts.append(lambda module=module, name=name: getattr(module, name)(secret))
    if hasattr(module, "write"):
        attempts.append(lambda module=module: module.write(path=outside, geometry=[],
                                                            field_data=[], fields=[]))
    if hasattr(module, "write_arrow"):
        attempts.append(lambda module=module: module.write_arrow(None, path=outside))
    if hasattr(module, "write_dataframe"):
        attempts.append(lambda module=module: module.write_dataframe(df=frame, path=outside))
    for name in ("vsi_unlink", "vsi_rmtree", "vsi_listtree"):
        if hasattr(module, name):
            attempts.append(lambda module=module, name=name: getattr(module, name)(path=secret))
for attempt in attempts:
    try:
        attempt()
    except PermissionError:
        continue
    raise AssertionError("unguarded public pyogrio entry point")
pyogrio.write_dataframe(df=frame, path=RUN_DIR / "keyword.geojson", driver="GeoJSON")
assert len(pyogrio.raw.read(RUN_DIR / "keyword.geojson")[0]["fields"]) == 0
assert len(pyogrio.list_layers(RUN_DIR / "keyword.geojson")) == 1
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert "synthetic-secret" not in result["stdout"]
    assert secret.is_file() and not (tmp_path / "outside.geojson").exists()
    assert "keyword.geojson" in {Path(value["path"]).name for value in result["files"]}


async def test_optional_fiona_public_paths_with_api_test_double(tmp_path: Path):
    # Fiona is an optional backend and is absent in this test environment. Verify
    # wrapper binding/validation using its documented signatures without claiming
    # a native Fiona integration test.
    result = await run_script(
        tmp_path,
        """
import sys, types, __main__
calls = []
fiona = types.ModuleType("fiona")
def open(fp, mode="r", **kwargs):
    calls.append(("open", str(fp), mode))
def listlayers(fp, vfs=None, **kwargs):
    calls.append(("listlayers", str(fp)))
    return ["layer"]
def listdir(fp):
    calls.append(("listdir", str(fp)))
    return []
def remove(path_or_collection, driver=None, layer=None, opener=None):
    calls.append(("remove", str(getattr(path_or_collection, "path", path_or_collection))))
fiona.open, fiona.listlayers, fiona.listdir, fiona.remove = open, listlayers, listdir, remove
sys.modules["fiona"] = fiona
__main__._install_guards({"run_dir": str(RUN_DIR), "read_roots": [str(SOURCE_CWD)]})
attempts = [
    lambda: fiona.open(fp=SOURCE_CWD / "outside.geojson", mode="w"),
    lambda: fiona.remove(path_or_collection=SOURCE_CWD / "source.geojson"),
    lambda: fiona.remove(types.SimpleNamespace(path=SOURCE_CWD / "source.geojson"), layer="x"),
    lambda: fiona.listlayers(fp=SOURCE_CWD / ".env.geojson"),
    lambda: fiona.listdir(fp=SOURCE_CWD / ".ssh"),
]
for attempt in attempts:
    try:
        attempt()
    except PermissionError:
        continue
    raise AssertionError("unguarded public Fiona entry point")
assert not calls
fiona.open(fp=RUN_DIR / "out.geojson", mode="w")
fiona.remove(path_or_collection=RUN_DIR / "out.geojson", layer="x")
assert fiona.listlayers(fp=RUN_DIR / "out.geojson") == ["layer"]
assert len(calls) == 3
""",
    )
    assert result["exit_code"] == 0, result["stderr"]


async def test_environment_excludes_keys_and_logs_are_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-key")
    monkeypatch.setenv("DATABASE_URL", "private-db-url")
    monkeypatch.setenv("CUSTOM_SECRET", "private-custom")
    result = await run_script(
        tmp_path,
        """
import os, sys
assert not any(key in os.environ for key in ("OPENAI_API_KEY", "DATABASE_URL", "CUSTOM_SECRET"))
print("中" * 10000)
print("error" * 10000, file=sys.stderr)
""",
        max_output_chars=120,
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"]) == len(result["stderr"]) == 120
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert "private" not in json.dumps(result)


async def test_script_path_environment_matches_injected_globals(tmp_path: Path, monkeypatch):
    # Generated scripts use both os.environ and the injected Path globals.
    # Explicit execution paths must win over any inherited parent values.
    monkeypatch.setenv("SOURCE_CWD", "parent-source")
    monkeypatch.setenv("RUN_DIR", "parent-run")
    monkeypatch.setenv("DATA_DIRS", "parent-data")
    data = tmp_path / "数据"
    data.mkdir()
    (data / "source.txt").write_text("source", encoding="utf-8")
    result = await run_script(
        tmp_path,
        """
import os
from pathlib import Path
source = Path(os.environ.get("SOURCE_CWD"))
output = Path(os.environ.get("RUN_DIR"))
assert source == SOURCE_CWD
assert output == RUN_DIR == Path.cwd()
assert [Path(value) for value in os.environ["DATA_DIRS"].split(os.pathsep)] == DATA_DIRS
assert (source / "数据" / "source.txt").read_text(encoding="utf-8") == "source"
(output / "result.txt").write_text("done", encoding="utf-8")
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert [Path(value["path"]).name for value in result["files"]] == ["result.txt"]
    assert (Path(result["run_dir"]) / "result.txt").read_text(encoding="utf-8") == "done"
    assert (data / "source.txt").read_text(encoding="utf-8") == "source"


async def test_shutil_native_copy_keeps_sources_and_provenance_protected(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("original", encoding="utf-8")
    provenance = tmp_path / "inputs" / "analysis.py"
    provenance.parent.mkdir()
    provenance.write_text("replacement", encoding="utf-8")
    sensitive = tmp_path / "sensitive"
    sensitive.mkdir()
    (sensitive / ".env").write_text("synthetic-secret", encoding="utf-8")
    result = await run_script(
        tmp_path,
        """
import shutil
source = SOURCE_CWD / "source.txt"
attempts = [
    lambda: shutil.copy2(src=source, dst=SOURCE_CWD / "outside.txt"),
    lambda: shutil.copy2(SOURCE_CWD / "sensitive" / ".env", RUN_DIR / "leak.txt"),
    lambda: shutil.copy2(SOURCE_CWD / "inputs" / "analysis.py", RUN_DIR),
    lambda: shutil.copytree(SOURCE_CWD / "sensitive", RUN_DIR / "tree"),
]
for attempt in attempts:
    try:
        attempt()
    except (PermissionError, shutil.Error):
        continue
    raise AssertionError("File copy bypassed analysis path protection")
shutil.copy2(src=source, dst=RUN_DIR / "local.txt")
local = RUN_DIR / "local"
local.mkdir()
shutil.copy2(source, local)
shutil.copytree(SOURCE_CWD / "inputs", RUN_DIR / "copied-inputs")
assert (RUN_DIR / "local.txt").read_text(encoding="utf-8") == "original"
assert (local / "source.txt").read_text(encoding="utf-8") == "original"
assert (RUN_DIR / "copied-inputs" / "analysis.py").read_text() == "replacement"
""",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert source.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "outside.txt").exists()
    assert not (Path(result["run_dir"]) / "leak.txt").exists()
    assert not (Path(result["run_dir"]) / "tree" / ".env").exists()
    assert Path(result["script_path"]).read_text(encoding="utf-8").startswith("\nimport shutil")


async def test_exception_is_recorded(tmp_path: Path):
    result = await run_script(tmp_path, "print('before')\nraise ValueError('analysis failed')")
    assert result["exit_code"] != 0
    assert "before" in result["stdout"]
    assert "analysis failed" in result["stderr"]
    assert not result["timed_out"]


async def test_timeout_terminates_and_waits_for_child(tmp_path: Path, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    children = []

    async def observe_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    result = await run_script(tmp_path, "import time; time.sleep(60)", timeout_seconds=0.2)
    assert result["timed_out"]
    assert result["exit_code"] != 0
    assert len(children) == 1 and children[0].returncode is not None
    assert await asyncio.wait_for(children[0].wait(), 1) == result["exit_code"]


async def test_tool_enforces_config_and_timeout_limit(tmp_path: Path):
    tool = GeoPythonTool()
    context = ToolExecutionContext(cwd=tmp_path, metadata={}, hook_executor=None)
    assert (await tool.execute(GeoPythonInput(code="print(1)"), context)).is_error
    context.metadata["geoharness_config"] = SimpleNamespace(
        execution=SimpleNamespace(
            enabled=True, outputs_dir="runs", timeout_seconds=30, max_output_chars=12000
        ),
        data=SimpleNamespace(file_dir=""),
    )
    denied = await tool.execute(GeoPythonInput(code="print(1)", timeout_seconds=31), context)
    assert denied.is_error and "maximum" in denied.output
    result = await tool.execute(GeoPythonInput(code="print('tool success')"), context)
    assert not result.is_error, result.output
    assert "tool success" in result.metadata["stdout"]
    assert result.metadata["manifest_path"]


def test_python_schema_exposes_configured_numeric_timeout_maximum():
    tool = GeoPythonTool(timeout_limit=45)
    schema = tool.to_api_schema()["input_schema"]
    timeout = schema["properties"]["timeout_seconds"]
    assert timeout["anyOf"][0]["maximum"] == 45
    assert timeout["default"] is None
    assert "45" in timeout["description"]
    assert "timeout_seconds" not in schema["required"]


async def test_timeout_rejection_reports_requested_and_allowed_without_execution(
    tmp_path, monkeypatch,
):
    async def forbidden(**kwargs):
        pytest.fail("timeout must be rejected before creating any run")

    monkeypatch.setattr("geoharness.tools.geo_python.execute_python", forbidden)
    config = GeoConfig(execution=ExecutionConfig(timeout_seconds=120))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoPythonTool(timeout_limit=120).execute(
        GeoPythonInput(code="print(1)", timeout_seconds=280), context,
    )
    assert result.is_error
    assert "requested=280s" in result.output
    assert "allowed=120s" in result.output
    assert result.metadata["requested_timeout_seconds"] == 280
    assert result.metadata["allowed_timeout_seconds"] == 120
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("code", "is_error"),
    [
        ("import warnings; warnings.warn('analysis warning')", False),
        ("raise ValueError('analysis failed')", True),
    ],
)
async def test_tool_error_status_uses_child_exit_not_stderr(
    tmp_path: Path, code: str, is_error: bool
):
    config = SimpleNamespace(
        execution=SimpleNamespace(
            enabled=True, outputs_dir="runs", timeout_seconds=30, max_output_chars=12000
        ),
        data=SimpleNamespace(file_dir=""),
    )
    context = ToolExecutionContext(
        cwd=tmp_path, metadata={"geoharness_config": config}, hook_executor=None
    )
    result = await GeoPythonTool().execute(GeoPythonInput(code=code), context)
    assert result.is_error is is_error
    assert result.metadata["stderr"]
    assert (result.metadata["exit_code"] != 0) is is_error
    assert json.loads(result.output) == result.metadata


@pytest.mark.parametrize("map_id", ["", "fire-map"])
async def test_legacy_budget_does_not_stop_python_maps_or_analysis(
    tmp_path: Path, monkeypatch, map_id: str
):
    executed = []

    async def capture_execution(**kwargs):
        executed.append(kwargs)
        return {"exit_code": 0, "timed_out": False, "files": []}

    monkeypatch.setattr("geoharness.tools.geo_python.execute_python", capture_execution)
    config = GeoConfig(
        execution=ExecutionConfig(outputs_dir="runs"),
        map_review=MapReviewConfig(max_repairs=0),
    )
    context = ToolExecutionContext(
        cwd=tmp_path, metadata={"geoharness_config": config}, hook_executor=None
    )
    tool = GeoPythonTool()
    names = ("different_first", "different_second") if map_id else ("fire_v1", "fire_final")
    first = await tool.execute(
        GeoPythonInput(code="plt.savefig('map.png')", name=names[0], map_id=map_id), context
    )
    assert not first.is_error
    stopped = await tool.execute(
        GeoPythonInput(code="plt.savefig('map.png')", name=names[1], map_id=map_id), context
    )
    assert not stopped.is_error
    assert not stopped.metadata.get("render_budget_exhausted", False)
    assert len(executed) == 2
    analysis = await tool.execute(
        GeoPythonInput(code="print('plt.savefig is only text')", name=names[1], map_id=map_id),
        context,
    )
    assert not analysis.is_error
    assert len(executed) == 3


async def test_tool_can_read_analysis_maps_and_prior_runs_without_granting_source_writes(
    tmp_path: Path,
):
    from PIL import Image

    analysis = tmp_path / ".geoharness" / "analysis"
    cartography = tmp_path / ".geoharness" / "maps"
    execution = tmp_path / ".geoharness" / "runs"
    previous = execution / "previous"
    for directory in (analysis, cartography, previous):
        directory.mkdir(parents=True)
    (analysis / "manifest.json").write_text('{"count": 2}', encoding="utf-8")
    (analysis / "results.json").write_text('{"affected": 1}', encoding="utf-8")
    Image.new("RGB", (2, 2), "red").save(cartography / "map.png")
    Image.new("RGB", (3, 3), "blue").save(previous / "prior.png")
    (analysis / ".env").write_text("synthetic-secret", encoding="utf-8")
    (analysis / "secret.json").write_text("synthetic-secret", encoding="utf-8")
    (analysis / "model.key").write_text("synthetic-secret", encoding="utf-8")
    (tmp_path / ".geoharness" / "config.yaml").write_text("synthetic-secret", encoding="utf-8")
    tool = GeoPythonTool()
    config = SimpleNamespace(
        execution=SimpleNamespace(
            enabled=True, outputs_dir=".geoharness/runs", timeout_seconds=30, max_output_chars=12000
        ),
        analysis=SimpleNamespace(outputs_dir=".geoharness/analysis"),
        cartography=SimpleNamespace(outputs_dir=".geoharness/maps"),
        data=SimpleNamespace(file_dir=""),
    )
    context = ToolExecutionContext(
        cwd=tmp_path, metadata={"geoharness_config": config}, hook_executor=None
    )
    result = await tool.execute(
        GeoPythonInput(
            code="""
import json
from PIL import Image
analysis = SOURCE_CWD / ".geoharness" / "analysis"
manifest = analysis / "manifest.json"
assert json.loads(manifest.read_text())["count"] == 2
assert json.loads((analysis / "results.json").read_text())["affected"] == 1
with Image.open(SOURCE_CWD / ".geoharness" / "maps" / "map.png") as image:
    assert image.size == (2, 2)
with Image.open(SOURCE_CWD / ".geoharness" / "runs" / "previous" / "prior.png") as image:
    assert image.size == (3, 3)
for path in (analysis / ".env", analysis / "secret.json", analysis / "model.key",
             SOURCE_CWD / ".geoharness" / "config.yaml"):
    try:
        path.read_text()
    except PermissionError:
        continue
    raise AssertionError("Artifact grant exposed credentials")
for path in (manifest, SOURCE_CWD / ".geoharness" / "runs" / "previous" / "prior.png"):
    try:
        path.write_text("changed")
    except PermissionError:
        continue
    raise AssertionError("Read-only artifact grant allowed writes")
print("artifacts readable; credentials and source writes blocked")
"""
        ),
        context,
    )
    assert not result.is_error, result.output
    assert json.loads((analysis / "manifest.json").read_text(encoding="utf-8")) == {"count": 2}
    assert Image.open(previous / "prior.png").size == (3, 3)
    assert str(analysis.resolve()) in result.metadata["read_roots"]
    assert str(cartography.resolve()) in result.metadata["read_roots"]
    assert str(execution.resolve()) in result.metadata["read_roots"]


async def test_explicit_read_root_cannot_grant_geoharness_credentials(tmp_path: Path):
    config_root = tmp_path / ".geoharness"
    config_root.mkdir()
    (config_root / "config.yaml").write_text("synthetic-secret", encoding="utf-8")
    result = await run_script(
        tmp_path,
        """
print((SOURCE_CWD / ".geoharness" / "config.yaml").read_text())
""",
        read_dirs=(Path(".geoharness"),),
    )
    assert result["exit_code"] != 0
    assert "PermissionError" in result["stderr"]
    assert "synthetic-secret" not in result["stdout"]


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "two words"])
def test_names_cannot_escape_run_directory(name: str):
    with pytest.raises(ValidationError):
        GeoPythonInput(code="print(1)", name=name)


def test_empty_tool_arguments_explain_no_script_was_executed():
    with pytest.raises(ValidationError, match="no script was executed"):
        GeoPythonInput.model_validate({})


def test_whitespace_script_is_not_complete_code():
    with pytest.raises(ValidationError, match="no script was executed"):
        GeoPythonInput.model_validate({"code": " \n "})


@pytest.mark.asyncio
async def test_failed_python_render_does_not_block_the_first_successful_map(tmp_path, monkeypatch):
    from geoharness.config.settings import GeoConfig, MapReviewConfig

    replies = iter([
        {"exit_code": 1, "timed_out": False, "stderr": "RuntimeError: setup failed"},
        {"exit_code": 0, "timed_out": False, "stderr": ""},
    ])

    async def execute(**kwargs):
        return next(replies)

    monkeypatch.setattr("geoharness.tools.geo_python.execute_python", execute)
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(map_review=MapReviewConfig(max_repairs=0)),
    })
    tool = GeoPythonTool()
    args = GeoPythonInput(code="plt.savefig('map.png')", name="map", map_id="risk")
    failure = await tool.execute(args, ctx)
    assert failure.is_error
    assert ctx.metadata["map_workflow_state"]["maps"]["risk"]["renders"] == 0
    success = await tool.execute(args, ctx)
    assert not success.is_error
    assert success.metadata["status"] == "succeeded"
    assert ctx.metadata["map_workflow_state"]["maps"]["risk"]["renders"] == 1
