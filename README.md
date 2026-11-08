# 有声书字幕（audiobook_srt）

MyBooks 外置工具包：用 **faster-whisper** 给书库里的有声书批量生成 SRT 字幕。

- 工具 ID：`audiobook_srt`
- 形态：外置工具包（`manifest.json` + `backend/` + `frontend/` + `icon.png`）
- CoreAPI：`1.3.0`
- 许可：MIT（依赖与模型的许可见 `NOTICE.md`）

## 它怎么接进 MyBooks

宿主播放器会扫描音频同目录、同文件名的字幕（`0001_第一章.wav` → `0001_第一章.srt`），
播放页自己解析 SRT/VTT。本工具只做一件事：把 `.srt` 写到音频旁边，**不改宿主代码**、
不改音频、不改元数据。

m4b 内嵌章节共享同一个外挂字幕，时间轴按整条音轨的绝对时间——工具在长文件上会按 30 分钟
切片转写，再把时间轴拼回去。

生成完一本书后，工具会删掉宿主缓存的 `audio_collection_<book_id>.zip`，否则下次下载的
音频包仍不含新字幕（下次下载时宿主会自动重建）。

## 安装

1. 在 MyBooks 里开启开发者模式（`ENABLE_TOOLBOX_DEV_MODE`）
2. 工具箱 → 上传工具包 → 选择 `dist/audiobook_srt-0.1.0.zip`
3. **重启 MyBooks**（外置工具的导入与路由只在进程启动时挂载）
4. 打开「有声书字幕」→ 安装依赖 → 下载模型 → 选书转写

## 依赖放在哪里

工具包本体只有几百 KB（纯 Python 源码 + 前端静态文件 + 依赖清单文本），
**不含 wheels、不含模型**。运行环境落在 Docker 数据卷里：

```
/data/toolbox/audiobook_srt/
├── config.json                       # 工具持久配置
├── deps/
│   └── py312-linux-aarch64/          # pip install --target，按 Python+平台分目录
├── models/
│   ├── small/                        # CTranslate2 模型
│   └── large-v3/
├── jobs/                             # 每本书的逐文件状态，断点续跑
└── tmp/                              # 安装日志、分片临时文件
```

- 工具更新只替换 `/data/books/tools/audiobook_srt/`，依赖/模型不会重装重下。
- 服务器进程**从不 import** faster-whisper；每个音频文件起独立 worker 子进程，
  `PYTHONPATH` 指向 deps，取消=杀进程组，内存随进程退出释放。
- 依赖目录名带 Python/平台指纹。镜像升级到新 Python 大版本后，工具页会提示重新安装依赖。
- 首次安装依赖约 250–350 MB。faster-whisper 直接依赖 6 个
  （`ctranslate2` / `huggingface-hub` / `tokenizers` / `onnxruntime` / `av` / `tqdm`），
  Linux 下 pip 实测解析 23 个包（2026-11-08，含传递依赖；版本随上游漂移）。

### 一键卸载

运行环境卡片底部有「一键卸载依赖和模型」：删除数据卷里的 `deps/` 和 `models/`
全部内容，**不动字幕、音频、`jobs/` 任务记录和 `config.json`**。按钮会先显示当前占用并二次确认；
有转写/安装/下载任务在跑时会被拒绝。卸载后重新点「安装依赖 / 下载模型」即可恢复。

## 模型从哪下载

默认 **`https://hf-mirror.com`**（Hugging Face 的国内镜像；`huggingface.co` 在不少网络环境
不可直连）。模型仓库：

| 名称 | 仓库 | 约体积 |
|---|---|---|
| tiny | `Systran/faster-whisper-tiny` | 75 MB |
| base | `Systran/faster-whisper-base` | 145 MB |
| small | `Systran/faster-whisper-small` | 486 MB |
| medium | `Systran/faster-whisper-medium` | 1.5 GB |
| large-v3 | `Systran/faster-whisper-large-v3` | 3.1 GB |

实际文件地址形如：

```
https://hf-mirror.com/Systran/faster-whisper-small/resolve/main/model.bin
https://hf-mirror.com/Systran/faster-whisper-small/resolve/main/config.json
https://hf-mirror.com/Systran/faster-whisper-small/resolve/main/tokenizer.json
https://hf-mirror.com/Systran/faster-whisper-small/resolve/main/vocabulary.txt
```

下载器用标准库直连镜像的文件列表 API，不依赖 `huggingface_hub`，也**不走 Xet 通道**
（新版 HF 客户端可能绕过 `HF_ENDPOINT` 访问 `xethub`，国内容易卡住）。支持断点续传：
中断后重新点下载会从 `.part` 文件继续。

- 页面里可以改下载地址（Hugging Face 兼容端点均可）。
- 也可以手工把已下载的模型目录放进 `/data/toolbox/audiobook_srt/models/<name>/`，
  必备文件：`config.json`、`model.bin`、`tokenizer.json`、`vocabulary.txt`（或
  `vocabulary.json`）。
- 模型体积大，下载前先看「数据卷剩余」；删除模型在模型表格里操作。

## 使用

1. **运行环境**：安装依赖；下载至少一个模型（ARM CPU 日常推荐 `small`）。
2. **有声书列表**：列出 `audio_output_folder` 下所有有音频的书，显示序号、书名、字幕覆盖率；可只看缺字幕。
3. **选书**：处理范围三选一——只补缺失 / 全部重转 / 手动勾选；逐文件显示已有字幕与状态。
4. **参数**（可选）：模型、语言、计算精度、CPU 线程、beam size、初始提示词、是否长文件分片。
   点「保存为默认」持久化到数据卷 `config.json`。
5. **开始转写**：进度按文件大小加权，逐文件落盘；可取消；服务重启后页面会显示「已中断」，
   再次开始只补未完成的文件，长文件已完成的切片不会重跑。

**处理范围决定覆盖语义**：「只补缺失」跳过已有 `.srt`/`.vtt`；「全部重转」和「手动勾选」
重写同名 `.srt`（不删除 `.vtt`）。

### 默认参数

| 参数 | 默认 | 说明 |
|---|---|---|
| 模型 | `small` | ARM CPU 折中 |
| 语言 | `zh` | `auto` 对长静音容易误判 |
| 计算精度 | `int8` | ARM CPU 推荐 |
| CPU 线程 | `min(4, ncpu-1)` | 避免把 NAS 打满 |
| beam size | 5 | |
| VAD | 开 | 跳过静音/音乐 |
| 覆盖已有 SRT | 按处理范围 | 只补缺失=跳过；全部/手动=重写同名 `.srt` |
| 长文件分片 | 开 | >30 分钟或 m4b 按 30 分钟切片 |

## 常见问题

- **依赖装不上**：工具默认把 pip 源填成阿里云（可清空改用容器/pip 自己的默认配置）。
  若 NAS 完全不能出网，先在能出网的机器上用 `pip download` 拉 wheels，再按数据卷目录
  手工放置（后续版本会提供离线 wheels 包的上传入口）。
- **模型下载慢/失败**：确认 `https://hf-mirror.com` 可访问；换端点；中断后重试会续传。
- **ARM CPU 很慢**：`large-v3` 可能慢于实时，先用 `small`；可调低 `cpu_threads` 让网页不卡。
- **装完依赖还是提示未安装**：看任务日志；镜像 Python 大版本变化后需要重新安装依赖。
- **报 `open() got an unexpected keyword argument 'metadata_errors'`**：PyAV 19 删除了
  faster-whisper 1.2.1 仍在使用的参数。工具已把 `av` 钉在 `18.1.0`；重新点「安装依赖」
  覆盖旧环境即可。
- **字幕没出现**：确认 `.srt` 与音频同名、同目录；播放页刷新；VIP 配额开启时字幕与音频
  走同一鉴权（文件名保留 `0001_` 序号前缀即可与音频同权）。
- **下载的音频 zip 没有字幕**：工具在完成一本书后会删缓存 zip；若之前手动下载过，
  再点一次下载会重建。

## 开发

```bash
python -m pytest tests -q          # 纯离线单测（无需 faster-whisper）
python scripts/make_icon.py        # 重新生成 icon.png
python scripts/build.py            # → dist/audiobook_srt-<revision>.zip + sha256
```

集成测试需要真实依赖与模型，默认不跑；本地预览可把 WSL 的 `/data` 指向测试目录，
在 MyBooks 工具箱里安装 zip 后验证播放页字幕。

## 已知限制

- 只做转写，不做翻译、不做人工时间轴编辑、不做字幕烧录。
- 分片边界可能切断一个词；VAD 与切片长度可调。
- 单任务：同一时刻只跑一本书的转写（装依赖与转写互斥；模型下载可与转写并行）。
