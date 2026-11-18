import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import analyze
from jobs import Workspace


class WorkspaceTests(unittest.TestCase):
    def test_follow_up_keeps_timestamped_evidence(self):
        index = {"cues": [{"start": 5.7, "end": 7.5, "text": "风力发电", "source": "speech"}]}
        result = analyze(index, "它如何工作？", "general", ["视频提到风力发电吗？"])
        self.assertEqual(result["method"], "retrieval")
        self.assertEqual(result["evidence"][0]["start"], 5.7)

    def test_deduplication_recovery_and_export(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "FRAMECUT_DB_NAMESPACE": uuid.uuid4().hex,
        }):
            root = Path(directory)
            video = root / "silent.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=160x90:rate=10:duration=3", "-c:v", "libx264", str(video),
            ], check=True)
            store = Workspace(root / "data")
            options = {"video": str(video), "model": "tiny", "captions": None,
                       "skip_speech": True, "ocr": None}
            job_id = store.submit("index", options)
            self.assertEqual(job_id, store.submit("index", options))
            with patch("jobs.scan", side_effect=RuntimeError("interrupted")):
                with self.assertLogs("jobs", level="ERROR"):
                    store.run_once()
            self.assertEqual(store.get(job_id)["state"], "failed")
            self.assertTrue((store.folder(job_id) / "transcript.json").exists())

            reopened = Workspace(root / "data")
            reopened.retry(job_id)
            with patch("jobs.index_video", side_effect=AssertionError("stage already saved")):
                reopened.run_once()
            self.assertEqual(reopened.get(job_id)["attempts"], 2)
            self.assertEqual(reopened.get(job_id)["state"], "done")

            plan_id = reopened.submit("plan", {"video": str(video), "index": job_id,
                                              "goal": "剪一条旅行短片", "count": 1,
                                              "seconds": 2, "vision": False})
            reopened.run_once()
            plan = reopened.result(plan_id)
            self.assertEqual(len(plan["clips"]), 1)
            rendered = reopened.submit("export", {"video": str(video), "plan": plan_id,
                                                  "clips": plan["clips"], "vertical": True})
            reopened.run_once()
            subprocess.run(["ffmpeg", "-v", "error", "-i", reopened.result(rendered)["file"],
                            "-f", "null", "-"], check=True)


if __name__ == "__main__":
    unittest.main()
