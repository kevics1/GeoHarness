import hashlib
import json
from collections import Counter
from types import SimpleNamespace

from geoharness.config.settings import GeoConfig
from geoharness.task_controller import TaskNode, _hash, _valid_artifacts, plan_status
from geoharness.task_state import begin_task
from geoharness.task_store import connect, task_directory


def test_target_validation_ignores_unrelated_history_and_deduplicates_within_pass(
    tmp_path, monkeypatch
):
    m = {"geoharness_config": GeoConfig()}
    begin_task(m, cwd=tmp_path, goal="校验长历史")
    context = SimpleNamespace(metadata=m, cwd=tmp_path)
    root = task_directory(m, cwd=tmp_path)
    monkeypatch.setattr("geoharness.task_controller._environment", lambda n, c: "env")
    source = tmp_path / "source.txt"
    source.write_text("real original input")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    nodes = [
        TaskNode(id="parent", tool="geo_python"),
        TaskNode(id="target", tool="geo_python", depends_on=["parent"]),
    ]
    nodes += [TaskNode(id="unrelated" + str(i), tool="geo_python") for i in range(100)]
    fingerprints = {}
    with connect(root) as db:
        for n in nodes:
            path = source if n.id in {"parent", "target"} else tmp_path / (n.id + ".txt")
            path.write_text("real original input")
            inputs = {"input": {"path": str(path), "sha256": sha}}
            deps = {d: fingerprints[d] for d in n.depends_on}
            fp = _hash([n.model_dump(mode="json"), "env", inputs, deps])
            fingerprints[n.id] = fp
            db.execute(
                "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    n.id,
                    n.model_dump_json(),
                    fp,
                    "succeeded",
                    "op",
                    None,
                    json.dumps({str(path): sha}),
                    json.dumps({"environment": "env", "inputs": inputs}),
                    None,
                ),
            )
    reads = Counter()

    def measured(path):
        reads[str(path)] += 1
        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr("geoharness.task_controller.sha256_file", measured)
    result = plan_status(context, node_id="target")
    assert result["complete"] and len(result["nodes"]) == 2
    assert sum(reads.values()) == 1
    source.write_text("changed original input")
    assert not plan_status(context, node_id="target")["complete"]
    assert sum(reads.values()) == 2  # A new pass must re-read real bytes.
    reads.clear()
    full = plan_status(context)
    assert len(full["nodes"]) == 102 and len(reads) == 101


def test_validation_cache_cannot_cross_an_execution_boundary(tmp_path):
    path = tmp_path / "artifact.txt"
    path.write_text("first")
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    assert _valid_artifacts({str(path): original})
    path.write_text("other")
    assert not _valid_artifacts({str(path): original})
