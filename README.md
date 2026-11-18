# FrameCut Studio

FrameCut Studio 是一款本地长视频理解与智能剪辑工具。把课程、访谈或会议录像转成带时间戳的内容索引，搜索原视频中的证据，再按主题导出高光合集。

- **视频索引**：导入 SRT / WebVTT，或用 faster-whisper 在 CPU 上分段转写。可选 Tesseract 识别画面文字。
- **内容理解**：浏览五分钟一组的章节摘录，按中英文关键词查找语音和画面文字，点击证据时间跳转播放器。
- **智能剪辑**：按主题匹配证据，延展上下文并跳过重叠区间。勾选片段后按原时间顺序合并，支持原画幅或保留完整画面的 9:16 竖屏。
- **结果导出**：下载 JSON 索引和 H.264 MP4。素材和推理均在本机处理，首次使用内置转写模型会下载权重。

## 本地启动

需要 Python 3.10+ 与带 libx264 编码器的 FFmpeg（包括 ffprobe）。OCR 可选，需要安装 Tesseract 和对应语言包（中文为 `chi_sim`）。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run studio.py
```

打开终端显示的本地地址。在左侧添加素材和可选字幕，点击「建立索引」。用「证据检索」定位原片，或在「智能剪辑」输入主题、调整片段数量和时长并导出 MP4。

## 命令行

命令行直接读取磁盘文件，适合超出工作台上传限制的长视频。导入字幕且关闭 OCR 时，CLI 只需要 Python 标准库和 FFmpeg。

```bash
python framecut.py index lecture.mp4 lecture.json --subtitles lecture.srt
python framecut.py chapters lecture.json
python framecut.py search lecture.json "太阳能"
python framecut.py cut lecture.json "太阳能" lecture.mp4 highlights.mp4 --count 3 --max-seconds 20 --vertical
```

移除 `--subtitles` 可自动转写。需要更高识别精度时可加 `--model small`，也可以传入本地 CTranslate2 Whisper 模型目录。可附加 `--ocr chi_sim+eng --interval 30` 定时采样画面文字。

## 当前版本

章节由时间桶内的原文摘录组成。检索和选段基于关键词匹配，支持检索字幕和 OCR 文本。视频画面中的动作、人物或不含文字的场景无法直接作为检索条件。切片边界和时长以原视频帧率为准。Web 工作台面向单机使用，上传及下载文件占用内存。
