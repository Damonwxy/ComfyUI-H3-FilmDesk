"""Read edit metadata from video or audio staged in the local input folder."""

import hashlib
import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path
from tempfile import NamedTemporaryFile


def file_stamp(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def media_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def video_frame_png(path, frame, ffmpeg):
    if not ffmpeg:
        raise NotImplementedError("抽取视频帧需要 FFmpeg")
    if type(frame) is not int or frame < 0:
        raise ValueError("视频帧号须为非负整数")
    try:
        raw = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file",
                              "-format_whitelist", "mov", "-i", str(path), "-map", "0:V:0",
                              "-vf", f"select=eq(n\\,{frame})", "-frames:v", "1",
                              "-c:v", "png", "-f", "image2pipe", "-"], capture_output=True,
                             timeout=120, check=True).stdout
    except subprocess.SubprocessError as exc:
        raise ValueError("无法解码所选视频帧") from exc
    if not raw:
        raise ValueError("视频未解出该帧")
    return raw


def candidate_file(path, info):
    path = Path(path)
    try:
        return path.is_file() and (
            info.get("source") != "imported"
            or (path.resolve() == path and file_stamp(path) == info.get("file_stamp"))
        )
    except OSError:
        return False


def staged_file(root, filename, suffixes):
    root = Path(root)
    staged = root.parent.resolve() / root.name
    if staged.resolve() != staged:
        raise ValueError("素材目录须位于 ComfyUI 的本机输入目录")
    if not isinstance(filename, str) or not filename.strip() or ":" in filename or "\x00" in filename:
        raise ValueError("素材须为 input/localhub_media 内的相对文件路径")
    relative = Path(filename.replace("\\", "/"))
    if relative.anchor or ".." in relative.parts:
        raise ValueError("素材须为 input/localhub_media 内的相对文件路径")
    path = (staged / relative).resolve()
    if not path.is_relative_to(staged) or path.suffix.lower() not in suffixes:
        raise ValueError("请选择 input/localhub_media 内的素材，支持 " + "、".join(sorted(suffixes)))
    if not path.is_file():
        raise ValueError("素材不存在。请先将文件放入 input/localhub_media")
    return path


def stash_input(path, root=None):
    """Publish a content-addressed copy and return its ComfyUI input name."""
    if root is None:
        import folder_paths
        root = folder_paths.get_input_directory()
    root, src = Path(root), Path(path)
    digest = hashlib.sha256()
    with src.open("rb") as original, NamedTemporaryFile(dir=root, prefix=".localhub-", delete=False) as temp:
        staged = Path(temp.name)
        try:
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                digest.update(chunk)
                temp.write(chunk)
            temp.close()
            dst = root / f"localhub_{digest.hexdigest()}{src.suffix.lower() or '.png'}"
            if not dst.exists():
                staged.replace(dst)
        finally:
            staged.unlink(missing_ok=True)
    return dst.name


def probe_media(root, filename, ffprobe):
    """Inspect a staged MP4/MOV or WAV. Do not follow out-of-folder links or remote URLs."""
    path = staged_file(root, filename, {".mp4", ".mov", ".wav"})
    return inspect_media(path, ffprobe)


def stream_bounds(stream):
    try:
        time_base = Fraction(stream["time_base"])
        start = int(stream["start_pts"]) * time_base
        duration = int(stream["duration_ts"]) * time_base
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return start, start + duration


def inspect_media(path, ffprobe):
    if not ffprobe:
        raise NotImplementedError("登记素材需要 ffprobe")
    stamp = file_stamp(path)
    try:
        reply = subprocess.run([
            ffprobe, "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", "mov,wav",
            "-show_streams", "-of", "json", "-i", str(path),
        ], stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=True)
        streams = json.loads(reply.stdout)["streams"]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        raise ValueError("无法读取该素材的音画信息") from exc
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(video) > 1 or len(audio) > 1 or not (video or audio):
        raise ValueError("素材须为单画面轨和最多一条声音轨的文件")
    if (path.suffix.lower() == ".wav") == bool(video):
        raise ValueError("画面素材请用 MP4 或 MOV，独立声音请用 WAV")
    try:
        if video:
            stream = video[0]
            fps = Fraction(stream["avg_frame_rate"])
            frames = int(stream["nb_frames"])
            seconds = int(stream["duration_ts"]) * Fraction(stream["time_base"])
            if (not 1 <= fps <= 240 or fps != Fraction(stream["r_frame_rate"])
                    or seconds * fps != frames):
                raise ValueError("素材无法按稳定帧率计数")
        else:
            fps = Fraction(24)
        audio_frames = 0
        if audio:
            stream = audio[0]
            audio_frames = int(int(stream["duration_ts"]) * Fraction(stream["time_base"]) * fps)
        if not video:
            frames = audio_frames
        if frames < 1:
            raise ValueError("素材至少要有一帧")
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("素材缺少可用的帧计数。视频请先转为恒定帧率，音频请使用有明确时长的 WAV") from exc
    if file_stamp(path) != stamp:
        raise ValueError("素材正在写入，完成后再登记")
    info = {
        "source": "imported", "frames": frames, "fps": fps.numerator if fps.denominator == 1 else str(fps),
        "has_audio": audio_frames > 0, "audio_frames": audio_frames, "file_stamp": stamp,
    }
    if video:
        picture = video[0]
        rotation = next((d["rotation"] for d in picture.get("side_data_list", []) if "rotation" in d), 0)
        info.update(width=picture.get("width"), height=picture.get("height"), video_codec=picture.get("codec_name"),
                    pixel_format=picture.get("pix_fmt"), sample_aspect_ratio=picture.get("sample_aspect_ratio"),
                    rotation=rotation)
    if audio:
        sound = audio[0]
        sample_rate = sound.get("sample_rate")
        info.update(audio_codec=sound.get("codec_name"), audio_sample_rate=int(sample_rate) if str(sample_rate).isdigit() else None,
                    audio_channels=sound.get("channels"))
        picture_bounds = stream_bounds(video[0]) if video else None
        sound_bounds = stream_bounds(sound)
        if picture_bounds and sound_bounds:
            info["audio_offset"] = [str(a - v) for v, a in zip(picture_bounds, sound_bounds)]
    return os.fspath(path), "video" if video else "audio", info
