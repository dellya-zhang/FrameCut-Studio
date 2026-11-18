"""Sample video frames and rank highlights using visual features."""
import math
import subprocess
import tempfile

SAMPLE_RATE = 2
WIDTH, HEIGHT = 128, 72


def scan(video, duration):
    import numpy as np

    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(video.resolve()),
        "-map", "0:V:0", "-vf", f"fps={SAMPLE_RATE}:round=up,scale={WIDTH}:{HEIGHT}:flags=area",
        "-an", "-c:v", "rawvideo", "-threads", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ]
    samples = []
    previous = previous_histogram = None
    frame_bytes = WIDTH * HEIGHT * 3
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors) as process:
            try:
                while raw := process.stdout.read(frame_bytes):
                    if len(raw) != frame_bytes:
                        raise RuntimeError("FFmpeg 输出了不完整的视频帧")
                    frame = np.frombuffer(raw, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
                    rgb = frame.astype(np.float32) / 255
                    gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
                    levels = frame.astype(np.int32) >> 6
                    bins = 16 * levels[:, :, 0] + 4 * levels[:, :, 1] + levels[:, :, 2]
                    histogram = np.bincount(bins.ravel(), minlength=64) / gray.size
                    motion = float(np.abs(rgb - previous).mean()) if previous is not None else 0
                    color_change = (float(np.abs(histogram - previous_histogram).sum()) / 2
                                    if previous_histogram is not None else 0)
                    cut = (motion > 0.48 or
                           motion > 0.27 and color_change > 0.24 or
                           motion > 0.16 and color_change > 0.45)
                    # Reward visible detail and avoid near-black or blown-out frames.
                    exposure = float(((gray > 0.08) & (gray < 0.95)).mean())
                    detail = min(1.0, float(np.abs(np.diff(gray, axis=1)).mean()
                                           + np.abs(np.diff(gray, axis=0)).mean()) * 8)
                    saturation = float((rgb.max(axis=2) - rgb.min(axis=2)).mean())
                    quality = exposure * (0.55 + 0.30 * detail + 0.15 * saturation)
                    at = len(samples) / SAMPLE_RATE
                    if at >= duration:
                        continue
                    samples.append({"at": at, "quality": round(quality, 4),
                                    "motion": round(motion if not cut else 0, 4), "cut": cut})
                    previous, previous_histogram = rgb, histogram
            except BaseException:
                process.kill()
                raise
            if process.wait():
                errors.seek(0)
                raise RuntimeError(errors.read().decode(errors="replace").strip() or "读取视频帧失败")
    if not samples:
        raise ValueError("无法抽取视频画面")
    return samples


def shots(samples, duration):
    if not samples:
        return []
    boundaries = [sample["at"] for sample in samples if sample["cut"]]
    return list(zip([0.0, *boundaries], [
        *boundaries, min(duration, samples[-1]["at"] + 1 / SAMPLE_RATE),
    ]))


def plan_visual(index, style="scenery", count=3, max_seconds=12, *, spread=False):
    if style not in {"scenery", "daily"}:
        raise ValueError("未知的画面剪辑风格")
    if count < 1 or not math.isfinite(max_seconds) or max_seconds < 1:
        raise ValueError("片段数量和最长时长必须至少为 1")
    samples = index.get("visual", [])
    candidates = []
    target = min(max_seconds, 8 if style == "scenery" else 5)
    for shot_number, (start, end) in enumerate(shots(samples, index["duration"])):
        if end - start < min(1.0, index["duration"]):
            continue
        windows = max(1, math.ceil((end - start) / target))
        size = (end - start) / windows
        for number in range(windows):
            begin, finish = start + number * size, min(end, start + (number + 1) * size)
            frames = samples[math.ceil(begin * SAMPLE_RATE):math.ceil(finish * SAMPLE_RATE)]
            if not frames:
                continue
            quality = sum(frame["quality"] for frame in frames) / len(frames)
            motion = sum(frame["motion"] for frame in frames) / len(frames)
            if quality < 0.08:
                continue
            if style == "scenery":
                score = quality - 0.65 * motion
                label = "风景候选 · 优先保留清晰、稳定的画面"
            else:
                score = quality + 0.55 * min(motion / 0.15, 1)
                label = "生活候选 · 优先保留清晰、有变化的画面"
            candidates.append((score, shot_number, {
                "start": begin, "end": finish, "source": "visual", "text": label,
            }))

    # Take the best window from each shot before using additional windows.
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[2]["start"]))
    primary, remaining, represented = [], [], set()
    for _, shot_number, clip in candidates:
        (remaining if shot_number in represented else primary).append(clip)
        represented.add(shot_number)
    ranked = primary + remaining
    if spread and len(ranked) > count:
        bins = {}
        for clip in ranked:
            midpoint = (clip["start"] + clip["end"]) / 2
            bins.setdefault(min(count - 1, int(midpoint * count / index["duration"])), clip)
        representatives = list(bins.values())
        ranked = representatives + [clip for clip in ranked if clip not in representatives]
    return sorted(ranked[:count], key=lambda clip: clip["start"])
