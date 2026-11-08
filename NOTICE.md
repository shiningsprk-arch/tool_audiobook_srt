# 第三方组件与模型

本工具自身以 MIT 分发。运行时按需安装/下载的第三方组件如下，均未随工具包分发：

| 组件 | 用途 | 许可 |
|---|---|---|
| faster-whisper | 语音识别封装 | MIT |
| CTranslate2 | Whisper 推理运行时 | MIT |
| PyAV（av） | 音频解码（wheel 内自带 FFmpeg 库） | BSD-3-Clause |
| onnxruntime | Silero VAD | MIT |
| tokenizers | 分词 | Apache-2.0 |
| Whisper 模型权重（Systran/faster-whisper-*） | 语音识别模型 | MIT（以模型卡为准） |

`frontend/lib/theme.css` 与 `frontend/lib/i18n.js` 来自 MyBooks Toolbox 前端脚手架
（PoxenStudio，随 MyBooks 主仓以 BSD-2-Clause 分发），仅作为共享视觉变量与 i18n 胶水；
本工具自己的业务代码仍为 MIT。

本工具没有复制 GPL/AGPL 项目的代码。宿主的无声书播放器、字幕关联逻辑只通过公开的
文件约定（同目录同名 `.srt`）对接，不修改宿主代码。

模型下载默认走 `https://hf-mirror.com` 镜像；用户可以改成其他 Hugging Face 兼容端点，
或把已下载的模型目录手工放进数据卷的 `models/` 下。
