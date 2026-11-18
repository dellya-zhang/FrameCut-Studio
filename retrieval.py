"""Optional semantic retrieval with an always-available lexical path."""
import json
import logging
import math
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


def config():
    model = os.getenv("FRAMECUT_EMBEDDING_MODEL")
    endpoint = (os.getenv("FRAMECUT_EMBEDDING_API_BASE") or os.getenv("FRAMECUT_API_BASE")
                or "http://127.0.0.1:11434/v1")
    return {"model": model, "endpoint": endpoint.rstrip("/")} if model else None


def embed(settings, texts):
    headers = {"Content-Type": "application/json"}
    if key := os.getenv("FRAMECUT_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"model": settings["model"], "input": texts}).encode()
    request = Request(settings["endpoint"] + "/embeddings", body, headers)
    with urlopen(request, timeout=12) as response:
        items = sorted(json.load(response)["data"], key=lambda item: item["index"])
    if [item["index"] for item in items] != list(range(len(texts))):
        raise ValueError("embedding response is missing input positions")
    vectors = []
    for item in items:
        vector = [float(x) for x in item["embedding"]]
        magnitude = math.hypot(*vector)
        if not math.isfinite(magnitude) or magnitude == 0:
            raise ValueError("invalid embedding")
        vectors.append([x / magnitude for x in vector])
    return vectors


def build(cues, settings):
    if not settings:
        return None
    groups = {}
    for number, cue in enumerate(cues):
        groups.setdefault(int(cue["start"] // 60), []).append(number)
    chunks = list(groups.values())
    if not chunks:
        return None
    vectors = []
    try:
        for offset in range(0, len(chunks), 24):
            batch = chunks[offset:offset + 24]
            vectors.extend(embed(settings, [
                " ".join(cues[i]["text"] for i in indexes)[:3500] for indexes in batch
            ]))
    except (URLError, TimeoutError, ValueError, TypeError, KeyError) as error:
        LOGGER.warning("Semantic index unavailable, keeping lexical index (%s)", type(error).__name__)
        return None
    return {"settings": settings, "chunks": chunks, "vectors": vectors}


def semantic_candidates(index, query, limit):
    semantic = index.get("semantic")
    settings = config()
    if not settings or not semantic or settings != semantic["settings"] or limit <= 0:
        return []
    try:
        target, = embed(settings, [query])
        scored = []
        for vector, members in zip(semantic["vectors"], semantic["chunks"], strict=True):
            score = sum(a * b for a, b in zip(target, vector, strict=True))
            if score >= 0.3:
                scored.append((score, members))
        scored.sort(key=lambda item: -item[0])
        result = []
        for _, members in scored[:limit]:
            result.extend(index["cues"][number] for number in members[:3])
        return result[:limit]
    except (URLError, TimeoutError, ValueError, TypeError, KeyError, IndexError) as error:
        LOGGER.warning("Semantic query unavailable, using keywords (%s)", type(error).__name__)
        return []


def hybrid(index, query, lexical, limit):
    ranked = {}
    for candidates in (lexical, semantic_candidates(index, query, limit)):
        for rank, cue in enumerate(candidates):
            key = (cue["start"], cue["end"], cue["text"], cue["source"])
            score, _ = ranked.get(key, (0, cue))
            ranked[key] = (score + 1 / (12 + rank), cue)
    return [cue for _, cue in sorted(ranked.values(), key=lambda item: -item[0])[:limit]]
