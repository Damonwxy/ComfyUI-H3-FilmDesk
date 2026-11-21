"""Versioned picture/sound cuts and OpenTimelineIO interchange."""

import json
import math
import os
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from localhub_storage import RequestConflict
from localhub_media import candidate_file
from localhub_return_media import ReturnMedia, delivered_media


def read_edit(jobs, project, revision=None):
    with jobs.db() as db:
        return read_edit_in_db(db, project, revision)


def read_edit_in_db(db, project, revision=None):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edits'").fetchone():
        if revision is None:
            row = db.execute(
                "SELECT revision,data FROM edits WHERE project=? ORDER BY revision DESC LIMIT 1", (project,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT revision,data FROM edits WHERE project=? AND revision=?", (project, revision),
            ).fetchone()
        if row:
            return {"revision": row["revision"], "timeline": json.loads(row["data"])}
    if revision is not None:
        raise LookupError("该剪辑版本不存在")
    return {"revision": 0, "timeline": {"rate": "24", "tracks": []}}


def save_edit(jobs, project, expected, timeline):
    if type(expected) is not int or expected < 0:
        raise ValueError("expected_revision 须为非负整数")
    timeline = validate_edit(jobs, project, timeline)
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS edits (
                project TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(project, revision)
            )
        """)
        current, = db.execute("SELECT COALESCE(MAX(revision),0) FROM edits WHERE project=?", (project,)).fetchone()
        if current != expected:
            raise RequestConflict("剪辑清单已有新版本。重新读取后再保存。")
        db.execute("INSERT INTO edits VALUES (?,?,?)", (project, expected + 1, json.dumps(timeline, ensure_ascii=False)))
    return {"revision": expected + 1, "timeline": timeline}


def list_deliveries(jobs, project):
    with jobs.db() as db:
        rows = db.execute("""
            SELECT revision,name,market,language,aspect FROM deliveries WHERE project=?
            ORDER BY revision DESC, name, market, language, aspect
        """, (project,))
        return [dict(row) for row in rows]


def require_delivery(jobs, project, delivery):
    if not isinstance(delivery, dict) or type(delivery.get("revision")) is not int:
        raise ValueError("请选择已登记的剪辑交付规格")
    if delivery not in list_deliveries(jobs, project):
        raise LookupError("找不到完全匹配的项目交付规格")
    return read_edit(jobs, project, delivery["revision"])["timeline"]


def cut_duration(timeline):
    rate = timeline["rate"]
    return max((sum(Fraction(clip["duration_frames"]) / Fraction(clip.get("rate", rate))
                    for clip in track["clips"]) for track in timeline["tracks"]), default=0)


def register_delivery(jobs, project, values):
    revision = values.get("revision")
    if type(revision) is not int or not 1 <= revision < 2**63:
        raise ValueError("请选择已保存的剪辑版号")
    delivery = {"revision": revision}
    for key, label in (("name", "交付用途"), ("market", "市场"), ("language", "语言")):
        value = values.get(key)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
            raise ValueError(f"{label}须为 1–120 字符")
        delivery[key] = value.strip()
    aspect = values.get("aspect")
    if aspect not in ("16:9", "9:16", "1:1", "4:5", "4:3", "2.39:1"):
        raise ValueError("请选择支持的目标画幅")
    delivery["aspect"] = aspect
    cut = read_edit(jobs, project, revision)["timeline"]
    if not any(track["kind"] == "Video" and any(not clip.get("gap") for clip in track["clips"])
               for track in cut["tracks"]):
        raise ValueError("交付版至少需要一个画面片段")
    with jobs.db() as db:
        db.execute("""
            INSERT INTO deliveries(project,revision,name,market,language,aspect)
            VALUES (:project,:revision,:name,:market,:language,:aspect) ON CONFLICT DO NOTHING
        """, {"project": project, **delivery})
    return delivery


def _rate(value):
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("帧率须为数字或有理数，例如 24 或 24000/1001") from exc
    if not 1 <= rate <= 240:
        raise ValueError("帧率范围为 1–240")
    return rate


def _frames(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} 须为不小于 {minimum} 的整数")
    return value


def _recorded_clip(jobs, project, job_id, index):
    row = jobs.find(job_id)
    if row is None or row["project"] != project or row["state"] != "completed":
        raise ValueError("该候选不属于当前项目或尚未就绪")
    files = json.loads(row["files"])
    if not 0 <= index < len(files):
        raise ValueError(f"候选中找不到这个输出 · {row['shot']}")
    return Path(files[index]), row


def clip_path(jobs, project, job_id, index):
    path, row = _recorded_clip(jobs, project, job_id, index)
    if not candidate_file(path, json.loads(row["info"])):
        raise ValueError(f"候选文件缺失或已变动 · {row['shot']}")
    return path.resolve(), row


def validate_edit(jobs, project, timeline, *, allow_offline=False):
    if not isinstance(timeline, dict) or not isinstance(timeline.get("tracks"), list):
        raise ValueError("timeline 须包含 tracks 列表")
    rate = _rate(timeline.get("rate", "24"))
    tracks = []
    for track in timeline["tracks"]:
        if not isinstance(track, dict) or track.get("kind") not in ("Video", "Audio"):
            raise ValueError("轨道类型须为 Video 或 Audio")
        if not isinstance(track.get("clips"), list):
            raise ValueError("轨道须包含 clips 列表")
        normalized = []
        for clip in track["clips"]:
            if not isinstance(clip, dict):
                raise ValueError("片段须为对象")
            duration = _frames(clip.get("duration_frames"), "duration_frames", 1)
            if clip.get("gap") is True:
                normalized.append({"gap": True, "duration_frames": duration})
                continue
            job_id = clip.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("片段须关联项目候选")
            index = _frames(clip.get("file_index", 0), "file_index")
            lookup = _recorded_clip if allow_offline else clip_path
            _, row = lookup(jobs, project, job_id, index)
            start = _frames(clip.get("start_frame", 0), "start_frame")
            source_rate = _rate(clip.get("rate", str(rate)))
            info = json.loads(row["info"])
            if info.get("source") == "imported" and (
                (track["kind"] == "Video" and row["kind"] != "video")
                or (track["kind"] == "Audio" and not info.get("has_audio"))
            ):
                raise ValueError(f"素材没有该轨道所需的媒体 · {row['shot']}")
            if info.get("frames") and info.get("fps"):
                frames = info.get("audio_frames", info["frames"]) if track["kind"] == "Audio" else info["frames"]
                available = frames / _rate(info["fps"])
                if Fraction(start + duration, source_rate) > available:
                    raise ValueError(f"取用范围超过候选长度 · {row['shot']}")
            normalized.append({
                "job_id": job_id, "file_index": index, "start_frame": start,
                "duration_frames": duration, "rate": str(source_rate),
            })
        tracks.append({"kind": track["kind"], "name": str(track.get("name") or track["kind"]), "clips": normalized})
    return {"rate": str(rate), "tracks": tracks}


def _otio():
    try:
        import opentimelineio
    except ImportError as exc:
        raise NotImplementedError("剪辑交换需要安装 requirements-timeline.txt 中的 OpenTimelineIO") from exc
    return opentimelineio


def export_otio(jobs, project, data, *, for_resolve=False):
    otio = _otio()
    data = validate_edit(jobs, project, data)
    rate = float(_rate(data["rate"]))
    rt, tr = otio.opentime.RationalTime, otio.opentime.TimeRange
    timeline = otio.schema.Timeline(name=project, global_start_time=rt(0, rate))
    for value in data["tracks"]:
        track = otio.schema.Track(name=value["name"], kind=value["kind"])
        for item in value["clips"]:
            if item.get("gap"):
                track.append(otio.schema.Gap(source_range=tr(rt(0, rate), rt(item["duration_frames"], rate))))
                continue
            path, row = clip_path(jobs, project, item["job_id"], item["file_index"])
            fps = float(_rate(item["rate"]))
            target = str(path) if for_resolve and os.name == "nt" else path.as_uri()
            media = otio.schema.ExternalReference(target_url=target)
            info = json.loads(row["info"])
            if info.get("frames") and info.get("fps"):
                media_rate = float(_rate(info["fps"]))
                frames = info.get("audio_frames", info["frames"]) if value["kind"] == "Audio" else info["frames"]
                media.available_range = tr(rt(0, media_rate), rt(frames, media_rate))
            source = tr(rt(item["start_frame"], fps), rt(item["duration_frames"], fps))
            track.append(otio.schema.Clip(name=row["shot"], media_reference=media, source_range=source))
        timeline.tracks.append(track)
    return otio.adapters.write_to_string(timeline, adapter_name="otio_json")


def _whole(value, label):
    if not math.isfinite(value) or not math.isclose(value, round(value), abs_tol=0.00001, rel_tol=0):
        raise ValueError(f"{label} 无法精确落在整数帧上")
    return round(value)


def _media_key(url):
    # Resolve can use plain Windows paths or file: URLs for local source media.
    parts = urlsplit(url)
    if parts.scheme.casefold() == "file":
        root = (f"//{parts.netloc}" if parts.netloc.lower() not in ("", "localhost") else "")
        url = url2pathname(root + parts.path)
    return os.path.normcase(str(Path(url).resolve()))


def _has_active_effects(item):
    for effect in item.effects:
        data = effect.metadata.get("Resolve_OTIO")
        if effect.effect_name != "Resolve Effect" or not data:
            return True
        if data.get("Enabled") is False:
            continue
        # Resolve emits default inspector settings even for unmodified clips.
        params = data.get("Parameters")
        if params is None or any(
            "Default Parameter Value" not in param or "Parameter Value" not in param
            or param["Parameter Value"] != param["Default Parameter Value"] or param.get("Key Frames")
            for param in params
        ):
            return True
    return False


def import_otio(jobs, project, text, fallback_rate, *, inspect_sources=False, relink=None):
    otio = _otio()
    try:
        timeline = otio.adapters.read_from_string(text, adapter_name="otio_json")
    except (ValueError, RuntimeError) as exc:
        raise ValueError(f"OTIO 读取失败 · {exc}") from exc
    if not isinstance(timeline, otio.schema.Timeline) or timeline.tracks.effects or timeline.tracks.source_range:
        raise ValueError("请提供普通 OTIO Timeline")
    rate = _rate(fallback_rate)
    if timeline.global_start_time:
        rate = Fraction(timeline.global_start_time.rate).limit_denominator(1001)

    with jobs.db() as db:
        rows = db.execute("SELECT id,files,shot,info FROM jobs WHERE project=? AND state='completed'", (project,)).fetchall()
    known = {}
    rows_by_id = {row["id"]: row for row in rows}
    for row in rows:
        for index, path in enumerate(json.loads(row["files"])):
            known.setdefault(_media_key(path), []).append((row["id"], index))

    tracks = []
    for source in timeline.tracks:
        if not isinstance(source, otio.schema.Track) or source.effects or source.source_range:
            raise ValueError("当前回导支持普通视频与音频轨道")
        items = []
        for item in source:
            if isinstance(item, otio.schema.Gap):
                items.append({"gap": True, "duration_frames": _whole(
                    item.duration().rescaled_to(float(rate)).value, "空隙长度",
                )})
                continue
            if not isinstance(item, otio.schema.Clip) or _has_active_effects(item):
                raise ValueError(f"当前回导支持普通片段和空隙 · {item.name} 含效果、转场、变速或嵌套")
            media = item.media_reference
            if not isinstance(media, otio.schema.ExternalReference):
                raise ValueError(f"片段缺少外部媒体引用 · {item.name}")
            matches = known.get(_media_key(media.target_url), [])
            needs_review = len(matches) != 1
            if relink is not None and not needs_review:
                key = matches[0]
                row = rows_by_id[key[0]]
                needs_review = not relink.available(json.loads(row["files"])[key[1]], json.loads(row["info"]),
                                                    relink.delivered.get(key))
            if relink is not None and (needs_review or media.target_url in relink.evidence):
                identity = relink.resolve(media.target_url, rows, matches)
                if identity is None:
                    choices = relink.report[media.target_url]["choices"]
                    identity = {"unresolved": media.target_url, "name": item.name or "未命名素材",
                                "reason": "需确认候选记录" if choices else "未匹配到可核实的项目候选"}
            elif len(matches) == 1:
                job_id, index = matches[0]
                identity = {"job_id": job_id, "file_index": index}
            elif inspect_sources:
                identity = {"unresolved": media.target_url, "name": item.name or "未命名素材",
                            "reason": "匹配多个候选" if matches else "未匹配到项目候选"}
            elif matches:
                raise ValueError(f"回导时匹配多个项目候选 · {item.name}")
            else:
                raise ValueError(f"回导时无法匹配项目候选 · {item.name}")
            take = item.trimmed_range()
            fps = take.duration.rate
            offset = take.start_time.rescaled_to(fps).value
            if media.available_range:
                offset -= media.available_range.start_time.rescaled_to(fps).value
            items.append({
                **identity,
                "start_frame": _whole(offset, f"{item.name} 入点"),
                "duration_frames": _whole(take.duration.value, f"{item.name} 长度"),
                "rate": str(Fraction(fps).limit_denominator(1001)),
            })
        tracks.append({"kind": source.kind, "name": source.name, "clips": items})
    cut = {"rate": str(rate), "tracks": tracks}
    if inspect_sources and any("unresolved" in clip for track in tracks for clip in track["clips"]):
        # Unknown references are descriptive only. Validate all known portions before showing them.
        validate_edit(jobs, project, {**cut, "tracks": [
            {**track, "clips": [c for c in track["clips"] if "unresolved" not in c]} for track in tracks
        ]})
        return cut
    return validate_edit(jobs, project, cut)


def _clip_changes(before, after):
    # Keep equal occurrences at either end fixed before aligning the middle.
    # Otherwise [A, A] -> [trimmed A, A] can look like an insertion and a deletion.
    start, left_end, right_end = 0, len(before), len(after)
    while start < min(left_end, right_end) and before[start] == after[start]:
        start += 1
    while min(left_end, right_end) > start and before[left_end - 1] == after[right_end - 1]:
        left_end -= 1
        right_end -= 1
    left = [json.dumps(clip, sort_keys=True) for clip in before[start:left_end]]
    right = [json.dumps(clip, sort_keys=True) for clip in after[start:right_end]]
    return [{"before_at": start + a, "before": before[start+a:start+b],
             "after_at": start + c, "after": after[start+c:start+d]}
            for op, a, b, c, d in SequenceMatcher(None, left, right, autojunk=False).get_opcodes() if op != "equal"]


def _changed_tracks(base, returned):
    changes = []
    for kind in ("Video", "Audio"):
        old = [t for t in base["tracks"] if t["kind"] == kind]
        new = [t for t in returned["tracks"] if t["kind"] == kind]
        for index in range(max(len(old), len(new))):
            left = old[index] if index < len(old) else {}
            right = new[index] if index < len(new) else {}
            before, after = left.get("clips", []), right.get("clips", [])
            ranges = _clip_changes(before, after)
            if ranges or left.get("name") != right.get("name"):
                changes.append({"kind": kind, "index": index + 1, "before_name": left.get("name"),
                                "after_name": right.get("name"), "ranges": ranges})
    return changes


def _draft_span(change, local_changes):
    """Map a base interval through local edits; a boundary inside a changed block is ambiguous."""
    start = change["before_at"]
    end = start + len(change["before"])
    shift = inside = 0
    for local in local_changes:
        a = local["before_at"]
        b = a + len(local["before"])
        delta = len(local["after"]) - len(local["before"])
        if a == b == start == end:
            inside += delta
        elif b <= start:
            shift += delta
        elif a >= end:
            break
        elif start <= a and b <= end:
            inside += delta
        else:
            return None
    return start + shift, end + shift + inside


def _partial_choices(base, returned, draft, changes):
    """Attach draft-relative splice locations to a read-only comparison."""
    for change in changes:
        kind, index = change["kind"], change["index"] - 1
        original = [t for t in base["tracks"] if t["kind"] == kind]
        incoming = [t for t in returned["tracks"] if t["kind"] == kind]
        lanes = [(i, t) for i, t in enumerate(draft["tracks"]) if t["kind"] == kind]
        old_name, new_name = change["before_name"], change["after_name"]
        wanted = old_name or new_name
        matches = [(i, t) for i, t in lanes if t["name"] == wanted]
        # The interchange has no stable track ID. Avoid guessing across reorders or duplicate names.
        if ((old_name and sum(t["name"] == old_name for t in original) != 1)
                or (new_name and sum(t["name"] == new_name for t in incoming) != 1)
                or len(matches) > 1
                or (old_name != new_name and (any(t["name"] == new_name for t in original)
                                              or any(t["name"] == old_name for t in incoming)))):
            change["pick"] = {"blocked": "轨道身份有歧义，请按整份剪版核对。"}
            continue
        if old_name is None:
            after = incoming[index]
            if matches:
                change["pick"] = {"blocked": "草稿已含该轨道。" if matches[0][1] == after else "草稿已有同名轨道。"}
            elif any("unresolved" in c for c in after["clips"]):
                change["pick"] = {"blocked": "先登记或重连这条轨道的素材。"}
            else:
                change["pick"] = {"op": "add", "track": after}
            continue
        if not matches:
            change["pick"] = {"blocked": "草稿中找不到原轨道。"}
            continue
        track_index, local_track = matches[0]
        if old_name != new_name:
            if new_name is not None:
                change["pick"] = {"blocked": "轨道已改名或重排，请按整份剪版核对。"}
            else:
                change["pick"] = {"op": "remove", "track_index": track_index,
                                  "conflict": local_track != original[index], "local": local_track["clips"]}
            continue
        edits = _clip_changes(original[index]["clips"], local_track["clips"])
        for part in change["ranges"]:
            span = _draft_span(part, edits)
            if any("unresolved" in c for c in part["after"]):
                part["pick"] = {"blocked": "先登记或重连这段素材。"}
            elif span is None:
                part["pick"] = {"blocked": "本机改动跨过这组切点，无法确定替换边界。"}
            else:
                start, end = span
                local = local_track["clips"][start:end]
                if local == part["after"]:
                    part["pick"] = {"blocked": "草稿已采用这处改动。"}
                else:
                    part["pick"] = {"op": "splice", "track_index": track_index, "at": start, "count": end - start,
                                    "conflict": local != part["before"], "local": local}


def return_review(jobs, project, text, base_revision, current, draft=None, *, evidence=None, manifest=None):
    if type(base_revision) is not int or not 0 <= base_revision < 2**63:
        raise ValueError("请选择上次交出的剪辑版本")
    if base_revision == 0 and current["revision"]:
        raise ValueError("请选择已保存的交出版号")
    base = read_edit(jobs, project, base_revision)["timeline"] if base_revision else current["timeline"]
    relink = ReturnMedia({} if evidence is None else evidence, delivered_media(manifest, project, base_revision, base))
    returned = import_otio(jobs, project, text, base["rate"], inspect_sources=True, relink=relink)
    unresolved = any("unresolved" in c for track in returned["tracks"] for c in track["clips"])
    changes = _changed_tracks(base, returned)
    partial_error = ""
    try:
        local = current["timeline"] if draft is None else validate_edit(jobs, project, draft, allow_offline=True)
        if base["rate"] == local["rate"] == returned["rate"]:
            _partial_choices(base, returned, local, changes)
        else:
            partial_error = "剪版帧率不同，请按整份剪版核对。"
    except ValueError as exc:
        partial_error = f"请先修正当前草稿 · {exc}"
    return {"revision": current["revision"], "base_revision": base_revision, "base_rate": base["rate"],
            "rate": returned["rate"], "changes": changes, "partial_error": partial_error,
            "current_differs": current["timeline"] != base and current["timeline"] != returned,
            "timeline": None if unresolved else returned, "source_review": relink.report}
