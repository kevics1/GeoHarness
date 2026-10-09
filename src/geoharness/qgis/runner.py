"""Launch the separate QGIS interpreter and persist an auditable job record."""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .discovery import child_environment, discover_qgis

if TYPE_CHECKING:
    from geoharness.config.settings import QgisConfig

_ACTIONS = {
    "doctor",
    "search",
    "help",
    "run",
    "model",
    "project_create",
    "project_inspect",
    "project_export",
    "project_preflight",
}


class _WindowsJob:
    """Windows job-object containment lets timeout/cancel kill native descendants."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            (
                "SetInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self._kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self._kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self._kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self._kernel.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._kernel.CloseHandle(process)
        except BaseException:
            self.close()
            raise

    def terminate(self) -> None:
        if self.handle:
            self._kernel.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


async def _terminate_process_tree(process: Any, job: _WindowsJob | None) -> None:
    if job is not None:
        job.terminate()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        process.kill()
    await process.wait()


async def _capture(stream: asyncio.StreamReader, path: Path, limit: int) -> tuple[str, bool]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    chunks: list[str] = []
    length = 0
    truncated = False
    with path.open("w", encoding="utf-8") as log:
        while data := await stream.read(8192):
            value = decoder.decode(data)
            log.write(value)
            remaining = max(0, limit - length)
            if remaining:
                chunks.append(value[:remaining])
            length += min(remaining, len(value))
            truncated = truncated or len(value) > remaining
        value = decoder.decode(b"", final=True)
        log.write(value)
        remaining = max(0, limit - length)
        chunks.append(value[:remaining])
        truncated = truncated or len(value) > remaining
    return "".join(chunks), truncated


async def execute_qgis(
    action: str,
    payload: dict[str, Any],
    *,
    cwd: Path,
    settings: QgisConfig,
    read_roots: list[Path] | tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Execute a structured QGIS request; no shell or user Python code is evaluated."""
    if action not in _ACTIONS:
        raise ValueError(f"Unsupported QGIS action: {action}")
    if action == "search" and not payload.get("limit"):
        payload = {**payload, "limit": settings.max_search_results}
    cwd = Path(cwd).resolve()
    output_root = Path(settings.outputs_dir).expanduser()
    output_root = (output_root if output_root.is_absolute() else cwd / output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / f"{action}-{stamp}-{uuid4().hex[:12]}"
    run_dir.mkdir()
    paths = {
        name: run_dir / name
        for name in ("request.json", "result.json", "manifest.json", "stdout.txt", "stderr.txt")
    }
    request = {
        "action": action,
        "payload": payload,
        "cwd": str(cwd),
        "run_dir": str(run_dir),
        "read_roots": list(
            dict.fromkeys(
                [
                    str(output_root),
                    *(
                        str((path if path.is_absolute() else cwd / path).resolve())
                        for path in map(Path, read_roots)
                    ),
                ]
            )
        ),
    }
    paths["request.json"].write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result: dict[str, Any] = {
        "status": "error",
        "result": None,
        "error_summary": None,
        "run_dir": str(run_dir),
        "manifest_path": str(paths["manifest.json"]),
        "request_path": str(paths["request.json"]),
        "result_path": str(paths["result.json"]),
        "stdout_path": str(paths["stdout.txt"]),
        "stderr_path": str(paths["stderr.txt"]),
        "stdout": "",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "exit_code": None,
        "timeout_seconds": settings.timeout_seconds,
        "guard": "trusted QGIS runtime with application-level file guards; not an OS sandbox",
    }
    process = None
    job = None
    captures: list[asyncio.Task] = []
    cancelled = False
    try:
        runtime = discover_qgis(settings, cwd=cwd)
        result["runtime"] = runtime.as_dict()
        environment = child_environment(runtime, run_dir)
        kwargs: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {}
        process = await asyncio.create_subprocess_exec(
            str(runtime.python_executable),
            "-s",
            "-B",
            "-X",
            "utf8",
            str(Path(__file__).with_name("worker.py")),
            str(paths["request.json"]),
            cwd=str(run_dir),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        assert process.stdout is not None and process.stderr is not None
        captures = [
            asyncio.create_task(
                _capture(process.stdout, paths["stdout.txt"], settings.max_output_chars)
            ),
            asyncio.create_task(
                _capture(process.stderr, paths["stderr.txt"], settings.max_output_chars)
            ),
        ]
        if os.name == "nt":
            job = _WindowsJob(process.pid)
        # The worker waits for this marker before importing QGIS or starting native jobs.
        (run_dir / ".start").touch()
        try:
            await asyncio.wait_for(process.wait(), timeout=settings.timeout_seconds)
        except asyncio.TimeoutError:
            result.update(
                status="timeout",
                error_summary=f"QGIS exceeded {settings.timeout_seconds:g}s timeout",
            )
            await _terminate_process_tree(process, job)
        if result["status"] != "timeout":
            if paths["result.json"].is_file():
                response = json.loads(paths["result.json"].read_text(encoding="utf-8"))
                result.update(response)
                if process.returncode != 0 and response.get("status") == "success":
                    result.update(
                        status="error", error_summary=f"QGIS exited with code {process.returncode}"
                    )
            else:
                result["error_summary"] = (
                    f"QGIS exited without a result file (code {process.returncode})"
                )
    except asyncio.CancelledError:
        cancelled = True
        result.update(status="cancelled", error_summary="QGIS request was cancelled")
        if process is not None:
            await asyncio.shield(_terminate_process_tree(process, job))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        result.update(status="error", error_summary=f"{type(exc).__name__}: {exc}")
        if process is not None:
            await _terminate_process_tree(process, job)
    finally:
        if job is not None:
            job.close()  # Also removes native children remaining after a successful parent exit.
        if captures:
            (
                (result["stdout"], result["stdout_truncated"]),
                (result["stderr"], result["stderr_truncated"]),
            ) = await asyncio.gather(*captures)
        for name in ("stdout.txt", "stderr.txt"):
            if not paths[name].exists():
                paths[name].write_text("", encoding="utf-8")
        result["exit_code"] = process.returncode if process is not None else None
        result["status"] = {
            "success": "succeeded",
            "error": "failed",
            "timeout": "timed_out",
        }.get(result["status"], result["status"])
        paths["result.json"].write_text(
            json.dumps(
                {
                    "status": result["status"],
                    "result": result["result"],
                    "error_summary": result["error_summary"],
                    "diagnostics": result.get("diagnostics"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        reserved = set(paths) | {".start"}
        result["files"] = [
            {"path": str(path), "size_bytes": path.stat().st_size}
            for path in sorted(run_dir.rglob("*"))
            if path.is_file()
            and path.resolve().is_relative_to(run_dir)
            and path.relative_to(run_dir).parts[0] != ".profile"
            and path.relative_to(run_dir).as_posix() not in reserved
        ]
        paths["manifest.json"].write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if cancelled:
        raise asyncio.CancelledError
    return result
