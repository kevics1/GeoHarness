"""Execution/validation tests do not require importing QGIS into pytest's interpreter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from enum import IntFlag
from pathlib import Path
from types import SimpleNamespace

import pytest

from geoharness.config.settings import QgisConfig
from geoharness.qgis import discovery, runner
from geoharness.qgis.discovery import QgisRuntime, child_environment, discover_qgis
from geoharness.qgis.processing_worker import execution_policy, prepare_parameters
from geoharness.qgis.worker_context import WorkerContext


@pytest.fixture
def runtime(tmp_path: Path) -> QgisRuntime:
    root = tmp_path / "QGIS runtime"
    for directory in (
        "bin",
        "apps/qgis/python/qgis",
        "apps/qgis/python/plugins",
        "apps/qgis/bin",
        "apps/Python312",
        "apps/Qt5/bin",
        "apps/Qt5/plugins",
        "share/proj",
        "apps/gdal/share/gdal",
        "apps/gdal/lib/gdalplugins",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    executable = root / "bin/python.exe"
    executable.touch()
    return QgisRuntime(root, executable, root / "apps/qgis", root / "apps/Python312")


def test_runtime_discovery_and_environment(runtime: QgisRuntime, tmp_path: Path, monkeypatch):
    settings = QgisConfig(install_dir=str(runtime.install_dir))
    assert discover_qgis(settings, cwd=tmp_path) == runtime
    for key in ("OPENAI_API_KEY", "POSTGIS_DSN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "PROJ_LIB"):
        monkeypatch.setenv(key, "must-not-leak")
    environment = child_environment(runtime, tmp_path / "job")
    assert not any("must-not-leak" in value for value in environment.values())
    assert environment["PYTHONHOME"] == str(runtime.python_home)
    assert environment["QGIS_PREFIX_PATH"] == str(runtime.prefix_path)
    assert environment["PROJ_DATA"] == str(runtime.install_dir / "share/proj")
    assert environment["QT_QPA_PLATFORM"] == "offscreen"
    assert environment["PROJ_NETWORK"] == "OFF"


def test_runtime_does_not_execute_shell_launchers(tmp_path: Path):
    with pytest.raises(ValueError, match="shell launcher"):
        discover_qgis(QgisConfig(python_executable="python-qgis.bat"), cwd=tmp_path)
    with pytest.raises(RuntimeError, match="disabled"):
        discover_qgis(QgisConfig(enabled=False), cwd=tmp_path)


@pytest.mark.parametrize("layout", ["src", "Lib/site-packages"])
def test_worker_bootstrap_keeps_qgis_dependencies_ahead_of_agent_packages(tmp_path, layout):
    """Run the actual bootstrap with a poisoned agent-side NumPy beside the package."""
    host_root = tmp_path / "agent" / layout
    package = host_root / "geoharness"
    qgis_package = package / "qgis"
    qgis_package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = 'bootstrap-test'\n", encoding="utf-8")
    (qgis_package / "__init__.py").write_text("", encoding="utf-8")
    source = Path(runner.__file__).parent
    shutil.copyfile(source / "worker.py", qgis_package / "worker.py")
    shutil.copyfile(source / "worker_context.py", qgis_package / "worker_context.py")
    (host_root / "numpy.py").write_text(
        "raise RuntimeError('agent NumPy entered QGIS runtime')\n", encoding="utf-8"
    )
    native = tmp_path / "qgis-native"
    native.mkdir()
    (native / "numpy.py").write_text("runtime_origin = 'qgis'\n", encoding="utf-8")
    probe = (
        "import json, runpy, sys; "
        "runpy.run_path(sys.argv[1], run_name='bootstrap_probe'); "
        "import numpy, geoharness; "
        "print(json.dumps({'origin': numpy.runtime_origin, 'package': geoharness.__file__, "
        "'paths': sys.path}))"
    )
    environment = {
        key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot", "WINDIR") if key in os.environ
    }
    environment.update(PYTHONPATH=str(native), PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    result = subprocess.run(
        [sys.executable, "-s", "-B", "-c", probe, str(qgis_package / "worker.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["origin"] == "qgis"
    assert Path(response["package"]) == package / "__init__.py"
    assert str(host_root) not in response["paths"]


def test_secondary_fixed_disk_standalone_is_discovered_without_registry(tmp_path, monkeypatch):
    root = tmp_path / "Program Files/QGIS 3.99"
    (root / "apps/qgis/python/qgis").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin/python.exe").touch()
    monkeypatch.setattr(discovery, "_fixed_drive_roots", lambda: [tmp_path])
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "system/Program Files"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("OSGEO4W_ROOT", raising=False)
    assert root in discovery._windows_installations()


class FakeProcess:
    pid = 4242

    def __init__(self, *, immediate: bool = False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data("原生日志".encode() + b"x" * 160)
        self.stderr.feed_data(b"native warning")
        self.returncode = 0 if immediate else None
        self.done = asyncio.Event()
        self.killed = False
        self.waited = False
        if immediate:
            self.finish()

    def finish(self):
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.done.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.finish()

    async def wait(self):
        await self.done.wait()
        self.waited = True
        return self.returncode


def fake_runtime(monkeypatch, runtime: QgisRuntime, process: FakeProcess, callback=None):
    monkeypatch.setattr(runner, "discover_qgis", lambda *_args, **_kwargs: runtime)
    calls = []

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        if callback:
            callback(args, kwargs)
        return process

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", create)

    class Job:
        def __init__(self, pid):
            assert pid == process.pid

        def terminate(self):
            process.kill()

        def close(self):
            pass

    monkeypatch.setattr(runner, "_WindowsJob", Job)

    async def terminate(proc, job):
        proc.kill()
        await proc.wait()

    monkeypatch.setattr(runner, "_terminate_process_tree", terminate)
    return calls


async def test_runner_uses_native_executable_and_persists_response(runtime, tmp_path, monkeypatch):
    process = FakeProcess(immediate=True)

    def respond(args, _kwargs):
        path = Path(args[-1])
        (path.parent / "result.json").write_text(
            json.dumps(
                {"status": "success", "result": {"algorithm_count": 100}, "error_summary": None}
            ),
            encoding="utf-8",
        )

    calls = fake_runtime(monkeypatch, runtime, process, respond)
    result = await runner.execute_qgis(
        "doctor", {}, cwd=tmp_path, settings=QgisConfig(max_output_chars=100)
    )
    args, kwargs = calls[0]
    assert args[0] == str(runtime.python_executable)
    assert args[1:5] == ("-s", "-B", "-X", "utf8")
    assert "shell" not in kwargs
    assert result["status"] == "succeeded"
    assert result["result"]["algorithm_count"] == 100
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == 100
    assert len(Path(result["stdout_path"]).read_text(encoding="utf-8")) > 100
    assert (
        json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))["status"]
        == "succeeded"
    )
    assert (
        json.loads(Path(result["request_path"]).read_text(encoding="utf-8"))["action"] == "doctor"
    )
    request = json.loads(Path(result["request_path"]).read_text(encoding="utf-8"))
    output_root = tmp_path / ".geoharness/qgis"
    assert str(output_root) in request["read_roots"]
    prior_output = output_root / "prior/result.gpkg"
    prior_output.parent.mkdir()
    prior_output.touch()
    context = WorkerContext(
        tmp_path, Path(result["run_dir"]), [Path(value) for value in request["read_roots"]]
    )
    assert context.read_path(str(prior_output)) == prior_output


async def test_runner_preserves_worker_parameter_error(runtime, tmp_path, monkeypatch):
    process = FakeProcess(immediate=True)
    process.returncode = 1

    def respond(args, _kwargs):
        (Path(args[-1]).parent / "result.json").write_text(
            json.dumps(
                {
                    "status": "error",
                    "result": None,
                    "error_summary": "Missing required parameter: INPUT",
                }
            ),
            encoding="utf-8",
        )

    fake_runtime(monkeypatch, runtime, process, respond)
    result = await runner.execute_qgis("run", {}, cwd=tmp_path, settings=QgisConfig())
    assert result["error_summary"] == "Missing required parameter: INPUT"


async def test_runner_timeout_terminates_and_waits(runtime, tmp_path, monkeypatch):
    process = FakeProcess()
    fake_runtime(monkeypatch, runtime, process)
    result = await runner.execute_qgis(
        "doctor", {}, cwd=tmp_path, settings=QgisConfig(timeout_seconds=0.01)
    )
    assert result["status"] == "timed_out"
    assert process.killed and process.waited
    assert json.loads(Path(result["result_path"]).read_text())["status"] == "timed_out"


async def test_runner_cancel_terminates_waits_and_saves_manifest(runtime, tmp_path, monkeypatch):
    process = FakeProcess()
    calls = fake_runtime(monkeypatch, runtime, process)
    task = asyncio.create_task(
        runner.execute_qgis("doctor", {}, cwd=tmp_path, settings=QgisConfig())
    )
    while not calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited
    manifest = next((tmp_path / ".geoharness/qgis").glob("*/manifest.json"))
    assert json.loads(manifest.read_text())["status"] == "cancelled"


async def test_disabled_runtime_still_records_failure(tmp_path):
    result = await runner.execute_qgis(
        "doctor", {}, cwd=tmp_path, settings=QgisConfig(enabled=False)
    )
    assert result["status"] == "failed" and "disabled" in result["error_summary"]
    assert Path(result["manifest_path"]).is_file()
    assert Path(result["stderr_path"]).is_file()


@pytest.fixture
def context(tmp_path: Path):
    source = tmp_path / "workspace"
    run = source / "runs/job"
    run.mkdir(parents=True)
    (source / "input.geojson").write_text("{}")
    return WorkerContext(source, run)


def test_context_read_roots_secrets_and_output_escape(context, tmp_path):
    assert context.read_path("input.geojson") == context.cwd / "input.geojson"
    for name in (".env", "credentials.json", "private.key"):
        (context.cwd / name).touch()
        with pytest.raises(PermissionError, match="Credential"):
            context.read_path(name)
    outside = tmp_path / "outside.geojson"
    outside.touch()
    with pytest.raises(PermissionError, match="read roots"):
        context.read_path(str(outside))
    with pytest.raises(ValueError, match="existing files"):
        context.read_path(str(context.cwd))
    for value in (
        "https://data.example/layer",
        "PG:dbname=data",
        "/vsicurl/a",
        "a|layername=secret",
    ):
        with pytest.raises(PermissionError, match="local file paths"):
            context.read_path(value)
    for value in ("../outside.gpkg", "request.json", "result.json", ".profile/foo", str(outside)):
        with pytest.raises(PermissionError):
            context.output_path(value)
    assert Path(context.output_path("nested/result.gpkg")).parent.is_dir()


def test_context_checks_vrt_references(context, tmp_path):
    vrt = context.cwd / "hidden.xml"
    outside = tmp_path / "outside.tif"
    outside.touch()
    vrt.write_text(f"<VRTDataset><SourceFilename>{outside}</SourceFilename></VRTDataset>")
    with pytest.raises(PermissionError, match="read roots"):
        context.read_path(str(vrt))
    vrt.write_text(
        "<VRTDataset><SourceFilename>/vsicurl/https://host/a.tif</SourceFilename></VRTDataset>"
    )
    with pytest.raises(PermissionError, match="local file paths"):
        context.read_path(str(vrt))


class Flags(IntFlag):
    FlagOptional = 1


class Parameter:
    def __init__(self, name, kind="string", *, default=None, optional=False, output=False):
        self._name, self.kind = name, kind
        self.default, self.optional, self.output = default, optional, output

    def name(self):
        return self._name

    def type(self):
        return self.kind

    def flags(self):
        return Flags.FlagOptional if self.optional else Flags(0)

    def defaultValue(self):
        return self.default

    def isDestination(self):
        return self.output


class Algorithm:
    def __init__(self, identifier, parameters=()):
        self.identifier, self.parameters = identifier, parameters

    def id(self):
        return self.identifier

    def parameterDefinitions(self):
        return self.parameters


@pytest.fixture
def qgis_flags(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "qgis.core",
        SimpleNamespace(QgsProcessingParameterDefinition=SimpleNamespace(Flag=Flags)),
    )


def test_scalar_extent_and_crs_cannot_load_outside_layer_sources(context, qgis_flags):
    algorithm = Algorithm(
        "native:createconstantrasterlayer",
        [
            Parameter("EXTENT", "extent"),
            Parameter("TARGET_CRS", "crs"),
        ],
    )
    with pytest.raises(PermissionError, match="Extent must be explicit"):
        prepare_parameters(
            algorithm, {"EXTENT": "/outside/secret.tif", "TARGET_CRS": "EPSG:32647"}, context
        )
    with pytest.raises(PermissionError, match="CRS must be an explicit"):
        prepare_parameters(
            algorithm,
            {"EXTENT": "0,20,0,20 [EPSG:32647]", "TARGET_CRS": "/outside/secret.tif"},
            context,
        )
    fake_crs = SimpleNamespace(isValid=lambda: True, authid=lambda: "EPSG:32647")
    sys.modules["qgis.core"].QgsCoordinateReferenceSystem = lambda _value: fake_crs
    result = prepare_parameters(
        algorithm, {"EXTENT": "0,20,0,20 [EPSG:32647]", "TARGET_CRS": "EPSG:32647"}, context
    )
    assert result["TARGET_CRS"].authid() == "EPSG:32647"


def test_metadata_default_save_modes_are_rejected(context, qgis_flags):
    for identifier in ("native:copylayermetadata", "native:setlayermetadata"):
        algorithm = Algorithm(
            identifier,
            [Parameter("INPUT", "layer"), Parameter("DEFAULT", "boolean", default=False)],
        )
        with pytest.raises(PermissionError, match="saves metadata into source"):
            prepare_parameters(algorithm, {"INPUT": "input.geojson", "DEFAULT": True}, context)
        assert (
            prepare_parameters(algorithm, {"INPUT": "input.geojson"}, context)["DEFAULT"] is False
        )


def test_registry_parameters_validate_names_required_inputs_and_destinations(context, qgis_flags):
    algorithm = Algorithm(
        "native:buffer",
        [
            Parameter("INPUT", "source"),
            Parameter("DISTANCE", "distance", default=10),
            Parameter("OUTPUT", "sink", output=True),
        ],
    )
    with pytest.raises(ValueError, match="Unknown parameter"):
        prepare_parameters(algorithm, {"TYPO": 1}, context)
    with pytest.raises(ValueError, match="Missing required.*INPUT"):
        prepare_parameters(algorithm, {}, context)
    result = prepare_parameters(algorithm, {"INPUT": "input.geojson"}, context)
    assert result["INPUT"] == str(context.cwd / "input.geojson")
    assert result["DISTANCE"] == 10
    assert result["OUTPUT"] == str(context.run_dir / "OUTPUT.gpkg")
    with pytest.raises(PermissionError, match="run directory"):
        prepare_parameters(algorithm, {"INPUT": "input.geojson", "OUTPUT": "../bad.gpkg"}, context)


def test_processing_rejects_source_mutation_arbitrary_code_and_cli_options(context, qgis_flags):
    for identifier in (
        "native:createspatialindex",
        "native:createattributeindex",
        "native:definecurrentprojection",
        "gdal:assignprojection",
        "gdal:extractprojection",
        "script:evil",
        "native:executesql",
        "native:filedownloader",
        "native:batchnominatimgeocoder",
    ):
        assert execution_policy(Algorithm(identifier))[0] is False
    assert execution_policy(Algorithm("native:buffer"))[0] is True
    assert (
        execution_policy(Algorithm("native:any", [Parameter("CONNECTION", "providerconnection")]))[
            0
        ]
        is False
    )
    with pytest.raises(PermissionError, match="Free-form command"):
        prepare_parameters(
            Algorithm("gdal:warp", [Parameter("EXTRA")]), {"EXTRA": "--config X Y"}, context
        )


def test_area_and_layer_registry_types_are_supported(context, qgis_flags):
    algorithm = Algorithm(
        "native:deleteholes",
        [
            Parameter("INPUT", "layer"),
            Parameter("MIN_AREA", "area", default=0),
            Parameter("OUTPUT", "sink", output=True),
        ],
    )
    result = prepare_parameters(algorithm, {"INPUT": "input.geojson", "MIN_AREA": 1}, context)
    assert result["INPUT"] == str(context.cwd / "input.geojson")
    assert result["MIN_AREA"] == 1
    with pytest.raises(ValueError, match="Unsupported parameter type"):
        prepare_parameters(
            Algorithm("native:any", [Parameter("CUSTOM", "custom-plugin")]),
            {"CUSTOM": "a"},
            context,
        )


def test_model_cannot_disguise_file_input_or_output_as_string(context, monkeypatch):
    from geoharness.qgis.processing_worker import _load_model

    kinds = SimpleNamespace(StaticValue=0, ModelParameter=1, ChildOutput=2)
    source = SimpleNamespace(source=lambda: 1, parameterName=lambda: "URI")
    algorithm = Algorithm("native:buffer", [Parameter("INPUT", "source")])
    child = SimpleNamespace(
        algorithmId=lambda: "native:buffer", parameterSources=lambda: {"INPUT": [source]}
    )
    model = SimpleNamespace(
        fromFile=lambda _path: True,
        parameterDefinitions=lambda: [Parameter("URI", "string")],
        childAlgorithms=lambda: {"buffer": child},
    )
    monkeypatch.setitem(
        sys.modules,
        "qgis.core",
        SimpleNamespace(
            Qgis=SimpleNamespace(ProcessingModelChildParameterSource=kinds),
            QgsProcessingModelAlgorithm=lambda: model,
        ),
    )
    registry = SimpleNamespace(algorithmById=lambda _identifier: algorithm)
    with pytest.raises(PermissionError, match="local-file parameters"):
        _load_model(context.cwd / "model.model3", registry, context)
    algorithm.parameters = [Parameter("INPUT", "sink", output=True)]
    with pytest.raises(PermissionError, match="destination parameters"):
        _load_model(context.cwd / "model.model3", registry, context)
    for kind in ("extent", "crs"):
        algorithm.parameters = [Parameter("INPUT", kind)]
        with pytest.raises(PermissionError, match="matching declared types"):
            _load_model(context.cwd / "model.model3", registry, context)


def test_model_rejects_python_formula_and_static_external_destination(context, monkeypatch):
    from geoharness.qgis.processing_worker import _load_model

    assert not execution_policy(Algorithm("gdal:rastercalculator"))[0]
    kinds = SimpleNamespace(StaticValue=0, ModelParameter=1, ChildOutput=2)
    source = SimpleNamespace(source=lambda: 0, staticValue=lambda: "/outside/data.gpkg")
    algorithm = Algorithm("native:buffer", [Parameter("OUTPUT", "sink", output=True)])
    child = SimpleNamespace(
        algorithmId=lambda: "native:buffer", parameterSources=lambda: {"OUTPUT": [source]}
    )
    model = SimpleNamespace(
        fromFile=lambda _path: True,
        parameterDefinitions=lambda: [],
        childAlgorithms=lambda: {"buffer": child},
    )
    monkeypatch.setitem(
        sys.modules,
        "qgis.core",
        SimpleNamespace(
            Qgis=SimpleNamespace(ProcessingModelChildParameterSource=kinds),
            QgsProcessingModelAlgorithm=lambda: model,
        ),
    )
    registry = SimpleNamespace(algorithmById=lambda _identifier: algorithm)
    with pytest.raises(PermissionError, match="expose outputs"):
        _load_model(context.cwd / "model.model3", registry, context)
