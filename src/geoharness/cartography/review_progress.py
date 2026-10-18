"""Track persistent located concerns without treating wording changes as progress."""

import re
from math import isfinite


def _location(issue):
    value = issue.get("location")
    return re.sub(r"[\W_]+", "", value).lower() if isinstance(value, str) else ""


def _box(issue):
    value = issue.get("bbox")
    return value if (isinstance(value, list) and len(value) == 4
        and all(type(v) in (int, float) and isfinite(v) and 0 <= v <= 1 for v in value)
        and value[0] < value[2] and value[1] < value[3]) else None


def _same_concern(left, right):
    if (not left.get("issue_code") or left.get("issue_code") != right.get("issue_code")
            or left.get("frame_id", "") != right.get("frame_id", "")):
        return False
    a, b = left.get("feature_ids"), right.get("feature_ids")
    if (isinstance(a, list) and isinstance(b, list) and a and b
            and all(type(v) is int for v in [*a, *b]) and left.get('source_indices')
            and left.get('source_indices') == right.get('source_indices')):
        return bool(set(a) & set(b))
    a, b = _box(left), _box(right)
    if a and b and left.get("roi_ref", "") == right.get("roi_ref", ""):
        intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
            0, min(a[3], b[3]) - max(a[1], b[1]))
        union = ((a[2] - a[0]) * (a[3] - a[1])
                 + (b[2] - b[0]) * (b[3] - b[1]) - intersection)
        return bool(union and intersection / union >= .6)
    return bool(_location(left) and _location(left) == _location(right))


def _issues(review):
    return [i for i in review.get("issues", []) if isinstance(i, dict)
            and i.get("severity") in {"warning", "error"}]


def _page(review):
    diagnostics = review.get('diagnostics')
    return review.get("page_index", diagnostics.get("review_page_index")
                      if isinstance(diagnostics, dict) else None)


def assess_repair_progress(history: list, current: dict) -> dict:
    page = _page(current)
    previous = []
    seen = {current.get("image_sha256")}
    for review in reversed(history):
        if (not isinstance(review, dict) or not review.get("image_sha256")
                or review["image_sha256"] in seen
                or _page(review) != page):
            continue
        if review.get('status') != 'issues':
            break  # A passed/unverified intervening version cannot prove persistence.
        previous.append(review)
        seen.add(review["image_sha256"])
        if len(previous) == 2:
            break
    now = _issues(current) if current.get("status") == "issues" else []
    severity = {"warning": 1, "error": 2}
    persistent = [issue for issue in now if len(previous) == 2 and all(
        any(_same_concern(issue, old) and severity[issue["severity"]] >= severity[old["severity"]]
            for old in _issues(review)) for review in previous)]
    resolved = sum(not any(_same_concern(old, issue) for issue in now)
                   for old in _issues(previous[0])) if previous and current.get("status") in {
                       "passed", "issues"} else 0
    repeated = bool(persistent and current.get("image_sha256"))
    return {"state": "persistent_concerns" if repeated else "not_established",
            "requires_user_decision": False,
            "repeated_concern_count": len(persistent), "resolved_concern_count": resolved,
            "concerns": [{key: issue.get(key) for key in
                ("issue_code", "frame_id", "roi_ref", "location", "bbox", "feature_ids")}
                for issue in persistent],
            "scope": "same_page_located_concerns; no_independent_pixel_verification",
            "next_action": "change_repair_method" if repeated else "verify_findings_before_repair",
            "message": ("不同版本仍报告同类可定位问题，换措辞不算改善。先核实原因并更换方法，"
                        "不要重复相同改动；只有涉及业务或表达取舍时调用ask_user_question。"
                        if repeated else "核实读图疑点并合并有依据的修改；"
                        "不猜测、不按轮数强制停止。")}
