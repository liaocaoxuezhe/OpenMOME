# OpenMOME

> 粘贴一个 **小宇宙 / YouTube / B站** 链接，在本机拿到带时间戳、分说话人的逐字稿，以及一份 AI 生成的结构化分析笔记。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![Platform](https://img.shields.io/badge/platform-Apple%20Silicon-lightgrey)

**English**: OpenMOME is a local-first web tool for turning podcast / video links (Xiaoyuzhou, YouTube, Bilibili) into timestamped, speaker-labeled transcripts and structured notes. Speech recognition runs entirely on your Mac (Qwen3-ASR on MLX + pyannote diarization); only the note-writing step calls an OpenAI-compatible LLM (DeepSeek by default, or a local Ollama model).

## 特性

- **字幕优先**：有平台字幕（YouTube 人工字幕 / 原语言自动字幕、B站 CC / AI 字幕）就直接用，跳过下载和转写
- **本地转写**：没有字幕时只下载音频，用 [Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B)（MLX）在本机转写，ForcedAligner 对齐出时间戳
- **说话人分离**：[pyannote](https://github.com/pyannote/pyannote-audio) 在独立子进程中与转写同时运行
- **分析笔记**：任意 OpenAI 兼容接口（默认 DeepSeek，可换 Ollama 等本地模型），提示词模板可自定义
- **断点重试**：失败的任务从失败的步骤继续，已下载的音频和已有的逐字稿都会复用
- **多种导出**：逐字稿 `.md` / `.srt` / `.json`，笔记 `.md`
- **隐私**：服务只绑定 `127.0.0.1`；密钥只存在 `.env`，不写入数据库或日志

实测（Apple M4 / 16GB）：49 分钟中文双人播客，0.6B 转写 + 说话人分离约 8 分钟。详见 [docs/benchmark.md](docs/benchmark.md)。

## 工作流程

```
链接 ──► 解析元数据 ──► 有平台字幕？ ──是──► 解析字幕 ──────────────┐
                           │                                        ▼
                           否 ──► 下载音频 ──► Qwen3-ASR 转写 ─► 合并 ──► LLM 生成笔记 ──► 网页查看 / 导出
                                         └─► pyannote 说话人分离 ─┘
```

设计细节见 [docs/方案.md](docs/方案.md)。

## 环境要求

- **Apple Silicon Mac**（MLX 只支持 Apple 芯片），建议 16GB 内存以上
- 磁盘：模型约 3.7GB（0.6B + 对齐器）+ 依赖约 2GB；另装 1.7B 再加 4.7GB
- Python 3.11 / 3.12（推荐用 [uv](https://github.com/astral-sh/uv) 建虚拟环境）、`ffmpeg`、`deno`（yt-dlp 下载 YouTube 需要）

## 快速开始

```bash
brew install ffmpeg deno uv
git clone https://github.com/liaocaoxuezhe/OpenMOME.git
cd OpenMOME
uv venv --python 3.11 .venv
uv sync
cp .env.example .env                          # 填写 DEEPSEEK_API_KEY 等，见下
.venv/bin/python scripts/download_models.py   # 下载 ASR 模型（ModelScope，国内快）
./start.sh
```

浏览器打开 <http://127.0.0.1:8765>，粘贴链接，点「开始」。

> `download_models.py` 从 ModelScope 下载 Qwen 模型到 `models/`，支持断点续传；pyannote 说话人模型从 Hugging Face 下载（需要 `HF_TOKEN`）。
> 不运行这一步也能用，首次转写时会自动从 Hugging Face 下载，只是国内网络较慢。

## 配置 `.env`

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | 生成笔记用。换别家模型时改 `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` |
| `HF_TOKEN` | 可选 | 说话人分离用。需先在 [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) 页面接受条款 |
| `BILIBILI_COOKIE` | 可选 | 获取 B站字幕用。可填浏览器复制的完整 Cookie 串，或只填 `SESSDATA` 的值。不填则 B站一律本地转写 |
| `ASR_MODEL` | 可选 | 默认 `Qwen/Qwen3-ASR-0.6B`；更看重专有名词准确率时改为 `Qwen/Qwen3-ASR-1.7B`（慢约 2.5 倍，需另行下载：`scripts/download_models.py --models Qwen/Qwen3-ASR-1.7B`） |
| `KEEP_AUDIO` | 可选 | 默认 `false`：转写完删除下载的音频 |
| `MIN_FREE_GB` | 可选 | 默认 5：磁盘剩余低于此值时拒绝新任务 |

完整模板见 [.env.example](.env.example)。`.env` 已被 `.gitignore` 忽略，**请勿提交**。

使用本地 Ollama 生成笔记（完全离线）：

```ini
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=ollama
```

## 使用

**网页**：

- **强制本地转写**：忽略平台字幕（例如 YouTube 中文自动字幕质量较差时）
- **说话人分离**：默认小宇宙开、YouTube / B站关；可手动开关，也可指定说话人数
- **重新生成笔记**：只重跑 LLM，不重新下载和转写；修改 [app/llm/prompts/note.md](app/llm/prompts/note.md) 后可用它重新生成
- **重试**：失败的任务从失败的步骤继续
- 笔记里的时间戳：YouTube / B站可点击跳到对应秒数；小宇宙无法按秒定位，只显示文本

**命令行**（结果同样出现在网页历史里）：

```bash
.venv/bin/python -m app.cli "https://www.xiaoyuzhoufm.com/episode/..." --diarize
```

## 不支持

小宇宙付费单集、B站大会员 / 充电专属内容、播放列表 / 批量 / 订阅、视频画面截图。

## 开发与维护

```bash
.venv/bin/python -m pytest                    # 单元测试
.venv/bin/python scripts/secret_scan.py       # 提交前检查：代码 / 日志 / 数据库中是否混入 .env 里的密钥
.venv/bin/python scripts/live_probe.py <url>  # 在线探测某个链接的元数据与字幕（需联网）
uv pip install -U yt-dlp                      # YouTube / B站下载失败（403 等）时先更新 yt-dlp
curl -s http://127.0.0.1:8765/api/health | python3 -m json.tool   # 环境自检
```

运行数据都在 `data/`：`openmome.db`（任务列表）和 `data/tasks/<任务id>/`（逐字稿、笔记、日志）。删掉 `data/` 即清空历史。

## 目录结构

```
app/
  main.py            FastAPI 入口、健康检查
  api/tasks.py       REST 路由
  worker.py          单线程任务队列
  pipeline.py        流水线与状态机
  platforms/         小宇宙 / YouTube / B站（含 WBI 签名）
  asr/               Qwen3-ASR 子进程封装（qwen3_worker.py 转写，diarize_worker.py 并行做说话人分离）
  llm/               OpenAI 兼容客户端、笔记生成、prompts/
  transcript.py      字幕解析、合并、导出
  cli.py             命令行入口
web/index.html       单页前端
scripts/             模型下载、密钥扫描、在线探测
docs/                设计方案、性能实测
test/                单元测试与夹具
```

## 贡献

欢迎提 Issue 和 PR。提交前请运行 `pytest` 和 `scripts/secret_scan.py`，确认没有把密钥、Cookie 或 `data/` 下的个人数据带进提交。

## 免责声明

本项目仅供个人学习与研究使用。下载、转写的音视频内容版权归原作者和平台所有，请遵守各平台的服务条款，不要传播或商用他人内容。

## 致谢

- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) / [mlx-qwen3-asr](https://pypi.org/project/mlx-qwen3-asr/)
- [pyannote.audio](https://github.com/pyannote/pyannote-audio)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [FastAPI](https://fastapi.tiangolo.com/)

## License

[MIT](LICENSE)。模型权重遵循各自的许可证（Qwen3-ASR 为 Apache-2.0；pyannote 模型需在 Hugging Face 接受其使用条款）。
