"""Move a legacy canvas manifest and its usable media into a project."""

import argparse
import hashlib
import json
from pathlib import Path

from ffmpeg_tools import locate_ffprobe
from localhub_jobs import Jobs, register_media_in_db
from localhub_media import candidate_file, inspect_media, stash_input


_EDITABLE = {".mp4", ".mov", ".wav"}
_SHOT = "旧画布素材"


def _original_file(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        return None
    try:
        path = Path(value).resolve(strict=True)
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


def _collection(manifest, field):
    rows = manifest.get(field)
    if not isinstance(rows, list):
        raise ValueError(f"画布的 {field} 须为清单")
    seen = set()
    for row in rows:
        identity = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError(f"画布的 {field} 含无效或重复的记录 ID")
        seen.add(identity)
    return rows


def _archive_snapshot(jobs, project, digest, manifest):
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS legacy_canvas_snapshots (
                project TEXT NOT NULL, digest TEXT NOT NULL, manifest TEXT NOT NULL,
                PRIMARY KEY (project, digest)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS legacy_canvas_media (
                project TEXT NOT NULL, digest TEXT NOT NULL, asset_id TEXT NOT NULL,
                job_id TEXT NOT NULL, recovered_from TEXT,
                PRIMARY KEY (project, digest, asset_id)
            )
        """)
        db.execute("INSERT OR IGNORE INTO legacy_canvas_snapshots VALUES (?,?,?)",
                   (project, digest, json.dumps(manifest, ensure_ascii=False)))


def _link_asset(jobs, project, digest, asset, media_dir, ffprobe, replacement):
    identity = (project, digest, asset["id"])
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        linked = db.execute("""
            SELECT recovered_from FROM legacy_canvas_media WHERE project=? AND digest=? AND asset_id=?
        """, identity).fetchone()
        if linked is not None:
            if replacement and linked["recovered_from"] != str(replacement):
                raise ValueError("该记录已有档案片段")
            return
        source = replacement or _original_file(asset.get("path"))
        if source is None:
            return
        if source.suffix.lower() not in _EDITABLE:
            raise ValueError("剪辑候选需为 MP4、MOV 或 WAV")
        copy_path = media_dir / stash_input(source, media_dir)
        with copy_path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if copy_path.stem != f"localhub_{actual}":
            raise ValueError("档案副本的内容与文件名校验值不一致")
        path, kind, info = inspect_media(copy_path, ffprobe)
        job, _ = register_media_in_db(db, project, _SHOT, path, kind, info)
        db.execute("INSERT INTO legacy_canvas_media VALUES (?,?,?,?,?)",
                   (*identity, job["id"], str(replacement) if replacement else None))


def archive_canvas(jobs, project, canvas, media_dir, *, relink=None):
    raw = Path(canvas).read_bytes()
    manifest = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("画布文件须包含资产与组清单")
    assets = _collection(manifest, "assets")
    for group in _collection(manifest, "groups"):
        members = group.get("asset_ids")
        if not isinstance(members, list) or not all(isinstance(key, str) for key in members):
            raise ValueError("画布组的 asset_ids 须为 ID 清单")
    if relink is None:
        relink = {}
    if not isinstance(relink, dict) or relink.keys() - {asset["id"] for asset in assets}:
        raise ValueError("重连映射只能包含该画布的资产 ID")
    replacements = {key: _original_file(value) for key, value in relink.items()}
    if any(path is None for path in replacements.values()):
        raise ValueError("重连映射须使用当前可读取的文件绝对路径")
    media_dir = Path(media_dir).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    _archive_snapshot(jobs, project, digest, manifest)
    ffprobe = locate_ffprobe()
    errors = {}
    for asset in assets:
        key = asset["id"]
        try:
            _link_asset(jobs, project, digest, asset, media_dir, ffprobe, replacements.get(key))
        except (ValueError, OSError, NotImplementedError) as exc:
            errors[key] = str(exc)
    return {"digest": digest, "archives": read_archives(jobs, project), "errors": errors}


def read_archives(jobs, project):
    with jobs.db() as db:
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_canvas_snapshots'").fetchone():
            return []
        manifests = db.execute("SELECT digest, manifest FROM legacy_canvas_snapshots WHERE project=? ORDER BY rowid",
                               (project,)).fetchall()
        linked = {(row["digest"], row["asset_id"]): row for row in db.execute("""
            SELECT m.digest, m.asset_id, m.job_id, m.recovered_from, j.files, j.info
            FROM legacy_canvas_media m LEFT JOIN jobs j ON j.id=m.job_id AND j.project=m.project
            WHERE m.project=?
        """, (project,))}
    archives = []
    for row in manifests:
        digest, manifest = row["digest"], json.loads(row["manifest"])
        assets = []
        for source in manifest["assets"]:
            entry = {"original": source}
            binding = linked.get((digest, source["id"]))
            if binding is None:
                entry["state"] = "unlinked" if _original_file(source.get("path")) else "missing"
            else:
                files = json.loads(binding["files"] or "[]")
                available = files and candidate_file(files[0], json.loads(binding["info"]))
                entry.update(state="linked" if available else "archived_file_missing",
                             job_id=binding["job_id"], recovered_from=binding["recovered_from"])
            assets.append(entry)
        archives.append({"digest": digest, "assets": assets, "groups": manifest["groups"]})
    return archives


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canvas", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--media-directory", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--relink-map", type=Path, help="已确认重连文件的 JSON，内容为资产 ID 到绝对路径")
    args = parser.parse_args()
    project = args.project.strip()
    if not project or len(project) > 120:
        parser.error("项目名须为 1–120 字符")
    relink = json.loads(args.relink_map.read_text(encoding="utf-8-sig")) if args.relink_map else None
    jobs = Jobs(args.database.resolve(), base_url=None, output_paths=None)
    report = archive_canvas(jobs, project, args.canvas, args.media_directory, relink=relink)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc))
