"""Export a saved ad delivery from the local bridge database while ComfyUI is offline."""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def add_media(archive, path, name):
    from localhub_media import file_stamp

    stamp = file_stamp(path)
    digest = hashlib.sha256()
    with path.open("rb") as src, archive.open(name, "w", force_zip64=True) as dst:
        while chunk := src.read(1024 * 1024):
            digest.update(chunk)
            dst.write(chunk)
    if file_stamp(path) != stamp:
        raise ValueError("打包期间媒体发生变更")
    return {"path": name, "bytes": stamp[0], "sha256": digest.hexdigest()}


def make_pack(database, spec, destination, ffprobe):
    import opentimelineio as otio
    from localhub_copy import list_copy
    from localhub_delivery import describe_video_sources, verify_master, video_finishing
    from localhub_media import file_stamp
    from localhub_storage import ProjectDatabase
    from localhub_subtitles import read_subtitles, subtitle_srt
    from localhub_timeline import clip_path, export_otio, require_delivery

    reader = ProjectDatabase(database)
    project, delivery = spec["project"], spec["delivery"]
    cut = require_delivery(reader, project, delivery)
    timeline = otio.adapters.read_from_string(export_otio(reader, project, cut), adapter_name="otio_json")
    sheet = captions = None
    if spec.get("subtitle_revision") is not None:
        sheet = read_subtitles(reader, project, delivery, spec["subtitle_revision"])
        captions = subtitle_srt(sheet).encode("utf-8")
    entries = list(spec.get("copy") or [])
    if sheet:
        entries.extend(sheet["cues"])
    copies = []
    if entries:
        approved = {(r["name"], r["revision"]): r for r in list_copy(reader, project) if r["decision"] == "approved"}
        for entry in entries:
            name, revision = entry["name"], entry["revision"]
            selected = approved.get((name, revision)) if isinstance(name, str) and type(revision) is int else None
            if not selected:
                raise ValueError("所选文案版本缺少通过记录")
            if selected not in copies:
                copies.append(selected)
    profile = spec.get("master_profile")
    path, master_stamp, report = verify_master(reader, project, cut, delivery["aspect"], spec["master"], profile, ffprobe)
    sources = describe_video_sources(reader, project, cut, ffprobe)
    finishing = video_finishing(sources, spec.get("video_finishing", []))
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    manifest = {"format_version": 1, "project": project, "delivery": delivery, "copy": copies,
                "credits": spec.get("credits", []), "timeline": cut, "media": []}
    if profile is not None:
        manifest["master_profile"] = profile
    with TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / "delivery.zip"
        with ZipFile(staged, "w") as archive:
            manifest["master"] = {**add_media(archive, path, "master" + path.suffix.lower()), **report}
            if file_stamp(path) != master_stamp:
                raise ValueError("母版在规格核对后发生变更")
            paths = {}
            for track, originals in zip(timeline.tracks, cut["tracks"], strict=True):
                for item, clip in zip(track, originals["clips"], strict=True):
                    if clip.get("gap"):
                        continue
                    key = (clip["job_id"], clip["file_index"])
                    if key not in paths:
                        src, row = clip_path(reader, project, *key)
                        name = f"media/{len(paths) + 1:03d}{src.suffix.lower()}"
                        record = add_media(archive, src, name)
                        if key in sources:
                            source = sources[key]
                            if file_stamp(src) != source["stamp"]:
                                raise ValueError("画面源片在分辨率核对后发生变更")
                            record.update(source=source["source"], raster=source["raster"])
                            if key in finishing:
                                record["finishing"] = finishing[key]
                        manifest["media"].append({**record, "shot": row["shot"], "job_id": key[0], "file_index": key[1]})
                        paths[key] = name
                    item.media_reference.target_url = paths[key]
            edit = otio.adapters.write_to_string(timeline, adapter_name="otio_json").encode("utf-8")
            manifest["edit_sha256"] = hashlib.sha256(edit).hexdigest()
            archive.writestr("edit.otio", edit)
            if captions:
                archive.writestr("subtitles.srt", captions)
                manifest["subtitles"] = {
                    "path": "subtitles.srt", "bytes": len(captions), "sha256": hashlib.sha256(captions).hexdigest(),
                    **{key: sheet[key] for key in ("revision", "rate", "cues")},
                }
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.link(staged, destination)
    return manifest


def relink_pack(directory):
    """Verify a moved handoff and create a location-specific Resolve timeline."""
    import opentimelineio as otio

    root = Path(directory).resolve(strict=True)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["format_version"] != 1:
        raise ValueError("不支持的交接包版本")
    edit = (root / "edit.otio").read_bytes()
    if hashlib.sha256(edit).hexdigest() != manifest["edit_sha256"]:
        raise ValueError("剪辑文件校验失败")
    urls = {}
    records = [(manifest["master"], r"master\.[a-z0-9]+")]
    if "subtitles" in manifest:
        records.append((manifest["subtitles"], r"subtitles\.srt"))
    records.extend((r, r"media/[0-9]{3,}\.[a-z0-9]+") for r in manifest["media"])
    for record, pattern in records:
        name = record["path"]
        if not re.fullmatch(pattern, name) or name in urls:
            raise ValueError(f"无效或重复的包内路径 {name}")
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != record["bytes"]:
            raise ValueError(f"文件位置或大小有误 {name}")
        with path.open("rb") as media:
            if hashlib.file_digest(media, "sha256").hexdigest() != record["sha256"]:
                raise ValueError(f"文件内容校验失败 {name}")
        urls[name] = path.as_uri()
    del urls[manifest["master"]["path"]]
    if "subtitles" in manifest:
        del urls["subtitles.srt"]
    timeline = otio.adapters.read_from_string(edit.decode("utf-8"), adapter_name="otio_json")
    used = set()
    for clip in timeline.find_clips():
        name = clip.media_reference.target_url
        if name not in urls:
            raise ValueError(f"剪辑使用了清单外的素材 {name}")
        used.add(name)
        clip.media_reference.target_url = urls[name]
    if used != set(urls):
        raise ValueError("媒体清单与剪辑引用不一致")
    destination = root / "resolve.otio"
    content = otio.adapters.write_to_string(timeline, adapter_name="otio_json").encode("utf-8")
    if destination.is_file() and destination.read_bytes() == content:
        return destination
    with TemporaryDirectory(dir=root) as temp:
        staged = Path(temp) / "resolve.otio"
        staged.write_bytes(content)
        os.link(staged, destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    pack = commands.add_parser("pack", help="按指定交付规格打包")
    pack.add_argument("database", type=Path)
    pack.add_argument("spec", type=Path)
    pack.add_argument("destination", type=Path)
    pack.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    relink = commands.add_parser("relink", help="校验解压目录并生成 Resolve 工程交换文件")
    relink.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.action == "pack":
        make_pack(args.database, json.loads(args.spec.read_text(encoding="utf-8-sig")), args.destination, args.ffprobe)
        print(args.destination)
    else:
        print(relink_pack(args.directory))
