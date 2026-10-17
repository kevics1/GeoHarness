"""Publish explicit analysis artifacts into the user's workspace exports folder."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import AliasChoices, BaseModel, Field

from geoharness.cartography.project import MapProject
from geoharness.cartography.quality import MapReview

_MAP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".pdf", ".svg", ".html"}
_RESERVED_NAMES = {"delivery.json", "map-receipt.json", "summary.json", "summary.md", "project"}


class _QgisDeliveryProject(BaseModel):
    """Portable files exported by the independent QGIS worker."""

    backend: Literal["qgis"]
    project_file: str = Field(pattern=r"(?i)^.+\.qg[sz]$")
    preview: str
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: list[str] = Field(min_length=1, max_length=1000)


class GeoDeliverInput(BaseModel):
    map_id: str = Field(default="", description="Recorded logical map to deliver or select by ID.")
    version_policy: Literal["prefer_checked", "selected"] = Field(default="prefer_checked",
        description="Prefer a recorded unchanged layout pass when the selected map has known "
        "issues/stale checks. selected explicitly retains the requested map as a draft.")
    files: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Explicit map, table, report or script file paths to deliver.",
    )
    project_dir: str = Field(default="", description="Portable map project folder to include.")
    primary_map: str = Field(
        default="",
        validation_alias=AliasChoices("primary_map", "map_path"),
        description=(
            "Actual main map to deliver (map_path alias). When a project is included, its "
            "preview must have the same SHA256. Inferred from matching selected maps when omitted."
        ),
    )
    review_path: str = Field(
        default="",
        description="Optional recorded map-review JSON; passed requires the actual image SHA256.",
    )
    name: str = Field(
        default="delivery",
        min_length=1,
        max_length=80,
        pattern=r'^[^\\/:*?"<>|.][^\\/:*?"<>|]*$',
        description="User-facing delivery folder name; existing files are preserved.",
    )


def _source(path: Path, roots: list[Path], *, check_producer=True) -> Path:
    path = path.resolve()
    matched = [root for root in roots if path.is_relative_to(root)]
    if not matched:
        raise ValueError("Delivery inputs must be in the workspace or configured artifact folders.")
    relative = path.relative_to(max(matched, key=lambda root: len(root.parts)))
    if any(
        part.lower()
        in {
            ".env",
            "credentials.json",
            "secrets.json",
            "settings.json",
            "auth.json",
            ".ssh",
            ".aws",
            ".codex",
            ".agents",
            ".openharness",
            ".geoharness",
        }
        or part.lower().startswith(".env.")
        for part in relative.parts
    ) or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}:
        raise ValueError("Credential files cannot be delivered.")
    if not path.is_file():
        raise ValueError(f"Delivery input is not an existing file: {path}")
    from geoharness.data.artifacts import assert_usable_artifact

    if check_producer:
        assert_usable_artifact(path)
    return path


class GeoDeliverTool(BaseTool):
    name = "geo_deliver"
    description = (
        "Copy selected maps, data, reports and an editable map project into workspace exports. "
        "Use this after Python/native analysis; scripts must not write outside RUN_DIR. "
        "Returns the actual user-facing delivery folder and files; preserves prior deliveries."
        " A selected primary map must match the included project's preview. "
        "Quality is passed only with data, layout and visual checks for the same image; "
        "the delivered project must also match its recorded snapshot."
    )
    input_model = GeoDeliverInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoDeliverInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        workspace = context.cwd.resolve()
        directory = Path(config.delivery.outputs_dir).expanduser()
        directory = (directory if directory.is_absolute() else workspace / directory).resolve()
        if not directory.is_relative_to(workspace) or directory == workspace:
            return ToolResult(
                output="Error: delivery.outputs_dir must be a subfolder of workspace.",
                is_error=True,
            )
        roots = [workspace]
        for section in ("analysis", "execution", "cartography", "qgis"):
            path = Path(getattr(config, section).outputs_dir).expanduser()
            roots.append((path if path.is_absolute() else workspace / path).resolve())

        def resolve(value: str) -> Path:
            path = Path(value).expanduser()
            path = path if path.is_absolute() else workspace / path
            try:
                return _source(path, roots)
            except ValueError:
                # A review is produced after the original run manifest. Only
                # its exact hash-bound ledger reference can authorize that file;
                # this does not upgrade its quality or trust arbitrary JSON.
                target = _source(path, roots, check_producer=False)
                if target.suffix.lower() != ".json" or target.stat().st_size > 1024*1024:
                    raise
                from geoharness.cartography.jobs import map_job_receipt

                report = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(report, dict) or not isinstance(report.get("image_path"), str):
                    raise ValueError("Review evidence must be an object with an image_path")
                image = Path(report.get("image_path", ""))
                _source(image if image.is_absolute() else workspace / image, roots)
                receipt = map_job_receipt(context.metadata, cwd=workspace, config=config,
                                         artifact_path=image)
                references = [stage.get("report_reference", {}) for version in
                    receipt.get("versions", []) for stage in [
                        *version.get("stages", {}).values(),
                        *version.get("page_reviews", {}).values()]]
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if not any(reference.get("path") == str(target) and
                           reference.get("sha256") == digest for reference in references):
                    raise ValueError("Review is not an unchanged registered evidence file")
                return target

        def publish() -> dict:
            from geoharness.cartography.jobs import map_job_receipt

            ledger_errors = []

            def read_receipt(*args, **kwargs):
                try:
                    return map_job_receipt(*args, **kwargs)
                except (OSError, ValueError) as exc:
                    if arguments.version_policy != "selected" or not (
                            arguments.primary_map or arguments.files):
                        raise ValueError(f"Ledger unavailable: {exc}. Inspect/recover the map, "
                            "or explicitly use version_policy=selected with actual draft files")
                    ledger_errors.append(str(exc))
                    return {"job_id": None, "map_id": None, "status": "draft",
                            "quality_status": "draft", "versions": [],
                            "stages": {key: {"status": "unverified"} for key in (
                                "data_checked", "rendered", "layout_checked", "visual_checked",
                                "packaged", "delivered")}}

            files = [resolve(value) for value in arguments.files]
            primary = resolve(arguments.primary_map) if arguments.primary_map else None
            project_directory = arguments.project_dir
            candidate_report = None
            selection = {"policy": arguments.version_policy, "changed": False}
            selected_maps = [file for file in files if file.suffix.lower() in _MAP_SUFFIXES]
            selection_source = primary or (selected_maps[0] if len(selected_maps) == 1 else None)
            if selection_source is None and project_directory:
                root = Path(project_directory).expanduser()
                root = (root if root.is_absolute() else workspace / root).resolve()
                document = json.loads(resolve(str(root / "project.json")).read_text(
                    encoding="utf-8"))
                if isinstance(document, dict) and isinstance(document.get("preview"), str):
                    relative = Path(document["preview"])
                    preview = (root / relative).resolve()
                    if relative.is_absolute() or relative.drive or ".." in relative.parts \
                            or not preview.is_relative_to(root):
                        raise ValueError("Project preview reference must stay inside its folder.")
                    selection_source = resolve(str(preview))
            initial_receipt = read_receipt(context.metadata, cwd=workspace, config=config,
                map_id=arguments.map_id, artifact_path=selection_source)
            if initial_receipt.get("job_id") and initial_receipt.get("artifact_registered"):
                selection["selected_version"] = initial_receipt.get("selected_version")
            if (arguments.version_policy == "prefer_checked" and initial_receipt.get("job_id")
                    and initial_receipt.get("artifact_registered", False)):
                stages = initial_receipt["stages"]
                known_problem = any(stages[key]["status"] in {"issues", "stale", "failed"}
                    for key in ("rendered", "layout_checked", "data_checked", "visual_checked"))
                candidate = initial_receipt.get("delivery_candidate")
                if known_problem or (arguments.map_id and selection_source is None):
                    if candidate is None:
                        raise ValueError("Selected map has no unchanged candidate passing measured "
                            "layout checks. Repair it, or explicitly use version_policy=selected "
                            "to retain the requested draft.")
                    primary = resolve(candidate["image_path"])
                    files = [file for file in files if file != selection_source]
                    project_directory = candidate.get("project_dir") or ""
                    candidate_report = candidate.get("review_path")
                    selection.update(changed=primary != selection_source,
                        selected_version=candidate["version"],
                        requested_version=initial_receipt.get("selected_version"),
                        reason=candidate["reason"], project_available=bool(project_directory))
            if primary is None and not files and arguments.map_id and selection_source is not None:
                primary = selection_source
            elif (primary is None and not files and arguments.map_id
                  and arguments.version_policy == "selected"
                  and initial_receipt.get("image_path")):
                primary = resolve(initial_receipt["image_path"])
                packaged = initial_receipt["stages"]["packaged"]
                if not project_directory and packaged["status"] == "passed":
                    project_directory = packaged.get("project_dir") or ""
            if primary is not None and primary.suffix.lower() not in _MAP_SUFFIXES:
                raise ValueError("Primary map must be a PNG/JPEG/WebP/PDF/SVG/HTML map artifact.")
            if primary is not None and primary not in files:
                files.append(primary)
            report_file = (resolve(arguments.review_path)
                           if arguments.review_path and not selection["changed"] else None)
            if selection["changed"] and arguments.review_path:
                old_report = resolve(arguments.review_path)
                files = [file for file in files if file != old_report]
            if selection["changed"] and candidate_report:
                report_file = resolve(candidate_report)
            if report_file is not None and report_file not in files:
                files.append(report_file)
            project_files: list[tuple[Path, Path]] = []
            project_snapshots: dict[Path, str] = {}
            project_digest = None
            project_file = "map.qgs"
            project_fidelity = "unverified"
            if project_directory:
                project_root = Path(project_directory).expanduser()
                project_root = (
                    project_root if project_root.is_absolute() else workspace / project_root
                ).resolve()
                manifest = resolve(str(project_root / "project.json"))
                manifest_bytes = manifest.read_bytes()
                project_snapshots[manifest] = hashlib.sha256(manifest_bytes).hexdigest()
                recorded_project = json.loads(manifest_bytes.decode("utf-8"))
                project_fidelity = (recorded_project.get("project_fidelity", "unverified")
                                    if isinstance(recorded_project, dict) else "unverified")
                if isinstance(recorded_project, dict) and recorded_project.get("backend") == "qgis":
                    project = _QgisDeliveryProject.model_validate(recorded_project)
                    project_file = project.project_file
                    relative_files = {
                        "project.json",
                        project_file,
                        project.preview,
                        *project.files,
                    }
                else:
                    project = MapProject.model_validate(recorded_project)
                    relative_files = {
                        "project.json",
                        "map.qgs",
                        "redraw.py",
                        "README.md",
                        project.preview,
                        *(layer.file for layer in project.layers),
                    }
                preview = (project_root / project.preview).resolve()
                if not preview.is_relative_to(project_root):
                    raise ValueError("Project preview reference must stay inside its folder.")
                preview = resolve(str(preview))
                project_digest = hashlib.sha256(preview.read_bytes()).hexdigest()
                if project.preview_sha256 and project.preview_sha256 != project_digest:
                    raise ValueError(
                        "Project preview SHA256 no longer matches its recorded snapshot."
                    )
                maps = [path for path in files if path.suffix.lower() in _MAP_SUFFIXES]
                if primary is None and maps:
                    matching = [
                        path
                        for path in maps
                        if hashlib.sha256(path.read_bytes()).hexdigest() == project_digest
                    ]
                    if not matching:
                        raise ValueError(
                            "Selected map differs from the project's preview SHA256. "
                            "Create the project from the actual final map; "
                            "no rerender fallback is used."
                        )
                    primary = matching[0]
                if (
                    primary is not None
                    and hashlib.sha256(primary.read_bytes()).hexdigest() != project_digest
                ):
                    raise ValueError("Primary map differs from the project's preview SHA256.")
                for relative in sorted(relative_files):
                    relative_path = Path(relative)
                    if (
                        relative_path.is_absolute()
                        or relative_path.drive
                        or ".." in relative_path.parts
                    ):
                        raise ValueError(
                            "Project references must stay inside its folder as relative paths."
                        )
                    target = (project_root / relative_path).resolve()
                    if not target.is_relative_to(project_root):
                        raise ValueError("Project references must stay inside its folder.")
                    source = resolve(str(target))
                    if source not in project_snapshots:
                        project_snapshots[source] = hashlib.sha256(source.read_bytes()).hexdigest()
                    project_files.append(
                        (
                            source,
                            Path("project") / target.relative_to(project_root),
                        )
                    )
            if not files and not project_files:
                raise ValueError("Select at least one artifact or a portable project.")
            if primary is None:
                primary = next(
                    (file for file in files if file.suffix.lower() in _MAP_SUFFIXES), None
                )
            digest = hashlib.sha256(primary.read_bytes()).hexdigest() if primary else project_digest
            recorded = (
                context.metadata.get("map_workflow_state", {}).get("cache", {}).get(digest)
                if digest
                else None
            )
            if recorded is None and digest:
                candidates = (
                    [report_file]
                    if report_file
                    else [path for path in files if path.suffix.lower() == ".json"]
                )
                for candidate in candidates:
                    try:
                        report = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, ValueError):
                        continue
                    if isinstance(report, dict) and report.get("image_sha256") == digest:
                        recorded = {
                            key: report[key] for key in MapReview.model_fields if key in report
                        }
                        break
            try:
                review = MapReview.model_validate(recorded) if recorded is not None else None
            except ValueError:
                review = None
            review_status = review.status if review else "unverified"
            if (
                review
                and review.status == "passed"
                and any(issue.severity in {"error", "warning"} for issue in review.issues)
            ):
                review_status = "issues"
            if (
                review
                and review.status == "passed"
                and review.layout_patch.model_dump(exclude_none=True)
            ):
                review_status = "issues"
            from geoharness.cartography.tracking import receipt_summary

            receipt = read_receipt(
                context.metadata,
                cwd=workspace,
                config=config,
                artifact_path=primary or (preview if project_files else None),
            )
            visual_stage = receipt.get("stages", {}).get("visual_checked", {})
            if visual_stage.get("scope") == "all_native_map_pages":
                review_status = visual_stage["status"]
            packaged = receipt.get("stages", {}).get("packaged", {})
            package_matches = not project_files or (
                packaged.get("status") == "passed"
                and packaged.get("project_dir") == str(project_root)
            )
            verified = (
                review_status == "passed"
                and receipt.get("quality_status") == "passed"
                and package_matches
                and receipt.get("status") != "stale"
            )
            expected_package = {
                item["path"]: item["sha256"]
                for item in packaged.get("files", [])
                if item.get("sha256")
            }
            # Rehash copied reports too: their sources can change and be restored
            # between the two receipt reads, just like packaged data files.
            for stage in receipt.get("stages", {}).values():
                reference = stage.get("report_reference", {})
                if reference.get("path") and reference.get("sha256"):
                    expected_package[reference["path"]] = reference["sha256"]
            directory.mkdir(parents=True, exist_ok=True)
            destination = Path(tempfile.mkdtemp(prefix=arguments.name + "-", dir=directory))
            copied = []
            delivered_primary = None
            snapshot_changed = False
            copied_package_hashes = {}
            for index, source in enumerate(files):
                target = destination / source.name
                suffix = index
                while target.exists() or target.name.casefold() in _RESERVED_NAMES:
                    target = destination / f"{source.stem}-{suffix}{source.suffix}"
                    suffix += 1
                shutil.copy2(source, target)
                copied.append(str(target))
                expected = expected_package.get(str(source))
                if expected and hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    snapshot_changed = True
                if source == primary:
                    delivered_primary = str(target)
            for source, relative in project_files:
                target = (destination / relative).resolve()
                if not target.is_relative_to(destination):
                    raise ValueError(
                        "Project copy destination must stay inside its delivery folder."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(str(target))
                expected = expected_package.get(str(source), project_snapshots.get(source))
                if expected and hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    snapshot_changed = True
                if expected:
                    copied_package_hashes[str(target)] = expected
            if (
                delivered_primary
                and hashlib.sha256(Path(delivered_primary).read_bytes()).hexdigest() != digest
            ):
                raise ValueError(
                    "Primary map changed while being delivered; retry from one snapshot."
                )
            if project_files:
                copied_preview = destination / "project" / project.preview
                if hashlib.sha256(copied_preview.read_bytes()).hexdigest() != project_digest:
                    raise ValueError("Project preview changed while being delivered.")
                if delivered_primary is None:
                    delivered_primary = str(copied_preview)
            # A file can change and be restored while the copy is running. Check
            # copied package bytes against the original evidence, as well as
            # refreshing the source receipt after the copy.
            receipt = read_receipt(
                context.metadata,
                cwd=workspace,
                config=config,
                artifact_path=primary or (preview if project_files else None),
            )
            verified = (
                verified
                and not snapshot_changed
                and receipt.get("quality_status") == "passed"
                and receipt.get("status") != "stale"
            )
            payload = {
                "delivery_dir": str(destination),
                "files": copied,
                "qgis_project": (
                    str(destination / "project" / project_file) if project_files else None
                ),
                "primary_map": delivered_primary,
                "primary_map_sha256": digest,
                "project_preview_sha256": project_digest,
                "project_fidelity": project_fidelity,
                "selection": selection,
                "project_note": ("Reference QGIS layout differs from the main image."
                                 if project_fidelity == "reference" else ""),
                "quality_status": "passed" if verified else "draft",
                "ledger_errors": list(dict.fromkeys(ledger_errors)),
                "snapshot_status": "changed" if snapshot_changed else "passed",
                "map_id": receipt.get("map_id"),
                "map_receipt": receipt_summary(receipt),
                "review_status": review_status,
                "review_summary": review.summary
                if review
                else ("No visual review verifies the delivered image SHA256."),
                "remaining_issues": (
                    [issue.model_dump() for issue in review.issues] if review else []
                ),
            }
            manifest = destination / "delivery.json"
            if receipt.get("job_id"):
                from geoharness.cartography.jobs import record_map_delivery

                try:
                    receipt = record_map_delivery(
                        receipt["job_id"], context.metadata, cwd=workspace, config=config,
                        image_path=primary or preview, delivery=payload,
                    )
                except (OSError, ValueError) as exc:
                    # Copying finished. Preserve the real destination and manifest
                    # even when registration fails after the initial health check.
                    payload['ledger_errors'].append(str(exc))
                    payload['receipt_status'] = 'failed'
                    payload['quality_status'] = 'draft'
                if receipt.get("status") == "stale" or receipt.get("quality_status") != "passed":
                    payload["quality_status"] = "draft"
                receipt_file = destination / "map-receipt.json"
                receipt_file.write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                payload["map_receipt"] = receipt_summary(receipt)
                payload["receipt_path"] = str(receipt_file)
            from geoharness.cartography.delivery_summary import write_delivery_summary

            payload.update(write_delivery_summary(
                destination, payload, receipt,
                manifest_path=destination / "project/project.json" if project_files else None,
                package_hashes=copied_package_hashes,
            ))
            manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {**payload, "manifest_path": str(manifest)}

        try:
            publication = asyncio.create_task(asyncio.to_thread(publish))
            try:
                result = await asyncio.shield(publication)
            except asyncio.CancelledError:
                # Publication writes files and a receipt. Drain the worker
                # before reporting cancellation, so a following request cannot
                # race an abandoned publisher.
                while not publication.done():
                    try:
                        await asyncio.shield(publication)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                try:
                    publication.result()
                except BaseException:
                    pass
                raise
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: artifact delivery failed: {exc}", is_error=True)
        return ToolResult(output=json.dumps(result, ensure_ascii=False, indent=2), metadata=result)

    def is_read_only(self, arguments: BaseModel) -> bool:
        return False
