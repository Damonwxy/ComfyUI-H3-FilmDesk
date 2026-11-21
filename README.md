# ComfyUI H3 FilmDesk

ComfyUI H3 FilmDesk 面向使用 ComfyUI 和 MiniMax H3 的本地视频创作者。项目从商品广告起步，也面向微电影和短剧，目标是把分镜、素材参考、候选审片和剪辑交接放进同一份工程。

仓库包含独立制作台、ComfyUI 自定义节点、Codex 可调用的 MCP 服务、剪版与交付工具和测试。制作台可以直接用本地媒体做片。生成新镜头时，再连接带有 MiniMax H3 节点和相应模型的 ComfyUI。

## 从两条本地素材做一条广告

准备两个本地视频，第一条展示商品全貌，第二条展示细节。安装 Python 3.12 和含 libx264 的 FFmpeg，将 `ffmpeg`、`ffprobe` 加入 PATH。在 PowerShell 中运行：

```powershell
git clone https://github.com/Damonwxy/ComfyUI-H3-FilmDesk.git
cd ComfyUI-H3-FilmDesk
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-studio.txt -r requirements-timeline.txt
.\.venv\Scripts\python two_shot_demo.py reveal.mp4 detail.mp4 demo-project
```

将命令中的输入文件名换为自己的素材路径，输出目录取一个新名字。示例启动临时本机制作台，登记两条素材，保存分镜与剪版，按保存的源帧范围渲染，每镜最多 3 秒。成片是 960×540、24 fps 的无声横版预览。可在 `demo-project/input/localhub_media/master.mp4` 播放，在 `receipt.json` 查看交付规格和实测结果。`edit.otio` 供剪辑软件导入，项目目录内保留了它引用的文件。

要继续编辑，用同一环境运行：

```powershell
.\.venv\Scripts\python studio_local.py --database demo-project/jobs.sqlite3 --input demo-project/input --port 8210
```

打开 [本机制作台](http://127.0.0.1:8210/localhub/studio)，选择 `Two-shot product ad`。本地路径、媒体登记和文件处理接口供本机使用。

## ComfyUI 和 Codex 接入

将本仓库克隆到 ComfyUI 的 `custom_nodes` 下，使用该实例的 Python 安装制作台和时间线依赖，重启后从同一实例的 `/localhub/health` 检查节点、工具和模型。仅在本机回环地址运行。H3 生成需要 ComfyUI 的原生 MiniMax H3 节点及匹配的权重。图的构造依据原生节点接口，音视频解码接法参考 Director；模型、官方 skills 和外部素材分别由其发布方提供。[工具映射](tools.json) · [ComfyUI](https://github.com/Comfy-Org/ComfyUI) · [MiniMax H3](https://github.com/MiniMax-AI/MiniMax-H3)

可选的 Codex 接入使用 `requirements-mcp.txt` 和 `studio_mcp.py`。将 MCP 客户端的启动命令设为安装依赖的 Python，并用脚本绝对路径作参数。`LOCALHUB_URL` 指向本机实例，`LOCALHUB_DATABASE` 指向同一实例的项目数据库。浏览器、命令行和 MCP 共用项目版本。

## 在两个候选间保留切点

原片为 24 fps，从第 12 帧开始取 48 帧。要换成同一动作的 48 fps 素材，相同的两秒应从第 24 帧开始，持续 96 帧。以下示例可在仓库根目录以 Node.js 运行。

```sh
node --input-type=module -e "import { matchedRange } from './studio/compare-cut.mjs'; console.log(matchedRange({ start_frame: 12, duration_frames: 48 }, 24, { fps: 48, frames: 120 }))"
```

返回值为 `{ start_frame: 24, duration_frames: 96, rate: '48' }`。帧号从 0 开始。对于 23.976 等广播帧率，传入精确分数，例如 `24000/1001`。如果切点落在两帧之间，或者候选素材过短，函数会抛出异常，让剪辑者决定如何调整。该函数只计算帧范围，画面里的动作是否同步仍需看素材判断。

使用 Node.js 22 运行测试，无需安装依赖。

```sh
node --test tests/compare_cut.test.mjs
```

## 验证

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
node --test tests/compare_cut.test.mjs tests/site_tools.test.mjs
```

代码的开放源代码许可证待定。
