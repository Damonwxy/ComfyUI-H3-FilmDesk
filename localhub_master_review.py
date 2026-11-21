"""Persist review notes against the delivery and the bytes of its reviewed master."""

import base64
import json

import ffmpeg_tools
from localhub_delivery import verify_master
from localhub_media import file_stamp, media_sha256, video_frame_png
from localhub_storage import RequestConflict
from localhub_timeline import list_deliveries, require_delivery


def list_master_reviews(jobs, project):
    """Read recorded opinions even if the reviewed file is offline or has changed."""
    with jobs.db() as db:
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
        raw = video_frame_png(path, frame, ffmpeg_tools.locate_ffmpeg())
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
    return result
