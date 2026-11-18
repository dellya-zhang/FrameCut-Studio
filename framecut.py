"""Timestamped video search and highlight export."""
import argparse
import json
import math
import re
import subprocess
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from visual import plan_visual, scan


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    source: str = "speech"


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"{command[0]} failed")
    return result.stdout


def duration(video):
    data = json.loads(run(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
        str(video.resolve()),
    ))
    if not any(s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
               for s in data["streams"]):
        raise ValueError("文件没有视频轨道")
    seconds = float(data["format"]["duration"])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("无法读取视频时长")
    return seconds


def subtitles(path):
    def seconds(value):
        total = 0
        for part in value.replace(",", ".").split(":"):
            total = total * 60 + float(part)
        return total

    pattern = re.compile(
        r"(?m)^[ \t]*((?:\d+:)?\d{2}:\d{2}[.,]\d{3})[ \t]+-->[ \t]+"
        r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})[^\n]*\n(.+?)(?=\n[ \t]*\n|\Z)",
        re.S,
    )
    for start, end, body in pattern.findall(path.read_text(encoding="utf-8-sig")):
        text = " ".join(re.sub(r"<[^>]+>", "", body).split())
        if text:
            yield Cue(seconds(start), seconds(end), text)


def transcribe(video, length, model, cache=None):
    import numpy as np
    from faster_whisper import WhisperModel

    whisper = None
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        audio = Path(temp) / "chunk.wav"
        for offset in range(0, math.ceil(length), 300):
            saved = cache / f"{offset}.json" if cache is not None else None
            if saved is not None and saved.exists():
                yield from (Cue(**item) for item in json.loads(saved.read_text(encoding="utf-8")))
                continue
            run("ffmpeg", "-v", "error", "-y", "-ss", str(offset), "-i", str(video.resolve()),
                "-t", "300", "-map", "0:a:0", "-ac", "1", "-ar", "16000", str(audio))
            with wave.open(str(audio), "rb") as pcm:
                samples = np.frombuffer(pcm.readframes(pcm.getnframes()), dtype="<i2")
            cues = []
            if samples.size:
                if whisper is None:
                    whisper = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=4)
                waveform = samples.astype(np.float32) / 32768
                segments, _ = whisper.transcribe(waveform, vad_filter=True, beam_size=3)
                for segment in segments:
                    if segment.text.strip():
                        cues.append(Cue(offset + segment.start, offset + segment.end, segment.text.strip()))
            if saved is not None:
                partial = saved.with_suffix(".tmp")
                partial.write_text(json.dumps([asdict(cue) for cue in cues], ensure_ascii=False),
                                   encoding="utf-8")
                partial.replace(saved)
            yield from cues


def screen_text(video, length, language, interval):
    previous = ""
    with tempfile.TemporaryDirectory() as temp:
        frame = Path(temp) / "frame.png"
        for offset in range(0, math.ceil(length), interval):
            frame.unlink(missing_ok=True)
            run("ffmpeg", "-v", "error", "-ss", str(offset), "-i", str(video.resolve()),
                "-map", "0:V:0", "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
                "-threads", "1", str(frame))
            if not frame.exists():
                continue
            text = " ".join(run("tesseract", str(frame), "stdout", "-l", language).split())
            if text and text != previous:
                yield Cue(offset, min(offset + 1, length), text, "screen")
            previous = text


def index_video(video, caption_file=None, model="tiny", ocr=None, interval=30, *,
                visual=False, skip_speech=False, speech_cache=None):
    if interval < 1:
        raise ValueError("OCR 采样间隔必须至少为 1 秒")
    length = duration(video)
    cues = []
    if caption_file:
        cues = list(subtitles(caption_file))
        if not cues:
            raise ValueError("字幕中没有可读的时间戳和文本")
    elif not skip_speech:
        metadata = run("ffprobe", "-v", "error", "-select_streams", "a:0",
                       "-show_entries", "stream=index", "-of", "csv=p=0", str(video.resolve()))
        cues = list(transcribe(video, length, model, speech_cache)) if metadata.strip() else []
    if ocr:
        cues.extend(screen_text(video, length, ocr, interval))
    cues = [Cue(c.start, min(c.end, length), c.text, c.source)
            for c in cues if 0 <= c.start < min(c.end, length)]
    data = {"duration": length, "cues": [asdict(c) for c in sorted(cues, key=lambda c: c.start)]}
    if visual:
        data["visual"] = scan(video, length)
    return data


def tokens(text):
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold())
    terms = set()
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 1:
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms.add(word)
    return terms


def search(index, query, limit=8):
    terms = tokens(query)
    if not terms:
        return []
    candidates = []
    for cue in index["cues"]:
        match = terms & tokens(cue["text"])
        if match:
            score = len(match) / len(terms)
            if query.strip().casefold() in cue["text"].casefold():
                score += 1
            candidates.append((score, cue))
    candidates.sort(key=lambda item: (-item[0], item[1]["start"]))
    return [cue for _, cue in candidates[:limit]]


def chapters(index):
    sections = {}
    for cue in index["cues"]:
        sections.setdefault(int(cue["start"] // 300), []).append(cue)
    result = []
    for group in sections.values():
        distinct = list(dict.fromkeys(cue["text"] for cue in group))
        result.append({
            "start": group[0]["start"],
            "end": max(cue["end"] for cue in group),
            "excerpt": " / ".join(distinct[:3]),
        })
    return result


def plan(index, query, count=3, max_seconds=30):
    if count < 1 or not math.isfinite(max_seconds) or max_seconds < 1:
        raise ValueError("片段数量和最长时长必须至少为 1")
    clips = []
    for cue in search(index, query, len(index["cues"])):
        start = max(0, cue["start"] - min(1.5, max_seconds / 4))
        end = min(index["duration"], start + max_seconds, max(cue["end"] + 1.5, start + 4))
        if any(start < clip["end"] and end > clip["start"] for clip in clips):
            continue
        clips.append({**cue, "start": start, "end": end})
        if len(clips) == count:
            break
    return sorted(clips, key=lambda clip: clip["start"])


def export(video, clips, output, vertical=False):
    if not clips:
        raise ValueError("没有可导出的片段")
    length = duration(video)
    if any(not (0 <= clip["start"] < clip["end"] <= length) for clip in clips):
        raise ValueError("剪辑区间超出视频范围")
    if output.suffix.lower() != ".mp4" or output.resolve() == video.resolve():
        raise ValueError("输出必须是不同于源文件的 MP4 路径")
    output.parent.mkdir(parents=True, exist_ok=True)
    video_filter = (
        "scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1"
        if vertical else "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    )
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        temp = Path(directory).resolve()
        manifest = []
        for number, clip in enumerate(clips):
            part = f"clip-{number:04}.mp4"
            run("ffmpeg", "-v", "error", "-ss", str(clip["start"]), "-i", str(video.resolve()),
                "-t", str(clip["end"] - clip["start"]), "-map", "0:V:0", "-map", "0:a:0?",
                "-vf", video_filter, "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(temp / part))
            manifest.append(f"file '{part}'\n")
        (temp / "clips.txt").write_text("".join(manifest), encoding="utf-8")
        final = temp / "highlight.mp4"
        run("ffmpeg", "-v", "error", "-f", "concat", "-safe", "1", "-i", str(temp / "clips.txt"),
            "-c", "copy", "-movflags", "+faststart", str(final))
        final.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("index")
    build.add_argument("video", type=Path)
    build.add_argument("output", type=Path)
    build.add_argument("--subtitles", type=Path)
    build.add_argument("--model", default="tiny")
    build.add_argument("--ocr", help="Tesseract language, e.g. chi_sim+eng")
    build.add_argument("--interval", type=int, default=30)
    build.add_argument("--visual", action="store_true", help="index visual shot and quality features")
    build.add_argument("--skip-speech", action="store_true", help="skip automatic audio transcription")
    for name in ("chapters", "search", "cut", "highlights", "edit"):
        command = commands.add_parser(name)
        command.add_argument("index", type=Path)
        if name in {"search", "cut", "edit"}:
            command.add_argument("query")
        if name in {"cut", "highlights", "edit"}:
            command.add_argument("video", type=Path)
            command.add_argument("output", type=Path)
            command.add_argument("--count", type=int, default=3)
            command.add_argument("--max-seconds", type=float, default=30 if name == "cut" else 12)
            command.add_argument("--vertical", action="store_true")
        if name == "highlights":
            command.add_argument("--style", choices=["scenery", "daily"], default="scenery")
        if name == "edit":
            command.add_argument("--vision", action="store_true")
            command.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "index":
            if args.output.resolve() in {args.video.resolve(),
                                        args.subtitles.resolve() if args.subtitles else None}:
                raise ValueError("索引输出必须使用独立路径")
            data = index_video(args.video, args.subtitles, args.model, args.ocr, args.interval,
                               visual=args.visual, skip_speech=args.skip_speech)
            args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{len(data['cues'])} evidence records, {len(data.get('visual', []))} visual samples")
            return
        data = json.loads(args.index.read_text(encoding="utf-8"))
        if args.command == "chapters":
            result = chapters(data)
        elif args.command == "search":
            result = search(data, args.query)
        elif args.command == "edit":
            from agent import edit

            result = edit(args.video, data, args.query, args.count, args.max_seconds, args.vision)
            if not args.plan_only and result["clips"]:
                export(args.video, result["clips"], args.output, args.vertical)
                result["file"] = str(args.output)
        else:
            if args.command == "highlights":
                if not data.get("visual"):
                    raise ValueError("请先使用 index --visual 建立画面索引")
                clips = plan_visual(data, args.style, args.count, args.max_seconds)
            else:
                clips = plan(data, args.query, args.count, args.max_seconds)
            export(args.video, clips, args.output, args.vertical)
            result = {"file": str(args.output), "clips": clips}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
