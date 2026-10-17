"""QGIS page previews stay readable across tools without broad directory grants."""

from __future__ import annotations

import json

import pytest
from openharness.tools.base import ToolExecutionContext
from PIL import Image

from geoharness.config.settings import GeoConfig
from geoharness.qgis.artifacts import returned_preview_paths
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool, qgis_diagnostic_files


def _job(root, *, status="succeeded"):
    job = root / "project_create-test"
    job.mkdir(parents=True)
    for name in ("preview.png", "detail_page_2.png", "roster_page_3.png", "unreturned.png"):
        Image.new("RGB", (3, 2), (12, 34, 56)).save(job / name)
    (job / "project.json").write_text('{}', encoding="utf-8")
    payload = {"preview_path": str(job / "preview.png"), "project_manifest": "project.json",
               "native_facts": {"page_previews": [
                   {"page_index": 1, "path": "detail_page_2.png", "frames": ["detail_map"]}],
                   "roster_previews": ["roster_page_3.png"]}}
    (job / "result.json").write_text(json.dumps({"status": status, "result": payload}))
    (job / "manifest.json").write_text(json.dumps({"run_dir": str(job)}))
    return job, payload


async def test_all_returned_pages_are_readable_but_unreturned_paths_and_writes_are_denied(tmp_path):
    root = tmp_path / ".geoharness/qgis"
    job, _ = _job(root)
    code = f"""
from pathlib import Path
from PIL import Image
job = Path({str(job)!r})
for name in ['preview.png', 'detail_page_2.png', 'roster_page_3.png']:
    with Image.open(job / name) as image:
        assert image.size == (3, 2) and image.getpixel((0, 0)) == (12, 34, 56)
for operation in ['unreturned', 'write', 'list']:
    try:
        if operation == 'unreturned': (job / 'unreturned.png').read_bytes()
        elif operation == 'write': (job / 'detail_page_2.png').write_bytes(b'changed')
        else: list(job.iterdir())
    except PermissionError: pass
    else: raise AssertionError(operation)
print('three explicit pages read, directory/unreturned/write rejected')
"""
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoPythonTool().execute(GeoPythonInput(code=code), context)
    assert not result.is_error, result.output


def test_failed_jobs_do_not_grant_previews_and_malformed_lists_are_ignored(tmp_path):
    job, payload = _job(tmp_path / "qgis", status="failed")
    allowed = qgis_diagnostic_files(job.parent)
    assert job / "detail_page_2.png" not in allowed
    assert returned_preview_paths({"native_facts": {"page_previews": "bad"}}) == ()
    assert "detail_page_2.png" in returned_preview_paths(payload)


def test_returned_previews_cannot_escape_job_or_follow_links(tmp_path):
    job, payload = _job(tmp_path / "qgis")
    outside = tmp_path / "outside.png"
    Image.new("RGB", (2, 2)).save(outside)
    payload["native_facts"]["page_previews"].append({"path": str(outside)})
    linked = job / "redirect.png"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink unavailable: {exc}")
    payload["native_facts"]["page_previews"].append({"path": str(linked)})
    (job / "result.json").write_text(json.dumps({"status": "succeeded", "result": payload}))
    allowed = qgis_diagnostic_files(job.parent)
    assert job / "detail_page_2.png" in allowed
    assert outside not in allowed and linked not in allowed
