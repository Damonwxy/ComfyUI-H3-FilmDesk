"""Keep separate subtitle versions for an immutable saved delivery."""

import json
from fractions import Fraction

from localhub_copy import list_copy
from localhub_storage import RequestConflict
from localhub_timeline import cut_duration, require_delivery


def _delivery_context(jobs, project, delivery):
    timeline = require_delivery(jobs, project, delivery)
    rate = Fraction(timeline["rate"])
    key = json.dumps(delivery, ensure_ascii=False, sort_keys=True)
    return key, {"rate": str(rate), "frames": int(cut_duration(timeline) * rate)}


def _latest(db, project, key):
    return db.execute(
        "SELECT revision,cues FROM delivery_subtitles WHERE project=? AND delivery=? ORDER BY revision DESC LIMIT 1",
        (project, key),
    ).fetchone()


def read_subtitles(jobs, project, delivery, revision=None):
    if revision is not None and (type(revision) is not int or not 1 <= revision < 2**63):
        raise ValueError("请选择已保存的字幕版号")
    key, timing = _delivery_context(jobs, project, delivery)
    row = latest = None
    with jobs.db() as db:
        db.execute("BEGIN")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_subtitles'").fetchone():
            row = latest = _latest(db, project, key)
            if revision is not None and (row is None or row["revision"] != revision):
                row = db.execute("SELECT revision,cues FROM delivery_subtitles WHERE project=? AND delivery=? AND revision=?",
                                 (project, key, revision)).fetchone()
    if revision is not None and row is None:
        raise LookupError("该交付没有这个字幕版本")
    return {**timing, "revision": row["revision"] if row else 0,
            "latest_revision": latest["revision"] if latest else 0,
            "cues": json.loads(row["cues"]) if row else []}


def save_subtitles(jobs, project, delivery, expected, cues):
    if type(expected) is not int or not 0 <= expected < 2**63 - 1:
        raise ValueError("需要明确的字幕基准版号")
    if not isinstance(cues, list) or len(cues) > 500:
        raise ValueError("字幕须为列表，每个版本最多 500 条")
    key, timing = _delivery_context(jobs, project, delivery)
    approved = {(r["name"], r["revision"]): r["wording"] for r in list_copy(jobs, project)
                if r["decision"] == "approved"} if cues else {}
    values = []
    previous_end = 0
    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, dict):
            raise ValueError(f"字幕第 {index} 条须为对象")
        start, end = cue.get("start_frame"), cue.get("end_frame")
        if not (type(start) is int and type(end) is int and previous_end <= start < end <= timing["frames"]):
            raise ValueError(f"字幕第 {index} 条须按时间排列，区间不得交叠或超出剪辑")
        name, revision = cue.get("name"), cue.get("revision")
        text = approved.get((name, revision)) if isinstance(name, str) and type(revision) is int else None
        if text is None:
            raise ValueError(f"字幕第 {index} 条须选用当前项目已审核通过的文案版本")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if any(not line.strip() for line in text.split("\n")) or "<" in text or ">" in text:
            raise ValueError(f"字幕第 {index} 条文案含有空行或尖括号，请审核适合字幕的文字版本")
        values.append({"start_frame": start, "end_frame": end, "name": name, "revision": revision, "text": text})
        previous_end = end
    data = json.dumps(values, ensure_ascii=False)
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS delivery_subtitles (
                project TEXT NOT NULL, delivery TEXT NOT NULL, revision INTEGER NOT NULL, cues TEXT NOT NULL,
                PRIMARY KEY(project, delivery, revision)
            )
        """)
        row = _latest(db, project, key)
        current = row["revision"] if row else 0
        if row and expected in (current, current - 1) and row["cues"] == data:
            return current
        if expected != current:
            raise RequestConflict("该交付已有新字幕版本，请重新读取后再保存")
        db.execute("INSERT INTO delivery_subtitles VALUES (?,?,?,?)", (project, key, current + 1, data))
    return current + 1


def subtitle_srt(sheet):
    if not sheet["cues"]:
        raise ValueError("这个字幕版本没有字幕条目")
    rate = Fraction(sheet["rate"])

    def timestamp(frame):
        total_ms = round(Fraction(frame * 1000) / rate)
        seconds, ms = divmod(total_ms, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"

    return "\n".join(
        f'{i}\n{timestamp(cue["start_frame"])} --> {timestamp(cue["end_frame"])}\n{cue["text"]}\n'
        for i, cue in enumerate(sheet["cues"], 1)
    )
