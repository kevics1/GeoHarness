"""Registry-driven Processing operations, imported exclusively inside QGIS Python."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .worker_context import WorkerContext

_PROVIDERS = {"native", "qgis", "gdal", "3d", "pdal"}
_DENIED_IDS = {
    "native:createspatialindex",
    "native:createattributeindex",
    "native:definecurrentprojection",
    "native:createrasterattributeTable".lower(),
    "native:setlayerencoding",
    "native:spatialiteexecutesql",
    "native:postgisexecutesql",
    "native:importintopostgis",
    "native:importintospatialite",
    "qgis:importintopostgis",
    "gdal:assignprojection",
    "gdal:extractprojection",
    "gdal:overviews",
    "gdal:executesql",
    "gdal:rastercalculator",  # gdal_calc evaluates a Python formula.
    "gdal:importvectorintopostgisdatabaseavailableconnections",
    "gdal:importvectorintopostgisdatabasenewconnection",
}
_INPUT_TYPES = {
    "source",
    "vector",
    "raster",
    "maplayer",
    "layer",
    "mesh",
    "pointcloud",
    "vectortile",
}
_SCALAR_TYPES = {
    "boolean",
    "number",
    "distance",
    "area",
    "scale",
    "duration",
    "enum",
    "string",
    "expression",
    "field",
    "band",
    "crs",
    "extent",
    "point",
    "range",
    "matrix",
    "color",
    "datetime",
    "file",
    "multilayer",
    "coordinateoperation",
    "field_mapping",
    "aggregates",
}
_UNSAFE_PARAMETER_TYPES = {
    "providerconnection",
    "database",
    "databaseschema",
    "databasetable",
    "authcfg",
}
_UNSAFE_OPTION_NAMES = {"EXTRA", "COMMAND", "SCRIPT", "PYTHON_CODE", "SQL", "QUERY", "OPEN_OPTIONS"}
_SOURCE_WRITE_MODES = {
    "native:copylayermetadata": {"DEFAULT"},
    "native:setlayermetadata": {"DEFAULT"},
}


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if hasattr(value, "authid"):
        return value.authid()
    if hasattr(value, "toString"):
        return value.toString()
    return str(value)


def _optional(parameter: Any) -> bool:
    from qgis.core import QgsProcessingParameterDefinition

    return bool(parameter.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional)


def execution_policy(algorithm: Any) -> tuple[bool, str | None]:
    identifier = algorithm.id().lower()
    if identifier.split(":", 1)[0] not in _PROVIDERS:
        return False, "Only built-in native/QGIS/GDAL/3D/PDAL algorithms can execute"
    if identifier in _DENIED_IDS or any(
        token in identifier
        for token in (
            "executesql",
            "importintopostgis",
            "importintospatialite",
            "python",
            "deletedataset",
            "truncate",
            "executecommand",
            "download",
            "upload",
            "httprequest",
            "networkrequest",
            "geocod",
        )
    ):
        return False, "Algorithm edits sources, uses remote services, or executes database/commands"
    if any(
        parameter.type().lower() in _UNSAFE_PARAMETER_TYPES
        for parameter in algorithm.parameterDefinitions()
    ):
        return False, "Database connections and credential parameters are unsupported"
    return True, None


def _summary(algorithm: Any) -> dict[str, Any]:
    allowed, reason = execution_policy(algorithm)
    return {
        "id": algorithm.id(),
        "name": algorithm.displayName(),
        "group": algorithm.group(),
        "provider": algorithm.provider().id() if algorithm.provider() else "model",
        "execution_allowed": allowed,
        "restriction": reason,
        "mode_restrictions": [
            f"{name}=true is unavailable because it writes source metadata"
            for name in _SOURCE_WRITE_MODES.get(algorithm.id().lower(), set())
        ],
    }


def algorithm_help(algorithm: Any) -> dict[str, Any]:
    parameters = []
    for parameter in algorithm.parameterDefinitions():
        definition = {
            "name": parameter.name(),
            "description": parameter.description(),
            "type": parameter.type(),
            "optional": _optional(parameter),
            "default": _json(parameter.defaultValue()),
            "destination": parameter.isDestination(),
        }
        for method, key in (
            ("options", "options"),
            ("minValue", "minimum"),
            ("maxValue", "maximum"),
            ("parentLayerParameterName", "parent_layer"),
            ("fileFilter", "file_filter"),
            ("defaultFileExtension", "default_extension"),
        ):
            if hasattr(parameter, method):
                definition[key] = _json(getattr(parameter, method)())
        parameters.append(definition)
    return {
        **_summary(algorithm),
        "help": algorithm.shortHelpString(),
        "parameters": parameters,
        "outputs": [
            {"name": item.name(), "description": item.description(), "type": item.type()}
            for item in algorithm.outputDefinitions()
        ],
    }


def _prepare_value(parameter: Any, value: Any, context: WorkerContext) -> Any:
    kind = parameter.type().lower()
    if parameter.isDestination():
        if not isinstance(value, (str, type(None))):
            raise ValueError(f"Output {parameter.name()} must be a local filename")
        extension = (
            parameter.defaultFileExtension() if hasattr(parameter, "defaultFileExtension") else ""
        )
        extension = extension or {"sink": "gpkg", "rasterdestination": "tif"}.get(kind, "")
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", parameter.name())
        name = stem + ("." + extension if extension else "")
        return context.output_path(value, name)
    if kind in _INPUT_TYPES or kind == "file":
        if not isinstance(value, str):
            raise ValueError(f"Input {parameter.name()} must be an existing local file")
        return str(context.read_path(value))
    if kind == "multilayer":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Input {parameter.name()} must be a list of local filenames")
        return [str(context.read_path(item)) for item in value]
    if kind in _UNSAFE_PARAMETER_TYPES or kind not in _SCALAR_TYPES:
        raise ValueError(f"Unsupported parameter type {parameter.type()} for {parameter.name()}")
    if kind == "crs" and isinstance(value, str):
        explicit = re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.-]+", value.strip())
        wkt = (
            value.lstrip()
            .upper()
            .startswith(
                (
                    "GEOGCS[",
                    "PROJCS[",
                    "GEOGCRS[",
                    "PROJCRS[",
                    "BOUNDCRS[",
                    "COMPOUNDCRS[",
                )
            )
        )
        proj = value.lstrip().startswith("+proj=") and "+init=" not in value.lower()
        if not (explicit or wkt or proj):
            raise PermissionError("CRS must be an explicit authority ID, WKT or PROJ declaration")
        from qgis.core import QgsCoordinateReferenceSystem

        crs = QgsCoordinateReferenceSystem(value)
        if not crs.isValid():
            raise ValueError("Invalid explicit CRS declaration")
        return crs  # A resolved CRS cannot fall back to loading a datasource string.
    if kind == "extent" and isinstance(value, str):
        number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
        coordinates = rf"\s*{number}\s*,\s*{number}\s*,\s*{number}\s*,\s*{number}\s*"
        suffix = r"(?:\[[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.-]+\])?"
        if not re.fullmatch(coordinates + suffix, value):
            raise PermissionError(
                "Extent must be explicit xmin,xmax,ymin,ymax coordinates with optional [CRS]"
            )
    if parameter.name().upper() in _UNSAFE_OPTION_NAMES and value not in (None, "", []):
        raise PermissionError(f"Free-form command/SQL options are unavailable: {parameter.name()}")
    if isinstance(value, (dict, list)) and kind not in {
        "matrix",
        "field_mapping",
        "aggregates",
        "enum",
        "field",
    }:
        raise ValueError(f"Parameter {parameter.name()} does not accept structured dynamic values")
    return value


def prepare_parameters(
    algorithm: Any, inputs: dict[str, Any], context: WorkerContext
) -> dict[str, Any]:
    if not isinstance(inputs, dict):
        raise ValueError("parameters must be an object keyed by registry parameter names")
    definitions = {parameter.name(): parameter for parameter in algorithm.parameterDefinitions()}
    unknown = set(inputs) - definitions.keys()
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}. Use action=help.")
    for name in _SOURCE_WRITE_MODES.get(algorithm.id().lower(), set()):
        parameter = definitions.get(name)
        value = inputs.get(name, parameter.defaultValue() if parameter else None)
        if value not in (None, False, 0, ""):
            raise PermissionError(
                f"{name}=true saves metadata into source files and is unavailable"
            )
    result: dict[str, Any] = {}
    for name, parameter in definitions.items():
        supplied = name in inputs
        value = inputs.get(name, parameter.defaultValue())
        if parameter.isDestination():
            if value in (None, "") and _optional(parameter) and not supplied:
                continue
            result[name] = _prepare_value(parameter, value, context)
        elif value is None or value == "":
            if not _optional(parameter) and parameter.defaultValue() in (None, ""):
                raise ValueError(f"Missing required parameter: {name}. Use action=help.")
            if supplied:
                result[name] = value
        else:
            result[name] = _prepare_value(parameter, value, context)
    destinations = [value for name, value in result.items() if definitions[name].isDestination()]
    if len(destinations) != len(set(destinations)):
        raise ValueError("Each output requires a distinct filename")
    return result


def layer_metadata(layer: Any) -> dict[str, Any]:
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    extent, crs = layer.extent(), layer.crs()
    metadata = {
        "name": layer.name(),
        "valid": layer.isValid(),
        "source": layer.source(),
        "provider": layer.providerType(),
        "crs": crs.authid(),
        "crs_description": crs.description(),
        "bounds": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
    }
    if isinstance(layer, QgsVectorLayer):
        metadata.update(
            feature_count=layer.featureCount(),
            field_count=len(layer.fields()),
            fields=[{"name": field.name(), "type": field.typeName()} for field in layer.fields()],
            geometry_type=layer.wkbType(),
        )
    elif isinstance(layer, QgsRasterLayer):
        metadata.update(band_count=layer.bandCount(), width=layer.width(), height=layer.height())
    return metadata


def _run(
    algorithm: Any, inputs: dict[str, Any], context: WorkerContext, *, model: bool = False
) -> dict[str, Any]:
    import processing
    from qgis.core import (
        QgsMapLayer,
        QgsProcessingContext,
        QgsProcessingFeedback,
        QgsProcessingUtils,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )

    if not model:
        allowed, reason = execution_policy(algorithm)
        if not allowed:
            raise PermissionError(reason)
    parameters = prepare_parameters(algorithm, inputs, context)
    processing_context = QgsProcessingContext()
    processing_context.setTemporaryFolder(str(context.run_dir / "temporary"))
    valid, message = algorithm.checkParameterValues(parameters, processing_context)
    if not valid:
        raise ValueError(f"Invalid parameters for {algorithm.id()}: {message}")

    class Feedback(QgsProcessingFeedback):
        def pushInfo(self, message: str) -> None:
            print(message, flush=True)

        def reportError(self, message: str, fatalError: bool = False) -> None:
            print(f"Processing error: {message}", flush=True)
            super().reportError(message, fatalError)

    feedback = Feedback()
    outputs = processing.run(algorithm, parameters, context=processing_context, feedback=feedback)
    definitions = {item.name(): item for item in algorithm.outputDefinitions()}
    layers = []
    persisted: dict[str, Any] = {}
    for name, value in outputs.items():
        definition = definitions.get(name)
        kind = definition.type().lower() if definition else ""
        is_layer = (
            isinstance(value, QgsMapLayer)
            or "layer" in kind
            or kind
            in {
                "outputvector",
                "outputraster",
                "outputpointcloud",
                "outputvectortile",
            }
        )
        if is_layer:
            values = value if isinstance(value, list) else [value]
            paths = []
            for index, item in enumerate(values):
                layer = (
                    item
                    if isinstance(item, QgsMapLayer)
                    else QgsProcessingUtils.mapLayerFromString(str(item), processing_context)
                )
                if layer is None or not layer.isValid():
                    raise RuntimeError(f"Algorithm output {name} is not a valid layer: {item}")
                source = layer.source().split("|", 1)[0]
                path = Path(source).resolve()
                if not path.is_file() or not path.is_relative_to(context.run_dir):
                    if not isinstance(layer, QgsVectorLayer):
                        raise RuntimeError(f"Algorithm returned an unpersisted output: {name}")
                    target = context.output_path(None, f"{name}_{index}.gpkg")
                    options = QgsVectorFileWriter.SaveVectorOptions()
                    options.driverName = "GPKG"
                    error, message, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
                        layer, target, processing_context.transformContext(), options
                    )
                    if error != QgsVectorFileWriter.NoError:
                        raise RuntimeError(f"Could not persist {name}: {message}")
                    path = Path(target)
                    layer = QgsVectorLayer(str(path), name, "ogr")
                layers.append({"output": name, "path": str(path), **layer_metadata(layer)})
                paths.append(str(path))
            persisted[name] = paths if isinstance(value, list) else paths[0]
        elif "file" in kind or "folder" in kind or "html" in kind:
            path = Path(str(value)).resolve()
            if not path.is_relative_to(context.run_dir) or not path.exists():
                raise RuntimeError(
                    f"Algorithm output {name} is missing or outside the run directory"
                )
            persisted[name] = str(path)
        else:
            persisted[name] = _json(value)
    return {
        "algorithm_id": algorithm.id(),
        "parameters": _json(parameters),
        "outputs": persisted,
        "layers": layers,
    }


def _load_model(path: Path, registry: Any, context: WorkerContext) -> Any:
    from qgis.core import Qgis, QgsProcessingModelAlgorithm

    if path.suffix.lower() != ".model3":
        raise ValueError("Models must be local QGIS .model3 files")
    model = QgsProcessingModelAlgorithm()
    if not model.fromFile(str(path)):
        raise ValueError("Could not load the QGIS model")
    model_parameters = {parameter.name(): parameter for parameter in model.parameterDefinitions()}
    for child in model.childAlgorithms().values():
        algorithm = registry.algorithmById(child.algorithmId())
        if algorithm is None:
            raise ValueError(f"Model references an unavailable algorithm: {child.algorithmId()}")
        allowed, reason = execution_policy(algorithm)
        if not allowed:
            raise PermissionError(f"Model child {child.algorithmId()}: {reason}")
        definitions = {
            parameter.name(): parameter for parameter in algorithm.parameterDefinitions()
        }
        for name, sources in child.parameterSources().items():
            if name not in definitions:
                raise ValueError(f"Unknown model child parameter: {name}")
            parameter = definitions[name]
            for source in sources:
                if source.source() == Qgis.ProcessingModelChildParameterSource.StaticValue:
                    value = source.staticValue()
                    if name in _SOURCE_WRITE_MODES.get(
                        algorithm.id().lower(), set()
                    ) and value not in (None, False, 0, ""):
                        raise PermissionError("Models cannot save metadata into source files")
                    if parameter.isDestination() and value not in (None, "", "TEMPORARY_OUTPUT"):
                        raise PermissionError(
                            "Models must expose outputs or use temporary child outputs"
                        )
                    if value not in (None, "", "TEMPORARY_OUTPUT"):
                        _prepare_value(parameter, value, context)
                        if parameter.type().lower() in _INPUT_TYPES | {"file", "multilayer"}:
                            paths = value if isinstance(value, list) else [value]
                            if not all(Path(item).is_absolute() for item in paths):
                                raise ValueError(
                                    "Model static file inputs must be absolute; expose relative "
                                    "files as model parameters instead"
                                )
                elif source.source() == Qgis.ProcessingModelChildParameterSource.ModelParameter:
                    outer = model_parameters.get(source.parameterName())
                    if outer is None:
                        raise ValueError(f"Unknown model parameter: {source.parameterName()}")
                    if parameter.name().upper() in _UNSAFE_OPTION_NAMES:
                        raise PermissionError("Models cannot supply dynamic command/SQL options")
                    if name in _SOURCE_WRITE_MODES.get(algorithm.id().lower(), set()):
                        raise PermissionError("Models cannot supply dynamic source-write flags")
                    if parameter.isDestination() and not outer.isDestination():
                        raise PermissionError("Model output paths must be destination parameters")
                    if parameter.type().lower() in _INPUT_TYPES | {
                        "file",
                        "multilayer",
                    } and outer.type().lower() not in _INPUT_TYPES | {"file", "multilayer"}:
                        raise PermissionError("Model file inputs must use local-file parameters")
                    if (
                        parameter.type().lower() in {"extent", "crs"}
                        and outer.type().lower() != parameter.type().lower()
                    ):
                        raise PermissionError(
                            "Model extent/CRS inputs must use matching declared types"
                        )
                elif source.source() == Qgis.ProcessingModelChildParameterSource.ChildOutput:
                    if (
                        parameter.name().upper() in _UNSAFE_OPTION_NAMES
                        or parameter.isDestination()
                    ):
                        raise PermissionError(
                            "Model child outputs cannot become command/output options"
                        )
                    if name in _SOURCE_WRITE_MODES.get(algorithm.id().lower(), set()):
                        raise PermissionError("Models cannot supply dynamic source-write flags")
                elif source.source() not in {
                    Qgis.ProcessingModelChildParameterSource.ModelParameter,
                    Qgis.ProcessingModelChildParameterSource.ChildOutput,
                }:
                    raise PermissionError(
                        "Model expression/generated parameter sources are unsupported"
                    )
    return model


def dispatch(action: str, payload: dict[str, Any], context: WorkerContext) -> dict[str, Any]:
    from processing.core.Processing import Processing
    from qgis.core import Qgis, QgsApplication

    Processing.initialize()
    registry = QgsApplication.processingRegistry()
    # User script/model providers are unnecessary; requests load only checked local models.
    for provider in list(registry.providers()):
        if provider.id() not in _PROVIDERS:
            registry.removeProvider(provider)
    algorithms = list(registry.algorithms())
    if action == "doctor":
        return {
            "qgis_version": Qgis.QGIS_VERSION,
            "prefix_path": QgsApplication.prefixPath(),
            "algorithm_count": len(algorithms),
            "providers": [
                {
                    "id": provider.id(),
                    "name": provider.name(),
                    "active": provider.isActive(),
                    "algorithm_count": len(list(provider.algorithms())),
                }
                for provider in registry.providers()
            ],
            "guard": "local-file application guards for trusted algorithms; not an OS sandbox",
        }
    if action == "search":
        query = str(payload.get("query", "")).casefold()
        limit = int(payload.get("limit") or 20)
        offset = int(payload.get("offset", 0))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Search limit must be 1..100 and offset must be non-negative")
        matches = sorted(
            (
                algorithm
                for algorithm in algorithms
                if query
                in " ".join(
                    (
                        algorithm.id(),
                        algorithm.displayName(),
                        algorithm.group(),
                        " ".join(algorithm.tags()),
                    )
                ).casefold()
            ),
            key=lambda algorithm: algorithm.id(),
        )
        return {
            "query": query,
            "total": len(matches),
            "offset": offset,
            "limit": limit,
            "algorithms": [_summary(algorithm) for algorithm in matches[offset : offset + limit]],
        }
    if action == "model":
        model_path = context.read_path(str(payload.get("model_path", "")))
        model = _load_model(model_path, registry, context)
        return {
            "model_path": str(model_path),
            **_run(model, payload.get("parameters", {}), context, model=True),
        }
    identifier = str(payload.get("algorithm_id", ""))
    algorithm = registry.algorithmById(identifier)
    if algorithm is None:
        raise ValueError(f"Unknown QGIS algorithm: {identifier!r}. Use action=search.")
    if action == "help":
        return algorithm_help(algorithm)
    if action == "run":
        return _run(algorithm, payload.get("parameters", {}), context)
    raise ValueError(f"Unsupported Processing action: {action}")
