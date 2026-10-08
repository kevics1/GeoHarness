"""Find standalone/OSGeo4W QGIS installations and assemble a clean environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from geoharness.config.settings import QgisConfig


@dataclass(frozen=True)
class QgisRuntime:
    install_dir: Path
    python_executable: Path
    prefix_path: Path
    python_home: Path | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "install_dir": str(self.install_dir),
            "python_executable": str(self.python_executable),
            "prefix_path": str(self.prefix_path),
            "python_home": str(self.python_home) if self.python_home else None,
        }


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"\d+", path.name))


def _fixed_drive_roots() -> list[Path]:
    """Enumerate only local fixed volumes; avoid network/removable-drive discovery."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetLogicalDrives.argtypes, kernel.GetLogicalDrives.restype = [], wintypes.DWORD
    kernel.GetDriveTypeW.argtypes, kernel.GetDriveTypeW.restype = [wintypes.LPCWSTR], wintypes.UINT
    mask = kernel.GetLogicalDrives()
    return [
        Path(f"{chr(65 + index)}:/")
        for index in range(26)
        if mask & (1 << index) and kernel.GetDriveTypeW(f"{chr(65 + index)}:\\") == 3
    ]


def _windows_installations() -> list[Path]:
    """Consult installed-app records and conventional roots, without running launchers."""
    roots: list[Path] = []
    if value := os.environ.get("OSGEO4W_ROOT"):
        roots.append(Path(value))
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if value := os.environ.get(key):
            roots.extend(sorted(Path(value).glob("QGIS*"), key=_version_key, reverse=True))
    if drive := os.environ.get("SystemDrive"):
        roots.extend(Path(drive + "/") / name for name in ("OSGeo4W", "OSGeo4W64"))
    # Standalone installers/portable installs can live on a secondary fixed disk
    # without an uninstall record. Look only in conventional installation folders.
    program_folder_names = {
        Path(value).name
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
        if (value := os.environ.get(key))
    }
    for drive in _fixed_drive_roots():
        for folder in program_folder_names:
            roots.extend(sorted((drive / folder).glob("QGIS*"), key=_version_key, reverse=True))
        roots.extend(drive / name for name in ("OSGeo4W", "OSGeo4W64"))
    try:
        import winreg

        location = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, location, 0, winreg.KEY_READ | view) as key:
                        for index in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                                    name = winreg.QueryValueEx(entry, "DisplayName")[0]
                                    if "qgis" in name.lower() or "osgeo4w" in name.lower():
                                        root = winreg.QueryValueEx(entry, "InstallLocation")[0]
                                        if root:
                                            roots.append(Path(root))
                            except OSError:
                                continue
                except OSError:
                    continue
    except ImportError:
        pass
    return list(dict.fromkeys(roots))


def discover_qgis(settings: QgisConfig, *, cwd: Path) -> QgisRuntime:
    """Prefer explicit configuration; installation metadata supplies missing locations."""
    if not settings.enabled:
        raise RuntimeError("QGIS integration is disabled in configuration")

    def configured(value: str) -> Path | None:
        if not value:
            return None
        path = Path(value).expanduser()
        return (path if path.is_absolute() else cwd / path).resolve()

    explicit_root = configured(settings.install_dir)
    explicit_python = configured(settings.python_executable)
    explicit_prefix = configured(settings.prefix_path)
    if explicit_python and explicit_python.suffix.lower() in {".bat", ".cmd", ".ps1", ".sh"}:
        raise ValueError("qgis.python_executable must be a Python executable, not a shell launcher")
    roots = [explicit_root] if explicit_root else []
    if explicit_prefix:
        roots.append(
            explicit_prefix.parent.parent
            if explicit_prefix.parent.name.lower() == "apps"
            else explicit_prefix
        )
    if explicit_python:
        ancestors = [explicit_python.parent, *explicit_python.parents]
        roots.extend(root for root in ancestors if (root / "apps").is_dir())
    if not explicit_root:
        roots.extend(_windows_installations() if os.name == "nt" else [])
    for root in dict.fromkeys(roots):
        prefixes = (
            [explicit_prefix]
            if explicit_prefix
            else [root / "apps/qgis", root / "apps/qgis-ltr", root]
        )
        for prefix in prefixes:
            if not prefix or not (prefix / "python/qgis").is_dir():
                continue
            homes = sorted((root / "apps").glob("Python*"), key=_version_key, reverse=True)
            candidates = (
                [explicit_python]
                if explicit_python
                else [
                    root / "bin/python.exe",
                    *(home / "python.exe" for home in homes),
                    root / "bin/python3",
                ]
            )
            for executable in candidates:
                if executable and executable.is_file():
                    return QgisRuntime(
                        root.resolve(),
                        executable.resolve(),
                        prefix.resolve(),
                        homes[0].resolve() if homes else None,
                    )
    raise RuntimeError(
        "No usable QGIS Python installation found. Set qgis.install_dir, or set "
        "qgis.python_executable and qgis.prefix_path (prefix must contain python/qgis)."
    )


def child_environment(runtime: QgisRuntime, run_dir: Path) -> dict[str, str]:
    """Only OS essentials and derived runtime paths are inherited, never credentials."""
    root, prefix = runtime.install_dir, runtime.prefix_path
    profile = run_dir / ".profile"
    profile.mkdir(parents=True, exist_ok=True)
    environment = {
        key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot", "WINDIR") if key in os.environ
    }
    dll_dirs = [root / "bin", prefix / "bin", root / "apps/Qt5/bin"]
    if runtime.python_home:
        dll_dirs.extend([runtime.python_home, runtime.python_home / "Scripts"])
        environment["PYTHONHOME"] = str(runtime.python_home)
    system_root = environment.get("SYSTEMROOT", environment.get("SystemRoot"))
    if system_root:
        dll_dirs.append(Path(system_root) / "System32")
    environment.update(
        {
            "PATH": os.pathsep.join(str(path) for path in dll_dirs if path.is_dir()),
            "PYTHONPATH": os.pathsep.join((str(prefix / "python"), str(prefix / "python/plugins"))),
            "QGIS_PREFIX_PATH": str(prefix),
            "QT_PLUGIN_PATH": os.pathsep.join(
                str(path)
                for path in (prefix / "qtplugins", root / "apps/Qt5/plugins")
                if path.is_dir()
            ),
            "QT_QPA_PLATFORM": "offscreen",
            "HOME": str(profile),
            "USERPROFILE": str(profile),
            "APPDATA": str(profile),
            "LOCALAPPDATA": str(profile),
            "TMP": str(profile),
            "TEMP": str(profile),
            "TMPDIR": str(profile),
            "XDG_CONFIG_HOME": str(profile),
            "XDG_CACHE_HOME": str(profile),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PROJ_NETWORK": "OFF",
            "GDAL_FILENAME_IS_UTF8": "YES",
            "QGIS_CUSTOM_CONFIG_PATH": str(profile),
        }
    )
    for name, candidates in {
        "PROJ_DATA": (root / "share/proj", prefix / "share/proj"),
        "GDAL_DATA": (root / "apps/gdal/share/gdal", root / "share/gdal"),
        "GDAL_DRIVER_PATH": (root / "apps/gdal/lib/gdalplugins",),
    }.items():
        if path := next((path for path in candidates if path.is_dir()), None):
            environment[name] = str(path)
    if os.name != "nt":
        environment["LD_LIBRARY_PATH"] = str(prefix / "lib")
    return environment
