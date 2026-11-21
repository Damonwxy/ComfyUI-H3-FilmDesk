"""Persist review notes against the delivery and the bytes of its reviewed master."""

import base64
import json

import ffmpeg_tools
from localhub_delivery import verify_master
from localhub_media import crop_frame_png, file_stamp, media_sha256, normalized_region, video_frame_png
from localhub_storage import RequestConflict
from localhub_timeline import list_deliveries, require_delivery


def _followups(db, project, binding=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='master_note_events'").fetchone():
        return []
    sql = "SELECT source,note,version,event,recorded_at FROM master_note_events WHERE project=?"
    args = [project]
    if binding is not None:
        sql += " AND source=?"
        args.append(binding)
    return [{"source_binding": r["source"], "annotation": json.loads(r["note"]),
             "version": r["version"], "recorded_at": r["recorded_at"], **json.loads(r["event"])}
            for r in db.execute(sql + " ORDER BY source,note,version", args)]


def list_master_reviews(jobs, project):
    """Read recorded opinions even if the reviewed file is offline or has changed."""
    with jobs.db() as db:
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scopes = {}
        for table, region in (("frame_notes", "NULL"), ("frame_region_notes", "n.region")):
            if table not in tables or "jobs" not in tables:
                continue
            rows = db.execute(f"""SELECT n.stamp,n.frame,n.note,{region} AS region
                FROM jobs j JOIN {table} n ON n.job_id=j.id
                WHERE j.project=? AND substr(n.stamp,1,1)='{{' ORDER BY n.frame,n.rowid""", (project,))
            for row in rows:
                binding = row["stamp"]
                if binding not in scopes:
                    scope = json.loads(binding)
                    scopes[binding] = {**scope, "binding": binding, "notes": []}
                item = {"frame": row["frame"], "note": row["note"]}
                if row["region"]:
                    item["region"] = json.loads(row["region"])
                scopes[binding]["notes"].append(item)
        for event in _followups(db, project):
            scopes[event["source_binding"]].setdefault("followups", []).append(event)
    reviews = sorted(scopes.values(), key=lambda r: (-r["delivery"]["revision"], r["binding"]))
    for review in reviews:
        review["notes"].sort(key=lambda n: n["frame"])
    return {"project": project, "deliveries": list_deliveries(jobs, project) if "deliveries" in tables else [],
            "reviews": reviews}


def master_review(jobs, project, spec, action):
    if not isinstance(spec, dict):
        raise ValueError("请指定交付、母版和审片版式")
    delivery = spec.get("delivery")
    cut = require_delivery(jobs, project, delivery)
    placement = spec.get("placement", "original")
    if placement != "original" and not (placement == "youtube-vertical-2026-11-20" and delivery["aspect"] == "9:16"):
        raise ValueError("该参考版式不适用于当前交付")
    path, stamp, info = verify_master(jobs, project, cut, delivery["aspect"], spec.get("master"),
                                      None, ffmpeg_tools.locate_ffprobe())
    master = {"job_id": info["job_id"], "file_index": info["file_index"], "sha256": media_sha256(path)}
    scope = {"project": project, "delivery": delivery, "master": master, "placement": placement}
    binding = json.dumps(scope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if action != "read" and spec.get("binding") != binding:
        raise RequestConflict("母版或交付版式已变，请重读原帧后继续")
    result = {**scope, "filename": path.name, "frames": info["frames"], "fps": info["fps"], "binding": binding}
    if action == "frame":
        frame = spec.get("frame")
        if type(frame) is not int or not 0 <= frame < info["frames"]:
            raise ValueError("帧号超出成片范围")
        region = normalized_region(spec["region"]) if "region" in spec else None
        ffmpeg = ffmpeg_tools.locate_ffmpeg()
        raw = video_frame_png(path, frame, ffmpeg)
        if region is not None:
            raw, bounds, size = crop_frame_png(raw, region, ffmpeg)
            result.update(region=region, pixel_bounds=bounds, frame_size=size)
        result.update(frame=frame, png=base64.b64encode(raw).decode("ascii"))
    if file_stamp(path) != stamp:
        raise RequestConflict("核对时母版文件已变动，请重新打开")
    if action != "frame":
        new = None
        if action == "save":
            note = spec.get("annotation")
            if not isinstance(note, dict):
                raise ValueError("请提交母版帧意见")
            new = {**note, "stamp": binding}
        result["notes"] = jobs.frame_notes(master["job_id"], master["file_index"], binding, info["frames"], new)
        with jobs.db() as db:
            result["followups"] = _followups(db, project, binding)
    return result


def _scope(binding, project):
    if not isinstance(binding, str):
        raise ValueError("请选择已核对的母版")
    value = json.loads(binding)
    if (not isinstance(value, dict) or value.get("project") != project
            or not isinstance(value.get("master"), dict) or not isinstance(value.get("delivery"), dict)):
        raise ValueError("请选择当前项目的母版回执")
    return value


def record_followup(jobs, project, payload):
    if not isinstance(payload, dict):
        raise ValueError("请提交意见、修订母版和复核状态")
    source = payload.get("source_binding")
    original = _scope(source, project)
    target = payload.get("target_binding")
    revised = _scope(target, project)
    state = payload.get("state")
    expected = payload.get("expected_version")
    if state not in ("submitted", "accepted", "changes_requested") or type(expected) is not int or not 0 <= expected < 2**63-1:
        raise ValueError("复核状态或记录版本无效")
    source_master = original["master"]
    if (original.get("delivery", {}).get("revision") == revised.get("delivery", {}).get("revision")
            and source_master.get("sha256") == revised["master"].get("sha256")):
        raise ValueError("请选择修订后的母版或剪版")
    job_id, index = source_master.get("job_id"), source_master.get("file_index")
    if not isinstance(job_id, str) or type(index) is not int or index < 0:
        raise ValueError("原意见的母版回执无效")
    row = jobs.find(job_id)
    if row is None or row["project"] != project:
        raise LookupError("找不到原意见所在的母版")
    notes = jobs.frame_notes(job_id, index, source, None)
    note = next((n for n in notes if n == payload.get("annotation")), None)
    if note is None:
        raise LookupError("找不到该帧和选区的原始意见")
    review = master_review(jobs, project, revised, "read")
    if review["binding"] != target:
        raise RequestConflict("修订母版已变，请重新看过对应帧后再记录")
    frame = payload.get("target_frame")
    if type(frame) is not int or not 0 <= frame < review["frames"]:
        raise ValueError("修订帧号超出成片范围")
    comment, reviewer = payload.get("comment"), payload.get("reviewer")
    if not isinstance(comment, str) or not 1 <= len(comment.strip()) <= 1000:
        raise ValueError("请用 1–1000 字记录本次修改或复核意见")
    event = {"state": state, "target_binding": target, "target_frame": frame, "comment": comment.strip()}
    if state != "submitted":
        if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 120:
            raise ValueError("请填写实际复看者的称呼")
        event["reviewer"] = reviewer.strip()
    key = (project, source, json.dumps(note, sort_keys=True, ensure_ascii=False))
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS master_note_events (
            project TEXT NOT NULL, source TEXT NOT NULL, note TEXT NOT NULL, version INTEGER NOT NULL,
            event TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(project,source,note,version))""")
        last = db.execute("""SELECT version,event FROM master_note_events WHERE project=? AND source=? AND note=?
                             ORDER BY version DESC LIMIT 1""", key).fetchone()
        version, previous = (last["version"], json.loads(last["event"])) if last else (0, {})
        if version == expected + 1 and previous == event:
            return {"version": version, **event}
        if version != expected:
            raise RequestConflict("该意见已有新的处理记录，请重读后再提交")
        if state != "submitted" and (previous.get("state") != "submitted" or previous["target_binding"] != target
                                     or previous["target_frame"] != frame):
            raise RequestConflict("请先找到对应修订母版和帧的待复看记录")
        db.execute("INSERT INTO master_note_events(project,source,note,version,event) VALUES (?,?,?,?,?)",
                   (*key, version + 1, json.dumps(event, ensure_ascii=False)))
    return {"version": version + 1, **event}
