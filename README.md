# ComfyUI H3 FilmDesk

ComfyUI H3 FilmDesk 面向使用 ComfyUI 和 MiniMax H3 的本地视频创作者。项目从商品广告起步，也面向微电影和短剧，目标是把分镜、素材参考、候选审片和剪辑交接放进同一份工程。

仓库包含独立制作台、ComfyUI 自定义节点、Codex 可调用的 MCP 服务、剪版与交付工具和测试。制作台可以直接用本地媒体做片。生成新镜头时，再连接带有 MiniMax H3 节点和相应模型的 ComfyUI。

## 从两条本地素材做一条广告

准备两个本地视频，第一条展示商品全貌，第二条展示细节。Windows 验证环境使用独立 CPython 3.12.14 和含 libx264 的 FFmpeg。将 `ffmpeg`、`ffprobe` 加入 PATH，并确认 `py -3.12` 指向准备使用的解释器；也可以在创建 venv 时直接使用解释器的完整路径。在 PowerShell 中运行：

```powershell
git clone https://github.com/Damonwxy/ComfyUI-H3-FilmDesk.git
cd ComfyUI-H3-FilmDesk
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-studio.txt -r requirements-timeline.txt
.\.venv\Scripts\python -c "import opentimelineio as otio; print(otio.__version__)"
.\.venv\Scripts\python two_shot_demo.py reveal.mp4 detail.mp4 demo-project
```

将命令中的输入文件名换为自己的素材路径，输出目录取一个新名字。示例启动临时本机制作台，登记两条素材，保存分镜与剪版，按保存的源帧范围渲染，每镜最多 3 秒。成片是 960×540、24 fps 的无声横版预览。可在 `demo-project/input/localhub_media/master.mp4` 播放，在 `receipt.json` 查看交付规格和实测结果。`edit.otio` 供剪辑软件导入，项目目录内保留了它引用的文件。

若 Windows 在 `import opentimelineio` 时直接退出，使用独立 CPython 新建 venv 再安装。本次在 Anaconda 3.12.4 环境中复现了原生模块导入崩溃；相同 OTIO 版本在独立 CPython 3.12.14 中通过导入及序列化检查。

要继续编辑，用同一环境运行：

```powershell
.\.venv\Scripts\python studio_local.py --database demo-project/jobs.sqlite3 --input demo-project/input --port 8210
```

打开 [本机制作台](http://127.0.0.1:8210/localhub/studio)，选择 `Two-shot product ad`。本地路径、媒体登记和文件处理接口供本机使用。

## ComfyUI 和 Codex 接入

将本仓库克隆到 ComfyUI 的 `custom_nodes` 下，使用该实例的 Python 安装制作台和时间线依赖，重启后从同一实例的 `/localhub/health` 检查节点、工具和模型。仅在本机回环地址运行。H3 生成需要 ComfyUI 的原生 MiniMax H3 节点及匹配的权重。图的构造依据原生节点接口，音视频解码接法参考 Director；模型、官方 skills 和外部素材分别由其发布方提供。[工具映射](tools.json) · [ComfyUI](https://github.com/Comfy-Org/ComfyUI) · [MiniMax H3](https://github.com/MiniMax-AI/MiniMax-H3)

可选的 Codex 接入使用 `requirements-mcp.txt` 和 `studio_mcp.py`。将 MCP 客户端的启动命令设为安装依赖的 Python，并用脚本绝对路径作参数。`LOCALHUB_URL` 指向本机实例，`LOCALHUB_DATABASE` 指向同一实例的项目数据库。浏览器、命令行和 MCP 共用项目版本。

## 预检白模运动参考

在 Codex 中说「给我看这镜的白模参考最后会用到哪一帧」。`localhub_preview_shot_reference_ends` 会预检已保存的分镜，返回每条普通视频参考最后采用的画面及同一次预检的提交回执。制作台的镜头预检也会显示末帧。帧号从 0 起，按转换后的 24 fps 视频计算，预处理先限于计划生成时长。H3 再向下对齐画面长度：72 帧参考采用第 0–55 帧，73 帧参考采用第 0–72 帧。观察缩略图后可调整参考片并重新预检。原声有独立的编码路径。

如果希望用 Blender 深度片控制运动，可在对话中提供本机控制片路径，或在制作台镜头预检里展开「用深度片或轮廓片控制运动」。需要 H3 Fun ControlNet Union 模型。预检工具的 `params.control_video` 接受控制片路径，`control_range` 返回原片帧数、最后采用的帧号与重复或舍弃的帧数。以 73 帧镜头为例，72 帧控制片会用到第 71 帧，再保持一帧。控制分支按帧号读取，若要保留原动作速度，按输出的 24 fps 导出控制片。缩略图显示居中裁切前的原片末帧，出片后的运动跟随程度需要另行审片。

## 检查剪版里的对白

在 Codex 中说「检查 v2 里第二镜的对白」。`localhub_check_cut_audio` 按已保存剪版的声音入出点测量，可指定对白轨。每段返回候选、源帧范围、响度、峰值和成片上的静音位置。帧号从 0 起算，结束帧不含在区间内。剪辑留白单独列出。多条声音轨分别测量，响度和峰值提示对应整段，台词可懂度及最终混音仍需试听。直连项目库时，可关闭制作台后继续检查本机素材。

## 在交付母版上审片

在 Codex 中说「为 v2 准备一份竖屏交付」。确认市场、语言和用途后，`localhub_prepare_delivery` 可登记 9:16 规格并读回对应字幕。导出成片并在制作台登记后，`localhub_check_delivery_master` 可核对画幅、帧率、片长、指定的编码和像素尺寸，同时列出剪版中各画面源片的实际分辨率。竖屏构图由创作者决定。

在制作台为已有剪版登记交付，选择对应的母版文件并核对规格。展开「在母版画面上审片」，输入从 0 起算的成片帧号，点「查看此帧」，可选中两个对角点再保存意见。意见随项目记录交付规格、剪版、母版文件摘要和审片版式。

换到新的 Codex 会话后，可用 `localhub_list_master_reviews` 找回意见。有多个交付或母版符合描述时，先请创作者选择。用 `localhub_read_master_review` 取得所选母版的回执，`localhub_read_master_frame` 可把指定帧以 PNG 图像带进对话，`localhub_save_master_note` 保存整帧或选区意见。读取历史意见可直连本机项目库，取图和新增意见需要本机制作台或桥接服务在线。

有修改要交回时，可以说「把 v2 的第 73 帧交给这条意见复看」。`localhub_submit_master_fix` 将旧意见（含原帧和选区）连接到修订母版和指定帧，并记下改了什么。制作台的原意见下会出现「待复看」，展开即可看修订帧。创作者看过并明确表示接受或需要再改后，`localhub_record_master_verdict` 才记录这项决定、复看者称呼和反馈。每次提交或复核都会追加一条记录，可从旧意见追到后续各版。

要看玻璃壶上的一个字，可以说「只给我看原意见圈出的字标，再看修订版同一处」。`localhub_read_master_frame` 可选传批注的 `region`，按左、上、右、下四个 0–1 坐标截取母版原像素，保留 PNG 位深。省略选区可查看整帧。如果商品在修订版移动了，先看修订帧，再圈对应位置。查看局部不会改变审片意见或复核状态。

## 在两个候选间保留切点

在 Codex 里说「给我看第二镜原版和另一个候选，比较切入后第 12 帧」。`localhub_compare_takes` 返回同一时刻的两张图，A 是剪版当前使用的素材，B 是备选。可以换一个时间点再看。你选 A 会保留当前剪版，选 B 时 `localhub_choose_take` 只替换这一次出现的片段并保存新版。同一素材在别处的用法和独立声音轨都会保留。用 `localhub_read_project` 指定版号，可在另一次会话中读回旧剪版。

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

提交修改和复现 CI 的步骤见[贡献指南](CONTRIBUTING.md)。

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
node --test tests/compare_cut.test.mjs tests/site_tools.test.mjs
```

## 商业使用

FilmDesk 采用自定义的[商用一次通知许可](LICENSE)。公司内部做片、为客户制作收费广告、闭源集成或提供在线服务都可以。首次商用时由个人或单位通知维护者一次，发出即完成。以后换项目、升级到沿用同一许可的版本或改变用途，都可继续使用。非商业学习和试用无需通知。

可以直接[发送商用通知](https://github.com/Damonwxy/ComfyUI-H3-FilmDesk/issues/new?title=FilmDesk%20commercial%20use&body=We%20at%20%5Bcompany%20name%5D%20have%20started%20using%20FilmDesk%20commercially.)，例如“示例公司已开始商用 FilmDesk”。Issues 对公众可见；已有维护者私下联系方式的团队也可以私下通知。无需提交客户、项目或收入信息。ComfyUI、模型和素材的许可分别适用。
