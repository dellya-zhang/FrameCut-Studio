"""Authenticated, offset-based browser upload service."""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlencode
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from jobs import QuotaExceeded, Workspace, exclusive
from retrieval import config as embedding_config

CHUNK = 4 * 1024**2
HEX = re.compile(r"[a-f0-9]{64}\Z")
MEDIA = {".mp4", ".mov", ".mkv", ".webm"}


def mint(tenant, secret, scope="upload"):
    raw = json.dumps([tenant, int(time.time()) + 7200, scope]).encode()
    body = base64.urlsafe_b64encode(raw).decode()
    return body + "." + hmac.new(secret.encode(), body.encode(), "sha256").hexdigest()


def verify(token, secret, scope="upload"):
    body, signature = token.split(".", 1)
    expected = hmac.new(secret.encode(), body.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("签名无效")
    tenant, expires, permitted = json.loads(base64.urlsafe_b64decode(body))
    if permitted != scope or expires <= time.time() or (tenant != "local" and not HEX.fullmatch(tenant)):
        raise ValueError("上传凭证已过期或无效")
    return tenant


def media_url(base, tenant, secret, file, *, download=False):
    path = f"/media/{file.parent.name}/{file.name}"
    return f"{base.rstrip('/')}{path}?" + urlencode({
        "token": mint(tenant, secret, path), "download": int(download),
    })


def media_response(environ, respond, path, secret):
    match = re.fullmatch(r"/media/(assets|[a-f0-9]{64})/([a-f0-9]{64}\.(?:mp4|mkv|webm|mov)|highlights\.mp4|result\.json)", path)
    params = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        if match is None:
            raise ValueError("invalid path")
        tenant = verify(params.get("token", [""])[0], secret, path)
        root = Path(os.getenv("FRAMECUT_WORKSPACE", ".framecut"))
        folder = root if tenant == "local" else root / "tenants" / tenant
        file = folder / match[1] / match[2]
        size = file.stat().st_size
    except (ValueError, TypeError, OSError):
        respond("404 Not Found", [("Cache-Control", "no-store")])
        return []
    start, end = 0, size - 1
    request_range = environ.get("HTTP_RANGE", "")
    if request_range:
        requested = re.fullmatch(r"bytes=(\d*)-(\d*)", request_range)
        if requested and requested[1]:
            start = int(requested[1])
            if requested[2]:
                end = min(end, int(requested[2]))
        elif requested and requested[2] and int(requested[2]):
            start = max(0, size - int(requested[2]))
        else:
            start = size
        if start > end:
            respond("416 Range Not Satisfiable", [("Content-Range", f"bytes */{size}")])
            return []
    headers = [("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream"),
               ("Content-Length", str(end - start + 1)), ("Accept-Ranges", "bytes"),
               ("Cache-Control", "private, no-store")]
    if request_range:
        headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
    if params.get("download") == ["1"]:
        name = "index.json" if file.suffix == ".json" else "highlights.mp4"
        headers.append(("Content-Disposition", f'attachment; filename="{name}"'))
    respond("206 Partial Content" if request_range else "200 OK", headers)
    if environ["REQUEST_METHOD"] == "HEAD":
        return []

    def stream():
        with file.open("rb") as handle:
            handle.seek(start)
            left = end - start + 1
            while left and (chunk := handle.read(min(CHUNK, left))):
                yield chunk
                left -= len(chunk)
    return stream()


class NamedFile:
    def __init__(self, handle, name):
        self.handle, self.name = handle, name

    def read(self, size):
        return self.handle.read(size)

    def seek(self, offset):
        self.handle.seek(offset)


def application(environ, respond):
    def send(status, result):
        data = json.dumps(result, ensure_ascii=False).encode()
        respond(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store")])
        return [data]

    path, method = environ["PATH_INFO"], environ["REQUEST_METHOD"]
    if path == "/" and method == "GET":
        respond("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                          ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store")])
        return [Path(__file__).with_name("upload.html").read_bytes()]
    secret = os.getenv("FRAMECUT_UPLOAD_SECRET", "")
    if len(secret) < 32:
        return send("503 Service Unavailable", {"error": "续传服务未配置"})
    if path.startswith("/media/") and method in {"GET", "HEAD"}:
        return media_response(environ, respond, path, secret)
    try:
        scheme, token = environ.get("HTTP_AUTHORIZATION", "").split(" ", 1)
        if scheme != "Bearer":
            raise ValueError("凭证格式错误")
        tenant = verify(token, secret)
    except (TypeError, ValueError):
        return send("401 Unauthorized", {"error": "请从工作台重新打开续传页面"})

    match = re.fullmatch(r"/uploads/([0-9a-f]{64})", path)
    if match is None or method not in {"POST", "PATCH"}:
        return send("404 Not Found", {"error": "上传地址不存在"})
    root = Path(os.getenv("FRAMECUT_WORKSPACE", ".framecut"))
    store = Workspace(root if tenant == "local" else root / "tenants" / tenant)
    staging = store.root / ".uploads"
    staging.mkdir(exist_ok=True)
    key = match[1]
    metadata, partial = staging / f"{key}.json", staging / f"{key}.part"
    try:
        with exclusive(staging / ".lock"):
            size = int(environ.get("CONTENT_LENGTH") or 0)
            if not 0 < size <= (CHUNK if method == "PATCH" else 4096):
                return send("413 Content Too Large", {"error": "请求大小无效"})
            if method == "POST":
                request = json.loads(environ["wsgi.input"].read(size))
                name, length = request["name"], request["length"]
                if (not isinstance(name, str) or len(name) > 255 or
                        Path(name).suffix.lower() not in MEDIA or type(length) is not int or length <= 0):
                    raise ValueError("视频文件无效")
                if metadata.exists():
                    state = json.loads(metadata.read_text())
                    if state["length"] != length or state["name"] != name:
                        raise ValueError("该续传编号关联了不同的文件")
                else:
                    cap = int(os.getenv("FRAMECUT_MAX_UPLOAD_BYTES", str(20 * 1024**3)))
                    assets = store.root / "assets"
                    stored = sum(p.stat().st_size for p in assets.glob("*")
                                 if p.is_file() and not p.name.startswith("."))
                    reserved = 0
                    for item in staging.glob("*.json"):
                        entry = json.loads(item.read_text())
                        if entry.get("job"):
                            continue
                        if time.time() - item.stat().st_mtime > 7 * 86400:
                            item.with_suffix(".part").unlink(missing_ok=True)
                            item.unlink()
                        else:
                            reserved += entry["length"]
                    if stored + reserved + length > cap:
                        raise QuotaExceeded("素材库容量不足")
                    state = {"name": name, "length": length,
                             "skip_speech": request.get("skip_speech") is True}
                    persist(metadata, state)
            elif not metadata.exists():
                return send("404 Not Found", {"error": "请先初始化上传"})
            else:
                state = json.loads(metadata.read_text())
            if state.get("job"):
                return send("200 OK", {"offset": state["length"], "job": state["job"]})
            offset = partial.stat().st_size if partial.exists() else 0
            if method == "PATCH":
                expected_offset = int(environ["HTTP_UPLOAD_OFFSET"])
                if expected_offset != offset:
                    return send("409 Conflict", {"offset": offset})
                if offset + size > state["length"]:
                    raise ValueError("分片超出文件大小")
                chunk = environ["wsgi.input"].read(size)
                if len(chunk) != size or hashlib.sha256(chunk).hexdigest() != environ.get("HTTP_CHUNK_SHA256"):
                    raise ValueError("分片校验失败")
                with partial.open("ab") as output:
                    output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                offset += size
                metadata.touch()
            if offset == state["length"]:
                with partial.open("rb") as handle:
                    video = store.ingest(NamedFile(handle, state["name"]), active_reservation=key)
                state["job"] = store.submit("index", {
                    "video": video, "name": state["name"], "captions": None, "model": "tiny",
                    "ocr": None, "skip_speech": state["skip_speech"], "embedding": embedding_config(),
                })
                persist(metadata, state)
                partial.unlink(missing_ok=True)
            return send("200 OK", {"offset": offset, "job": state.get("job")})
    except QuotaExceeded as error:
        return send("429 Too Many Requests", {"error": str(error)})
    except (KeyError, TypeError, ValueError) as error:
        return send("400 Bad Request", {"error": str(error)})


def persist(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    temp.replace(path)


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class PrivateRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # The request URL may contain a short-lived media capability.
        self.log_message("%s %s", self.command, code)


if __name__ == "__main__":
    host = os.getenv("FRAMECUT_UPLOAD_BIND", "127.0.0.1")
    make_server(host, 8502, application, server_class=Server,
                handler_class=PrivateRequestHandler).serve_forever()
