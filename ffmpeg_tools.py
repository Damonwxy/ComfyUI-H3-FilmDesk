# ffmpeg 工具 — hub_video_edit / hub_analyse_media 的本地实现
# 输出写入 output/localhub/edit/

import os
import json
import math
import shutil
import asyncio


def locate_ffmpeg():
    p = os.environ.get("LOCALHUB_FFMPEG")
    if p and os.path.isfile(p):
        return p
    w = shutil.which("ffmpeg")
    if w:
        return w
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return None


def locate_ffprobe():
    ff = locate_ffmpeg()
    if ff:
        cand = os.path.join(os.path.dirname(ff), "ffprobe" + os.path.splitext(ff)[1])
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffprobe")


def _out_dir():
    import folder_paths

    d = os.path.join(folder_paths.get_output_directory(), "localhub", "edit")
    os.makedirs(d, exist_ok=True)
    return d


async def _run(cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 失败: {}".format(err.decode("utf-8", "ignore")[-800:]))
    return True


async def edit(op, inputs, out_name="", trim=None, audio=None, reencode=False):
    ff = locate_ffmpeg()
    if not ff:
        raise RuntimeError("未找到 ffmpeg, 请安装或设置环境变量 LOCALHUB_FFMPEG")
    if not inputs:
        raise ValueError("inputs 不能为空")
    for p in inputs:
        if not os.path.isfile(p):
            raise FileNotFoundError("输入不存在: {}".format(p))

    out_name = out_name or ("edit_%d.mp4" % int(asyncio.get_event_loop().time()))
    out = os.path.join(_out_dir(), os.path.basename(out_name))
    ext = os.path.splitext(out)[1].lower()
    if ext not in (".mp4", ".mkv", ".webm", ".mov"):
        out += ".mp4"

    if op == "concat":
        lst = out + ".list.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for p in inputs:
                f.write("file '{}'\n".format(p.replace("'", "'\\''")))
        cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out]
        try:
            await _run(cmd)
        except RuntimeError:
            # 编码参数不一致时回退为重编码
            cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", lst,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                   "-c:a", "aac", out]
            await _run(cmd)
        os.remove(lst)

    elif op == "cut":
        src = inputs[0]
        trim = trim or {}
        start = float(trim.get("start") or 0)
        end = float(trim["end"]) if trim.get("end") is not None else None
        if not math.isfinite(start) or start < 0 or (end is not None and (not math.isfinite(end) or end <= start)):
            raise ValueError("裁切范围须满足 0 ≤ start < end")
        cmd = [ff, "-y"]
        if start:
            cmd += ["-ss", str(start)]
        cmd += ["-i", src]
        if end is not None:
            cmd += ["-t", str(end - start)]
        if reencode:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac"]
        else:
            cmd += ["-c", "copy"]
        cmd += [out]
        await _run(cmd)

    elif op == "replace_audio":
        if not audio or not os.path.isfile(audio):
            raise FileNotFoundError("audio 文件不存在: {}".format(audio))
        cmd = [ff, "-y", "-i", inputs[0], "-i", audio,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
               "-c:a", "aac", "-shortest", out]
        await _run(cmd)

    elif op == "mute":
        cmd = [ff, "-y", "-i", inputs[0], "-an", "-c:v", "copy", out]
        await _run(cmd)

    else:
        raise ValueError("op 仅支持 concat / cut / replace_audio / mute")

    return {"path": out, "size_bytes": os.path.getsize(out)}


async def analyse(path):
    fp = locate_ffprobe()
    if not fp:
        ff = locate_ffmpeg()
        if not ff:
            raise RuntimeError("未找到 ffmpeg/ffprobe")
        proc = await asyncio.create_subprocess_exec(
            ff, "-i", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        return {"path": os.path.abspath(path), "raw_info": err.decode("utf-8", "ignore")[-1200:],
                "note": "未找到 ffprobe, 仅返回 ffmpeg 原始信息"}
    proc = await asyncio.create_subprocess_exec(
        fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "ignore")[-400:])
    data = json.loads(out.decode("utf-8"))
    fmt = data.get("format", {})
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "path": os.path.abspath(path),
        "duration_s": round(float(fmt.get("duration", 0)), 2),
        "size_bytes": int(fmt.get("size", 0)),
        "bit_rate": fmt.get("bit_rate"),
        "video": {k: video.get(k) for k in ("codec_name", "width", "height", "r_frame_rate", "pix_fmt")},
        "audio": {k: audio.get(k) for k in ("codec_name", "sample_rate", "channels")} if audio else None,
        "semantic_analysis": "本地无 VLM, 语义级理解不可用(此为 Hub 端 hub_analyse_media 的降级实现)",
    }
