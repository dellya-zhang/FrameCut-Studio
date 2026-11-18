"""Check actual Chinese semantic retrieval against timestamped transcript fixtures."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framecut import search
from retrieval import build, config, hybrid


def main():
    settings = config()
    if not settings:
        raise SystemExit("Set FRAMECUT_EMBEDDING_MODEL and FRAMECUT_EMBEDDING_API_BASE.")
    cues = [{"start": 60 * number, "end": 60 * number + 5, "text": text, "source": "speech"}
            for number, text in enumerate([
                "花园里的粉色花瓣随风轻晃。",
                "远处的白色浪花不断冲刷礁石。",
                "球场中很多球迷举起双手庆祝。",
            ])]
    index = {"cues": cues, "semantic": build(cues, settings)}
    results = []
    for query, expected in [("滨海涌动的潮汐", 60), ("体育看台上的人群欢呼", 120), ("植物开花", 0)]:
        lexical = search(index, query)
        matches = hybrid(index, query, lexical, 1)
        actual = matches[0]["start"] if matches else None
        results.append({"query": query, "expected": expected, "actual": actual,
                        "keyword_hits": len(lexical), "passed": actual == expected and not lexical})
    print(json.dumps({"model": settings["model"], "cases": results}, ensure_ascii=False, indent=2))
    return 0 if all(case["passed"] for case in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
