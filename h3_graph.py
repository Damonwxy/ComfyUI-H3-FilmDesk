# H3 / SD 工作流构建器 — 生成 API 格式(即 /prompt 接口格式)的节点图
#
# H3 图结构依据:
# - comfy_extras/nodes_minimax_h3.py (核心节点, v0.34.5 本机源码)
# - Director executor_core._decode_av_latent 注释确认的官方解码接法:
#   VAEDecode 与 VAEDecodeAudio 共享同一个 AV latent
# - 帧数对齐规则: n % 17 == 5 (17k+5 网格, 24fps); 画布: 短边768/面积上限768*1344/轴对齐32

import math
import os
import time
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from ffmpeg_tools import locate_ffmpeg
from localhub_media import stash_input as _copy_to_input


def snap_frames(n):
    """帧数向上对齐到 17k+5 网格(核心源码 align_frame_count)。"""
    n = max(5, int(n))
    while n % 17 != 5:
        n += 1
    return n


def adapt_canvas(width, height):
    """轴对齐32 + 面积上限保护。

    注意: 不使用核心源码 adapt_canvas 的"短边768放大"逻辑——那会把 864x480
    自动放大到 1376x768, 在 6G 显存机器上不可行。这里保持用户给的画布,
    仅做对齐与超上限缩放。
    """
    CANVAS_MULTIPLE = 32
    MAX_PIXELS = 768 * 1344
    width = max(32, int(width) - int(width) % CANVAS_MULTIPLE)
    height = max(32, int(height) - int(height) % CANVAS_MULTIPLE)
    if width * height > MAX_PIXELS:
        s = math.sqrt(MAX_PIXELS / (width * height))
        width = max(CANVAS_MULTIPLE, round(width * s / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        height = max(CANVAS_MULTIPLE, round(height * s / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    return width, height


def duration_to_frames(duration_s):
    return snap_frames(round(float(duration_s) * 24))


def _reference_video(path, include_audio, frames):
    """Conform a reference to H3's 24 fps time base before native decoding."""
    import folder_paths

    ffmpeg = locate_ffmpeg()
    if not ffmpeg:
        raise ValueError("视频参考需要 FFmpeg")
    root = Path(folder_paths.get_input_directory())
    src = root / _copy_to_input(path)
    mode = "av" if include_audio else "v"
    dst = root / f"{src.stem}_ref24v1_{frames}_{mode}.mp4"
    if dst.exists():
        return dst.name
    with NamedTemporaryFile(dir=root, prefix=".localhub-", suffix=".mp4", delete=False) as temp:
        staged = Path(temp.name)
    try:
        audio = ["-map", "0:a:0", "-c:a", "aac"] if include_audio else ["-an"]
        subprocess.run([
            ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(src),
            "-map", "0:v:0", *audio, "-t", str(frames / 24),
            "-vf", "fps=24,pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-preset", "veryfast", str(staged),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", timeout=180, check=True)
        if not dst.exists():
            staged.replace(dst)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"参考视频处理失败 · {exc.stderr.strip()[-400:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("参考视频转为 24 fps 超时") from exc
    finally:
        staged.unlink(missing_ok=True)
    return dst.name


def _reference_inputs(graph, params, frames):
    cond, refs = {}, []
    audio_index = 0
    for index, path in enumerate(params.get("ref_images") or []):
        node, copied = f"ref-image-{index}", _copy_to_input(path)
        graph[node] = {"class_type": "LoadImage", "inputs": {"image": copied}}
        cond[f"ref_images.ref_image_{index}"] = [node, 0]
        refs.append({"tag": f"<Picture {index + 1}>", "role": "image", "input": copied, "source": os.fspath(path)})
    for index, ref in enumerate(params.get("ref_videos") or []):
        path = ref["path"] if isinstance(ref, dict) else ref
        include_audio = ref.get("include_audio", False) if isinstance(ref, dict) else False
        if not isinstance(include_audio, bool):
            raise ValueError("include_audio 须为布尔值")
        copied = _reference_video(path, include_audio, frames)
        node, split = f"ref-video-{index}", f"ref-video-components-{index}"
        graph[node] = {"class_type": "LoadVideo", "inputs": {"file": copied}}
        graph[split] = {"class_type": "GetVideoComponents", "inputs": {"video": [node, 0]}}
        cond[f"ref_videos.ref_video_{index}"] = [split, 0]
        if include_audio:
            cond[f"ref_video_audios.ref_video_audio_{index}"] = [split, 1]
            audio_index += 1
            refs.append({"tag": f"<Audio {audio_index}>", "role": "soundtrack", "input": copied,
                         "source": os.fspath(path)})
        refs.append({"tag": f"<Video {index + 1}>", "role": "video", "input": copied, "source": os.fspath(path)})
    for index, path in enumerate(params.get("ref_audios") or []):
        node, copied = f"ref-audio-{index}", _copy_to_input(path)
        graph[node] = {"class_type": "LoadAudio", "inputs": {"audio": copied}}
        cond[f"ref_audios.ref_audio_{index}"] = [node, 0]
        audio_index += 1
        refs.append({"tag": f"<Audio {audio_index}>", "role": "audio", "input": copied, "source": os.fspath(path)})
    return cond, refs


def detect_models():
    """按命名约定自动选择本机已下载的 H3 模型文件。"""
    import folder_paths
    def pick(key, prefs, fallback=True):
        """fallback=True 时找不到匹配就取第一个文件(主模型/通用槽);
        fallback=False 时严格匹配, 用于新增能力(music/controlnet)避免误认。"""
        try:
            files = folder_paths.get_filename_list(key)
        except Exception:
            return None
        files = [f for f in files if not f.startswith("put_")]
        for p in prefs:
            for f in files:
                if all(k in f for k in p):
                    return f
        return files[0] if (fallback and files) else None

    return {
        "unet": pick("diffusion_models", [["fl2va", "pruned_int8"], ["fl2va", "int8"], ["fl2va"]]),
        "unet_ref2va": pick("diffusion_models", [["ref2va", "pruned_int8"], ["ref2va", "int8"], ["ref2va"]], fallback=False),
        "lora": pick("loras", [["fl2v_turbo_8step", "comfyui"], ["fl2v_turbo", "comfyui"], ["fl2v_turbo"]]),
        "lora_ref2v": pick("loras", [["ref2v_turbo", "comfyui"], ["ref2v_turbo"], ["ref2va_turbo"]], fallback=False),
        # 保留已安装 int8 优先的选择顺序，其次 NVFP4。
        # 官方 NVFP4 文本编码器支持非 Blackwell GPU；cu130 优先 int8 的建议针对扩散模型。
        # 2026-10-23: 长提示词异常已在 Sage 路径复现，不能据此认定 NVFP4 有 16-token 上限。
        "text_encoder": pick("text_encoders", [["minimax_h3_int8_convrot"], ["nvfp4_awq"], ["qwen3vl"]])
                        or pick("clip", [["minimax_h3_int8_convrot"], ["nvfp4_awq"], ["qwen3vl"]]),
        # int8 VAE 优先: 2.8GB vs fp16 5.2GB, 对 6G 显存/16G 内存更友好
        "video_vae": pick("vae", [["video_vae_int8"], ["video_vae"]]),
        "audio_vae": pick("vae", [["audio_vae"]]),
        "checkpoints": pick("checkpoints", []),
        # 新增能力(对应文件存在才可用); fallback=False: 不误认其它模型
        "model_patch": pick("model_patches", [["fun_controlnet", "union", "pruned_int8"],
                                              ["fun_controlnet", "union"], ["fun_controlnet"]],
                            fallback=False),
        "music_dit": pick("diffusion_models", [["minimax_music3_dit_int8"], ["minimax_music3_dit"]],
                          fallback=False),
        "music_te": pick("text_encoders", [["minimax_music3_text_encoder_pruned_int8"],
                                           ["minimax_music3_text_encoder"]],
                         fallback=False),
        "music_vae": pick("vae", [["minimax_music3_dav"]], fallback=False),
    }


def _node_available(node_id):
    """检查当前 ComfyUI 进程是否注册了某节点(用于可选的低显存节点)。"""
    try:
        import nodes
        return node_id in nodes.NODE_CLASS_MAPPINGS
    except Exception:
        return False


def build_h3_graph(params):
    """构建 t2v / i2v / fl2v / r2v 的 API 格式工作流。

    params: mode, prompt, negative, width, height, duration_s, seed, steps,
            first_frame, last_frame, models(dict, 可选覆盖),
            unet_name/lora_name/te_name/video_vae/audio_vae 单项覆盖,
            lora_strength, shift_video, shift_audio, prefix
    返回 (graph, info)
    """
    m = dict(params.get("models") or detect_models())
    for k in ("unet_name", "lora_name", "te_name", "video_vae", "audio_vae"):
        if params.get(k):
            key = {"unet_name": "unet", "lora_name": "lora", "te_name": "text_encoder",
                   "video_vae": "video_vae", "audio_vae": "audio_vae"}[k]
            m[key] = params[k]
    mode = params.get("mode", "t2v")
    if mode not in ("t2v", "i2v", "fl2v", "r2v"):
        raise ValueError("mode 仅支持 t2v / i2v / fl2v / r2v")
    if mode in ("i2v", "fl2v") and not params.get("first_frame"):
        raise ValueError("{} 模式需要 first_frame 图片路径".format(mode))
    if mode == "fl2v" and not params.get("last_frame"):
        raise ValueError("fl2v 模式需要 last_frame 图片路径")
    if mode == "r2v":
        reference_lists = {key: params.get(key) or [] for key in ("ref_images", "ref_videos", "ref_audios")}
        for key, limit in (("ref_images", 9), ("ref_videos", 3), ("ref_audios", 3)):
            if not isinstance(reference_lists[key], list) or len(reference_lists[key]) > limit:
                raise ValueError(f"{key} 须为列表，当前节点最多接受 {limit} 项")
        if not any(reference_lists.values()):
            raise ValueError("r2v 需要至少一项图片、视频或音频参考")
        if not params.get("unet_name"):
            m["unet"] = m.get("unet_ref2va") or (params.get("models") or {}).get("unet")
        if not m.get("unet"):
            raise ValueError("r2v 缺少 ref2va 模型")
        if not params.get("lora_name"):
            m["lora"] = m.get("lora_ref2v") or (params.get("models") or {}).get("lora")

    missing = [k for k in ("unet", "text_encoder", "video_vae", "audio_vae") if not m.get(k)]
    if missing:
        raise ValueError("缺少模型文件: {}".format(", ".join(missing)))

    width, height = adapt_canvas(params.get("width", 864), params.get("height", 480))
    length = duration_to_frames(params.get("duration_s", 5))
    steps = int(params.get("steps", 8))
    seed = int(params.get("seed", 0)) if params.get("seed") is not None else None
    if seed is None:
        import random
        seed = random.randint(0, 2**48)

    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}}
    model_src = ["1", 0]

    # 6G 显存优化(KJNodes, Director 同款): FFN 分块 + 注意力头分块
    # auto: 标准档(<=864x480 且 <=124帧)关闭以保速度; 更大画布/更长时长自动开启防 OOM
    lv = params.get("low_vram", "auto")
    if isinstance(lv, str) and lv == "auto":
        low_vram = (width * height > 864 * 480) or (length > 124)
    else:
        low_vram = bool(lv)
    if low_vram and _node_available("MiniMaxChunkFeedForward") and _node_available("MiniMaxLowVRAMAttention"):
        g["1a"] = {"class_type": "MiniMaxChunkFeedForward",
                   "inputs": {"model": model_src,
                              "chunks": int(params.get("chunks", 8)),
                              "seq_threshold": int(params.get("seq_threshold", 4096))}}
        g["1b"] = {"class_type": "MiniMaxLowVRAMAttention",
                   "inputs": {"model": ["1a", 0],
                              "head_chunks": int(params.get("head_chunks", 12))}}
        model_src = ["1b", 0]

    # INT8 注意力后端(核心 ModelAttentionBackend; Director 同款默认)
    attn = params.get("attention_backend", "comfy kitchen attention")
    if attn and _node_available("ModelAttentionBackend"):
        g["1c"] = {"class_type": "ModelAttentionBackend",
                   "inputs": {"model": model_src, "attention": attn}}
        model_src = ["1c", 0]

    if m.get("lora") and params.get("use_lora", True):
        g["2"] = {"class_type": "LoraLoaderModelOnly",
                  "inputs": {"lora_name": m["lora"],
                             "strength_model": float(params.get("lora_strength", 1.0)),
                             "model": model_src}}
        model_src = ["2", 0]
    g["3"] = {"class_type": "MiniMaxH3SigmaShift",
              "inputs": {"model": model_src,
                         "shift_video": float(params.get("shift_video", 12.0)),
                         "shift_audio": float(params.get("shift_audio", 3.0))}}
    g["4"] = {"class_type": "CLIPLoader",
              "inputs": {"clip_name": m["text_encoder"], "type": "minimax", "device": "default"}}
    g["5"] = {"class_type": "VAELoader", "inputs": {"vae_name": m["video_vae"]}}
    g["6"] = {"class_type": "VAELoader", "inputs": {"vae_name": m["audio_vae"]}}

    prompt_text = params.get("prompt", "")
    if params.get("music_direction"):
        prompt_text = prompt_text.rstrip() + "\n" + params["music_direction"].strip()

    cond_inputs = {"clip": ["4", 0], "vae": ["5", 0],
                   "prompt": prompt_text, "width": width, "height": height, "length": length}
    if mode in ("i2v", "fl2v"):
        g["20"] = {"class_type": "LoadImage", "inputs": {"image": _copy_to_input(params["first_frame"])}}
        cond_inputs["first_frame"] = ["20", 0]
    if mode == "fl2v":
        g["21"] = {"class_type": "LoadImage", "inputs": {"image": _copy_to_input(params["last_frame"])}}
        cond_inputs["last_frame"] = ["21", 0]
    refs = []
    if mode == "r2v":
        ref_inputs, refs = _reference_inputs(g, params, length)
        g["7"] = {"class_type": "MiniMaxH3ReferenceToVideo",
                  "inputs": dict(cond_inputs,
                                 audio_vae=["6", 0],
                                 ref_image_size=params.get("ref_image_size", "match"),
                                  **ref_inputs)}
    else:
        g["7"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": cond_inputs}

    g["8"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}}
    g["9"] = {"class_type": "KSampler",
              "inputs": {"model": ["3", 0], "positive": ["7", 0], "negative": ["8", 0],
                         "latent_image": ["7", 1], "seed": seed, "steps": steps,
                         "cfg": float(params.get("cfg", 1.0)),
                         "sampler_name": params.get("sampler", "euler"),
                         "scheduler": params.get("scheduler", "beta"), "denoise": 1.0}}
    g["10"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["5", 0]}}
    g["11"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["9", 0], "vae": ["6", 0]}}
    g["12"] = {"class_type": "CreateVideo",
               "inputs": {"images": ["10", 0], "audio": ["11", 0],
                          "fps": 24.0, "bit_depth": "auto"}}
    # API 直连不经过前端, %date:% 占位符不会被替换 — 日期由服务端直接生成
    prefix = params.get("prefix") or time.strftime("video/localhub/%Y-%m-%d/%H%M%S")
    g["13"] = {"class_type": "SaveVideo",
               "inputs": {"video": ["12", 0], "filename_prefix": prefix,
                          "format": "auto", "codec": "auto"}}

    info = {"mode": mode, "width": width, "height": height, "frames": length,
             "fps": 24, "references": refs,
            "duration_s": round(length / 24.0, 2), "steps": steps, "seed": seed,
            "low_vram": ("1b" in g), "low_vram_mode": lv,
            "attention_backend": attn if "1c" in g else None,
            "chunks": int(params.get("chunks", 8)) if "1a" in g else None,
            "head_chunks": int(params.get("head_chunks", 12)) if "1b" in g else None,
            "models": {k: m.get(k) for k in ("unet", "lora", "text_encoder", "video_vae", "audio_vae")},
            "native_audio": True}
    return g, info


def build_sd_image_graph(params, checkpoint):
    """SD 系列文生图(hub_generate_image 的本地实现, 需要至少一个 checkpoint)。"""
    import random
    width = int(params.get("width", 1024))
    height = int(params.get("height", 1024))
    width -= width % 32
    height -= height % 32
    seed = params.get("seed")
    seed = random.randint(0, 2**48) if seed is None else int(seed)
    g = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": params.get("prompt", ""),
                                                         "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": params.get("negative", ""),
                                                         "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height,
                                                           "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0], "seed": seed,
                         "steps": int(params.get("steps", 24)),
                         "cfg": float(params.get("cfg", 7.0)),
                         "sampler_name": params.get("sampler", "euler"),
                         "scheduler": params.get("scheduler", "normal"), "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0],
                         "filename_prefix": params.get("prefix") or time.strftime("localhub/image/%Y-%m-%d/img")}},
    }
    info = {"width": width, "height": height, "seed": seed, "checkpoint": checkpoint}
    return g, info


def build_control_video_graph(params):
    """Fun ControlNet-Union: 在基础图之上插入控制分支。

    官方模板(video_minimax_h3_fun_controlnet_union.json)依据:
      ModelPatchLoader -> MiniMaxH3FunControlNetApply(model, model_patch, vae, strength,
      start_percent, end_percent, control_video) -> KSampler
    支持 Canny / Depth / HED / MLSD / Pose 控制视频(控制视频由调用方预先处理好)。
    官方说明: 同一 checkpoint 可用于 fl2va 或 ref2va 主模型。
    """
    cv = params.get("control_video")
    if not cv:
        raise ValueError("ControlNet 模式需要 control_video 视频路径")

    g, info = build_h3_graph(params)
    m = params.get("models") or detect_models()
    patch = params.get("model_patch") or m.get("model_patch")
    if not patch:
        raise ValueError("缺少 Fun ControlNet 模型: 请下载 "
                         "model_patches/minimax_h3_fun_controlnet_union_pruned_int8_convrot.safetensors "
                         "(约 2.1GB) 放入 models/model_patches/")

    g["40"] = {"class_type": "LoadVideo", "inputs": {"file": _copy_to_input(cv)}}
    g["41"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["40", 0]}}
    g["42"] = {"class_type": "ModelPatchLoader", "inputs": {"name": patch}}
    g["43"] = {"class_type": "MiniMaxH3FunControlNetApply",
               "inputs": {"model": ["3", 0], "model_patch": ["42", 0], "vae": ["5", 0],
                          "strength": float(params.get("control_strength", 1.0)),
                          "start_percent": float(params.get("control_start", 0.0)),
                          "end_percent": float(params.get("control_end", 1.0)),
                          "control_video": ["41", 0]}}
    g["9"]["inputs"]["model"] = ["43", 0]

    info["controlnet"] = {"model_patch": patch, "control_video": os.path.abspath(cv),
                          "strength": float(params.get("control_strength", 1.0)),
                          "start_percent": float(params.get("control_start", 0.0)),
                          "end_percent": float(params.get("control_end", 1.0))}
    return g, info


MUSIC_DOWNLOADS = (
    "minimax_music3_dit_int8_convrot.safetensors (~2.5GB) -> models/diffusion_models\n"
    "minimax_music3_text_encoder_pruned_int8_convrot.safetensors (~9.2GB) -> models/text_encoders\n"
    "minimax_music3_dav.safetensors (~207MB) -> models/vae\n"
    "下载源: https://huggingface.co/Comfy-Org/MiniMax-Music-3"
)


def build_music_graph(params):
    """MiniMax Music 3 文生音乐(官方 audio_minimax_music_3.json 图结构)。

    caption 三段式(Global Metadata / Vocal Details / Arrangement),
    lyrics 支持 [intro] [verse] [chorus] [bridge] [outro] 结构标签。
    """
    m = params.get("models") or detect_models()
    missing = [k for k in ("music_dit", "music_te", "music_vae") if not m.get(k)]
    if missing:
        raise ValueError("缺少 Music3 模型: {}\n{}".format(", ".join(missing), MUSIC_DOWNLOADS))

    caption = params.get("caption") or params.get("prompt") or ""
    if not caption:
        raise ValueError("music 模式需要 caption(音乐描述)")
    lyrics = params.get("lyrics", "")
    max_duration = float(params.get("duration_s", 120))
    seed = params.get("seed")
    if seed is None:
        import random
        seed = random.randint(0, 2**48)
    seed = int(seed)
    steps = int(params.get("steps", 30))
    cfg = float(params.get("cfg", 1.7))

    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["music_dit"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["music_te"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["music_vae"]}},
        "4": {"class_type": "MiniMaxMusic3TextEncode",
              "inputs": {"clip": ["2", 0], "caption": caption, "lyrics": lyrics, "seed": seed,
                         "max_duration": max_duration, "cfg_scale": cfg,
                         "top_k": int(params.get("top_k", 50))}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyMiniMaxMusic3LatentAudio",
              "inputs": {"seconds": ["4", 1], "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["6", 0], "seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": params.get("sampler", "euler"),
                         "scheduler": params.get("scheduler", "simple"), "denoise": 1.0}},
        "8": {"class_type": "VAEDecodeAudioTiled",
              "inputs": {"samples": ["7", 0], "vae": ["3", 0],
                         "tile_size": int(params.get("tile_size", 1536)),
                         "overlap": int(params.get("overlap", 64))}},
        "9": {"class_type": "SaveAudio",
              "inputs": {"audio": ["8", 0],
                         "filename_prefix": params.get("prefix") or time.strftime("audio/localhub/%Y-%m-%d/music")}},
    }
    info = {"caption": caption[:120], "lyrics_chars": len(lyrics),
            "max_duration_s": max_duration, "steps": steps, "cfg": cfg, "seed": seed,
            "models": {"dit": m["music_dit"], "text_encoder": m["music_te"], "vae": m["music_vae"]}}
    return g, info
