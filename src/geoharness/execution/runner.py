"""Python analysis execution and its child-process guard.

These guards constrain ordinary analysis scripts, not hostile native code. They
are intentionally not described as an OS sandbox: extensions can perform I/O
without emitting Python audit events. Native GIS file entry points are checked
as well, but operating-system isolation is required for untrusted code.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import inspect
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4


def _contracts():
    # The isolated child executes this file directly, without the source package
    # on sys.path. Load only its adjacent trusted implementation before guards.
    import importlib.util

    name = "_geoharness_execution_contracts"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("contracts.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _artifact_contracts():
    import importlib.util

    name = '_geoharness_artifact_contracts'
    if name not in sys.modules:
        path = Path(__file__).parent.parent / 'data' / 'artifacts.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _raster_helpers():
    import importlib.util

    name = "_geoharness_raster_helpers"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("raster.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _child_environment(run_dir: Path) -> dict[str, str]:
    """Pass only runtime essentials; never copy the parent's credential environment."""
    result = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC")
        if key in os.environ
    }
    cache = run_dir / ".cache"
    cache.mkdir()
    result.update(
        {
            "PATH": os.pathsep.join((str(Path(sys.executable).parent), str(Path(sys.base_prefix)))),
            "HOME": str(cache),
            "USERPROFILE": str(cache),
            "TMP": str(cache),
            "TEMP": str(cache),
            "TMPDIR": str(cache),
            "MPLCONFIGDIR": str(cache / "matplotlib"),
            "XDG_CACHE_HOME": str(cache),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PROJ_NETWORK": "OFF",
            "MPLBACKEND": "Agg",
        }
    )
    return result


async def _capture(stream: asyncio.StreamReader, limit: int) -> tuple[str, bool]:
    """Drain the pipe fully while retaining bounded output, including Unicode."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    chunks: list[str] = []
    length = 0
    truncated = False
    while data := await stream.read(8192):
        value = decoder.decode(data)
        remaining = max(0, limit - length)
        if remaining:
            chunks.append(value[:remaining])
        length += min(remaining, len(value))
        truncated = truncated or len(value) > remaining
    value = decoder.decode(b"", final=True)
    if length < limit:
        chunks.append(value[: limit - length])
    truncated = truncated or len(value) > max(0, limit - length)
    return "".join(chunks), truncated


async def execute_python(
    *,
    code: str,
    name: str,
    cwd: Path,
    data_dir: Path | None,
    outputs_dir: Path,
    timeout_seconds: float,
    max_output_chars: int,
    read_dirs: tuple[Path, ...] = (),
    read_files: tuple[Path, ...] = (),
    data_profiles: dict[str, Any] | None = None,
    data_refs: dict[str, Any] | None = None,
    previous_outputs: tuple[Path, ...] = (),
    input_artifacts: dict[str, str] | None = None,
    input_requirements: dict[str, dict] | None = None,
    expected_outputs: tuple[str, ...] = (),
    managed_inputs: bool = False,
    approved_score_policy: dict | None = None,
) -> dict[str, Any]:
    """Save and execute a script, returning and persisting its reproducibility record."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("name must be an ASCII slug of 1 to 64 letters, numbers, '_' or '-'")
    if timeout_seconds <= 0 or max_output_chars <= 0:
        raise ValueError("Execution timeout and output limit must be positive")
    cwd = cwd.resolve()
    outputs_dir = outputs_dir if outputs_dir.is_absolute() else cwd / outputs_dir
    root = outputs_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / f"{name}-{stamp}-{uuid4().hex[:12]}"
    run_dir.mkdir()
    script = run_dir / "analysis.py"
    script.write_text(code, encoding="utf-8")
    script_sha256 = hashlib.sha256(script.read_bytes()).hexdigest()
    read_roots = [str(cwd)]
    if data_dir is not None:
        data_dir = data_dir if data_dir.is_absolute() else cwd / data_dir
        read_roots.append(str(data_dir.resolve()))
    for directory in read_dirs:
        directory = Path(directory).expanduser()
        directory = directory if directory.is_absolute() else cwd / directory
        resolved = str(directory.resolve())
        if resolved not in read_roots:
            read_roots.append(resolved)
    exact_files = []
    for file in read_files:
        file = Path(file).expanduser()
        file = Path(os.path.abspath(file if file.is_absolute() else cwd / file))
        if not file.is_file() or file.resolve() != file:
            raise ValueError("Exact read_files must be existing files without redirected links")
        exact_files.append(str(file))
    spec = run_dir / ".execution.json"
    spec.write_text(
        json.dumps(
            {"run_dir": str(run_dir), "read_roots": read_roots, "source_cwd": str(cwd),
             "read_files": exact_files, "data_profiles": data_profiles or {},
             "data_refs": data_refs or {},
             "input_artifacts": input_artifacts or {},
             "input_requirements": input_requirements or {}, "managed_inputs": managed_inputs,
             "approved_score_policy": approved_score_policy,
             "expected_outputs": list(expected_outputs),
             "previous_outputs": [str(path) for path in previous_outputs]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stdout, stderr, timed_out, exit_code = "", "", False, None
    stdout_truncated = stderr_truncated = False
    process: asyncio.subprocess.Process | None = None
    preflight = []
    input_versions = {alias: {'path': value,
                             'sha256': _artifact_contracts().sha256_file(Path(value)),
                             'signature': _reference_signature(Path(value))}
                      for alias, value in (input_artifacts or {}).items()}
    try:
        preflight = _contracts().preflight_code(code, set(input_artifacts or {}))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            str(spec),
            cwd=str(run_dir),
            env=_child_environment(run_dir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        out_task = asyncio.create_task(_capture(process.stdout, max_output_chars))
        err_task = asyncio.create_task(_capture(process.stderr, max_output_chars))
        try:
            await asyncio.wait_for(process.wait(), timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            await asyncio.gather(out_task, err_task)
            raise
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
            out_task, err_task
        )
        exit_code = process.returncode
    except (SyntaxError, ModuleNotFoundError, ValueError) as exc:
        stderr = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    except (OSError, RuntimeError) as exc:
        stderr = f"Could not start analysis interpreter: {exc}"

    (run_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    files = []
    integrity_file = run_dir / 'integrity.json'
    try:
        integrity = json.loads(integrity_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        integrity = {}
    reserved = {"analysis.py", "manifest.json", "stdout.txt", "stderr.txt", ".execution.json",
                "integrity.json"}
    for path in sorted(run_dir.rglob("*")):
        relative = path.relative_to(run_dir)
        if not path.is_file() or relative.parts[0] == ".cache" or str(relative) in reserved:
            continue
        # Never follow a generated link into a source directory when enumerating artifacts.
        if not path.resolve().is_relative_to(run_dir.resolve()):
            continue
        files.append(_contracts().artifact_metadata(
            path, succeeded=exit_code == 0 and not timed_out))
        binding = integrity.get('vector_writes', {}).get(str(path))
        if isinstance(binding, dict):
            files[-1]['entity_binding'] = binding.get('entity_binding', 'unverified')
            files[-1]['identity_checks'] = binding.get('identity_checks', [])
    invalid = [entry for entry in files if entry.get("validation_state") == "failed"]
    if invalid and exit_code == 0:
        exit_code = 1
        stderr += "\nOutputContractError: " + "; ".join(
            entry["error_summary"] for entry in invalid)
        (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    missing = [name for name in expected_outputs if not (run_dir / name).is_file()]
    if missing and exit_code == 0:
        exit_code = 1
        stderr += f"\nOutputContractError: declared outputs missing: {missing}"
        (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    for version in input_versions.values():
        path = Path(version['path'])
        if (_reference_signature(path) != version['signature'] or not path.is_file()
                or _artifact_contracts().sha256_file(path) != version['sha256']):
            exit_code = 1
            stderr += f"\nInputContractError: input changed during execution: {path}"
            (run_dir / 'stderr.txt').write_text(stderr, encoding='utf-8')
    status = "timed_out" if timed_out else "succeeded" if exit_code == 0 else "failed"
    if status != "succeeded":
        for entry in files:
            entry["state"] = "failed"
    result = {
        "status": status,
        "execution_state": status,
        "preflight_checks": preflight,
        "validation_scope": "output geometry/CRS and observed entity bindings; "
                            "scientific correctness remains unverified",
        "inputs": input_artifacts or {},
        "input_versions": input_versions,
        "managed_inputs": managed_inputs,
        "execution_policy": approved_score_policy,
        "expected_outputs": list(expected_outputs),
        "run_dir": str(run_dir),
        "script_path": str(script),
        "script_sha256": script_sha256,
        "integrity_path": str(integrity_file) if integrity_file.is_file() else None,
        "integrity_sha256": (_artifact_contracts().sha256_file(integrity_file)
                             if integrity_file.is_file() else None),
        "manifest_path": str(run_dir / "manifest.json"),
        "source_cwd": str(cwd),
        "read_roots": read_roots,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "files": files,
        "guard": "application-level guards; not an OS security sandbox",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _install_guards(spec: dict[str, Any], tracker: Any = None) -> None:
    """Install Python audit checks and native GIS file checks in the child."""
    run_dir = Path(spec["run_dir"]).resolve()
    data_roots = [Path(value).resolve() for value in spec["read_roots"]]
    # Parent freezes canonical file identities. Resolving again here could grant a
    # different target if a file/parent link changes between discovery and startup.
    read_files = {Path(value).absolute() for value in spec.get("read_files", [])}
    runtime_roots = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    # Matplotlib reads font files lazily, after imports and after the audit hook.
    if system_root := os.environ.get("SYSTEMROOT", os.environ.get("SystemRoot")):
        runtime_roots.add((Path(system_root) / "Fonts").resolve())
    else:
        runtime_roots.update(
            Path(value) for value in ("/usr/share/fonts", "/usr/local/share/fonts")
        )
    denied_parts = {".git", ".ssh", ".aws", ".codex", ".agents", ".geoharness", "secret", "secrets"}

    def checked_path(value: Any, *, writing: bool = False) -> Path | None:
        if isinstance(value, int):
            # Standard descriptors are usable; opening arbitrary raw descriptors is disallowed.
            if value in (0, 1, 2) and not writing:
                return None
            raise PermissionError("Raw file descriptors are unavailable to analysis scripts")
        if not isinstance(value, (str, bytes, os.PathLike)):
            raise PermissionError("A local filesystem path is required")
        text = os.fsdecode(value)
        if "://" in text or text.startswith(("/vsi", "\\\\")):
            raise PermissionError("Network, GDAL virtual and UNC paths are unavailable")
        path = Path(text).resolve()
        if path.is_relative_to(run_dir):
            if writing and path in {
                run_dir / "analysis.py",
                run_dir / ".execution.json",
                run_dir / "manifest.json",
                run_dir / "stdout.txt",
                run_dir / "stderr.txt",
            }:
                raise PermissionError("Analysis provenance files are read-only to scripts")
            return path
        if writing:
            raise PermissionError(f"Analysis may write only inside its run directory: {path}")
        allowed_roots = [
            root for root in (*data_roots, *runtime_roots) if path.is_relative_to(root)
        ]
        # A worktree may itself live below .codex or the test directory below
        # .geoharness; restrictions apply to children of an explicit read root.
        selected_root = (
            max(allowed_roots, key=lambda root: len(root.parts)) if allowed_roots else None
        )
        relative = path.relative_to(selected_root) if selected_root is not None else path
        lower_parts = {part.lower() for part in relative.parts}
        # Narrow artifact directories below .geoharness may be granted, but
        # granting the configuration root itself must not expose credentials.
        if selected_root is not None and selected_root.name.lower() in denied_parts:
            lower_parts.add(selected_root.name.lower())
        filename = path.name.lower()
        names = {filename, Path(text).name.lower()}
        sensitive_name = (
            any(name.startswith(".env") for name in names)
            or any(re.match(r"(?:secret|secrets|credentials)(?:[._-]|$)", name) for name in names)
            or any(name in {"id_rsa", "id_ed25519"} for name in names)
            or any(name.endswith((".pem", ".key", ".p12", ".pfx")) for name in names)
        )
        exact_file = (path in read_files and Path(os.path.abspath(text)) == path
                      and path.is_file())
        if sensitive_name or (lower_parts & denied_parts or not allowed_roots) and not exact_file:
            raise PermissionError(f"Read path is outside allowed data/runtime directories: {path}")
        if (spec.get('managed_inputs') and not exact_file and
                any(path.is_relative_to(root) for root in data_roots) and
                path.suffix.lower() in {'.csv', '.json', '.geojson', '.gpkg', '.shp', '.dbf',
                                       '.shx', '.prj', '.cpg', '.tif', '.tiff', '.vrt'}):
            declared = {Path(v).resolve() for v in spec.get('input_artifacts', {}).values()}
            sidecar = any(p.suffix.lower() == '.shp' and p.parent == path.parent
                          and p.stem == path.stem and path.suffix.lower() in {
                              '.dbf', '.shx', '.prj', '.cpg'} for p in declared)
            if path not in declared and not sidecar:
                raise PermissionError(f'未绑定的业务输入：{path}；请在input_refs声明依赖。')
        return path

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            mode, flags = args[1:3]
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            writing = bool((flags or 0) & write_flags) or bool(
                mode and any(character in mode for character in "wax+")
            )
            checked_path(args[0], writing=writing)
        elif event in {"os.listdir", "os.scandir"}:
            checked_path(args[0] if args[0] is not None else os.getcwd())
        elif event == "os.chdir":
            checked_path(args[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            dir_fd_index = {"os.remove": 1, "os.rmdir": 1, "os.utime": 3}.get(event, 2)
            if len(args) > dir_fd_index and args[dir_fd_index] not in (None, -1):
                raise PermissionError("Directory-relative filesystem operations are unavailable")
            checked_path(args[0], writing=True)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if event != "os.rename":
                raise PermissionError("Creating filesystem links is unavailable")
            if any(value not in (None, -1) for value in args[2:]):
                raise PermissionError("Directory-relative filesystem operations are unavailable")
            checked_path(args[0], writing=True)
            checked_path(args[1], writing=True)
        elif (
            event.startswith(("socket.", "subprocess.", "ctypes."))
            or event
            in {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.spawn"}
            or event == "os.startfile"
        ):
            raise PermissionError(
                f"Network, native dynamic loading and process launch denied: {event}"
            )

    # On Windows, copy2 can call CopyFile2 without an audited Python open.
    # copytree captures copy2 as its default argument, so guard that default too.
    import shutil

    original_copy2 = shutil.copy2
    original_copytree = shutil.copytree
    copytree_signature = inspect.signature(original_copytree)

    @wraps(original_copy2)
    def file_copy2(src: Any, dst: Any, *, follow_symlinks: bool = True) -> Any:
        checked_path(src)
        destination = os.fspath(dst)
        if os.path.isdir(dst):
            destination = os.path.join(destination, os.path.basename(src))
        checked_path(destination, writing=True)
        return original_copy2(src, dst, follow_symlinks=follow_symlinks)

    @wraps(original_copytree)
    def file_copytree(*args: Any, **kwargs: Any) -> Any:
        bound = copytree_signature.bind(*args, **kwargs)
        checked_path(bound.arguments["src"])
        checked_path(bound.arguments["dst"], writing=True)
        if bound.arguments.get("copy_function", original_copy2) is original_copy2:
            bound.arguments["copy_function"] = file_copy2
        return original_copytree(*bound.args, **bound.kwargs)

    shutil.copy2 = file_copy2
    shutil.copytree = file_copytree

    # GDAL performs native I/O without Python open audit events. Wrap the supported
    # public entry points in addition to the Python-level checks.
    import geopandas
    import pandas
    import rasterio
    import rasterio.shutil

    original_read = geopandas.read_file
    original_to_file = geopandas.GeoDataFrame.to_file

    def guarded_table_reader(original):
        @wraps(original)
        def read(filepath_or_buffer, *args, **kwargs):
            if isinstance(filepath_or_buffer, (str, os.PathLike)):
                filepath_or_buffer = checked_native_path(filepath_or_buffer)
                _artifact_contracts().assert_usable_artifact(filepath_or_buffer)
            # In-memory buffers remain usable for explicit diagnostic inspection.
            return original(filepath_or_buffer, *args, **kwargs)

        return read

    pandas.read_csv = guarded_table_reader(pandas.read_csv)
    pandas.read_table = guarded_table_reader(pandas.read_table)
    original_raster_open = rasterio.open

    def checked_native_path(
        value: Any, *, writing: bool = False, seen: set[Path] | None = None
    ) -> Path:
        """Check native paths and local GDAL VRT references before native I/O."""
        path = checked_path(value, writing=writing)
        if path is None:
            raise PermissionError("Native GIS APIs require a local filesystem path")
        seen = set() if seen is None else seen
        if path in seen or not path.is_file():
            return path
        seen.add(path)
        with path.open("rb") as source:
            prefix = source.read(4096)
        encoding = "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = prefix.decode(encoding, errors="ignore").lstrip()
        # Content detection also covers VRT documents hidden under other suffixes.
        if not text.startswith("<"):
            return path
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            return path  # The native driver will report its normal format error.
        if root.tag.rsplit("}", 1)[-1] not in {"VRTDataset", "OGRVRTDataSource"}:
            return path
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] not in {"SourceFilename", "SrcDataSource"}:
                continue
            reference = (element.text or "").strip()
            if not reference:
                continue
            # SourceFilename/SrcDataSource are GDAL identifiers, so reject native
            # datasource schemes as well as normal network/VSI syntax before
            # interpreting them as local paths. Windows drive prefixes remain valid.
            scheme = re.match(r"^([A-Za-z][A-Za-z0-9_+.-]*):", reference)
            if (
                "://" in reference
                or reference.startswith(("/vsi", "\\\\"))
                or (scheme and len(scheme.group(1)) != 1)
            ):
                raise PermissionError("VRT references must identify local filesystem paths")
            target = Path(reference)
            if not target.is_absolute() and element.get("relativeToVRT") == "1":
                target = path.parent / target
            checked_native_path(target, writing=writing, seen=seen)
        return path

    @wraps(original_read)
    def vector_read(filename: Any, *args: Any, **kwargs: Any) -> Any:
        if filename is None:
            raise ValueError("No vector source selected: read_file received None. "
                             "Use read_vector(name/ref) or resolve_data(name/ref) from DATA_REFS. "
                             "DATA_DIRS are read roots, not dataset file locations.")
        filename = checked_native_path(filename)
        _artifact_contracts().assert_usable_artifact(filename)
        frame = original_read(filename, *args, **kwargs)
        return (tracker.record(filename, frame) if tracker is not None
                and not Path(filename).resolve().is_relative_to(run_dir) else frame)

    @wraps(original_to_file)
    def vector_write(self: Any, filename: Any, *args: Any, **kwargs: Any) -> Any:
        filename = checked_native_path(filename, writing=True)
        detail = tracker.check(self) if tracker is not None else None
        # GeoJSON is exchanged as RFC 7946 longitude/latitude. Preserve the real
        # CRS in GeoPackage when metric storage is required.
        is_geojson = (str(filename).lower().endswith(".geojson")
                      or str(kwargs.get("driver", "")).lower() == "geojson")
        output = self.to_crs(4326) if is_geojson and self.crs is not None else self
        result = original_to_file(output, filename, *args, **kwargs)
        if tracker is not None:
            tracker.writes[str(filename)] = {**detail, "output_crs": str(output.crs)}
        return result

    @wraps(original_raster_open)
    def raster_open(fp: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        fp = checked_native_path(fp, writing=mode != "r")
        if mode == 'r':
            _artifact_contracts().assert_usable_artifact(fp)
        if kwargs.get("opener") is not None:
            raise PermissionError("Custom native filesystem openers are unavailable")
        dataset = original_raster_open(fp, mode, *args, **kwargs)
        try:
            # VRT and other compound rasters can refer to data beyond the path
            # passed by the caller. Validate associated files before returning a
            # handle that can read pixels or update an external source.
            check_raster_files(dataset, writing=mode != "r")
        except BaseException:
            dataset.close()
            raise
        return dataset

    def guarded_function(original: Any, parameter: str, *, writing: bool = False) -> Any:
        """Preserve public signatures and check paths for positional and keyword calls."""
        signature = inspect.signature(original)

        @wraps(original)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            bound.arguments[parameter] = checked_native_path(
                bound.arguments.get(parameter), writing=writing
            )
            if kwargs.get("opener") is not None or kwargs.get("vfs") is not None:
                raise PermissionError("Custom native filesystem openers are unavailable")
            return original(*bound.args, **bound.kwargs)

        return guarded

    def check_raster_files(source: Any, *, writing: bool = False) -> None:
        if isinstance(source, rasterio.io.DatasetReaderBase):
            dataset = source
            owns_dataset = False
        else:
            source = checked_native_path(source, writing=writing)
            dataset = original_raster_open(source)
            owns_dataset = True
        try:
            # Source handles may be in-memory datasets. Their /vsimem files do
            # not reach the host filesystem; actual dataset files still need checks.
            for filename in (dataset.name, *dataset.files):
                if isinstance(source, rasterio.io.DatasetReaderBase) and str(filename).startswith(
                    "/vsimem/"
                ):
                    continue
                checked_path(filename, writing=writing)
        finally:
            if owns_dataset:
                dataset.close()

    original_raster_copy = rasterio.shutil.copy
    original_raster_copyfiles = rasterio.shutil.copyfiles
    original_raster_delete = rasterio.shutil.delete

    @wraps(original_raster_copy)
    def raster_copy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        dst = checked_native_path(dst, writing=True)
        check_raster_files(src)
        if not isinstance(src, rasterio.io.DatasetReaderBase):
            src = checked_native_path(src)
        return original_raster_copy(src, dst, *args, **kwargs)

    @wraps(original_raster_copyfiles)
    def raster_copyfiles(src: Any, dst: Any) -> Any:
        dst = checked_native_path(dst, writing=True)
        check_raster_files(src)
        src = checked_native_path(src)
        return original_raster_copyfiles(src, dst)

    @wraps(original_raster_delete)
    def raster_delete(path: Any, *args: Any, **kwargs: Any) -> Any:
        path = checked_native_path(path, writing=True)
        check_raster_files(path, writing=True)
        return original_raster_delete(path, *args, **kwargs)

    geopandas.read_file = vector_read
    geopandas.GeoDataFrame.to_file = vector_write
    rasterio.open = raster_open
    rasterio.shutil.copy = raster_copy
    rasterio.shutil.copyfiles = raster_copyfiles
    rasterio.shutil.delete = raster_delete
    rasterio.shutil.exists = guarded_function(rasterio.shutil.exists, "path")
    try:
        import pyogrio
    except ImportError:
        pass
    else:
        import pyogrio.core
        import pyogrio.geopandas
        import pyogrio.raw

        # Public functions are re-exported and imported under module-local aliases.
        # Guard all of those aliases, including raw/Arrow and native VSI mutations.
        readers = (
            "read_dataframe",
            "read_info",
            "read_bounds",
            "list_layers",
            "read",
            "read_arrow",
            "open_arrow",
        )
        writers = ("write_dataframe", "write", "write_arrow")
        for module in (pyogrio, pyogrio.core, pyogrio.raw, pyogrio.geopandas):
            for function in readers:
                if original := getattr(module, function, None):
                    setattr(module, function, guarded_function(original, "path_or_buffer"))
            for function in writers:
                if original := getattr(module, function, None):
                    setattr(module, function, guarded_function(original, "path", writing=True))
            for function in ("vsi_unlink", "vsi_rmtree", "vsi_listtree"):
                if original := getattr(module, function, None):
                    setattr(
                        module,
                        function,
                        guarded_function(original, "path", writing=function != "vsi_listtree"),
                    )
    try:
        import fiona
    except ImportError:
        pass
    else:
        original_fiona_open = fiona.open

        @wraps(original_fiona_open)
        def fiona_open(fp: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            fp = checked_native_path(fp, writing=mode != "r")
            if kwargs.get("opener") is not None or kwargs.get("vfs") is not None:
                raise PermissionError("Custom native filesystem openers are unavailable")
            return original_fiona_open(fp, mode, *args, **kwargs)

        fiona.open = fiona_open
        for function in ("listlayers", "listdir"):
            if original := getattr(fiona, function, None):
                setattr(fiona, function, guarded_function(original, "fp"))
        if original_fiona_remove := getattr(fiona, "remove", None):

            @wraps(original_fiona_remove)
            def fiona_remove(path_or_collection: Any, *args: Any, **kwargs: Any) -> Any:
                path = getattr(path_or_collection, "path", path_or_collection)
                validated = checked_native_path(path, writing=True)
                if isinstance(path_or_collection, (str, bytes, os.PathLike)):
                    path_or_collection = validated
                if kwargs.get("opener") is not None:
                    raise PermissionError("Custom native filesystem openers are unavailable")
                return original_fiona_remove(path_or_collection, *args, **kwargs)

            fiona.remove = fiona_remove
    sys.addaudithook(audit)


def mask_raster(raster_path: Any, geometries: Any, **options: Any) -> Any:
    """Clip using CRS-bearing geometries, transforming them to the real raster CRS.

    This intentionally refuses bare Shapely lists, whose coordinate reference cannot
    be inferred. The source GeoSeries/DataFrame and raster remain unchanged.
    """
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.mask import mask
    from shapely.geometry import box

    unsupported = set(options) - set(inspect.signature(mask).parameters)
    if unsupported:
        raise TypeError(f"mask_raster unsupported options={sorted(unsupported)}; "
                        "rasterio.mask.mask uses nodata, not fill_value; "
                        f"installed signature={inspect.signature(mask)}")
    if not isinstance(geometries, (gpd.GeoSeries, gpd.GeoDataFrame)) or geometries.crs is None:
        raise ValueError("mask_raster requires a GeoSeries/GeoDataFrame with verified source CRS; "
                         "bare Shapely geometries have no CRS")
    with rasterio.open(raster_path) as dataset:
        if dataset.crs is None:
            raise ValueError("mask_raster: raster CRS is unknown; inspect the source first")
        shapes = geometries.geometry.to_crs(dataset.crs)
        shapes = shapes[~shapes.isna() & ~shapes.is_empty]
        if shapes.empty or not shapes.is_valid.all() or not np.isfinite(shapes.total_bounds).all():
            raise ValueError("mask_raster requires nonempty valid finite geometries")
        if not shapes.intersects(box(*dataset.bounds)).any():
            raise ValueError(f"mask_raster: geometries do not overlap the raster after CRS "
                             f"transformation to {dataset.crs}; geometry_bounds="
                             f"{shapes.total_bounds.tolist()}, "
                             f"raster_bounds={list(dataset.bounds)}")
        return mask(dataset, shapes, **options)


def _reference_signature(path: Path) -> dict[str, Any] | None:
    """Match the parent snapshot, including shapefile schema/CRS sidecars."""
    try:
        if path.absolute() != path.resolve() or not path.is_file():
            return None
        parts = [path]
        if path.suffix.lower() == ".shp":
            parts.extend(path.with_suffix(ext) for ext in (".shx", ".dbf", ".prj", ".cpg"))
        signature = {}
        for part in parts:
            try:
                stat = part.stat()
            except FileNotFoundError:
                signature[part.name] = None
                continue
            signature[part.name] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                    "ctime_ns": stat.st_ctime_ns}
        return signature
    except (OSError, ValueError):
        return None


def _data_helpers(references: dict[str, Any], tracker: Any = None) -> dict[str, Any]:
    """Resolve only frozen records; never enumerate folders or reconstruct job paths."""
    import geopandas as gpd

    # Scripts can inspect DATA_REFS without changing the helpers' frozen identities.
    records = json.loads(json.dumps(references))

    def record_for(ref: Any) -> dict:
        if not isinstance(ref, (str, os.PathLike)):
            raise ValueError("Use a DATA_REFS ID, unique registered name, or registered path")
        value = os.fspath(ref)
        if value in records:
            matches = [records[value]]
        else:
            # Exact registered paths remain usable even when short names collide.
            matches = [record for record in records.values() if isinstance(record, dict)
                       and os.path.normcase(value) == os.path.normcase(record.get("path", ""))]
            if not matches:
                matches = []
                for record in records.values():
                    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                        continue
                    path = Path(record["path"])
                    aliases = {record.get("name"), record.get("requested_name"),
                               record.get("output_label"), path.name, path.stem}
                    if value in aliases:
                        matches.append(record)
        if not matches:
            _artifact_contracts().lookup_reference(value, records)
        if len(matches) != 1:
            _artifact_contracts().lookup_reference(value, records)
        record = matches[0]
        path = Path(record["path"])
        if not isinstance(record.get("signature"), dict) or (
            _reference_signature(path) != record["signature"]
        ):
            raise ValueError("Data ref is stale or missing; inspect the source again")
        expected = record.get("sha256")
        if expected is not None:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if (not isinstance(expected, str) or digest.hexdigest() != expected
                    or _reference_signature(path) != record["signature"]):
                raise ValueError("Data ref fingerprint changed; inspect the source again")
        if record.get("manifest") and (
            not isinstance(record.get("manifest_signature"), dict)
            or _reference_signature(Path(record["manifest"])) != record["manifest_signature"]
        ):
            raise ValueError("Data producer manifest is stale or missing; refresh DATA_REFS")
        return record

    def resolve_data(ref: Any) -> Path:
        return Path(record_for(ref)["path"])

    def read_vector(ref: Any, target_crs: Any = None) -> Any:
        record = record_for(ref)
        if record.get("kind") != "vector":
            raise ValueError("read_vector requires a registered vector data ref")
        encoding = record.get("encoding")
        options = {"encoding": encoding} if encoding and encoding != "unknown" else {}
        frame = gpd.read_file(record["path"], **options)
        record_for(ref)  # A read must not attach old metadata to a changed file.
        if '__geoh_id' not in frame:
            # Python-only row identity. The original file and native-layer schema
            # stay untouched. A changed source snapshot produces new identities.
            version = json.dumps(record.get('signature'), sort_keys=True)
            frame['__geoh_id'] = [hashlib.sha256(
                f"{record['path']}|{version}|{index}".encode()).hexdigest()[:24]
                for index in range(len(frame))]
        if tracker is not None:
            tracker.record(record['path'], frame)
        if target_crs is not None:
            if frame.crs is None:
                raise ValueError("read_vector source CRS is unknown; "
                                 "inspect it before transforming")
            frame = frame.to_crs(target_crs)
        return frame

    def native_layer(ref: Any, derivation: dict | None = None, *, name: str | None = None,
                     style: dict | None = None, label_field: str | None = None,
                     label_style: dict | None = None, layer_name: str | None = None) -> dict:
        record = record_for(ref)
        if record.get("kind") != "vector":
            raise ValueError("native_layer requires a registered vector data ref")
        layer = {"file_path": record["path"], "kind": "vector"}
        for key, value in (("name", name), ("style", style), ("label_field", label_field),
                           ("label_style", label_style), ("layer_name", layer_name)):
            if value is not None:
                expected_type = dict if key in {"style", "label_style"} else str
                if not isinstance(value, expected_type):
                    raise ValueError(f"native_layer {key} must be {expected_type.__name__}")
                layer[key] = value
        if derivation is not None:
            if not isinstance(derivation, dict) or set(derivation) - {
                "feature_indices", "keep_fields", "annotations",
            }:
                raise ValueError("native_layer derivation supports selections and annotations only")
            layer["derivation"] = derivation
        # The native tool validates style and derivation against the original
        # source. Keeping its path lets adjacent producer evidence still bind.
        return json.loads(json.dumps(layer, allow_nan=False))

    return {"resolve_data": resolve_data, "read_vector": read_vector, "native_layer": native_layer}


def _child_main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    run_dir = Path(spec["run_dir"]).resolve()
    source_cwd = Path(spec["source_cwd"]).resolve()
    data_dirs = [Path(value) for value in spec["read_roots"]]
    # Support generated scripts that read paths through os.environ as well as
    # the injected Path globals. Export only explicit, non-credential run data.
    os.environ.update(
        {
            "RUN_DIR": str(run_dir),
            "SOURCE_CWD": str(source_cwd),
            "DATA_DIRS": os.pathsep.join(str(directory) for directory in data_dirs),
        }
    )
    script = run_dir / "analysis.py"
    code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
    # Warm up native libraries and font/cache initialization before constraining
    # user code; all temporary/cache directories already point inside this run.
    import geopandas  # noqa: F401
    import matplotlib
    import pandas  # noqa: F401
    import rasterio  # noqa: F401
    import shapely  # noqa: F401

    matplotlib.use("Agg")
    import matplotlib.pyplot  # noqa: F401

    contracts = _contracts()
    _artifact_contracts()
    raster_helpers = _raster_helpers()
    tracker = contracts.EntityTracker()
    _install_guards(spec, tracker)
    input_checks = {}
    for alias, requirement in spec.get('input_requirements', {}).items():
        if alias not in spec.get('input_artifacts', {}):
            raise ValueError(f'Unbound input requirement: {alias}')
        input_checks[alias] = contracts.validate_input(spec['input_artifacts'][alias], requirement)
    data_refs = spec.get("data_refs", {})

    def confirmed_score(frame, *, key, components=None, missing_policy=None, classification=None):
        policy = spec.get('approved_score_policy')
        if spec.get('managed_inputs'):
            if not policy:
                raise ValueError('评分需要用户已确认的policy_ref；先使用geo_task.propose_score。')
            if ((components is not None and components != policy['components']) or
                    (missing_policy is not None and missing_policy != policy['missing_policy']) or
                    (classification is not None
                     and classification != policy.get('classification'))):
                raise ValueError('执行评分参数与用户确认的政策不一致')
            components, missing_policy = policy['components'], policy['missing_policy']
            classification = policy.get('classification')
        return contracts.score_risk(frame, key=key, components=components,
                                    missing_policy=missing_policy or 'error',
                                    classification=classification)

    namespace = {
        "__name__": "__main__",
        "__file__": str(script),
        "RUN_DIR": run_dir,
        "SOURCE_CWD": source_cwd,
        "DATA_DIRS": data_dirs,
        "GEO_DATA": spec.get("data_profiles", {}),
        "DATA_REFS": data_refs,
        "PREVIOUS_OUTPUTS": [Path(value) for value in spec.get("previous_outputs", [])],
        "INPUTS": {key: Path(value) for key, value in spec.get("input_artifacts", {}).items()},
        "mask_raster": mask_raster,
        "read_raster": raster_helpers.read_raster,
        "write_raster": raster_helpers.write_raster,
        "raster_backdrop": raster_helpers.raster_backdrop,
        "entity_join": contracts.entity_join,
        "score_risk": confirmed_score,
        "sample_raster_points": contracts.sample_raster_points,
        "extent_geometry": contracts.extent_geometry,
        "artifact_facts": contracts.artifact_facts,
        "check_claims": contracts.check_claims,
        "first_coverage": contracts.first_coverage,
        "make_leader_lines": contracts.make_leader_lines,
        "check_leader_lines": contracts.check_leader_lines,
        "fit_frame_extent": contracts.fit_frame_extent,
        "describe_input": contracts.describe_input,
        "write_analysis_report": contracts.write_analysis_report,
        **_data_helpers(data_refs, tracker),
    }
    exec(code, namespace)
    # Raw JSON/native writes also need a read-back check. Keep failed partial
    # files for diagnosis, but the parent never registers them as usable output.
    import geopandas as gpd

    for output in run_dir.rglob("*"):
        if output.suffix.lower() in {".geojson", ".gpkg", ".shp"} and output.is_file():
            frame = gpd.read_file(output)
            tracker.writes.setdefault(str(output), tracker.check(frame))
            if getattr(frame, 'crs', None) is not None:
                tracker.provenance.append({"role": "output_write", "path": str(output),
                                           "crs": frame.crs.to_string(),
                                           "feature_count": len(frame)})
    contracts.write_contract_report(run_dir / "integrity.json", tracker, input_checks=input_checks)


if __name__ == "__main__":
    _child_main()
