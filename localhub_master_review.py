"""Persist review notes against the delivery and the bytes of its reviewed master."""

import base64
import json
import subprocess
from fractions import Fraction

import ffmpeg_tools
from localhub_delivery import verify_master
from localhub_media import crop_frame_png, file_stamp, media_sha256, normalized_region, video_frame_png
from localhub_storage import RequestConflict
from localhub_timeline import list_deliveries, require_delivery
from localhub_references import resolve_reference


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


def _artwork_history(db, project, binding=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='master_note_artwork' AND type='table'").fetchone():
        return []
    sql = "SELECT source,note,version,artwork,recorded_at FROM master_note_artwork WHERE project=?"
    args = [project]
    if binding is not None:
        sql += " AND source=?"
        args.append(binding)
    return [{"source_binding": r["source"], "annotation": json.loads(r["note"]), "version": r["version"],
             "recorded_at": r["recorded_at"], **json.loads(r["artwork"])}
            for r in db.execute(sql + " ORDER BY source,note,version", args)]


def _range_notes(db, project, binding=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='master_range_notes'").fetchone():
        return []
    sql = "SELECT binding,frame,end_frame,note FROM master_range_notes WHERE project=?"
    args = [project]
    if binding is not None:
        sql += " AND binding=?"
        args.append(binding)
    return db.execute(sql + " ORDER BY frame,end_frame,note", args).fetchall()


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
        for row in _range_notes(db, project):
            binding = row["binding"]
            scope = scopes.setdefault(binding, {**json.loads(binding), "binding": binding, "notes": []})
            scope["notes"].append({k: row[k] for k in ("frame", "end_frame", "note")})
        for event in _followups(db, project):
            scopes[event["source_binding"]].setdefault("followups", []).append(event)
        for record in _artwork_history(db, project):
            scopes[record["source_binding"]].setdefault("artwork_history", []).append(record)
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
    if action == "motion":
        start, end = spec.get("frame"), spec.get("end_frame")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= info["frames"] or end-start > 96:
            raise ValueError("请选择母版内连续的 1–96 帧，结束帧不包含在内")
        def window(first, stop):
            return (f"trim=start_frame={first}:end_frame={stop},setpts=PTS-STARTPTS,"
                    "scale=320:320:force_original_aspect_ratio=decrease,pad=320:320:(ow-iw)/2:(oh-ih)/2,setsar=1")
        def source(filename):
            return ["-protocol_whitelist", "file", "-format_whitelist", "mov", "-i", str(filename)]
        inputs = source(path)
        filters = "[0:V:0]" + window(start, end) + "[out]"
        compare = spec.get("compare")
        if compare is not None:
            if not isinstance(compare, dict):
                raise ValueError("请选择用于对照的母版与起帧")
            scope_b = _scope(compare.get("binding"), project)
            cut_b = require_delivery(jobs, project, scope_b["delivery"])
            path_b, stamp_b, info_b = verify_master(jobs, project, cut_b, scope_b["delivery"]["aspect"],
                                                    scope_b["master"], None, ffmpeg_tools.locate_ffprobe())
            if media_sha256(path_b) != scope_b["master"].get("sha256"):
                raise RequestConflict("对照母版已变动")
            duration = Fraction(end-start) / Fraction(str(info["fps"]))
            count_b = max(1, round(duration * Fraction(str(info_b["fps"]))))
            first_b = compare.get("frame")
            if type(first_b) is not int or not 0 <= first_b <= info_b["frames"]-count_b:
                raise ValueError("对照帧段超出母版范围")
            inputs += source(path_b)
            filters = ("[0:V:0]" + window(start, end) + "[a];[1:V:0]" + window(first_b, first_b+count_b)
                       + f",fps={info['fps']}[b];[a][b]hstack=shortest=1[out]")
        try:
            proc = subprocess.run([ffmpeg_tools.locate_ffmpeg(), "-nostdin", "-v", "error", *inputs,
                                   "-filter_complex", filters, "-map", "[out]", "-an", "-loop", "0",
                                   "-f", "gif", "pipe:1"], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("运动预览解码超时") from exc
        if compare is not None and file_stamp(path_b) != stamp_b:
            raise RequestConflict("解码期间对照母版已变动")
        if proc.returncode or not proc.stdout.startswith(b"GIF"):
            raise ValueError("母版运动片段解码失败")
        result.update(frame=start, end_frame=end, gif=base64.b64encode(proc.stdout).decode("ascii"))
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
    if action not in ("frame", "motion"):
        new = None
        if action == "save":
            note = spec.get("annotation")
            if not isinstance(note, dict):
                raise ValueError("请提交母版帧意见")
            if "end_frame" in note:
                frame, end, text = note.get("frame"), note["end_frame"], note.get("note")
                if (type(frame) is not int or type(end) is not int or not 0 <= frame < end <= info["frames"]
                        or not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000 or "region" in note):
                    raise ValueError("请选择母版内的帧段并填写 1–1000 字的意见")
                with jobs.db() as db:
                    db.execute("""CREATE TABLE IF NOT EXISTS master_range_notes (
                        project TEXT NOT NULL,binding TEXT NOT NULL,frame INTEGER NOT NULL,end_frame INTEGER NOT NULL,
                        note TEXT NOT NULL,PRIMARY KEY(project,binding,frame,end_frame,note))""")
                    db.execute("INSERT OR IGNORE INTO master_range_notes VALUES (?,?,?,?,?)",
                               (project, binding, frame, end, text.strip()))
            else:
                new = {**note, "stamp": binding}
        result["notes"] = jobs.frame_notes(master["job_id"], master["file_index"], binding, info["frames"], new)
        with jobs.db() as db:
            db.execute("BEGIN")
            result["notes"].extend({k: row[k] for k in ("frame", "end_frame", "note")}
                                   for row in _range_notes(db, project, binding))
            result["notes"].sort(key=lambda n: n["frame"])
            result["followups"] = _followups(db, project, binding)
            result["artwork_history"] = _artwork_history(db, project, binding)
    return result


def _scope(binding, project):
    if not isinstance(binding, str):
        raise ValueError("请选择已核对的母版")
    value = json.loads(binding)
    if (not isinstance(value, dict) or value.get("project") != project
            or not isinstance(value.get("master"), dict) or not isinstance(value.get("delivery"), dict)):
        raise ValueError("请选择当前项目的母版回执")
    return value


def _note_key(jobs, project, source, annotation):
    original = _scope(source, project)
    master = original["master"]
    job_id, index = master.get("job_id"), master.get("file_index")
    if not isinstance(job_id, str) or type(index) is not int or index < 0:
        raise ValueError("原意见的母版回执无效")
    row = jobs.find(job_id)
    if row is None or row["project"] != project:
        raise LookupError("找不到原意见所在的母版")
    notes = jobs.frame_notes(job_id, index, source, None)
    with jobs.db() as db:
        notes.extend({k: row[k] for k in ("frame", "end_frame", "note")} for row in _range_notes(db, project, source))
    note = next((n for n in notes if n == annotation), None)
    if note is None:
        raise LookupError("找不到该帧和选区的原始意见")
    return project, source, json.dumps(note, sort_keys=True, ensure_ascii=False)


def link_artwork(jobs, root, project, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("reference"), dict):
        raise ValueError("请选择已登记的字稿版本")
    key = _note_key(jobs, project, payload.get("source_binding"), payload.get("annotation"))
    expected = payload.get("expected_version")
    if type(expected) is not int or not 0 <= expected < 2**63-1:
        raise ValueError("请提供原意见当前的字稿关联版本")
    _, ref = resolve_reference(jobs, root, project, payload["reference"])
    artwork = {"reference": {k: ref[k] for k in ("asset_id", "revision", "sha256", "source")}}
    for field, label, limit in (("approved_by", "核准人", 120), ("approval_record", "核准依据", 1000)):
        value = payload.get(field)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
            raise ValueError(f"请填写{label}，最多 {limit} 字")
        artwork[field] = value.strip()
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS master_note_artwork (
            project TEXT NOT NULL, source TEXT NOT NULL, note TEXT NOT NULL, version INTEGER NOT NULL,
            artwork TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(project,source,note,version))""")
        last = db.execute("""SELECT version,artwork FROM master_note_artwork WHERE project=? AND source=? AND note=?
                             ORDER BY version DESC LIMIT 1""", key).fetchone()
        version = last["version"] if last else 0
        if last and expected in (version - 1, version) and json.loads(last["artwork"]) == artwork:
            return {"version": version, **artwork}
        if expected != version:
            raise RequestConflict("该意见的字稿依据已更新，请重读后再关联")
        db.execute("INSERT INTO master_note_artwork(project,source,note,version,artwork) VALUES (?,?,?,?,?)",
                   (*key, version + 1, json.dumps(artwork, ensure_ascii=False)))
    return {"version": version + 1, **artwork}


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
    key = _note_key(jobs, project, source, payload.get("annotation"))
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
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        basis = payload.get("artwork_version")
        if basis is not None:
            if type(basis) is not int or not 1 <= basis < 2**63:
                raise ValueError("字稿关联版号须为正整数")
            event["artwork_version"] = basis
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
        current = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='master_note_artwork'").fetchone():
            current = db.execute("SELECT MAX(version) FROM master_note_artwork WHERE project=? AND source=? AND note=?",
                                 key).fetchone()[0]
        if basis != current:
            raise RequestConflict("请查看该意见当前的字稿依据，并带回对应关联版号")
        if state != "submitted" and previous.get("artwork_version") != basis:
            raise RequestConflict("字稿依据已经更换，请按新依据重新提交修订再复看")
        if state != "submitted" and (previous.get("state") != "submitted" or previous["target_binding"] != target
                                     or previous["target_frame"] != frame):
            raise RequestConflict("请先找到对应修订母版和帧的待复看记录")
        db.execute("INSERT INTO master_note_events(project,source,note,version,event) VALUES (?,?,?,?,?)",
                   (*key, version + 1, json.dumps(event, ensure_ascii=False)))
    return {"version": version + 1, **event}
