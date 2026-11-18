import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from agent import edit
from identity import tenant_workspace
from jobs import QuotaExceeded, Workspace
from retrieval import build, hybrid
from uploads import application, media_url, mint


class PlatformTests(unittest.TestCase):
    def test_concurrent_claim_recovery_and_quota(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "FRAMECUT_MAX_ACTIVE_JOBS": "2", "FRAMECUT_DB_NAMESPACE": uuid.uuid4().hex,
        }):
            ws = Workspace(root)
            first = ws.submit("index", {"video": "one"})
            second = ws.submit("index", {"video": "two"})
            self.assertEqual(first, ws.submit("index", {"video": "one"}))
            with self.assertRaises(QuotaExceeded):
                ws.submit("index", {"video": "three"})
            abandoned = ws.try_lock(first)
            ws.update(first, "running", "probe")
            claimed = []
            ready = threading.Event()
            release = threading.Event()

            def process(job):
                claimed.append(job["id"])
                ready.set()
                if not release.wait(5):
                    raise TimeoutError()

            with patch.object(ws, "process", side_effect=process):
                thread = threading.Thread(target=ws.run_once)
                thread.start()
                try:
                    self.assertTrue(ready.wait(5))
                    self.assertEqual(claimed, [second])
                    self.assertFalse(ws.run_once())
                finally:
                    release.set()
                    thread.join(5)
                    abandoned.close()
                self.assertTrue(ws.run_once())
            self.assertEqual(claimed, [second, first])
            self.assertEqual(ws.get(first)["state"], "done")

    def test_model_budget_counts_only_new_work(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "FRAMECUT_MAX_MODEL_JOBS_PER_DAY": "1", "FRAMECUT_DB_NAMESPACE": uuid.uuid4().hex,
        }):
            work = Workspace(root)
            spec = {"model": "fixture", "question": "first"}
            first = work.submit("question", spec)
            self.assertEqual(work.submit("question", spec), first)
            with self.assertRaises(QuotaExceeded):
                work.submit("question", {**spec, "question": "second"})
            self.assertTrue(work.submit("question", {"question": "offline"}))

    def test_upload_resumes_with_tenant_scoped_media(self):
        secret = "private-test-key-only" * 3
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "FRAMECUT_UPLOAD_SECRET": secret, "FRAMECUT_WORKSPACE": root,
            "FRAMECUT_DB_NAMESPACE": uuid.uuid4().hex,
        }):
            alice = tenant_workspace(root, {"iss": "https://id.example", "sub": "alice"})
            bob = tenant_workspace(root, {"iss": "https://id.example", "sub": "bob"})
            self.assertNotEqual(alice, bob)
            token = mint(alice.name, secret)
            key = "0" * 64
            upload_path = f"/uploads/{key}"
            file = b"firstsecond"

            def call(method, target, body=b"", **more):
                url = urlsplit(target)
                responses = []
                env = {"REQUEST_METHOD": method, "PATH_INFO": url.path, "QUERY_STRING": url.query,
                       "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
                       "HTTP_AUTHORIZATION": "Bearer " + token, **more}
                data = b"".join(application(env, lambda status, headers: responses.append(status)))
                return responses[0], data

            meta = json.dumps({"name": "fixture.mp4", "length": len(file)}).encode()
            response, data = call("POST", upload_path, meta, HTTP_AUTHORIZATION="Bearer invalid")
            self.assertTrue(response.startswith("401"))
            self.assertEqual(json.loads(call("POST", upload_path, meta)[1])["offset"], 0)
            first = file[:5]
            self.assertTrue(call("PATCH", upload_path, first, HTTP_UPLOAD_OFFSET="0",
                                 HTTP_CHUNK_SHA256=hashlib.sha256(first).hexdigest())[0].startswith("200"))
            self.assertEqual(json.loads(call("POST", upload_path, meta)[1])["offset"], 5)
            rest = file[5:]
            response, data = call("PATCH", upload_path, rest, HTTP_UPLOAD_OFFSET="0",
                                  HTTP_CHUNK_SHA256=hashlib.sha256(rest).hexdigest())
            self.assertTrue(response.startswith("409"))
            response, data = call("PATCH", upload_path, rest, HTTP_UPLOAD_OFFSET="5",
                                  HTTP_CHUNK_SHA256=hashlib.sha256(rest).hexdigest())
            self.assertTrue(response.startswith("200"))
            job = Workspace(alice).get(json.loads(data)["job"])
            self.assertEqual(Workspace(bob).list(), [])
            video = Path(json.loads(job["spec"])["video"])
            link = media_url("http://localhost", alice.name, secret, video)
            self.assertEqual(call("GET", link, HTTP_RANGE="bytes=1-3"), ("206 Partial Content", b"irs"))
            other = media_url("http://localhost", bob.name, secret, video)
            self.assertTrue(call("GET", other)[0].startswith("404"))

    def test_planner_and_reviewer_cannot_change_timeline(self):
        cue = {"start": 5, "end": 9, "text": "花卉", "source": "screen"}
        index = {"duration": 20, "cues": [cue]}
        with patch.dict(os.environ, {"FRAMECUT_PLANNER_MODEL": "planner",
                                     "FRAMECUT_REVIEW_MODEL": "reviewer"}), patch(
            "agent.model_reply", side_effect=[
                '{"queries":["花卉"],"style":"scenery"}',
                '{"keep":[1,100],"note":"invalid ID"}',
            ],
        ):
            result = edit(None, index, "保留一个特写", count=1, max_seconds=8)
        self.assertEqual(result["retrieval"]["queries"], ["保留一个特写", "花卉"])
        clip, = result["clips"]
        self.assertEqual(clip["evidence"], [cue])
        self.assertEqual((clip["start"], clip["end"]), (3.5, 10.5))
        self.assertEqual(result["review"]["method"], "timeline")

    def test_vector_lookup_and_fallback(self):
        settings = {"model": "fixture", "endpoint": "http://127.0.0.1:11434/v1"}
        cue = {"start": 5, "end": 10, "text": "落日", "source": "screen"}
        with patch.dict(os.environ, {"FRAMECUT_EMBEDDING_MODEL": "fixture",
                                     "FRAMECUT_API_BASE": settings["endpoint"]}):
            with patch("retrieval.embed", return_value=[[1.0, 0.0]]):
                index = {"cues": [cue], "semantic": build([cue], settings)}
                self.assertEqual(hybrid(index, "黄昏", [], 1), [cue])
            with patch("retrieval.embed", side_effect=TimeoutError):
                self.assertEqual(hybrid(index, "黄昏", [cue], 1), [cue])


if __name__ == "__main__":
    unittest.main()
