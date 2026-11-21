# LocalHub Studio

LocalHub Studio 是面向本地视频制作的项目。这里逐项发布可独立使用和验证的组件。首个公开组件用于在两个视频候选之间换片，判断新素材能否精确沿用原剪辑的入点和出点。

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

当前公开范围为帧范围换算模块、测试和本文档。代码的开放源代码许可证待定。
