"""Measure the first audio stream without writing to the candidate file."""

import json
import math
import re
import subprocess
from fractions import Fraction
from localhub_media import file_stamp, stream_bounds
from localhub_storage import RequestConflict
from localhub_timeline import clip_path, read_edit


REPORT_VERSION = 1
SILENCE_DBFS = -45
SILENCE_MIN_SECONDS = 0.25
QUIET_LUFS = -26
TRUE_PEAK_LIMIT_DBTP = -1
_FORMATS = "mov,matroska,webm,wav,flac,mp3,aac,ogg"
_LOCAL_INPUT = ["-protocol_whitelist", "file", "-format_whitelist", _FORMATS]


def _run(exe, arguments, timeout):
    try:
        return subprocess.run(
            [exe, *arguments], stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("音频质检超时，可先检查较短的候选") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError("无法测量候选的音频 · " + exc.stderr.strip()[-400:]) from exc


def _number(text):
    value = float(text)
    return value if math.isfinite(value) else None


def _measurements(log, duration):
    integrated = re.search(r"^\s*I:\s*(\S+)\s+LUFS", log, re.MULTILINE)
    true_peak = re.search(r"^\s*Peak:\s*(\S+)\s+dBFS", log, re.MULTILINE)
    sample_peak = re.search(r"\bPeak level dB:\s*(\S+)", log)
    if not (integrated and true_peak and sample_peak):
        raise ValueError("FFmpeg 未返回完整的响度及峰值测量")
    lufs = _number(integrated[1])
    dbfs = _number(sample_peak[1])
    count = re.search(r"\bPeak count:\s*(\d+(?:\.\d+)?)", log)
    ranges = []
    for start, end in re.findall(r"silence_start:\s*(\S+).*?silence_end:\s*(\S+)", log, re.DOTALL):
        left, right = max(0.0, float(start)), float(end)
        if duration is not None:
            right = min(right, duration)
        if right > left:
            ranges.append((left, right))
    silent_seconds = sum(end - start for start, end in ranges)
    return {
        "integrated_lufs": lufs if lufs is not None and lufs > -70 else None,
        "true_peak_dbtp": _number(true_peak[1]),
        "sample_peak_dbfs": dbfs,
        "peak_sample_count": int(float(count[1])) if dbfs is not None and count else None,
        "silent_seconds": round(silent_seconds, 6),
        "silence_ratio": round(silent_seconds / duration, 4) if duration else None,
        "longest_silence_s": round(max((end - start for start, end in ranges), default=0.0), 6),
        "silent_range_count": len(ranges),
        "silent_ranges": [{"start_s": round(start, 6), "end_s": round(end, 6)} for start, end in ranges[:50]],
    }


def inspect_audio(path, ffmpeg, ffprobe, *, source_range=None):
    """Measure the whole file or a half-open range in seconds from the first audio sample."""
    if not ffmpeg or not ffprobe:
        raise NotImplementedError("音频质检需要 FFmpeg 和 ffprobe")
    result = _run(ffprobe, [
        "-v", "error", *_LOCAL_INPUT, "-show_streams", "-of", "json", "-i", str(path),
    ], timeout=20)
    streams = json.loads(result.stdout)["streams"]
    video = next((s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audios = [s for s in streams if s["codec_type"] == "audio"]
    flags = []
    report = {
        "version": REPORT_VERSION, "status": "review", "flags": flags,
        "audio": None, "video_present": video is not None, "timing": None,
        "thresholds": {"silence_dbfs": SILENCE_DBFS, "min_silence_s": SILENCE_MIN_SECONDS,
                       "quiet_lufs": QUIET_LUFS, "true_peak_limit_dbtp": TRUE_PEAK_LIMIT_DBTP},
    }
    if not audios:
        flags.append("missing_audio")
        return report

    audio = audios[0]
    bounds = stream_bounds(audio)
    duration = float(bounds[1] - bounds[0]) if bounds else None
    # WAV and some other files omit start_pts. Their duration can still be measured.
    if duration is None:
        try:
            duration = float(int(audio["duration_ts"]) * Fraction(audio["time_base"]))
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            pass
    trim = ""
    if source_range is not None:
        hz = int(audio["sample_rate"])
        start, end = (math.ceil(t * hz) for t in source_range)
        if end <= start:
            raise ValueError("声音取用区间不足一个采样")
        trim = f"atrim=start_sample={start}:end_sample={end},"
        duration = (end - start) / hz
    filters = (trim + "asetpts=PTS-STARTPTS,ebur128=framelog=verbose:peak=true,"
               "astats=metadata=0:reset=0:measure_perchannel=none:measure_overall=Peak_level+Peak_count+Number_of_samples,"
               f"silencedetect=noise={SILENCE_DBFS}dB:d={SILENCE_MIN_SECONDS}")
    measured = _run(ffmpeg, [
        "-hide_banner", "-nostdin", "-v", "info", *_LOCAL_INPUT, "-i", str(path),
        "-map", "0:a:0", "-af", filters, "-f", "null", "-",
    ], timeout=90)
    metrics = _measurements(measured.stderr, duration)
    if source_range is not None:
        count = re.search(r"Number of samples:\s*(\d+)", measured.stderr)
        if count is None or int(count[1]) != end - start:
            raise ValueError("实际解码的采样数与剪版取用范围不符，请核对素材时长")
    report["audio"] = {"channels": audio.get("channels"), "sample_rate": int(audio["sample_rate"]),
                       "duration_s": round(duration, 6) if duration is not None else None, **metrics}
    dbfs, dbtp = metrics["sample_peak_dbfs"], metrics["true_peak_dbtp"]
    if dbfs is None:
        flags.append("no_signal")
    elif dbfs >= -0.001:
        flags.append("sample_at_full_scale")
    elif dbtp is not None and dbtp >= TRUE_PEAK_LIMIT_DBTP:
        flags.append("near_ceiling")
    if dbfs is not None and metrics["integrated_lufs"] is not None and metrics["integrated_lufs"] < QUIET_LUFS:
        flags.append("quiet")
    if metrics["longest_silence_s"] >= 0.5 or (metrics["silence_ratio"] or 0) >= 0.5:
        flags.append("extended_silence")
    if len(audios) > 1:
        flags.append("multiple_audio_streams")
    if video is not None and source_range is None:
        picture_bounds = stream_bounds(video)
        if bounds is None or picture_bounds is None:
            flags.append("timing_unavailable")
        else:
            try:
                fps = Fraction(video.get("avg_frame_rate") or "0")
            except (ValueError, ZeroDivisionError):
                fps = Fraction(0)
            tolerance = max(0.05, float(2 / fps)) if fps > 0 else 2 / 24
            start, end = (float(a - v) for a, v in zip(bounds, picture_bounds))
            report["timing"] = {"audio_start_delta_s": round(start, 6), "audio_end_delta_s": round(end, 6),
                                "tolerance_s": round(tolerance, 6)}
            if max(abs(start), abs(end)) > tolerance:
                flags.append("av_timing")
    report["status"] = "review" if flags else "pass"
    return report


def inspect_cut_audio(jobs, project, revision, ffmpeg, ffprobe, track_index=None):
    """Inspect explicit audio edits in their own track clocks, including repeated uses of one file."""
    if type(revision) is not int or not 1 <= revision < 2**63:
        raise ValueError("请指定已保存的剪版号")
    cut = read_edit(jobs, project, revision)["timeline"]
    tracks = cut["tracks"]
    if track_index is not None and (type(track_index) is not int or not 0 <= track_index < len(tracks)
                                    or tracks[track_index]["kind"] != "Audio"):
        raise ValueError("请选择剪版中的声音轨道序号")
    rate = Fraction(cut["rate"])
    segments = []
    reports = {}
    for index, track in enumerate(tracks):
        if track["kind"] != "Audio" or (track_index is not None and index != track_index):
            continue
        cursor = Fraction(0)
        for position, clip in enumerate(track["clips"]):
            clip_rate = Fraction(clip.get("rate", rate))
            duration = clip["duration_frames"] / clip_rate
            end = cursor + duration
            segment = {"track_index": index, "track": track["name"], "clip_index": position,
                       "start_frame": math.floor(cursor * rate), "end_frame": math.ceil(end * rate)}
            segments.append(segment)
            if clip.get("gap"):
                segment["gap"] = True
            else:
                path, row = clip_path(jobs, project, clip["job_id"], clip["file_index"])
                stamp = tuple(file_stamp(path))
                source_start = clip["start_frame"] / clip_rate
                key = (path, stamp, source_start, duration)
                if key not in reports:
                    reports[key] = inspect_audio(path, ffmpeg, ffprobe, source_range=(source_start, source_start + duration))
                if tuple(file_stamp(path)) != stamp:
                    raise RequestConflict("候选在质检时已变动，请刷新后重试")
                report = reports[key]
                segment.update(source=clip, shot=row["shot"], flags=report["flags"], audio=report["audio"])
                segment["silent_ranges"] = [
                    {"start_frame": math.floor((cursor + Fraction(str(span["start_s"]))) * rate),
                     "end_frame": math.ceil(min(end, cursor + Fraction(str(span["end_s"]))) * rate)}
                    for span in (report["audio"] or {}).get("silent_ranges", [])
                ]
            cursor = end
    checked = [s for s in segments if not s.get("gap")]
    return {"project": project, "revision": revision, "rate": cut["rate"], "segments": segments,
            "status": ("no_audio_clips" if not checked else "review" if any(s["flags"] for s in checked) else "pass")}
