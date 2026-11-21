"""Compile an H3 shot for review and check that submission matches it."""

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path

from ffmpeg_tools import locate_ffmpeg
from h3_graph import build_h3_graph
from localhub_jobs import RequestConflict


_TAG = re.compile(r"<\s*(?:Picture|Video|Audio)\b[^<>]*>", re.IGNORECASE)


def _video_range(ref, limit):
    # The bridge produces a constant-rate MP4 with one H.264 packet per picture.
    # Count the prepared video stream, independently of any longer soundtrack.
    import folder_paths

    path = Path(folder_paths.get_input_directory()) / ref["input"]
    try:
        result = subprocess.run([locate_ffmpeg(), "-nostdin", "-v", "error", "-i", str(path),
                                 "-map", "0:v:0", "-c:v", "copy", "-f", "framecrc", "pipe:1"],
                                capture_output=True, check=True, timeout=30)
        prepared = sum(line.startswith(b"0,") for line in result.stdout.splitlines())
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"{ref['tag']} 无法读取已转换参考视频的帧数") from exc
    available = min(prepared, limit)
    if available < 5:
        raise ValueError(f"{ref['tag']} 转为 24 fps 后仅有 {available} 帧，H3 视频参考至少需要 5 帧")
    retained = available - (available - 5) % 17
    return {"tag": ref["tag"], "fps": 24, "prepared_frames": prepared,
            "start_frame": 0, "end_frame": retained, "last_frame": retained - 1}


def _resolve_images(params, resolve):
    resolved, versions = dict(params), {}

    def choose(tag, value):
        if isinstance(value, dict):
            if resolve is None:
                raise ValueError("此预检没有项目参考图连接")
            value, versions[tag] = resolve(value)
        return value

    if params.get("mode") == "r2v" and isinstance(params.get("ref_images"), list):
        entries = params["ref_images"]
        if len(entries) > 9:
            raise ValueError("ref_images 最多接受 9 项")
        resolved["ref_images"] = [choose(f"<Picture {index}>", value) for index, value in enumerate(entries, 1)]
    if params.get("mode") in ("i2v", "fl2v"):
        resolved["first_frame"] = choose("首帧", params.get("first_frame"))
    if params.get("mode") == "fl2v":
        resolved["last_frame"] = choose("尾帧", params.get("last_frame"))
    return resolved, versions


def compile_plan(project, shot, params, take, resolve_reference=None, *, binding=None):
    if params.get("control_video"):
        raise ValueError("镜头预检支持 H3 标准模式。ControlNet 仍可从任务接口直接提交")
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("请填写镜头提示词")
    for name, default in (("width", 864), ("height", 480), ("duration_s", 5)):
        value = float(params.get(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 须为正数")

    params, image_versions = _resolve_images(params, resolve_reference)
    graph, info = build_h3_graph(params)
    frames = info["frames"]
    if take is None:
        take = {"start_frame": 0, "end_frame": frames}
    if not isinstance(take, dict):
        raise ValueError("take 须包含起帧和出帧")
    start, end = take.get("start_frame", 0), take.get("end_frame", frames)
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= frames:
        raise ValueError(f"取用范围须满足 0 ≤ 起帧 < 出帧 ≤ {frames}")
    take = {"start_frame": start, "end_frame": end}

    refs = info["references"]
    if info["mode"] in ("i2v", "fl2v"):
        refs.append({"tag": "首帧", "role": "first_frame", "source": os.fspath(params["first_frame"]),
                     "input": graph["20"]["inputs"]["image"]})
    if info["mode"] == "fl2v":
        refs.append({"tag": "尾帧", "role": "last_frame", "source": os.fspath(params["last_frame"]),
                     "input": graph["21"]["inputs"]["image"]})
    for ref in refs:
        version = image_versions.get(ref["tag"])
        if version:
            if ref["input"] != version["input_name"]:
                raise ValueError("参考图快照在预检时已变动")
            ref["source"] = version["source"]
            ref["version"] = {key: version[key] for key in ("asset_id", "revision", "sha256")}
    tags = {ref["tag"] for ref in refs if ref["tag"].startswith("<")}
    used = set(_TAG.findall(graph["7"]["inputs"]["prompt"]))
    unmatched = sorted(used - tags)
    if unmatched:
        raise ValueError(f"提示词中没有对应素材的参考标签 · {', '.join(unmatched)}")
    warnings = [
        f"提示词未点名 {ref['tag']}，该素材仍参与参考"
        for ref in refs if ref["tag"] in tags - used
    ]

    info["models"]["lora"] = graph.get("2", {}).get("inputs", {}).get("lora_name")
    # The native job adds a delivery prefix after review. It does not affect the shot.
    output = graph["13"]["inputs"]
    prefix = output.pop("filename_prefix")
    try:
        values = [project, shot, take, graph, refs]
        if binding:
            values.append(binding)
        bound = json.dumps(values, ensure_ascii=False, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(bound.encode("utf-8")).hexdigest()
    finally:
        output["filename_prefix"] = prefix
    info["preflight"] = {"plan_sha256": digest, "take": take, "warnings": warnings}
    ranges = [_video_range(ref, frames) for ref in refs if ref["role"] == "video"]
    if ranges:
        info["preflight"]["video_ranges"] = ranges
        for span in ranges:
            if span["end_frame"] < span["prepared_frames"]:
                warnings.append(f"{span['tag']} 的末段动作可能丢失。如需保留，可将 24 fps 参考的最后一帧"
                                f"延长 {span['end_frame'] + 17 - span['prepared_frames']} 帧，再预检。")
    if binding:
        info["planned_shot"] = binding
    return graph, info


def confirm_plan(project, shot, options, resolve_reference=None, *, binding=None):
    params = dict(options)
    expected = params.pop("plan_sha256")
    take = params.pop("take")
    try:
        graph, info = compile_plan(project, shot, params, take, resolve_reference, binding=binding)
    except (ValueError, OSError, TypeError, KeyError, OverflowError) as exc:
        raise RequestConflict(f"镜头方案无法复现，请重新预检 · {exc}") from exc
    if expected != info["preflight"]["plan_sha256"]:
        raise RequestConflict("镜头方案或参考素材已变化，请重新预检")
    return graph, info
