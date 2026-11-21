"""Compare video takes at the same source time and adopt one for a single saved edit."""

import base64
from fractions import Fraction

import ffmpeg_tools
from localhub_media import file_stamp, inspect_media, media_sha256, video_frame_png
from localhub_storage import RequestConflict
from localhub_timeline import clip_path, read_edit, save_edit


def _index(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}须为非负整数")
    return value


def _take(jobs, project, job_id, index):
    if not isinstance(job_id, str):
        raise ValueError("请选择已登记的候选")
    path, row = clip_path(jobs, project, job_id, _index(index, "输出序号"))
    stamp = file_stamp(path)
    _, kind, info = inspect_media(path, ffmpeg_tools.locate_ffprobe())
    if kind != "video":
        raise ValueError("请选择画面候选")
    return path, stamp, {"job_id": job_id, "file_index": index, "shot": row["shot"], "filename": path.name,
                         "fps": info["fps"], "frames": info["frames"], "sha256": media_sha256(path)}


def compare_takes(jobs, project, spec, choice=None):
    if not isinstance(spec, dict):
        raise ValueError("请指定剪版、片段及对照候选")
    revision = spec.get("revision")
    if type(revision) is not int or not 1 <= revision < 2**63:
        raise ValueError("请指定已保存的剪版号")
    track_index = _index(spec.get("track_index"), "轨道序号")
    clip_index = _index(spec.get("clip_index"), "片段序号")
    timeline = read_edit(jobs, project, revision)["timeline"]
    if track_index >= len(timeline["tracks"]):
        raise ValueError("该剪版找不到所选轨道")
    track = timeline["tracks"][track_index]
    if track["kind"] != "Video" or clip_index >= len(track["clips"]) or track["clips"][clip_index].get("gap"):
        raise ValueError("请选择画面轨上的一个实际片段")
    clip = track["clips"][clip_index]
    peer_id, peer_index = spec.get("job_id"), spec.get("file_index")
    if (peer_id, peer_index) == (clip["job_id"], clip["file_index"]):
        raise ValueError("所选两个候选是同一个输出")
    current = _take(jobs, project, clip["job_id"], clip["file_index"])
    peer = _take(jobs, project, peer_id, peer_index)
    start = Fraction(clip["start_frame"], Fraction(clip["rate"]))
    duration = Fraction(clip["duration_frames"], Fraction(clip["rate"]))
    peer_rate = Fraction(peer[2]["fps"])
    begin, end = start * peer_rate, (start + duration) * peer_rate
    if begin.denominator != 1 or end.denominator != 1:
        raise ValueError("两版帧率无法精确对齐原切点，请手动调整")
    if end > peer[2]["frames"]:
        raise ValueError("这版素材太短，无法覆盖原片段")
    binding = {"project": project, "revision": revision, "track_index": track_index, "clip_index": clip_index,
               "job_id": peer_id, "file_index": peer_index, "sha256_a": current[2]["sha256"], "sha256_b": peer[2]["sha256"]}
    if choice is None:
        offset = _index(spec.get("offset_frames", 0), "片段内帧号")
        elapsed = Fraction(offset, Fraction(timeline["rate"]))
        if elapsed >= duration:
            raise ValueError("对照位置超出所选片段")
        takes = []
        for path, _, info in (current, peer):
            frame = int((start + elapsed) * Fraction(info["fps"]))
            raw = video_frame_png(path, frame, ffmpeg_tools.locate_ffmpeg())
            takes.append({**info, "source_frame": frame, "png": base64.b64encode(raw).decode("ascii")})
        result = {"binding": binding, "track": track["name"], "rate": timeline["rate"],
                  "offset_frames": offset, "takes": takes}
    else:
        if choice not in ("A", "B") or spec != binding:
            raise RequestConflict("请使用同一项目的完整对照回执和明确的 A/B 选择")
        if read_edit(jobs, project)["revision"] != revision:
            raise RequestConflict("剪版已有更新，请在新版中重新选择片段")
        result = {"project": project, "revision": revision, "choice": choice}
    if any(file_stamp(path) != stamp for path, stamp, _ in (current, peer)):
        raise RequestConflict("对照期间候选已变动，请重新预览")
    if choice == "B":
        track["clips"][clip_index] = {"job_id": peer_id, "file_index": peer_index, "rate": str(peer_rate),
                                      "start_frame": int(begin), "duration_frames": int(end - begin)}
        result["revision"] = save_edit(jobs, project, revision, timeline)["revision"]
    return result
