"""Create a silent two-shot product ad from two local videos in a new project."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess

from aiohttp import ClientSession, web

from ffmpeg_tools import locate_ffmpeg, locate_ffprobe
from studio_local import create_app


async def make_ad(base, session, output, sources, ffmpeg):
    project = "Two-shot product ad"
    media = output / "input" / "localhub_media"

    async def request(method, route, payload=None, **params):
        async with session.request(method, base + route, json=payload,
                                   params={"project": project, **params}) as response:
            data = await response.json()
            if response.status >= 400:
                raise RuntimeError(f"{route}: {data}")
            return data

    shots = []
    files = {}
    for index, (source, title) in enumerate(zip(sources, ["Product reveal", "Detail"]), 1):
        take = media / f"shot-{index}.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-n", "-i", str(source),
                        "-map", "0:v:0", "-an", "-vf",
                        "fps=24,scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        "pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p",
                        "-frames:v", "72", "-c:v", "libx264", "-crf", "18", str(take)], check=True)
        job = (await request("POST", "/media", {"project": project, "shot": title, "file": take.name}))["job"]
        frames = job["info"]["frames"]
        clip = {"job_id": job["id"], "file_index": 0, "start_frame": 0, "duration_frames": frames, "rate": "24"}
        shots.append({"id": f"S{index}", "scene_id": "product", "title": title,
                      "action": title, "frames": frames, "picture": clip})
        files[job["id"]] = take
    plan = {"title": project, "genre": "ad", "rate": "24", "scenes": [{"id": "product", "title": "Product"}],
            "shots": shots}
    plan_rev = (await request("PUT", "/plan", {"expected_revision": 0, "plan": plan}))["revision"]
    draft = await request("GET", "/plan/cut", revision=str(plan_rev))
    cut_rev = (await request("PUT", "/timeline", {"expected_revision": 0, "timeline": draft["timeline"]}))["revision"]
    saved = await request("GET", "/timeline", revision=str(cut_rev))
    clips = saved["timeline"]["tracks"][0]["clips"]

    # Render the saved order and source ranges, including their cut boundary.
    inputs, filters = [], []
    for index, clip in enumerate(clips):
        inputs.extend(["-i", str(files[clip["job_id"]])])
        start = clip["start_frame"]
        end = start + clip["duration_frames"]
        filters.append(f"[{index}:v]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS[v{index}]")
    filters.append("[v0][v1]concat=n=2:v=1:a=0[out]")
    master = media / "master.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-n", *inputs, "-filter_complex", ";".join(filters),
                    "-map", "[out]", "-an", "-c:v", "libx264", "-crf", "0", "-movflags", "+faststart", str(master)],
                   check=True)
    job = (await request("POST", "/media", {"project": project, "shot": "Master", "file": master.name}))["job"]
    delivery = {"name": "Product preview", "market": "Demo", "language": "und", "aspect": "16:9", "revision": cut_rev}
    await request("POST", "/deliveries", delivery)
    checked = await request("POST", "/deliveries/master-check", {"delivery": delivery,
                            "master": {"job_id": job["id"], "file_index": 0},
                            "master_profile": {"width": 960, "height": 540, "video_codec": "h264", "pixel_format": "yuv420p"}})
    otio = await request("GET", "/timeline/otio", revision=str(cut_rev))
    (output / "edit.otio").write_text(json.dumps(otio, ensure_ascii=False), encoding="utf-8")
    receipt = {"project": project, "plan_revision": plan_rev, "cut_revision": cut_rev,
               "timeline": saved["timeline"], "delivery": delivery, "check": checked}
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(master)


async def run(output, sources):
    if not all(source.is_file() for source in sources):
        raise ValueError("Both source videos must exist")
    ffmpeg = locate_ffmpeg()
    if not ffmpeg or not locate_ffprobe():
        raise RuntimeError("Install ffmpeg and ffprobe first")
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(output / "jobs.sqlite3", output / "input", port)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.SockSite(runner, listener).start()
            async with ClientSession() as session:
                await make_ad(f"http://127.0.0.1:{port}/localhub/v2", session, output, sources, ffmpeg)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reveal", type=Path)
    parser.add_argument("detail", type=Path)
    parser.add_argument("output", type=Path, help="New project directory")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), [args.reveal, args.detail]))
