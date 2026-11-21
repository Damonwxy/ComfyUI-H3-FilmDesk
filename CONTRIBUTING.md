# 参与 FilmDesk 开发

从当前 `main` 建短期分支，通过发往 `main` 的 Pull Request 提交修改。每个 PR 解决一个可以单独验证和回退的问题。提交标题说明具体改变，PR 正文写清用户会看到什么变化、实际运行过的检查以及尚待验证的部分。

## 本地验证

准备 Python 3.12、Node.js 22、FFmpeg 和 ffprobe。在独立 Python 环境中从仓库根目录运行以下命令。

```sh
python -m pip install -r requirements-studio.txt -r requirements-timeline.txt -r requirements-mcp.txt
ffmpeg -version
ffprobe -version
python -c "import opentimelineio, studio_mcp"
python -m unittest discover -v -s tests -p "test_*.py"
node --test tests/compare_cut.test.mjs tests/site_tools.test.mjs
```

CI 会在 Windows 和 Linux 上运行 Python 与媒体测试，并用 Node.js 检查制作台脚本。涉及 ComfyUI 生成、Codex 对话或 Resolve 往返时，还需在独立项目库和端口上走一遍相关操作，在 PR 中写明结果。公开记录使用可公开的样例和复现步骤。

## 审阅与合入

提交前用 `git status`、`git diff --cached` 核对将要公开的内容。仓库采用公开文件白名单，新增源码、测试或工作流时，在 `.gitignore` 中逐项放行。私有素材、项目库、账户凭据和本机验收文件留在仓库外。

合入前审阅完整差异，等 PR 最新提交上的必要检查全部通过。需要撤回已合入的改动时，开新的回退 PR，保留公开历史。长期运行的制作台从已验收提交的干净 checkout 启动，开发在独立 worktree 中进行；更新实例时记录提交号、数据备份和切换后的核对结果。
