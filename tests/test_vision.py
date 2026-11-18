import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import choose_shots, describe_batch, vision_request


class VisionEvidenceTests(unittest.TestCase):
    def test_evidence_requires_exact_ids_and_selection_bounds(self):
        chapter = (1, {"start": 4, "end": 8, "evidence": []})
        with patch("agent.representative_frame", return_value="data:image/jpeg;base64,fixture"):
            for result in [
                {"shots": []},
                {"shots": [{"id": 2, "description": "sea"}]},
                {"shots": [{"id": 1, "description": ""}]},
            ]:
                with self.subTest(result=result), patch("agent.model_reply", return_value=json.dumps(result)):
                    with self.assertRaises(ValueError):
                        describe_batch(None, [chapter], "test", "http://localhost")
        for picked in [[2], [True], [1, 1]]:
            with self.subTest(picked=picked), patch("agent.model_reply", return_value=json.dumps(
                {"selected": picked},
            )):
                with self.assertRaises(ValueError):
                    choose_shots("test", "http://localhost", "keep the sea", [{"id": 1}], 1)

    def test_reuses_descriptions_across_different_goals(self):
        clip = {"start": 3, "end": 6, "evidence": []}
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"FRAMECUT_VISION_MODEL": "test"},
        ), patch("agent.describe_batch", return_value={1: "花朵"}) as describe, patch(
            "agent.choose_shots", side_effect=[[], [1]],
        ):
            cache = Path(folder)
            video = cache / "video.mp4"
            video.touch()
            result = vision_request(video, [clip], "只保留汽车", 1, cache)
            self.assertEqual(result[1], [])
            self.assertEqual(result[2][0]["frame"], 4.5)
            flower = vision_request(video, [clip], "只保留花朵", 1, cache)
            self.assertEqual(flower[1][0]["reason"], "花朵")
            self.assertEqual(describe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
