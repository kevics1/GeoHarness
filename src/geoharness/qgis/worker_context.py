"""Local input and per-job output checks shared by Processing and project workers.

These checks protect normal trusted QGIS workflows. They are not an OS sandbox;
native providers and user-installed QGIS code must be trusted.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_SECRET_PARTS = {".git", ".ssh", ".aws", ".codex", ".agents", ".geoharness", "secret", "secrets"}
_RESERVED = {"request.json", "result.json", "manifest.json", "stdout.txt", "stderr.txt"}


def _local_path(value: str) -> Path:
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9_+.-]*):", value)
    if (
        "://" in value
        or value.startswith(("/vsi", "\\\\", "//"))
        or (scheme and len(scheme.group(1)) != 1)
        or "|" in value
        or "\x00" in value
    ):
        raise PermissionError("Only local file paths are supported; URLs/provider URIs are denied")
    return Path(value).expanduser()


@dataclass
class WorkerContext:
    cwd: Path
    run_dir: Path
    read_roots: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cwd, self.run_dir = self.cwd.resolve(), self.run_dir.resolve()
        self.read_roots = [self.cwd, self.run_dir, *(Path(p).resolve() for p in self.read_roots)]

    def read_path(self, value: str, *, _seen: set[Path] | None = None) -> Path:
        path = _local_path(value)
        path = (path if path.is_absolute() else self.cwd / path).resolve()
        roots = [root for root in self.read_roots if path.is_relative_to(root)]
        if not roots:
            raise PermissionError(f"Input file is outside allowed read roots: {path}")
        root = max(roots, key=lambda item: len(item.parts))
        parts = {part.lower() for part in path.relative_to(root).parts}
        if root.name.lower() in _SECRET_PARTS and root != self.run_dir:
            parts.add(root.name.lower())
        name = path.name.lower()
        if (
            parts & _SECRET_PARTS
            or name.startswith(".env")
            or re.match(r"(?:secret|secrets|credentials)(?:[._-]|$)", name)
            or name in {"id_rsa", "id_ed25519"}
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
        ):
            raise PermissionError("Credential/configuration files are unavailable to QGIS jobs")
        if not path.is_file():
            raise ValueError(f"QGIS inputs must be existing files: {path}")
        # GDAL VRTs may reference remote or otherwise unapproved files invisibly.
        seen = set() if _seen is None else _seen
        if path not in seen:
            seen.add(path)
            with path.open("rb") as stream:
                prefix = stream.read(4096)
            encoding = "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            if prefix.decode(encoding, errors="ignore").lstrip().startswith("<"):
                try:
                    xml = ET.parse(path).getroot()
                except ET.ParseError:
                    xml = None
                if xml is not None and xml.tag.rsplit("}", 1)[-1] in {
                    "VRTDataset",
                    "OGRVRTDataSource",
                }:
                    for node in xml.iter():
                        if node.tag.rsplit("}", 1)[-1] in {"SourceFilename", "SrcDataSource"}:
                            value = (node.text or "").strip()
                            if value:
                                target = _local_path(value)
                                if not target.is_absolute() and node.get("relativeToVRT") == "1":
                                    target = path.parent / target
                                self.read_path(str(target), _seen=seen)
        return path

    def output_path(self, value: str | None = None, default_name: str = "output.gpkg") -> str:
        if value in (None, "", "TEMPORARY_OUTPUT"):
            value = default_name
        path = _local_path(value)
        path = (path if path.is_absolute() else self.run_dir / path).resolve()
        if not path.is_relative_to(self.run_dir):
            raise PermissionError("QGIS outputs must stay inside the current unique run directory")
        relative = path.relative_to(self.run_dir)
        if not relative.parts or relative.parts[0] == ".profile" or relative.name in _RESERVED:
            raise PermissionError("QGIS provenance/profile files cannot be used as outputs")
        if path.exists():
            raise PermissionError(f"Output already exists; choose a new output filename: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)
