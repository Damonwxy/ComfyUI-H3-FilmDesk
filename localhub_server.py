# Local Hub Bridge 路由层 — hub_* 工具的本地 HTTP 实现
#
# 全部路由前缀 /localhub/*, 仅监听本机(ComfyUI 默认 127.0.0.1)。
# 生成类路由通过回环调用本机 ComfyUI 的任务接口,
# 与手动点 Queue 走完全相同的执行链(含 DynamicVRAM / 显存优化节点集无关, 这里用核心节点图)。

import os
import base64
import asyncio
from functools import lru_cache, partial
from pathlib import Path
from uuid import UUID, uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout
from aiohttp import web
from server import PromptServer
import folder_paths

import h3_graph
import canvas_store
import ffmpeg_tools
from localhub_jobs import FINAL, Jobs, RequestConflict, observe_native
from localhub_preflight import compile_plan, confirm_plan
from localhub_media import video_frame_png
from localhub_project_http import _body, _err, _label, _ok, register_project_routes
from localhub_project_plan import bind_h3_shot
from localhub_references import resolve_reference


def _base_url():
    ps = PromptServer.instance
    port = getattr(ps, "port", None)
    if not port:
        try:
            port = ps.runner.addresses[0].port
        except Exception:
            port = 8188
    return "http://127.0.0.1:{}".format(port)


def _resolve_outputs(outputs, include_temp=True):
    """从 history outputs 提取生成文件绝对路径。"""
    roots = {"output": Path(folder_paths.get_output_directory()).resolve()}
    if include_temp:
        roots["temp"] = Path(folder_paths.get_temp_directory()).resolve()
    files = []
    for node_out in (outputs or {}).values():
        for key in ("videos", "images", "gifs", "audio"):
            for e in node_out.get(key, []) or []:
                base = roots.get(e.get("type", "output"))
                filename = e.get("filename")
                if base is not None and filename:
                    path = (base / (e.get("subfolder") or "") / filename).resolve()
                    if path.is_relative_to(base):
                        files.append(str(path))
    return list(dict.fromkeys(files))


@lru_cache(maxsize=1)
def _jobs():
    return Jobs(Path(folder_paths.get_user_directory()) / "localhub" / "jobs.sqlite3",
                _base_url, _resolve_outputs)


# ---------------- 生成类 ----------------

def _compile_video(params):
    if params.get("control_video"):
        return h3_graph.build_control_video_graph(params)
    return h3_graph.build_h3_graph(params)


def _compile_image(params):
    ckpt = params.get("checkpoint") or h3_graph.detect_models().get("checkpoints")
    if not ckpt:
        raise NotImplementedError("本机 models/checkpoints 为空, 无法本地生图。")
    return h3_graph.build_sd_image_graph(params, ckpt)


BUILDERS = {"video": _compile_video, "image": _compile_image, "music": h3_graph.build_music_graph}


def _reference_resolver(project):
    return partial(resolve_reference, _jobs(), folder_paths.get_input_directory(), project)


async def _legacy_generate(request, kind):
    params = await _body(request)
    if not isinstance(params, dict):
        return _err(400, "生成参数须为对象")
    try:
        timeout = max(0, min(int(params.get("timeout_s", 900 if kind == "image" else 1800)), 3600))
        key = request.headers.get("Idempotency-Key")
        project = _label(params.get("project"), "project", "本地项目")
        shot = _label(params.get("shot"), "shot", "未归入镜头")
        intent = {k: v for k, v in params.items() if k not in ("timeout_s", "project", "shot")}
        service = _jobs()
        row = await service.accept(kind, project, shot, key, intent, BUILDERS[kind])
    except RequestConflict as exc:
        return _err(409, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        return _err(501 if kind == "music" else 400, str(exc))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while row["state"] not in FINAL | {"needs_reconciliation"} and loop.time() < deadline:
        await asyncio.sleep(min(1.0, deadline - loop.time()))
        row = await service.refresh(row)
    job = service.public(row)
    if job["state"] == "needs_reconciliation" or job["state"] not in FINAL:
        return _err(504, job["error"] or f"超过 {timeout} 秒未完成，可继续查询原任务", job=job)
    if job["state"] != "completed" or job.get("delivery") != "ready":
        return _err(500, job["error"] or "生成失败、中断或缺少输出文件", job=job)

    info, files = job["info"], job["files"]
    asset_type, role = {
        "video": ("video", "h3_generate_video"),
        "image": ("image", "hub_generate_image"),
        "music": ("audio", "hub_generate_music"),
    }[kind]
    meta = {"job_id": job["id"], **{k: info[k] for k in ("mode", "seed", "controlnet", "checkpoint") if k in info}}
    assets = []
    for path in files:
        try:
            assets.append(canvas_store.record(asset_type, path, role=role, meta=meta))
        except Exception:
            pass
    return _ok({"info": info, "files": files, "assets": assets, "job_id": job["id"], "prompt_id": job["prompt_id"]})


async def h_generate_video(request):
    return await _legacy_generate(request, "video")


async def h_generate_image(request):
    return await _legacy_generate(request, "image")


async def h_generate_music(request):
    return await _legacy_generate(request, "music")


async def h_synthesize_speech(request):
    return _err(501, "hub_synthesize_speech 暂无本地实现。"
                     "可用替代: 1) H3 原生对白(直接写在提示词里, 支持11种语言); "
                     "2) 后续可接入本地 TTS(如 GPT-SoVITS/Fish Speech)。")


# ---------------- 剪辑 / 分析 ----------------

async def h_video_edit(request):
    p = await _body(request)
    try:
        result = await ffmpeg_tools.edit(p.get("op"), p.get("inputs"),
                                         p.get("out") or "", p.get("trim"),
                                         p.get("audio"), bool(p.get("reencode")))
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return _err(400, str(e))
    try:
        canvas_store.record("video", result["path"], role="hub_video_edit",
                            meta={"op": p.get("op")})
    except Exception:
        pass
    return _ok({"result": result})


async def h_analyse_media(request):
    p = await _body(request)
    path = p.get("path")
    if not path or not os.path.isfile(path):
        return _err(400, "path 不存在: {}".format(path))
    try:
        return _ok({"analysis": await ffmpeg_tools.analyse(path)})
    except RuntimeError as e:
        return _err(500, str(e))


# ---------------- 画布状态 ----------------

async def c_record(request):
    p = await _body(request)
    if not p.get("path"):
        return _err(400, "path 必填")
    asset = canvas_store.record(p.get("type", "file"), p["path"],
                                role=p.get("role", ""), source=p.get("source", "user"),
                                meta=p.get("meta"))
    return _ok({"asset": asset})


async def c_assets(request):
    return _ok({"assets": canvas_store.list_assets(request.query.get("type"),
                                                   int(request.query.get("limit", 100)))})


async def c_group_recent(request):
    p = await _body(request)
    return _ok({"group": canvas_store.group_recent(int(p.get("n", 5)), p.get("name", ""))})


async def c_clear(request):
    canvas_store.clear()
    return _ok({"cleared": True})


# ---------------- 任务与状态 ----------------

async def j_status(request):
    pid = request.match_info["job_id"]
    service = _jobs()
    row = service.find(prompt_id=pid) or service.find(pid)
    if row:
        job = service.public(await service.refresh(row))
        state, files, error = job["state"], job["files"], job["error"]
    else:
        try:
            if str(UUID(pid)) != pid:
                return _err(404, "任务不存在")
            async with ClientSession(timeout=ClientTimeout(total=8)) as session:
                state, outputs, error = await observe_native(session, _base_url(), pid)
            files = _resolve_outputs(outputs)
        except ValueError:
            return _err(404, "任务不存在")
        except (ClientError, TimeoutError):
            return _err(503, "暂时无法查询 ComfyUI 队列")
    if state == "missing":
        return _err(404, "任务不存在")
    status = {
        "accepted": "pending", "submitting": "pending", "pending": "pending",
        "in_progress": "running", "completed": "success", "cancelled": "error",
        "failed": "error",
    }.get(state, "needs_reconciliation")
    result = {"job_id": pid, "status": status, "native_status": state}
    if status == "success":
        result["files"] = files
    if error:
        result["error"] = error
    return _ok(result)


async def v_preflight(request):
    from execution import validate_prompt

    p = await _body(request)
    try:
        if not isinstance(p, dict) or not isinstance(p.get("params"), dict):
            raise ValueError("params 须为对象")
        project = _label(p.get("project"), "project", "本地项目")
        binding, params, take = None, p["params"], p.get("take")
        reference_plan = {}
        if "plan_shot" in p:
            shot, params, take, binding = bind_h3_shot(_jobs(), project, p["plan_shot"], params, take,
                                                       reference_plan=reference_plan)
        else:
            shot = _label(p.get("shot"), "shot")
        graph, info = await asyncio.to_thread(
            compile_plan, project, shot, params, take, _reference_resolver(project), binding=binding,
        )
        valid, error, _, node_errors = await validate_prompt(str(uuid4()), graph, None)
        if not valid:
            failures = [issue["details"] or issue["message"]
                        for node in node_errors.values() for issue in node["errors"]]
            raise ValueError("ComfyUI 工作流预检失败 · " + " · ".join(failures[:3] or [error["message"]]))
        if reference_plan:
            info["preflight"]["reference_plan"] = reference_plan
            review = [f"{ref['asset_id']} v{ref['revision']}" for ref in reference_plan["references"]
                      if ref["use"] == "review"]
            if review:
                info["preflight"]["warnings"].append("编排中仅供审片的引用：" + "、".join(review)
                                                      + "。模型实际接收的素材见输入清单。")
        previews = []
        if p.get("preview_reference_ends") is True:
            inputs = {ref["tag"]: ref for ref in info["references"]}
            spans = list(info["preflight"].get("video_ranges", []))
            if "control_range" in info["preflight"]:
                spans.append(info["preflight"]["control_range"])
            for span in spans:
                ref = inputs[span["tag"]]
                path = Path(folder_paths.get_input_directory()) / ref["input"]
                png = await asyncio.to_thread(video_frame_png, path, span["last_frame"],
                                              ffmpeg_tools.locate_ffmpeg(), thumbnail=True, control=ref["role"] == "control")
                previews.append({**span, "png": base64.b64encode(png).decode("ascii")})
    except (ValueError, TypeError, LookupError, OSError, RuntimeError, OverflowError) as exc:
        return _err(400, str(exc))
    result = {"project": project, "shot": shot, "info": info}
    if previews:
        result["reference_previews"] = previews
    if binding:
        result["submission"] = {
            "key": str(uuid4()),
            "body": {"project": project, "kind": "video", "plan_shot": binding,
                     "params": dict(p["params"], seed=str(info["seed"])), "take": info["preflight"]["take"],
                     "plan_sha256": info["preflight"]["plan_sha256"]},
        }
    return _ok(result)


async def j_create(request):
    p = await _body(request)
    try:
        if not isinstance(p, dict) or not isinstance(p.get("params"), dict):
            raise ValueError("params 须为对象")
        kind = p.get("kind", "video")
        if kind not in BUILDERS:
            raise ValueError("kind 须为 video、image 或 music")
        project = _label(p.get("project"), "project", "本地项目")
        shot = _label(p.get("shot"), "shot", "未归入镜头")
        key = _label(request.headers.get("Idempotency-Key"), "Idempotency-Key")
        service = _jobs()
        params, builder, take, binding = p["params"], BUILDERS[kind], p.get("take"), None
        if "plan_shot" in p:
            if kind != "video" or "plan_sha256" not in p:
                raise ValueError("按项目编排生成前，请先预检这个镜头")
            shot, params, take, binding = bind_h3_shot(service, project, p["plan_shot"], params, take)
            params["planned_shot"] = binding
        if "plan_sha256" in p:
            expected = p["plan_sha256"]
            if kind != "video" or not isinstance(expected, str) or len(expected) != 64:
                raise ValueError("plan_sha256 须为 H3 镜头预检返回的校验值")
            params = dict(params, plan_sha256=expected, take=take)
            resolver = _reference_resolver(project)
            builder = lambda options: confirm_plan(project, shot, options, resolver, binding=binding)
        row = await service.accept(kind, project, shot, key, params, builder)
    except RequestConflict as exc:
        return _err(409, str(exc))
    except NotImplementedError as exc:
        return _err(501, str(exc))
    except (ValueError, TypeError, LookupError, OSError, RuntimeError) as exc:
        return _err(400, str(exc))
    return web.json_response({"ok": True, "job": service.public(row)}, status=202)


async def j_cancel(request):
    service = _jobs()
    row = service.find(request.match_info["job_id"])
    if row is None:
        return _err(404, "任务不存在")
    try:
        return _ok({"job": service.public(await service.cancel(row))})
    except (ClientError, TimeoutError):
        return _err(503, "暂时无法确认取消结果，可查询原任务")


async def h_health(request):
    m = h3_graph.detect_models()
    compiler_disabled = None
    try:
        from comfy.cli_args import args as cli
        compiler_disabled = bool(getattr(cli, "disable_comfy_compiler", False))
    except Exception:
        pass
    return _ok({"routes": ROUTE_LIST, "models": m,
                "compiler_disabled": compiler_disabled,
                "capabilities": {
                    "video": bool(m.get("unet") and m.get("text_encoder") and m.get("video_vae")),
                    "controlnet": bool(m.get("model_patch")),
                    "music": bool(m.get("music_dit") and m.get("music_te") and m.get("music_vae")),
                    "image": bool(m.get("checkpoints")),
                },
                "ffmpeg": ffmpeg_tools.locate_ffmpeg(),
                "note": "语义级 analyse_media 与 synthesize_speech 仍为降级/不可用; "
                        "controlnet 与 music 需对应模型文件"})


ROUTE_LIST = [
    ("GET",  "/localhub/health",                  "工具清单+模型探测"),
    ("POST", "/localhub/hub/generate_video",      "← hub_generate_video (H3 原生音画, t2v/i2v/fl2v/r2v; 传 control_video 即启用 Fun ControlNet)"),
    ("POST", "/localhub/hub/generate_image",      "← hub_generate_image (SD checkpoint 文生图)"),
    ("POST", "/localhub/hub/video_edit",          "← hub_video_edit (concat/cut/replace_audio/mute)"),
    ("POST", "/localhub/hub/analyse_media",       "← hub_analyse_media (ffprobe 元数据级)"),
    ("POST", "/localhub/hub/generate_music",      "← hub_generate_music (MiniMax Music3; 需下载模型)"),
    ("POST", "/localhub/hub/synthesize_speech",   "→ 501 (可用 H3 原生对白替代)"),
    ("GET",  "/localhub/canvas/assets",           "← hub_canvas_get_node (资产清单)"),
    ("POST", "/localhub/canvas/record",           "资产登记"),
    ("POST", "/localhub/canvas/group_recent",     "← hub_canvas_group_recent_outputs"),
    ("POST", "/localhub/canvas/clear",            "清空画布"),
    ("GET",  "/localhub/jobs/{job_id}",           "任务状态查询"),
    ("POST", "/localhub/v2/video/preflight",      "预检 H3 镜头、参考、模型和取用范围"),
    ("POST", "/localhub/v2/jobs",                 "接收带请求键的生成任务"),
    ("POST", "/localhub/v2/media",                "登记本机实拍视频或独立音频候选"),
    ("GET",  "/localhub/v2/references",           "读取项目参考图版本"),
    ("POST", "/localhub/v2/references",           "登记参考图快照"),
    ("GET",  "/localhub/v2/jobs",                 "项目任务列表"),
    ("GET",  "/localhub/v2/jobs/{job_id}",        "查询及核对原任务"),
    ("POST", "/localhub/v2/jobs/{job_id}/cancel", "对指定任务发出取消"),
    ("GET",  "/localhub/v2/jobs/{job_id}/media/{index}", "预览已登记的候选"),
    ("GET",  "/localhub/v2/jobs/{job_id}/media/{index}/audio-qc", "读取已有音频质检报告"),
    ("GET",  "/localhub/v2/jobs/{job_id}/media/{index}/frame-notes", "读取候选源帧批注"),
    ("POST", "/localhub/v2/jobs/{job_id}/media/{index}/frame-notes", "记录候选源帧批注"),
    ("POST", "/localhub/v2/jobs/{job_id}/media/{index}/audio-qc", "测量候选的响度、峰值、静音与音画时长"),
    ("GET",  "/localhub/v2/timeline",             "当前项目剪辑清单"),
    ("GET",  "/localhub/v2/plan",                 "读取项目编排与历史版"),
    ("PUT",  "/localhub/v2/plan",                 "保存新编排版本"),
    ("GET",  "/localhub/v2/plan/cut",             "从项目编排起剪辑草稿"),
    ("GET",  "/localhub/v2/deliveries",           "项目交付版本"),
    ("GET",  "/localhub/v2/deliveries/subtitles", "读取交付字幕版"),
    ("PUT",  "/localhub/v2/deliveries/subtitles", "保存交付字幕版"),
    ("GET",  "/localhub/v2/deliveries/subtitles.srt", "下载指定字幕版"),
    ("POST", "/localhub/v2/deliveries/master-check", "检查交付母版技术规格"),
    ("GET",  "/localhub/v2/ad-copy",              "读取广告文案与审核历史"),
    ("POST", "/localhub/v2/ad-copy",              "保存广告文案版本"),
    ("PUT",  "/localhub/v2/ad-copy",              "审核指定文案版本"),
    ("POST", "/localhub/v2/deliveries",           "登记指向已存剪辑的交付规格"),
    ("PUT",  "/localhub/v2/timeline",             "保存新剪辑版本"),
    ("GET",  "/localhub/v2/timeline/otio",        "下载 OpenTimelineIO 文件"),
    ("POST", "/localhub/v2/timeline/otio",        "预览或保存回导版本"),
    ("GET",  "/localhub/v2/studio/status",        "制作台运行模式"),
    ("GET",  "/localhub/v2/legacy-canvas",         "读取项目的旧画布档案"),
    ("GET",  "/localhub/studio",                 "项目画布"),
]


def register_routes():
    ps = PromptServer.instance
    if ps is None:
        raise RuntimeError("PromptServer 未初始化")
    # PromptServer.routes 是 aiohttp RouteTableDef(延迟表), 在自定义节点导入之后
    # 才由 server.add_routes() 消费进 app — 与 Director http_routes.py 同款挂法
    table = ps.routes
    table.get("/localhub/health")(h_health)
    table.post("/localhub/hub/generate_video")(h_generate_video)
    table.post("/localhub/hub/generate_image")(h_generate_image)
    table.post("/localhub/hub/generate_music")(h_generate_music)
    table.post("/localhub/hub/synthesize_speech")(h_synthesize_speech)
    table.post("/localhub/hub/video_edit")(h_video_edit)
    table.post("/localhub/hub/analyse_media")(h_analyse_media)
    table.get("/localhub/canvas/assets")(c_assets)
    table.post("/localhub/canvas/record")(c_record)
    table.post("/localhub/canvas/group_recent")(c_group_recent)
    table.post("/localhub/canvas/clear")(c_clear)
    table.get("/localhub/jobs/{job_id}")(j_status)
    table.post("/localhub/v2/video/preflight")(v_preflight)
    table.post("/localhub/v2/jobs")(j_create)
    table.post("/localhub/v2/jobs/{job_id}/cancel")(j_cancel)
    register_project_routes(
        ps.app, table, jobs=_jobs, input_directory=folder_paths.get_input_directory,
        generation_enabled=True,
    )
    print("[LocalHubBridge] 本地 Hub 路由注册完成: {} 条 (前缀 /localhub)".format(len(ROUTE_LIST)))
