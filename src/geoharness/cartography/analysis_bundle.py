"""Follow declared, hash-checked producer relations; never guess report filenames."""

import json
from pathlib import Path

from geoharness.data.artifacts import (
    artifact_entries,
    assert_usable_artifact,
    sha256_file,
)


def complete_analysis_bundle(files, resolve):
    """Expand already-resolved selections; their publication checks belong to resolve.

    Registered QGIS review records can be valid selections without being listed
    in the ancestor render manifest. They are not Python report-bundle inputs.
    """
    result = list(dict.fromkeys(files))
    selected = set(result)
    manifests = {}
    for path in selected:
        try:
            record = assert_usable_artifact(path)
        except ValueError:
            # The caller has already verified the exact registered selection.
            # An optional relation lookup must not reject a valid review record.
            continue
        if record and record.get('producer') == 'geo_python':
            manifest = resolve(record['manifest'])
            manifests[manifest] = json.loads(manifest.read_text(encoding='utf-8'))
    added = []
    for manifest, producer in manifests.items():
        entries = artifact_entries(producer)
        published = {}
        for value, _, entry in entries[:1000]:
            path = Path(value)
            path = (path if path.is_absolute() else manifest.parent / path).absolute()
            if (path.resolve() == path and path.is_relative_to(manifest.parent)
                    and entry.get('sha256')):
                published[path] = entry
        for path, entry in published.items():
            if path.suffix.lower() != '.json' or not path.is_file() or path.stat().st_size > 262144:
                continue
            if sha256_file(path) != entry['sha256']:
                continue
            try:
                report = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(report, dict) or report.get('kind') != 'persisted_analysis_report':
                continue
            facts = report.get('facts')
            companions = report.get('companions') or {}
            if (not isinstance(facts, dict) or not isinstance(facts.get('source'), str)
                    or not isinstance(facts.get('sha256'), str)
                    or not isinstance(companions, dict)):
                continue
            source = Path(facts['source']).absolute()
            markdown = companions.get('markdown') or {}
            if not isinstance(markdown, dict):
                continue
            related = {path, source}
            if isinstance(markdown.get('path'), str):
                related.add(Path(markdown['path']).absolute())
            if not selected & related:
                continue
            candidates = [resolve(str(path))]
            if source not in selected:
                source = resolve(str(source))
                # Auto-attach a produced result, not arbitrary original data named by JSON.
                record = assert_usable_artifact(source)
                if not record or record.get('sha256') != facts.get('sha256'):
                    continue
                candidates.append(source)
            if sha256_file(source) != facts.get('sha256'):
                raise ValueError('Analysis report source changed; regenerate its facts')
            if isinstance(markdown.get('path'), str):
                md = Path(markdown['path']).absolute()
                if md in published and published[md]['sha256'] == markdown.get('sha256'):
                    md = resolve(str(md))
                    if sha256_file(md) != markdown['sha256']:
                        raise ValueError('Analysis report Markdown companion changed')
                    candidates.append(md)
            for candidate in candidates:
                if candidate not in result:
                    if len(result) >= 100:
                        raise ValueError('Analysis bundle exceeds delivery file limit (100)')
                    result.append(candidate)
                    added.append(str(candidate))
    return result, added
