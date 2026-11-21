"""Check a finished media file against the selected cut and technical requirements."""

import json
from fractions import Fraction

from localhub_media import file_stamp, inspect_media
from localhub_timeline import clip_path, cut_duration


PROFILE_FIELDS = {
    "width": "像素宽度", "height": "像素高度", "video_codec": "视频编码",
    "pixel_format": "像素格式", "audio_codec": "音频编码", "audio_sample_rate": "音频采样率",
    "audio_channels": "音频声道数",
}
MEASURED_FIELDS = (
    *PROFILE_FIELDS, "frames", "fps", "has_audio", "sample_aspect_ratio", "rotation", "audio_offset",
)
UPSCALE_METHODS = {"resize", "resolve_super_scale_2x"}


def verify_master(jobs, project, cut, aspect, master, profile, ffprobe):
    if not isinstance(master, dict) or not isinstance(master.get("job_id"), str):
        raise ValueError("请选择项目中的母版候选")
    index = master.get("file_index")
    if type(index) is not int or index < 0:
        raise ValueError("母版文件序号须为非负整数")
    path, row = clip_path(jobs, project, master["job_id"], index)
    stamp = file_stamp(path)
    if path.suffix.lower() not in (".mp4", ".mov"):
        raise ValueError("交付母版须为 MP4 或 MOV")
    _, kind, info = inspect_media(path, ffprobe)
    width, height = aspect.split(":")
    if (kind != "video" or not info["width"] or not info["height"]
            or Fraction(info["width"], info["height"]) != Fraction(width) / Fraction(height)):
        raise ValueError("母版像素宽高与目标画幅不符")
    rate = Fraction(info["fps"])
    if rate != Fraction(cut["rate"]) or Fraction(info["frames"]) / rate != cut_duration(cut):
        raise ValueError("母版帧率或时长与剪辑不符")
    audio_required = any(t["kind"] == "Audio" and any(not c.get("gap") for c in t["clips"]) for t in cut["tracks"])
    if audio_required and not info["has_audio"]:
        raise ValueError("有声音片段的剪辑需要带音轨的母版")
    if profile is not None:
        _check_profile(info, profile, rate)
    if file_stamp(path) != stamp:
        raise ValueError("母版在规格核对时已变动")
    report = {"job_id": row["id"], "file_index": index}
    report.update({key: info[key] for key in MEASURED_FIELDS if key in info})
    return path, stamp, report


def _check_profile(info, profile, rate):
    if not isinstance(profile, dict) or not {"width", "height"} <= profile.keys() <= PROFILE_FIELDS.keys():
        raise ValueError("母版要求须包含像素宽高，可附加编码、像素格式、采样率和声道数")
    mismatches = []
    for key, expected in profile.items():
        if key in ("width", "height", "audio_sample_rate", "audio_channels"):
            valid = type(expected) is int and expected > 0
        else:
            valid = isinstance(expected, str) and 1 <= len(expected) <= 40
        if not valid:
            raise ValueError(f"母版要求中的{PROFILE_FIELDS[key]}无效")
        if info.get(key) != expected:
            mismatches.append(f"{PROFILE_FIELDS[key]}要求 {expected}，实测 {info.get(key, '缺失')}")
    if info.get("sample_aspect_ratio") != "1:1" or info.get("rotation") != 0:
        mismatches.append("需使用方形像素和已转正的画面")
    if info["has_audio"]:
        offsets = info.get("audio_offset")
        if offsets is None or max(abs(Fraction(value)) for value in offsets) > 1 / rate:
            mismatches.append("音画轨起止时间须可核对且偏差不超过一帧")
    if mismatches:
        raise ValueError("母版技术规格不符 · " + "，".join(mismatches))


def describe_video_sources(jobs, project, cut, ffprobe):
    """Measure the file raster of each distinct video source in the saved cut."""
    sources = {}
    for track in cut["tracks"]:
        if track["kind"] != "Video":
            continue
        for clip in track["clips"]:
            if clip.get("gap"):
                continue
            key = clip["job_id"], clip["file_index"]
            if key in sources:
                continue
            path, row = clip_path(jobs, project, *key)
            stamp = file_stamp(path)
            raster = None
            if path.suffix.lower() in (".mp4", ".mov"):
                try:
                    _, kind, info = inspect_media(path, ffprobe)
                except ValueError:
                    pass
                else:
                    if kind == "video" and info["width"] and info["height"]:
                        raster = {"width": info["width"], "height": info["height"]}
            if file_stamp(path) != stamp:
                raise ValueError("核对时画面源片已变动，请重新读取")
            sources[key] = {
                "job_id": key[0], "file_index": key[1], "shot": row["shot"], "filename": path.name, "raster": raster,
                "source": json.loads(row["info"]).get("source", "generated"), "stamp": stamp,
            }
    return sources


def video_finishing(sources, entries):
    """Accept the editor's stated processing, scoped to sources in this exact cut."""
    if not isinstance(entries, list):
        raise ValueError("画面放大记录应为列表")
    finishing = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("画面放大记录须为对象")
        job_id, index, method = entry.get("job_id"), entry.get("file_index"), entry.get("method")
        if not isinstance(job_id, str) or type(index) is not int or not isinstance(method, str):
            raise ValueError("画面放大记录须指定候选、输出序号和处理方式")
        key = job_id, index
        if key not in sources or sources[key]["raster"] is None or key in finishing:
            raise ValueError("每条放大记录须对应一个有可核对分辨率的剪辑画面源片")
        if method not in UPSCALE_METHODS:
            raise ValueError("此交付工具无法记录该放大方式")
        finishing[key] = {"method": method, "recorded_by_editor": True}
    return finishing
