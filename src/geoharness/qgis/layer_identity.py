"""Dependency-free business layer identities shared with the standalone QGIS worker."""

from __future__ import annotations

import hashlib
import json
import os


def stable_layer_key(kind: str, label: str, file_path: str, layer_name: str | int = "") -> str:
    identity = {"kind": kind, "label": label, "file_path": os.path.normcase(file_path),
                "layer_name": layer_name}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "layer:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
