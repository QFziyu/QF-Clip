"""Cleanup preservation proof rejects missing, overwritten, and colliding videos."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_preserved_media import check_playable, snapshot, verify


class PreservedMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fcf-preserve-")
        self.root = Path(self.temporary.name)
        self.export = self.root / "exports" / "已改标题.mp4"
        self.export.parent.mkdir()
        self.export.write_bytes(b"valid mocked video content")
        self.other = self.root / "preview.MP4"
        self.other.write_bytes(b"preview")
        self.before = snapshot(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_preserves_all_videos_after_title_rename(self):
        renamed = self.export.with_name("新标题.mp4")
        self.export.rename(renamed)
        journal = {"moves": [{"old_path": str(self.export), "new_path": str(renamed)}]}
        result = verify(self.before, journal)
        self.assertTrue(result["passed"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total_bytes"], len(b"valid mocked video content") + len(b"preview"))

    def test_rejects_deleted_or_same_size_changed_video(self):
        self.other.write_bytes(b"changed")
        result = verify(self.before)
        self.assertFalse(result["passed"])
        self.assertIn("SHA-256", next(item["error"] for item in result["results"] if not item["passed"]))
        self.other.unlink()
        result = verify(self.before)
        self.assertFalse(result["passed"])
        self.assertIn("不存在", next(item["error"] for item in result["results"] if not item["passed"]))

    def test_decodes_formal_exports_only_and_uses_resolved_title(self):
        renamed = self.export.with_name("新标题.mp4")
        self.export.rename(renamed)
        journal = {"moves": [{"old_path": str(self.export), "new_path": str(renamed)}]}
        with patch("scripts.verify_preserved_media.check_playable", return_value={"decode": "passed"}) as media:
            result = verify(self.before, journal, decode_exports=True, ffmpeg="tool ffmpeg", ffprobe="tool ffprobe")
        media.assert_called_once_with(renamed.resolve(), "tool ffmpeg", "tool ffprobe", 600)
        self.assertTrue(result["passed"])
        self.assertEqual(result["decoded_count"], 1)

    def test_current_export_layout_uses_matching_metadata_video(self):
        current = self.root / "project" / "clip" / "horizontal_key" / "video.mp4"
        current.parent.mkdir(parents=True)
        current.write_bytes(b"current package")
        metadata = current.parent / "metadata.json"
        metadata.write_text(json.dumps({"video": str(current.resolve())}), encoding="utf-8")
        before = snapshot(self.root)
        self.assertTrue(next(item for item in before["files"] if item["path"] == str(current.resolve()))["formal_export"])
        self.assertEqual(before["formal_export_count"], 2)
        with patch("scripts.verify_preserved_media.check_playable", return_value={"decode": "passed"}) as media:
            result = verify(before, decode_exports=True)
        self.assertEqual(media.call_count, 2)
        self.assertEqual(result["decoded_count"], 2)
        metadata.write_text(json.dumps({"video": str(self.other.resolve())}), encoding="utf-8")
        changed = snapshot(self.root)
        self.assertFalse(next(item for item in changed["files"] if item["path"] == str(current.resolve()))["formal_export"])

    def test_rejects_conflicting_or_cyclic_journal(self):
        with self.assertRaisesRegex(ValueError, "冲突"):
            verify(self.before, {"moves": [
                {"old_path": str(self.export), "new_path": str(self.other)},
                {"old_path": str(self.export), "new_path": str(self.root / "third.mp4")},
            ]})
        with self.assertRaisesRegex(ValueError, "循环"):
            verify(self.before, {"moves": [
                {"old_path": str(self.export), "new_path": str(self.other)},
                {"old_path": str(self.other), "new_path": str(self.export)},
            ]})

    def test_media_validation_runs_probe_then_complete_decode_without_shell(self):
        probe = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}],
                 "format": {"duration": "2.5"}}
        responses = [subprocess.CompletedProcess([], 0, json.dumps(probe), ""),
                     subprocess.CompletedProcess([], 0, "", "")]
        with patch("scripts.verify_preserved_media.subprocess.run", side_effect=responses) as run:
            result = check_playable(self.export, "ffmpeg", "ffprobe")
        self.assertEqual(result["decode"], "passed")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.kwargs["shell"] is False for call in run.call_args_list))
        self.assertIn(str(self.export), run.call_args_list[0].args[0])
        self.assertIn("-xerror", run.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
