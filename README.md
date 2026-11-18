# FrameCut Studio

FrameCut Studio 是一个把**视频理解、剪辑规划和 MP4 导出**串起来的本地视频 Agent。它从同一份视频建立时间戳索引，读取语音、字幕和画面证据，再按你的剪辑目标提出可以回看片段来源的方案。

## 使用流程

1. 上传视频，可选导入 SRT / WebVTT。Agent 会抽样分析镜头，并在本机用 faster-whisper 转写语音。风景或无旁白素材可以勾选「仅分析画面」，跳过自动转写。也可用 Tesseract 识别画面文字。
2. 在「剪辑 Agent」描述目标，例如「保留风力发电的讲解，穿插稳定的镜头」或「剪一条节奏舒缓的旅行短片」。Agent 结合文字证据和镜头特征产生剪辑方案，也可接入视觉模型识别画面内容。
3. 核对内容摘要、时间戳和选段依据，预览、勾选后导出原画幅或 9:16 H.264 MP4。可在「内容与镜头」浏览索引，在「证据检索」点击时间戳回看原片。

## 本地启动

需要 Python 3.10+ 与带 libx264 的 FFmpeg（包括 ffprobe）。OCR 可选，需要 Tesseract 和对应语言包（简体中文为 `chi_sim`）。首次转写会下载模型权重。Web 工作台运行在本机，上传和下载占用内存，大文件可直接走命令行。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run studio.py
```

## 画面语义理解

默认可离线按文字证据、镜头稳定性、细节和变化程度做初剪。要按「保留海边和日落」等具体画面内容选段，可接入支持图文输入的 OpenAI 兼容模型，例如本机 Ollama 的 `qwen2.5vl:3b`。

```bash
ollama pull qwen2.5vl:3b
export FRAMECUT_VISION_MODEL=qwen2.5vl:3b
export FRAMECUT_API_BASE=http://127.0.0.1:11434/v1
streamlit run studio.py
```

如模型服务需要认证，可通过环境变量 `FRAMECUT_API_KEY` 设置密钥。开启视觉模型后，候选片段的代表帧和相关文字会发送到配置的服务。模型只能选择已存在的片段编号，实际剪切边界来自本地索引。视觉模型检查的是抽样后的代表帧，镜头边界精度取决于采样频率，且很短的画面事件可能遗漏。

## 命令行

```bash
python framecut.py index lecture.mp4 lecture.json --visual --subtitles lecture.srt
python framecut.py edit lecture.json "保留风力发电讲解" lecture.mp4 highlights.mp4 --count 3

python framecut.py index trip.mp4 trip.json --visual --skip-speech
python framecut.py edit trip.json "选出适合旅行短片的稳定镜头" trip.mp4 trip-reel.mp4 --vertical
python framecut.py edit trip.json "保留海边和日落" trip.mp4 sunset.mp4 --vision --vertical
```

用 `--plan-only` 只打印剪辑方案。移除 `--subtitles` 可自动转写，`--model small` 可选更大的模型。可用 `--ocr chi_sim+eng --interval 30` 定时识别画面文字。独立的 `chapters`、`search`、`cut` 和 `highlights` 命令可用于脚本集成。
