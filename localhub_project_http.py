"""Project HTTP entry points shared by ComfyUI and the standalone studio."""

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import web

import ffmpeg_tools
from legacy_canvas import read_archives
from localhub_audio_qc import REPORT_VERSION, inspect_audio, inspect_cut_audio
from localhub_compare import compare_takes
from localhub_copy import list_copy, update_copy
from localhub_delivery import describe_video_sources, verify_master
from localhub_master_review import list_master_reviews, master_review, record_followup
from localhub_jobs import Jobs
from localhub_media import file_stamp, inspect_media, probe_media
from localhub_project_plan import plan_cut, read_project_plan, save_project_plan
from localhub_references import list_references, register_reference, register_reference_file
from localhub_storage import RequestConflict
from localhub_subtitles import read_subtitles, save_subtitles, subtitle_srt
from localhub_timeline import (
    clip_path, export_otio, import_otio, list_deliveries, read_edit,
    register_delivery, require_delivery, return_review, save_edit,
)


@dataclass(frozen=True)
class StudioContext:
    jobs: Callable[[], Jobs]
    input_directory: Callable[[], str | Path]
    generation_enabled: bool


_STUDIO = web.AppKey("localhub.studio", StudioContext)


def _context(request):
    return request.app[_STUDIO]


def _ok(payload):
    payload.setdefault("ok", True)
    return web.json_response(payload)


def _err(status, msg, **extra):
    return web.json_response(dict(ok=False, error=msg, **extra), status=status)


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def _label(value, field, default=None):
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValueError(f"{field} 需为 1–120 字符的名称")
    return value.strip()


def _revision(query, key="revision"):
    value = query.get(key)
    if value is None:
        return None
    revision = int(value)
    if not 1 <= revision < 2**63:
        raise ValueError(f"{key} 须为正整数")
    return revision


async def j_get(request):
    service = _context(request).jobs()
    row = service.find(request.match_info["job_id"])
    project = request.query.get("project")
    if row is None or (project is not None and row["project"] != project):
        return _err(404, "任务不存在")
    if _context(request).generation_enabled:
        row = await service.refresh(row)
    return _ok({"job": service.public(row)})


async def j_register_media(request):
    p = await _body(request)
    if not isinstance(p, dict):
        return _err(400, "素材参数须为对象")
    try:
        project = _label(p.get("project"), "project", "本地项目")
        shot = _label(p.get("shot"), "shot")
        path, kind, info = await asyncio.to_thread(
            probe_media, Path(_context(request).input_directory()) / "localhub_media",
            p.get("file"), ffmpeg_tools.locate_ffprobe(),
        )
        service = _context(request).jobs()
        row, created = service.register_media(project, shot, path, kind, info)
    except RequestConflict as exc:
        return _err(409, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (ValueError, OSError, RuntimeError) as exc:
        return _err(400, str(exc))
    return web.json_response({"ok": True, "job": service.public(row)}, status=201 if created else 200)


async def r_references(request):
    try:
        if request.method == "GET":
            project = _label(request.query.get("project"), "project", "本地项目")
            return _ok({"references": list_references(_context(request).jobs(), project)})
        body = await _body(request)
        if not isinstance(body, dict):
            raise ValueError("参考图参数须为对象")
        project = _label(body.get("project"), "project", "本地项目")
        asset_id = _label(body.get("asset_id"), "asset_id")
        row, created = await asyncio.to_thread(
            register_reference, _context(request).jobs(), _context(request).input_directory(), project, asset_id, body.get("file"),
        )
    except (ValueError, OSError) as exc:
        return _err(400, str(exc))
    return web.json_response({"ok": True, "reference": row}, status=201 if created else 200)


async def upload_reference(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        asset_id = _label(request.query.get("asset_id"), "asset_id")
        filename = request.query.get("filename", "")
        suffix = Path(filename).suffix.lower()
        if (not filename or any(ch in filename for ch in ("/", "\\", ":", "\x00"))
                or suffix not in (".png", ".jpg", ".jpeg", ".webp")):
            raise ValueError("请选择 PNG、JPEG 或 WebP 商品图")
        ctx = _context(request)
        root = Path(ctx.input_directory())
        with TemporaryDirectory(dir=root) as directory:
            source = Path(directory) / ("upload" + suffix)
            with source.open("wb") as output:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    if output.tell() + len(chunk) > 64 * 1024 * 1024:
                        return _err(413, "单张商品图请控制在 64 MiB 以内")
                    output.write(chunk)
            row, created = await asyncio.to_thread(register_reference_file, ctx.jobs(), root, project,
                                                  asset_id, source, filename, private_upload=True)
    except (ValueError, OSError) as exc:
        return _err(400, str(exc))
    return web.json_response({"ok": True, "reference": row}, status=201 if created else 200)


async def j_list(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        limit = max(1, min(int(request.query.get("limit", 50)), 100))
    except ValueError as exc:
        return _err(400, str(exc))
    service = _context(request).jobs()
    rows = service.recent(project, limit)
    if _context(request).generation_enabled:
        for row in rows:
            service.schedule(row)
    return _ok({"project": project, "jobs": [service.public(row) for row in rows]})


async def j_media(request):
    service = _context(request).jobs()
    job_id = request.match_info["job_id"]
    row = service.find(job_id)
    if row:
        try:
            file_path, _ = clip_path(service, row["project"], job_id, int(request.match_info["index"]))
            return web.FileResponse(file_path)
        except ValueError:
            pass
    return _err(404, "候选文件不存在")


@lru_cache(maxsize=128)
def _review_timing(path, stamp):
    _, kind, info = inspect_media(path, ffmpeg_tools.locate_ffprobe())
    if kind != "video":
        raise ValueError("逐帧批注需要视频文件")
    return info["frames"], info["fps"]


async def j_frame_notes(request):
    service = _context(request).jobs()
    try:
        project = _label(request.query.get("project"), "project")
        job_id = request.match_info["job_id"]
        index = int(request.match_info["index"])
        path, _ = clip_path(service, project, job_id, index)
        stamp = ":".join(map(str, file_stamp(path)))
        frames, fps = await asyncio.to_thread(_review_timing, path, stamp)
        new = await _body(request) if request.method == "POST" else None
        if request.method == "POST" and not isinstance(new, dict):
            raise ValueError("请提交帧号和批注")
        if stamp != ":".join(map(str, file_stamp(path))):
            raise RequestConflict("候选文件已变更，请重新打开候选")
        notes = service.frame_notes(job_id, index, stamp, frames, new)
        return _ok(dict(stamp=stamp, fps=fps, frames=frames, notes=notes))
    except RequestConflict as exc:
        return _err(409, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (ValueError, OSError) as exc:
        return _err(400, str(exc))


async def j_audio_qc(request):
    service = _context(request).jobs()
    job_id = request.match_info["job_id"]
    row = service.find(job_id)
    if row is None or row["kind"] not in ("video", "audio", "music"):
        return _err(404, "音频质检需要已就绪的音视频候选")
    try:
        index = int(request.match_info["index"])
        path, _ = clip_path(service, row["project"], job_id, index)
        stamp = file_stamp(path)
    except (ValueError, OSError):
        return _err(404, "候选文件不存在或已变动")
    report = service.audio_report(job_id, index, stamp)
    if report is not None and report["version"] != REPORT_VERSION:
        report = None
    if request.method == "POST" and report is None:
        try:
            report = await asyncio.to_thread(inspect_audio, path, ffmpeg_tools.locate_ffmpeg(),
                                             ffmpeg_tools.locate_ffprobe())
            if file_stamp(path) != stamp:
                return _err(409, "候选在质检时已变动，请刷新后重试")
        except NotImplementedError as exc:
            return _err(501, str(exc))
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            return _err(400, str(exc))
        service.save_audio_report(job_id, index, stamp, report)
    return _ok({"report": report})


async def e_cut_audio(request):
    try:
        project = _label(request.query.get("project"), "project")
        track = request.query.get("track_index")
        report = await asyncio.to_thread(inspect_cut_audio, _context(request).jobs(), project,
                                         _revision(request.query), ffmpeg_tools.locate_ffmpeg(),
                                         ffmpeg_tools.locate_ffprobe(), int(track) if track is not None else None)
        return _ok({"report": report})
    except RequestConflict as exc:
        return _err(409, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (ValueError, OSError) as exc:
        return _err(400, str(exc))


async def e_compare_takes(request):
    try:
        project = _label(request.query.get("project"), "project")
        spec = await _body(request)
        choice = None
        if request.match_info["action"] == "choose":
            if not isinstance(spec, dict) or spec.get("choice") not in ("A", "B"):
                raise ValueError("请选择 A 或 B")
            choice, spec = spec["choice"], spec.get("binding")
        result = await asyncio.to_thread(compare_takes, _context(request).jobs(), project, spec, choice)
        return _ok(result)
    except RequestConflict as exc:
        return _err(409, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (ValueError, OSError) as exc:
        return _err(400, str(exc))


async def e_timeline(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        service = _context(request).jobs()
        if request.method == "GET":
            return _ok(read_edit(service, project, _revision(request.query)))
        p = await _body(request)
        return _ok(save_edit(service, project, p.get("expected_revision"), p.get("timeline")))
    except RequestConflict as exc:
        return _err(409, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except (ValueError, AttributeError) as exc:
        return _err(400, str(exc))


async def e_project_plan(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        service = _context(request).jobs()
        if request.method == "PUT":
            payload = await _body(request)
            if not isinstance(payload, dict):
                raise ValueError("请提交项目编排")
            return _ok(save_project_plan(service, project, payload.get("expected_revision"), payload.get("plan")))
        revision = _revision(request.query)
        if request.path.endswith("/cut"):
            return _ok(plan_cut(service, project, revision, request.query.get("episode")))
        return _ok(read_project_plan(service, project, revision))
    except RequestConflict as exc:
        return _err(409, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except (ValueError, OSError) as exc:
        return _err(400, str(exc))


async def e_copy(request):
    try:
        project = _label(request.query.get("project"), "project")
        if request.method == "GET":
            return _ok({"entries": list_copy(_context(request).jobs(), project)})
        revision = update_copy(_context(request).jobs(), project, await _body(request), review=request.method == "PUT")
        return _ok({"revision": revision})
    except RequestConflict as exc:
        return _err(409, str(exc))
    except ValueError as exc:
        return _err(400, str(exc))


async def e_deliveries(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        if request.method == "GET":
            return _ok({"deliveries": list_deliveries(_context(request).jobs(), project)})
        values = await _body(request)
        if not isinstance(values, dict):
            raise ValueError("请提交交付规格")
        return _ok({"delivery": register_delivery(_context(request).jobs(), project, values)})
    except LookupError as exc:
        return _err(404, str(exc))
    except ValueError as exc:
        return _err(400, str(exc))


async def e_subtitles(request):
    try:
        project = _label(request.query.get("project"), "project")
        jobs = _context(request).jobs()
        if request.method == "PUT":
            payload = await _body(request)
            if not isinstance(payload, dict):
                raise ValueError("请提交交付字幕")
            delivery = payload.get("delivery")
            revision = save_subtitles(jobs, project, delivery, payload.get("expected_revision"), payload.get("cues"))
        else:
            delivery = {key: request.query.get(key) for key in ("name", "market", "language", "aspect")}
            delivery["revision"] = _revision(request.query)
            revision = _revision(request.query, "subtitle_revision")
        sheet = read_subtitles(jobs, project, delivery, revision)
        if request.path.endswith(".srt"):
            return web.Response(text=subtitle_srt(sheet), content_type="application/x-subrip", headers={
                "Content-Disposition": f'attachment; filename="captions-v{sheet["revision"]}.srt"',
            })
        return _ok({"sheet": sheet})
    except RequestConflict as exc:
        return _err(409, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except ValueError as exc:
        return _err(400, str(exc))


async def e_master_check(request):
    try:
        project = _label(request.query.get("project"), "project")
        payload = await _body(request)
        if not isinstance(payload, dict):
            raise ValueError("请提交母版规格")
        jobs = _context(request).jobs()
        delivery = payload.get("delivery")
        cut = require_delivery(jobs, project, delivery)
        ffprobe = ffmpeg_tools.locate_ffprobe()
        _, _, report = await asyncio.to_thread(verify_master, jobs, project, cut, delivery["aspect"],
                                               payload.get("master"), payload.get("master_profile"), ffprobe)
        sources = await asyncio.to_thread(describe_video_sources, jobs, project, cut, ffprobe)
        return _ok({"master": report, "video_sources": [
            {key: item[key] for key in ("job_id", "file_index", "shot", "filename", "raster", "source")}
            for item in sources.values()
        ]})
    except LookupError as exc:
        return _err(404, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (OSError, ValueError) as exc:
        return _err(400, str(exc))


async def e_master_review(request):
    try:
        project = _label(request.query.get("project"), "project")
        if request.method == "GET":
            return _ok(list_master_reviews(_context(request).jobs(), project))
        action = request.match_info["action"]
        if action == "followup":
            event = await asyncio.to_thread(record_followup, _context(request).jobs(), project, await _body(request))
            return _ok({"event": event})
        result = await asyncio.to_thread(master_review, _context(request).jobs(), project, await _body(request), action)
        return _ok({"review": result})
    except RequestConflict as exc:
        return _err(409, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (OSError, ValueError) as exc:
        return _err(400, str(exc))


async def e_export(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        service = _context(request).jobs()
        saved = read_edit(service, project, _revision(request.query))
        if not saved["timeline"]["tracks"]:
            return _err(400, "先保存一份包含候选的剪辑清单")
        data = export_otio(service, project, saved["timeline"], for_resolve=request.query.get("target") == "resolve")
        return web.Response(text=data, content_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="localhub-v{saved["revision"]}.otio"',
        })
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except ValueError as exc:
        return _err(400, str(exc))


async def e_fingerprint(request):
    digest = hashlib.sha256()
    size = 0
    async for chunk in request.content.iter_chunked(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return _ok({"bytes": size, "sha256": digest.hexdigest()})


async def e_import(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        p = await _body(request)
        if not isinstance(p, dict) or not isinstance(p.get("otio"), str):
            raise ValueError("otio 须为剪辑交换文件的文本")
        service = _context(request).jobs()
        current = read_edit(service, project)
        if p.get("preview") is True and "base_revision" in p:
            return _ok(await asyncio.to_thread(return_review, service, project, p["otio"], p["base_revision"], current,
                                               p.get("draft"), evidence=p.get("source_evidence"), manifest=p.get("manifest")))
        timeline = import_otio(service, project, p["otio"], current["timeline"]["rate"])
        if p.get("preview") is True:
            return _ok({"timeline": timeline, "revision": current["revision"]})
        return _ok(save_edit(service, project, p.get("expected_revision"), timeline))
    except RequestConflict as exc:
        return _err(409, str(exc))
    except LookupError as exc:
        return _err(404, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except ValueError as exc:
        return _err(400, str(exc))


async def studio(request):
    name = request.match_info.get("name", "index.html")
    if name not in {"index.html", "studio.css", "studio.js", "site-tools.mjs", "copy-review.mjs", "delivery-review.mjs",
                    "project-plan.mjs", "frame-region.mjs", "compare-cut.mjs", "master-review.mjs"}:
        return _err(404, "页面不存在")
    headers = {
        "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; media-src 'self' blob:",
        "Cache-Control": "no-cache",
    }
    if name.endswith((".js", ".mjs")):
        headers["Content-Type"] = "text/javascript"
    return web.FileResponse(Path(__file__).with_name("studio") / name, headers=headers)


async def studio_status(request):
    return _ok({"generation": _context(request).generation_enabled})


async def legacy_canvas(request):
    try:
        project = _label(request.query.get("project"), "project", "本地项目")
        archives = await asyncio.to_thread(read_archives, _context(request).jobs(), project)
        return _ok({"archives": archives})
    except ValueError as exc:
        return _err(400, str(exc))


def register_project_routes(app, table, *, jobs, input_directory, generation_enabled):
    from localhub_projects import register_project_search
    register_project_search(table, jobs)
    app[_STUDIO] = StudioContext(jobs, input_directory, generation_enabled)
    table.post("/localhub/v2/media")(j_register_media)
    table.get("/localhub/v2/references")(r_references)
    table.post("/localhub/v2/references")(r_references)
    table.post("/localhub/v2/references/upload")(upload_reference)
    table.get("/localhub/v2/jobs")(j_list)
    table.get("/localhub/v2/jobs/{job_id}")(j_get)
    table.get("/localhub/v2/jobs/{job_id}/media/{index}")(j_media)
    table.get("/localhub/v2/jobs/{job_id}/media/{index}/audio-qc")(j_audio_qc)
    table.get("/localhub/v2/jobs/{job_id}/media/{index}/frame-notes")(j_frame_notes)
    table.post("/localhub/v2/jobs/{job_id}/media/{index}/frame-notes")(j_frame_notes)
    table.post("/localhub/v2/jobs/{job_id}/media/{index}/audio-qc")(j_audio_qc)
    table.get("/localhub/v2/timeline")(e_timeline)
    table.get("/localhub/v2/timeline/audio-qc")(e_cut_audio)
    table.post("/localhub/v2/timeline/takes/{action:preview|choose}")(e_compare_takes)
    table.get("/localhub/v2/plan")(e_project_plan)
    table.put("/localhub/v2/plan")(e_project_plan)
    table.get("/localhub/v2/plan/cut")(e_project_plan)
    table.get("/localhub/v2/deliveries")(e_deliveries)
    table.get("/localhub/v2/deliveries/subtitles")(e_subtitles)
    table.put("/localhub/v2/deliveries/subtitles")(e_subtitles)
    table.get("/localhub/v2/deliveries/subtitles.srt")(e_subtitles)
    table.post("/localhub/v2/deliveries/master-check")(e_master_check)
    table.get("/localhub/v2/deliveries/master-review")(e_master_review)
    table.post("/localhub/v2/deliveries/master-review/{action:read|frame|save|followup}")(e_master_review)
    table.get("/localhub/v2/ad-copy")(e_copy)
    table.post("/localhub/v2/ad-copy")(e_copy)
    table.put("/localhub/v2/ad-copy")(e_copy)
    table.post("/localhub/v2/deliveries")(e_deliveries)
    table.put("/localhub/v2/timeline")(e_timeline)
    table.get("/localhub/v2/timeline/otio")(e_export)
    table.post("/localhub/v2/timeline/otio")(e_import)
    table.post("/localhub/v2/timeline/source-fingerprint")(e_fingerprint)
    table.get("/localhub/studio")(studio)
    table.get("/localhub/studio/{name}")(studio)
    table.get("/localhub/v2/studio/status")(studio_status)
    table.get("/localhub/v2/legacy-canvas")(legacy_canvas)
