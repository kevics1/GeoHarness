"""Nearest measurements and attribute attachment checked against actual geometry."""

from __future__ import annotations

import uuid


def _contracts():
    import sys

    if '_geoharness_execution_contracts' in sys.modules:
        return sys.modules['_geoharness_execution_contracts']
    from geoharness.execution import contracts

    return contracts


def nearest_distance(source, reference, *, key, field, metric_crs):
    """Retain entity identity and original CRS/index; measure in explicit metres."""
    import pandas as pd

    contracts = _contracts()
    contracts.require_metric(metric_crs)
    contracts.validate_vector(source)
    contracts.validate_vector(reference)
    if key not in source or source[key].isna().any() or not source[key].is_unique:
        raise ValueError('nearest_distance要求唯一非空的实体key')
    if field in source or field == key:
        raise ValueError('nearest_distance的field必须是新的距离字段')
    distances = _distances(source.to_crs(metric_crs).geometry,
                           reference.to_crs(metric_crs).geometry)
    table = pd.DataFrame({key: source[key].to_numpy(), field: distances})
    result = contracts.entity_join(source, table, key=key)
    result.index = source.index.copy()
    return result


def _distances(geometries, reference, *, max_distance=None, exclusive=False):
    import numpy as np
    from shapely import STRtree

    if not len(reference) or reference.isna().any() or reference.is_empty.any():
        raise ValueError('最近距离参照几何不能为空；缺失政策需要显式处理')
    if geometries.isna().any() or geometries.is_empty.any():
        raise ValueError('最近距离的实体几何不能为空')
    indices, values = STRtree(reference.to_numpy()).query_nearest(
        geometries.to_numpy(), return_distance=True, all_matches=False,
        max_distance=max_distance, exclusive=exclusive)
    distances = np.full(len(geometries), np.nan)
    distances[indices[0]] = values
    return distances


def verify_nearest_distances(source, reference, *, key, field, metric_crs,
                             absolute_tolerance=0.11):
    """Independently recompute each persisted distance, with bounded ID diagnostics."""
    import math

    import numpy as np
    import pandas as pd

    contracts = _contracts()
    contracts.require_metric(metric_crs)
    contracts.validate_vector(source)
    contracts.validate_vector(reference)
    if key not in source or source[key].isna().any() or not source[key].is_unique:
        raise ValueError('距离复核要求唯一非空实体key')
    if field not in source or not math.isfinite(absolute_tolerance) or absolute_tolerance < 0:
        raise ValueError('距离复核要求真实field和有限非负absolute_tolerance')
    expected = _distances(source.to_crs(metric_crs).geometry,
                          reference.to_crs(metric_crs).geometry)
    actual = pd.to_numeric(source[field], errors='raise').to_numpy(dtype=float, na_value=np.nan)
    matched = np.isclose(actual, expected, atol=absolute_tolerance, rtol=0, equal_nan=False)
    bad = np.flatnonzero(~matched)
    samples = [{'key': str(source.iloc[i][key]),
                'stored': float(actual[i]) if np.isfinite(actual[i]) else None,
                'expected': float(expected[i])} for i in bad[:5]]
    return {'status': 'failed' if len(bad) else 'passed', 'field': field,
            'metric_crs': str(metric_crs), 'rows': len(source),
            'mismatches': len(bad), 'missing': int(np.isnan(actual).sum()),
            'absolute_tolerance': absolute_tolerance, 'samples': samples}


class NearestBindings:
    """Observe actual joins; reject index-aligned attachment to the wrong geometry.

    Pandas propagates the small token through drop_duplicates/set_index and Series
    selection. No monkeypatch of general pandas joins or implicit key inference.
    """

    token = '_geoharness_nearest_binding'

    def __init__(self):
        self.observations = {}
        self.checks = []

    def record(self, joined):
        identity = uuid.uuid4().hex
        # Retain actual joined geometry, including ties; attribute candidates at
        # coincident points are all valid until an explicit tie policy is given.
        snapshot = joined.copy(deep=True)
        snapshot.attrs = {}
        self.observations[identity] = snapshot
        joined.attrs[self.token] = identity
        return joined

    def check_assignment(self, target, field, value):
        import numpy as np
        import pandas as pd
        from shapely import normalize, set_precision, to_wkb

        if not isinstance(value, pd.Series) or not hasattr(target, 'geometry'):
            return
        original = self.observations.get(value.attrs.get(self.token))
        if original is None or value.name not in original or target.crs is None:
            return
        if not value.index.is_unique or not target.index.is_unique:
            return  # pandas retains its normal duplicate-index validation.
        aligned = original.to_crs(target.crs)
        precision = 1e-8 if target.crs.is_geographic else 1e-4

        def signature(geom):
            return to_wkb(normalize(set_precision(geom, precision)))

        expected = {}
        for geom, item in zip(aligned.geometry, aligned[value.name]):
            expected.setdefault(signature(geom), []).append(item)
        actual = value.reindex(target.index)
        index_key = value.index.name
        entity_keys = (target[index_key] if index_key in target else target.index
                       if index_key and target.index.name == index_key else None)
        keyed = index_key in original and entity_keys is not None
        indexed = original.index.is_unique and value.index.equals(original.index)
        checked = 0
        for row, (geom, item) in enumerate(zip(target.geometry, actual)):
            candidates = expected.get(signature(geom))
            if candidates is None:
                continue  # Deliberately changed geometry cannot establish identity.

            def equal(candidate):
                if pd.isna(item) or pd.isna(candidate):
                    return bool(pd.isna(item) and pd.isna(candidate))
                if isinstance(item, (int, float, np.number)) and isinstance(
                        candidate, (int, float, np.number)):
                    return bool(np.isclose(item, candidate, atol=1e-6, rtol=1e-9))
                return bool(item == candidate)

            if keyed:
                # Check lineage rather than numerical equality, so legitimate
                # unit conversions/rounding of a correctly keyed Series work.
                matched = target.index[row] == entity_keys[row] if not isinstance(
                    entity_keys, pd.Series) else target.index[row] == entity_keys.iloc[row]
            elif indexed and target.index[row] in aligned.index:
                matched = signature(aligned.loc[target.index[row]].geometry) == signature(geom)
            else:
                # Changed/unknown lineage can only be checked for an unmodified
                # joined attribute. Arbitrary derived Series require explicit
                # verification, rather than guessing their transformation.
                if not value.dropna().isin(original[value.name]).all():
                    continue
                matched = any(equal(candidate) for candidate in candidates)
            if not matched:
                raise ValueError(f'Nearest entity alignment mismatch: field={field!r}, '
                    f'row={row}. 最近邻结果的业务ID与目标行索引不一致；'
                    '用entity_join(source, table.reset_index(), key=真实ID)关联，'
                    '或用nearest_distance(source, reference, key=..., field=..., metric_crs=...)。'
                    '不要把set_index(业务ID)后的Series直接赋给RangeIndex。')
            checked += 1
        if checked:
            self.checks.append({'field': str(field), 'rows': checked,
                                'status': 'geometry_attachment_checked'})
