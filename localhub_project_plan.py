"""Saved project plans shared by Studio, the offline project desk, and Codex."""

import json
from fractions import Fraction

from localhub_references import list_references
from localhub_storage import RequestConflict
from localhub_timeline import _rate, read_edit, validate_edit


GENRES = ("ad", "film", "series")
REFERENCE_USES = ("reference", "first", "last", "review")


def blank_plan(project):
    return {"title": project, "genre": "ad", "rate": "24", "brief": "", "script": "",
            "episodes": [], "scenes": [], "shots": []}


def _project(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValueError("项目名须为 1–120 字符")
    return value.strip()


def _text(item, key, label, limit, *, required=False, exact=False):
    value = item.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{label}须为文本")
    value = value if exact else value.strip()
    if len(value) > limit or (required and not value) or "\x00" in value:
        raise ValueError(f"{label}须为{' 1–' if required else '最多 '}{limit} 个字符")
    return value


def _integer(value, label, *, optional=False, minimum=1, maximum=2**53 - 1):
    if value is None and optional:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}须为 {minimum} 到 {maximum} 之间的整数")
    return value


def _records(data, key, label, limit):
    values = data.get(key, [])
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError(f"{label}须为列表，最多 {limit} 条")
    seen = set()
    for row in values:
        if not isinstance(row, dict):
            raise ValueError(f"{label}条目须为对象")
        identity = _text(row, "id", label + "编号", 64, required=True)
        if any(ch.isspace() or ord(ch) < 32 for ch in identity) or identity in seen:
            raise ValueError(f"{label}编号须唯一且不含空白 · {identity}")
        seen.add(identity)
        yield row, {"id": identity, "title": _text(row, "title", label + "名称", 120, required=True)}


def _clip(jobs, project, rate, kind, value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("gap"):
        raise ValueError("选片须指向实际候选。暂未选片时置空")
    item = validate_edit(jobs, project, {"rate": rate, "tracks": [{"kind": kind, "clips": [value]}]},
                         allow_offline=True)
    return item["tracks"][0]["clips"][0]


def _references(values, known, title):
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError(f"{title} 的参考图须为列表，最多 32 项")
    result, used = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"{title} 的参考图须为对象")
        name = _text(value, "asset_id", "参考图名称", 120, required=True)
        revision = _integer(value.get("revision"), "参考图版号")
        if (name, revision) not in known:
            raise ValueError(f"{title} 找不到本项目参考图 · {name} v{revision}")
        use = value.get("use", "reference")
        if use not in REFERENCE_USES or (use in ("first", "last") and use in used):
            raise ValueError(f"{title} 的首尾帧或参考图用途有误")
        used.add(use)
        result.append({"asset_id": name, "revision": revision, "use": use,
                       "role": _text(value, "role", "参考用途说明", 120)})
    if "last" in used and "first" not in used:
        raise ValueError(f"{title} 锁定尾帧前须有首帧")
    return result


def validate_project_plan(jobs, project, data):
    if not isinstance(data, dict):
        raise ValueError("项目编排须为对象")
    genre = data.get("genre", "ad")
    if genre not in GENRES:
        raise ValueError("请选择广告、微电影或短剧")
    script = _text(data, "script", "剧本原文", 500000, exact=True)
    lines = len(script.replace("\r\n", "\n").replace("\r", "\n").split("\n")) if script else 0
    rate = str(_rate(data.get("rate", "24")))
    plan = {
        "title": _text(data, "title", "项目名称", 120, required=True), "genre": genre, "rate": rate,
        "brief": _text(data, "brief", "创作简报", 15000), "script": script,
        "episodes": [], "scenes": [], "shots": [],
    }
    episode_ids, scene_ids = set(), set()
    for row, episode in _records(data, "episodes", "分集", 250):
        pinned = _integer(row.get("cut_revision"), "分集剪辑版号", optional=True)
        if pinned is not None:
            read_edit(jobs, project, pinned)
        episode.update(summary=_text(row, "summary", "分集梗概", 4000), cut_revision=pinned)
        episode_ids.add(episode["id"])
        plan["episodes"].append(episode)
    for row, scene in _records(data, "scenes", "场次", 1000):
        episode = row.get("episode_id")
        if episode is not None and (not isinstance(episode, str) or episode not in episode_ids):
            raise ValueError(f"场次找不到所属分集 · {scene['title']}")
        scene.update(episode_id=episode, summary=_text(row, "summary", "场次说明", 4000),
                     script_line=_integer(row.get("script_line"), "场次剧本行号", optional=True, maximum=lines))
        scene_ids.add(scene["id"])
        plan["scenes"].append(scene)
    known = None
    for row, shot in _records(data, "shots", "镜头", 3000):
        scene = row.get("scene_id")
        if not isinstance(scene, str) or scene not in scene_ids:
            raise ValueError(f"镜头找不到所属场次 · {shot['title']}")
        if known is None and row.get("references"):
            known = {(ref["asset_id"], ref["revision"]) for ref in list_references(jobs, project)}
        shot.update(scene_id=scene, frames=_integer(row.get("frames"), "计划帧数", maximum=100000),
                    script_line=_integer(row.get("script_line"), "镜头剧本行号", optional=True, maximum=lines),
                    references=_references(row.get("references", []), known or set(), shot["title"]),
                    picture=_clip(jobs, project, rate, "Video", row.get("picture")),
                    sound=_clip(jobs, project, rate, "Audio", row.get("sound")))
        for key, label in (("action", "动作与构图"), ("prompt", "生成提示词"), ("dialogue", "对白"),
                           ("entry", "入镜状态"), ("exit", "出镜状态")):
            shot[key] = _text(row, key, label, 12000)
        plan["shots"].append(shot)
    return plan


def read_project_plan(jobs, project, revision=None):
    project = _project(project)
    revision = _integer(revision, "项目编排版号", optional=True)
    with jobs.db() as db:
        db.execute("BEGIN")
        row = latest = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_plans'").fetchone():
            latest = db.execute("SELECT revision,data FROM project_plans WHERE project=? ORDER BY revision DESC LIMIT 1",
                                (project,)).fetchone()
            row = latest
            if revision is not None and (row is None or row["revision"] != revision):
                row = db.execute("SELECT revision,data FROM project_plans WHERE project=? AND revision=?",
                                 (project, revision)).fetchone()
    if revision is not None and row is None:
        raise LookupError("找不到该项目编排版号")
    return {"project": project, "revision": row["revision"] if row else 0,
            "latest_revision": latest["revision"] if latest else 0,
            "plan": json.loads(row["data"]) if row else blank_plan(project)}


def save_project_plan(jobs, project, expected_revision, data):
    project = _project(project)
    expected = _integer(expected_revision, "项目编排基准版号", minimum=0, maximum=2**53 - 2)
    data = validate_project_plan(jobs, project, data)
    payload = json.dumps(data, ensure_ascii=False)
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS project_plans (
                project TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(project, revision)
            )
        """)
        current = db.execute("SELECT revision,data FROM project_plans WHERE project=? ORDER BY revision DESC LIMIT 1",
                             (project,)).fetchone()
        revision = current["revision"] if current else 0
        if not (current and current["data"] == payload and expected in (revision, revision - 1)):
            if expected != revision:
                raise RequestConflict("项目编排已有新版本。重新读取后再保存")
            revision += 1
            db.execute("INSERT INTO project_plans VALUES (?,?,?)", (project, revision, payload))
    return {"project": project, "revision": revision, "latest_revision": revision, "plan": data}


def plan_cut(jobs, project, plan_revision, episode_id=None):
    saved = read_project_plan(jobs, project, plan_revision)
    project = saved["project"]
    plan = saved["plan"]
    if episode_id is not None and episode_id not in {row["id"] for row in plan["episodes"]}:
        raise LookupError("这个编排版本中没有该分集")
    # Scene order governs cuts; within the scene, use the saved shot order.
    scenes = [row["id"] for row in plan["scenes"] if episode_id is None or row["episode_id"] == episode_id]
    by_scene = {scene: [] for scene in scenes}
    for shot in plan["shots"]:
        if shot["scene_id"] in by_scene:
            by_scene[shot["scene_id"]].append(shot)
    shots = [shot for group in by_scene.values() for shot in group]
    if not shots:
        raise ValueError("这个范围还没有镜头")
    rate = Fraction(plan["rate"])
    picture, sound, unselected = [], [], []
    has_sound = any(shot["sound"] for shot in shots)
    for shot in shots:
        clip = shot["picture"]
        seconds = Fraction(clip["duration_frames"]) / Fraction(clip["rate"]) if clip else shot["frames"] / rate
        picture.append(clip or {"gap": True, "duration_frames": shot["frames"]})
        if not clip:
            unselected.append(shot["id"])
        if not has_sound:
            continue
        audio = shot["sound"]
        audio_seconds = Fraction(0)
        if audio:
            sound.append(audio)
            audio_seconds = Fraction(audio["duration_frames"]) / Fraction(audio["rate"])
        gap = (seconds - audio_seconds) * rate
        if gap < 0 or gap.denominator != 1:
            raise ValueError(f"{shot['title']} 的音画长度无法按此计划帧率对齐，请核对取用帧")
        if gap:
            sound.append({"gap": True, "duration_frames": int(gap)})
    tracks = [{"kind": "Video", "name": "画面", "clips": picture}]
    if has_sound:
        tracks.append({"kind": "Audio", "name": "声音", "clips": sound})
    timeline = validate_edit(jobs, project, {"rate": str(rate), "tracks": tracks})
    return {"project": project, "plan_revision": saved["revision"], "episode_id": episode_id,
            "timeline": timeline, "unselected": unselected}


def bind_h3_shot(jobs, project, binding, options, take, *, reference_plan=None):
    """Use the immutable plan named by an H3 request and keep its identity in the receipt."""
    if not isinstance(binding, dict):
        raise ValueError("镜头编排须指定版号与镜头编号")
    revision = _integer(binding.get("revision"), "项目编排版号")
    shot_id = _text(binding, "shot_id", "镜头编号", 64, required=True)
    plan = read_project_plan(jobs, project, revision)["plan"]
    shot = next((row for row in plan["shots"] if row["id"] == shot_id), None)
    if shot is None:
        raise LookupError("所选编排版本中没有这个镜头")
    prompt = shot["prompt"] or shot["action"]
    if not prompt:
        raise ValueError("请在镜头编排中填写提示词或动作")
    duration = Fraction(shot["frames"]) / Fraction(plan["rate"])
    if duration > 15:
        raise ValueError("编排镜头超过 H3 预检的 15 秒范围，请先拆分镜头")
    frames = max(1, round(duration * 24))
    params, locked, pictures = dict(options, prompt=prompt, duration_s=float(duration)), {}, []
    for ref in shot["references"]:
        choice = {key: ref[key] for key in ("asset_id", "revision")}
        if ref["use"] == "reference":
            pictures.append(choice)
        elif ref["use"] in ("first", "last"):
            locked[ref["use"] + "_frame"] = choice
    if locked:
        if pictures or any(params.get(key) for key in ("ref_images", "ref_videos", "ref_audios")):
            raise ValueError("首尾帧模式请将其余图片标为审片参考，并清空附加多模态素材")
        params.update(locked, mode="fl2v" if "last_frame" in locked else "i2v")
    else:
        extra = params.get("ref_images", [])
        if not isinstance(extra, list):
            raise ValueError("附加参考图须为列表")
        params["ref_images"] = pictures + extra
        params["mode"] = "r2v" if any(params.get(key) for key in ("ref_images", "ref_videos", "ref_audios")) else "t2v"
    if take is None:
        take = {"start_frame": 0, "end_frame": frames}
    if reference_plan is not None:
        reference_plan.update(action=shot["action"], entry=shot["entry"], exit=shot["exit"],
                              references=shot["references"])
    return shot["title"], params, take, {"revision": revision, "shot_id": shot_id}
