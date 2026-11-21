"""Keep immutable, project-scoped reference-image versions."""

import base64
import hashlib
from pathlib import Path

from localhub_media import staged_file, stash_input


_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


def _image_size(path):
    from PIL import Image

    try:
        with Image.open(path) as image:
            if image.format != _FORMATS.get(path.suffix) or getattr(image, "n_frames", 1) != 1:
                raise ValueError("仅支持单帧 PNG、JPEG 或 WebP 图片")
            image.load()
            return image.size
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取参考图") from exc


def _snapshot(root, name, expected):
    folder = Path(root).resolve()
    path = folder / name
    if path.parent != folder or path.resolve() != path or not path.is_file():
        raise ValueError("参考图快照不存在或已移动")
    with path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
            raise ValueError("参考图快照内容已变动")
    return path


def register_reference(jobs, input_root, project, asset_id, filename):
    root = Path(input_root)
    source = staged_file(root / "localhub_media", filename, _FORMATS)
    source_name = source.relative_to(root.resolve() / "localhub_media").as_posix()
    return register_reference_file(jobs, root, project, asset_id, source, source_name)


def register_reference_file(jobs, root, project, asset_id, source, source_name, *, private_upload=False):
    # Reject invalid uploads before publishing. Read editable staged inputs
    # from the immutable snapshot so its dimensions always match its bytes.
    size = _image_size(source) if private_upload else None
    name = stash_input(source, root)
    digest = Path(name).stem.removeprefix("localhub_")
    snapshot = _snapshot(root, name, digest)
    width, height = size or _image_size(snapshot)
    with jobs.db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("""
            SELECT * FROM reference_images WHERE project=? AND asset_id=?
            ORDER BY revision DESC LIMIT 1
        """, (project, asset_id)).fetchone()
        if existing and existing["sha256"] == digest:
            return dict(existing), False
        revision = existing["revision"] + 1 if existing else 1
        stored = {
            "project": project, "asset_id": asset_id, "revision": revision, "sha256": digest,
            "input_name": name, "source": source_name, "width": width, "height": height,
        }
        db.execute("""
            INSERT INTO reference_images (project,asset_id,revision,sha256,input_name,source,width,height)
            VALUES (:project,:asset_id,:revision,:sha256,:input_name,:source,:width,:height)
        """, stored)
    return stored, True


def list_references(jobs, project):
    with jobs.db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_images'").fetchone():
            return []
        rows = db.execute("""
            SELECT * FROM reference_images WHERE project=? ORDER BY asset_id, revision DESC
        """, (project,)).fetchall()
    return [dict(row) for row in rows]


def resolve_reference(jobs, root, project, choice):
    asset_id, revision = choice.get("asset_id"), choice.get("revision")
    if not isinstance(asset_id, str) or type(revision) is not int or not 1 <= revision < 2**63:
        raise ValueError("请选择参考图名称和明确的版本号")
    with jobs.db() as db:
        row = db.execute("""
            SELECT * FROM reference_images WHERE project=? AND asset_id=? AND revision=?
        """, (project, asset_id, revision)).fetchone()
    if row is None:
        raise ValueError(f"项目中找不到参考图 · {asset_id} v{revision}")
    if "sha256" in choice and choice["sha256"] != row["sha256"]:
        raise ValueError("参考图与指定的字稿摘要不符")
    path = _snapshot(root, row["input_name"], row["sha256"])
    return str(path), dict(row)


def read_reference_image(jobs, root, project, choice):
    if not isinstance(choice, dict):
        raise ValueError("请选择项目中的参考图和版本")
    path, row = resolve_reference(jobs, root, project, choice)
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("参考图与指定的字稿摘要不符")
    return {"reference": row, "mime_type": "image/" + _FORMATS[Path(path).suffix].lower(),
            "data": base64.b64encode(data).decode("ascii")}
