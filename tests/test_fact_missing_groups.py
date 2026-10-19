from geoharness.execution.contracts import artifact_facts, write_analysis_report


def test_missing_population_categories_are_measured_not_inferred_from_total(tmp_path):
    path = tmp_path / "facts.csv"
    path.write_text("id,type,pop\n1,住宅区,10\n2,办公楼,4\n3,学校,\n4,博物馆,\n", encoding="utf8")
    report = write_analysis_report(
        tmp_path / "report.json",
        source=path,
        key="id",
        numeric_fields=["pop"],
        missing_by=["type"],
        method={"operation": "observed_values"},
    )
    facts = report["facts"]
    groups = {r["group"]["type"]: r["numeric"]["pop"] for r in facts["missing_by"]}
    assert groups["办公楼"] == {"count": 1, "missing": 0}
    assert groups["学校"] == {"count": 0, "missing": 1}
    assert facts == artifact_facts(
        path, key="id", numeric_fields=["pop"], missing_by=["type"], include_median=True
    )


def test_missing_groups_survive_publication_verification_and_copied_delivery(tmp_path):
    import json
    import shutil
    from pathlib import Path

    import pandas as pd
    from test_result_packages import produced, request

    from geoharness.analysis.result_package import publish_package, verify_package
    from geoharness.cartography.delivery_summary import _attached_analysis
    from geoharness.data.artifacts import sha256_file

    context, source, _ = produced(tmp_path)
    frame = pd.read_csv(source)
    frame["type"] = ["住宅区"] * 135 + ["办公楼"] * 7 + ["学校"] * 85 + ["博物馆"] * 7
    frame.to_csv(source, index=False)
    manifest = source.parent / "manifest.json"
    doc = json.loads(manifest.read_text("utf8"))
    next(f for f in doc["files"] if f["path"] == str(source))["sha256"] = sha256_file(source)
    manifest.write_text(json.dumps(doc), encoding="utf8")
    req = request().model_copy(update={"missing_by": ["type"]})
    package = publish_package(context, source_ref=str(source), request=req)
    assert verify_package(Path(package["package_path"]))["facts"]["missing_by"]
    destination = tmp_path / "exports"
    destination.mkdir()
    copied = [
        str(shutil.copy2(p, destination / p.name)) for p in [source, Path(package["package_path"])]
    ]
    reports = _attached_analysis({"delivery_dir": str(destination), "files": copied})
    assert reports[0]["status"] == "persisted_values_checked"
    groups = {
        r["group"]["type"]: r["numeric"]["population"] for r in reports[0]["facts"]["missing_by"]
    }
    assert groups["办公楼"]["missing"] == 0 and groups["学校"]["missing"] == 85
