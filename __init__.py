# Local Hub Bridge — 把 MiniMax 官方 skills 的 Hub 专属工具本地化到 ComfyUI
#
# 设计说明:
# - 官方 8 个风格化 skill 依赖 hub_* 系列工具(MiniMax Hub 画布专有), 本包在 ComfyUI 内
#   提供同名语义的本地 HTTP 路由, 由 AI 助手(如 ZCode/Claude Code)直接调用,
#   从而在本地 ComfyUI 上执行这些 skill 的制作方法论。
# - 生成能力基于本机已安装的 MiniMax H3 核心 comfy 节点(comfy_extras/nodes_minimax_h3.py),
#   不依赖任何在线服务。

import os
import sys
import json

_PY_DIR = os.path.dirname(os.path.abspath(__file__))
if _PY_DIR not in sys.path:
    sys.path.append(_PY_DIR)

from localhub_server import register_routes  # noqa: E402
from localhub_nodes import LocalHubStatus, LocalHubCanvasAssets  # noqa: E402

NODE_CLASS_MAPPINGS = {
    "LocalHubStatus": LocalHubStatus,
    "LocalHubCanvasAssets": LocalHubCanvasAssets,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LocalHubStatus": "Local Hub 状态",
    "LocalHubCanvasAssets": "Local Hub 画布资产",
}

# 路由注册失败不应阻断 ComfyUI 启动(例如被第三方脚本在无服务环境下导入)
try:
    register_routes()
except Exception as e:  # pragma: no cover
    print("[LocalHubBridge] 路由注册失败: {}".format(e))

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
