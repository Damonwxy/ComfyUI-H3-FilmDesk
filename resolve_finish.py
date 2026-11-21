"""Open a LocalHub OTIO or reapply recorded clip scaling in Resolve."""

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import url2pathname

from delivery_pack import relink_pack


SCALE = {"resize": 1, "resolve_super_scale_2x": 2}


def open_otio(project, exchange, title):
    """Conform from an explicit media-pool folder so the timeline links its sources."""
    import opentimelineio as otio

    if not title.strip():
        raise ValueError("请为导入的时间线指定名称")
    if project.IsRenderingInProgress():
        raise ValueError("Resolve 正在渲染，请在完成后导入时间线")
    for index in range(1, project.GetTimelineCount() + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline and timeline.GetName() == title:
            raise ValueError("当前 Resolve 项目已有这个时间线名称")
    exchange = Path(exchange).resolve(strict=True)
    timeline = otio.adapters.read_from_file(str(exchange), adapter_name="otio_json")
    if not isinstance(timeline, otio.schema.Timeline):
        raise ValueError("请选用桥接导出的普通 OTIO 时间线")
    sources = {}
    for clip in timeline.find_clips():
        if not isinstance(clip.media_reference, otio.schema.ExternalReference):
            raise ValueError(f"片段缺少媒体引用 · {clip.name}")
        target = urlsplit(clip.media_reference.target_url)
        path = Path(url2pathname(target.path))
        if (target.scheme != "file" or target.netloc or target.query or target.fragment
                or not path.is_absolute() or path.drive.startswith("\\\\")):
            raise ValueError(f"片段须使用本机绝对文件链接 · {clip.name}")
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"媒体引用不是文件 · {clip.name}")
        sources[path] = None
    if not sources:
        raise ValueError("这条 OTIO 没有可导入的源素材")

    pool = project.GetMediaPool()
    folder = pool.GetCurrentFolder()
    present = {Path(path).resolve() for clip in (folder.GetClipList() or [])
               if (path := clip.GetClipProperty("File Path"))}
    missing = sources.keys() - present
    if missing:
        imported = pool.ImportMedia([str(path) for path in sources if path in missing]) or []
        received = {Path(path).resolve() for clip in imported if (path := clip.GetClipProperty("File Path"))}
        if not missing <= received:
            raise ValueError("Resolve 媒体池未读入全部源素材")
    edit = pool.ImportTimelineFromFile(str(exchange), {
        "timelineName": title, "importSourceClips": False, "sourceClipsFolders": [folder],
    })
    if not edit:
        raise RuntimeError("Resolve 未能导入时间线")
    actual = set()
    for kind in ("video", "audio"):
        for index in range(1, edit.GetTrackCount(kind) + 1):
            for item in edit.GetItemListInTrack(kind, index) or []:
                clip = item.GetMediaPoolItem()
                if not clip or not (path := clip.GetClipProperty("File Path")):
                    raise RuntimeError(f"时间线片段尚未连接媒体 · {item.GetName()}")
                actual.add(Path(path).resolve())
    if actual != set(sources):
        raise RuntimeError("导入后的媒体与 OTIO 引用不同")
    return edit


def apply_scaling(project, timeline, directory):
    root = Path(directory).resolve(strict=True)
    relink_pack(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    planned = {}
    for source in manifest["media"]:
        if "finishing" not in source:
            continue
        method = source["finishing"]["method"]
        if method not in SCALE:
            raise ValueError(f"此脚本不支持清单中的放大方式 · {method}")
        planned[(root / source["path"]).resolve(strict=True)] = SCALE[method]
    if not planned:
        return []

    # Match the imported package's local files, even when two shots share a name.
    pool_items = {}
    for track in range(1, timeline.GetTrackCount("video") + 1):
        for item in timeline.GetItemListInTrack("video", track) or []:
            media = item.GetMediaPoolItem()
            if media and (path := media.GetClipProperty("File Path")):
                path = Path(path).resolve()
                if path in planned:
                    pool_items.setdefault(path, {})[media.GetMediaId()] = media
    if planned.keys() != pool_items.keys():
        raise ValueError("当前时间线缺少所选交付包的画面源片，请先导入重连后的 OTIO")
    changes = [(path, clip, int(clip.GetClipProperty("Super Scale")), planned[path])
               for path, items in pool_items.items() for clip in items.values()]
    if project.IsRenderingInProgress():
        raise ValueError("Resolve 正在渲染，请在完成后应用素材属性")
    updated = []
    try:
        for path, clip, before, after in changes:
            if before != after:
                updated.append((path, clip, before))
                if not clip.SetClipProperty("Super Scale", after) or int(clip.GetClipProperty("Super Scale")) != after:
                    raise RuntimeError(f"Resolve 未应用 Super Scale · {path.name}")
    except Exception as error:
        failed = []
        for path, clip, value in reversed(updated):
            try:
                if int(clip.GetClipProperty("Super Scale")) != value:
                    if not clip.SetClipProperty("Super Scale", value) or int(clip.GetClipProperty("Super Scale")) != value:
                        failed.append(path.name)
            except Exception:
                failed.append(path.name)
        if failed:
            raise RuntimeError("部分素材属性未能恢复，请在 Resolve 检查 " + "、".join(failed)) from error
        raise
    return [{"path": path.relative_to(root).as_posix(), "before": before, "after": after}
            for path, _, before, after in changes]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="已解压的交付目录，或使用 --open-otio 时的剪辑文件")
    parser.add_argument("--open-otio", action="store_true", help="先准备媒体池，再导入本机 OTIO")
    parser.add_argument("--project", required=True, help="当前打开的 Resolve 项目名")
    parser.add_argument("--timeline", required=True, help="时间线名。导入时指定新名称")
    parser.add_argument("--modules", type=Path, help="Resolve 脚本 API 的 Modules 目录")
    parser.add_argument("--library", type=Path, help="Resolve 的 fusionscript 动态库")
    args = parser.parse_args()
    if args.modules:
        sys.path.insert(0, str(args.modules.resolve(strict=True)))
    if args.library:
        os.environ["RESOLVE_SCRIPT_LIB"] = str(args.library.resolve(strict=True))
    from DaVinciResolveScript import scriptapp

    resolve = scriptapp("Resolve")
    if not resolve:
        raise RuntimeError("请先打开本机 Resolve Studio 并启用本机脚本访问")
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project or project.GetName() != args.project:
        raise ValueError("当前 Resolve 项目或时间线与指定名称不符")
    if args.open_otio:
        timeline = open_otio(project, args.path, args.timeline)
        print(timeline.GetName())
        return
    timeline = project.GetCurrentTimeline()
    if not timeline or timeline.GetName() != args.timeline:
        raise ValueError("当前 Resolve 项目或时间线与指定名称不符")
    print(json.dumps(apply_scaling(project, timeline, args.path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        sys.exit(str(error))
