"""Review an existing saved-layout preview once; preserve its old receipt and files.

Usage: python diagnostics/verify_test02_review.py QGIS_JOB_DIR
Uses the configured image provider. Writes the response to a new temporary directory.
"""

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from geoharness.cartography.quality import review_map
from geoharness.config.settings import load_geo_config


async def verify(job: Path) -> None:
    manifest_path = job / "project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = job / manifest["preview"]
    config = load_geo_config()
    workspace = job.parents[2]
    receipt = (workspace / config.cartography.jobs_dir / manifest.get("map_id", "")
               / "receipt.json").resolve()
    receipt_hash = (hashlib.sha256(receipt.read_bytes()).hexdigest()
                    if receipt.is_relative_to(workspace) and receipt.is_file() else None)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    assert digest == manifest["preview_sha256"] and manifest["backend"] == "qgis"
    result = await review_map(image, config=config,
        task="检查既有专题图的标签/符号遮挡、范围空白和底栏；只给可执行呈现建议。",
        evidence={"backend": "qgis", "remaining_render_versions": 2,
                  "render_metadata": {key: manifest[key] for key in ("layers", "layout")},
                  "backend_proof": {"manifest_path": str(manifest_path), "image_sha256": digest}})
    assert hashlib.sha256(image.read_bytes()).hexdigest() == digest
    if receipt_hash:
        assert hashlib.sha256(receipt.read_bytes()).hexdigest() == receipt_hash
    target = Path(tempfile.mkdtemp(prefix="geoharness-test02-review-")) / "verification.json"
    target.write_text(json.dumps({"image_sha256": digest,
        "image_unchanged": True, "old_receipt_sha256": receipt_hash,
        "old_receipt_unchanged": True if receipt_hash else None,
        "review": result.model_dump()}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), "status": result.status,
        "backend": result.diagnostics.get("backend"),
        "qgis_patch_returned": result.qgis_patch is not None,
        "failure_stage": result.diagnostics.get("failure_stage")}, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    asyncio.run(verify(Path(sys.argv[1]).resolve()))
