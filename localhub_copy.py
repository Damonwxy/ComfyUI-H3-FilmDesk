"""Local, version-specific review of advertising copy and its cited evidence."""

from localhub_storage import RequestConflict


def list_copy(jobs, project):
    with jobs.db() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM ad_copy WHERE project=? ORDER BY name, revision DESC", (project,)
        )]


def _text(data, key, label, limit):
    value = data.get(key)
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{label}须为 1–{limit} 个字符")
    return value.strip()


def update_copy(jobs, project, data, *, review):
    if not isinstance(data, dict):
        raise ValueError("请提交文案记录")
    name = _text(data, "name", "文案名称", 120)
    expected = data.get("expected_revision")
    if type(expected) is not int or not 0 <= expected < 2**63 - 1:
        raise ValueError("需要明确的文案版本")
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM ad_copy WHERE project=? AND name=? ORDER BY revision DESC LIMIT 1",
                         (project, name)).fetchone()
        current = row["revision"] if row else 0
        if review:
            if not row or expected != current:
                raise RequestConflict("文案版本已变化，请重新读取后审核")
            decision = data.get("decision")
            if decision not in ("approved", "returned"):
                raise ValueError("请选择通过或退回")
            reviewer = _text(data, "reviewer", "审核人", 120)
            reason = _text(data, "reason", "审核说明", 1200)
            result = (decision, reviewer, reason)
            if row["decision"] != "pending":
                if result == (row["decision"], row["reviewer"], row["reason"]):
                    return current
                raise RequestConflict("这个版本已有审核结论，请查看记录")
            db.execute("UPDATE ad_copy SET decision=?, reviewer=?, reason=? WHERE project=? AND name=? AND revision=?",
                       (*result, project, name, current))
            return current
        content = (_text(data, "wording", "文案", 2000), _text(data, "scope", "适用范围", 300),
                   _text(data, "evidence", "资料与依据", 2000))
        if row and expected in (current, current - 1) and content == (row["wording"], row["scope"], row["evidence"]):
            return current
        if expected != current:
            raise RequestConflict("同名文案已有新版本，请重新读取后修改")
        db.execute("INSERT INTO ad_copy(project,name,revision,wording,scope,evidence) VALUES(?,?,?,?,?,?)",
                   (project, name, current + 1, *content))
        return current + 1
