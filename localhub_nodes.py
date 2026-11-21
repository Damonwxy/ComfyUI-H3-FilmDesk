# 两个轻量节点: 让插件在 Manager/节点列表可见, 并提供画布资产的图内访问


class LocalHubStatus:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status_json",)
    FUNCTION = "run"
    CATEGORY = "localhub"

    def run(self):
        import json
        from localhub_server import ROUTE_LIST, _base_url
        import h3_graph
        import ffmpeg_tools
        info = {
            "base_url": _base_url(),
            "routes": ["{} {}".format(m, p) for m, p, _ in ROUTE_LIST],
            "models": h3_graph.detect_models(),
            "ffmpeg": ffmpeg_tools.locate_ffmpeg(),
        }
        return (json.dumps(info, ensure_ascii=False, indent=2),)


class LocalHubCanvasAssets:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "asset_type": (["video", "image", "file", "any"],),
                "limit": ("INT", {"default": 20, "min": 1, "max": 500}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("assets_json",)
    FUNCTION = "run"
    CATEGORY = "localhub"

    def run(self, asset_type, limit):
        import json
        import canvas_store
        items = canvas_store.list_assets(None if asset_type == "any" else asset_type, limit)
        return (json.dumps(items, ensure_ascii=False, indent=2),)


NODE_CLASS_MAPPINGS = {
    "LocalHubStatus": LocalHubStatus,
    "LocalHubCanvasAssets": LocalHubCanvasAssets,
}
