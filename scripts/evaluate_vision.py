"""Reproduce FrameCut's multilingual silent-video semantic editing smoke benchmark."""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import edit
from framecut import export, index_video, run


SOURCES = {
    "flower": "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4",
    "coast": "https://videos.pexels.com/video-files/1409899/1409899-hd_1280_720_25fps.mp4",
    "crowd": "https://videos.pexels.com/video-files/3192307/3192307-hd_1280_720_25fps.mp4",
}
CASES = [
    ("flower", "只保留花蕾和绿色叶子的近景。", (0, 4)),
    ("coast", "只剪海浪拍打岩石海岸的画面。", (4, 8)),
    ("crowd", "Keep the shot of people cheering with raised arms.", (8, 12)),
    ("absent", "只保留一辆正在雪地行驶的红色汽车。", None),
    ("absent_sailboat", "只保留海面上有一艘帆船的镜头。", None),
    ("absent_dog", "Keep only a dog running on the beach.", None),
]


def sample(directory):
    parts = []
    for name, url in SOURCES.items():
        source = directory / f"{name}-source.mp4"
        if not source.exists():
            partial = source.with_suffix(".part")
            request = Request(url, headers={"User-Agent": "FrameCut-Evaluation/0.4"})
            with urlopen(request, timeout=30) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output)
            partial.replace(source)
        parts.append(source)
    output = directory / "benchmark.mp4"
    if output.exists() and all(output.stat().st_mtime >= part.stat().st_mtime for part in parts):
        return output
    inputs = [arg for source in parts for arg in ("-i", str(source))]
    transforms = [
        f"[{i}:v]trim=duration=4,setpts=PTS-STARTPTS,"
        "scale=400:224:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad=400:224:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]"
        for i in range(len(parts))
    ]
    chain = ";".join(transforms + ["[v0][v1][v2]concat=n=3:v=1:a=0[out]"])
    run("ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex_threads", "1",
        "-filter_complex", chain, "-map", "[out]", "-an", "-c:v", "libx264", str(output))
    return output


def score(clips, expected):
    if expected is None:
        return not clips
    if len(clips) != 1:
        return False
    clip = clips[0]
    intersection = max(0, min(clip["end"], expected[1]) - max(clip["start"], expected[0]))
    union = max(clip["end"], expected[1]) - min(clip["start"], expected[0])
    return intersection / union >= 0.75


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--case", choices=[row[0] for row in CASES])
    args = parser.parse_args()
    directory = args.workdir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    video = sample(directory)
    index = index_video(video, visual=True, skip_speech=True)
    report = []
    for name, goal, expected in CASES:
        if args.case and name != args.case:
            continue
        started = time.monotonic()
        result = edit(video, index, goal, count=1, max_seconds=4, use_vision=True,
                      vision_cache=directory / "observations")
        elapsed = round(time.monotonic() - started, 1)
        baseline = edit(video, index, goal, count=1, max_seconds=4)
        passed = score(result["clips"], expected)
        movie = directory / f"{name}.mp4"
        movie.unlink(missing_ok=True)
        if result["clips"]:
            export(video, result["clips"], movie)
            run("ffmpeg", "-v", "error", "-i", str(movie), "-f", "null", "-")
        report.append({"case": name, "expected": expected, "passed": passed,
                       "baseline_passed": score(baseline["clips"], expected),
                       "seconds": elapsed, "result": result})
        print(f"{name}: {'PASS' if passed else 'FAIL'} ({elapsed:.1f}s)", flush=True)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return 0 if all(row["passed"] for row in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
