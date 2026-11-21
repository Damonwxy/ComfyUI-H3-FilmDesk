# 画布资产清单 — 模拟 hub_canvas_get_node / hub_canvas_group_recent_outputs
# 存储位置: ComfyUI/user/localhub/canvas.json

import os
import json
import time
import threading
import folder_paths

_LOCK = threading.Lock()


def _store_path():
    d = os.path.join(folder_paths.get_user_directory(), "localhub")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "canvas.json")


def _load():
    p = _store_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"assets": [], "groups": [], "next_id": 1}


def _save(state):
    with open(_store_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def record(asset_type, path, role="", source="local", meta=None):
    with _LOCK:
        st = _load()
        path = os.path.abspath(path)
        job_id = (meta or {}).get("job_id")
        if job_id:
            for asset in st["assets"]:
                if asset["path"] == path and asset.get("meta", {}).get("job_id") == job_id:
                    return asset
        aid = "asset_{:04d}".format(st["next_id"])
        st["next_id"] += 1
        asset = {
            "id": aid,
            "type": asset_type,
            "role": role,
            "source": source,
            "path": path,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "meta": meta or {},
        }
        st["assets"].append(asset)
        _save(st)
        return asset


def list_assets(asset_type=None, limit=100):
    st = _load()
    items = st["assets"]
    if asset_type:
        items = [a for a in items if a["type"] == asset_type]
    return list(reversed(items[-limit:]))


def group_recent(n=5, group_name=""):
    with _LOCK:
        st = _load()
        recent = st["assets"][-int(n):]
        gid = "group_{:03d}".format(len(st["groups"]) + 1)
        grp = {"id": gid, "name": group_name or gid,
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "asset_ids": [a["id"] for a in recent]}
        st["groups"].append(grp)
        _save(st)
        return grp


def clear():
    with _LOCK:
        _save({"assets": [], "groups": [], "next_id": 1})
