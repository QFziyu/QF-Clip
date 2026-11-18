"""Publication names must work on Windows without sacrificing existing files."""
import tempfile
from pathlib import Path
import unittest

from core.utils import (atomic_json, named_video_path, read_json,
                        rename_video_for_title, safe_filename_stem)


class ExportNameTests(unittest.TestCase):
    def test_unicode_reserved_characters_and_device_names(self):
        self.assertEqual(safe_filename_stem('曼城：战术/复盘? ⚽\n.'), "曼城：战术_复盘_ ⚽_")
        self.assertEqual(safe_filename_stem(' .. '), "clip")
        self.assertEqual(safe_filename_stem('', fallback="候选42"), "候选42")
        for title in ["CON", "con.txt", "NUL", "COM1", "lpt9", "COM¹", "CONOUT$"]:
            self.assertTrue(safe_filename_stem(title).startswith("_"), title)
        self.assertEqual(safe_filename_stem("COM10"), "COM10")

    def test_emoji_length_budget_is_utf16_and_distinct_long_titles_keep_hashes(self):
        a = safe_filename_stem("⚽😀" * 200 + "甲", max_units=100)
        b = safe_filename_stem("⚽😀" * 200 + "乙", max_units=100)
        self.assertLessEqual(len(a.encode("utf-16-le")) // 2, 100)
        self.assertNotEqual(a, b)
        self.assertNotIn("\ufffd", a)

    def test_collision_preserves_existing_files_and_migration_moves_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "video.mp4"
            old.write_bytes(b"reviewed video")
            atomic_json(old.with_suffix(".timeline.json"), {"output": str(old), "start": 4.5})
            conflict = root / "人工标题.mp4"
            conflict.write_bytes(b"unrelated original")
            moved = rename_video_for_title(old, "人工标题", "clip42")
            self.assertNotEqual(moved, conflict)
            self.assertEqual(moved.read_bytes(), b"reviewed video")
            self.assertEqual(conflict.read_bytes(), b"unrelated original")
            self.assertEqual(read_json(moved.with_suffix(".timeline.json")), {"output": str(moved), "start": 4.5})
            self.assertFalse(old.exists())
            self.assertEqual(rename_video_for_title(moved, "人工标题", "clip42"), moved)

    def test_orphan_timeline_is_a_collision_and_full_paths_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "标题.timeline.json").write_text("orphan", encoding="utf-8")
            self.assertNotEqual(named_video_path(root, "标题"), root / "标题.mp4")
            long_root = root / ("中" * 90)
            result = named_video_path(long_root, "很长标题😀" * 80)
            self.assertLessEqual(len(str(result.with_suffix(".timeline.json")).encode("utf-16-le")) // 2, 240)
            with self.assertRaisesRegex(ValueError, "路径过长"):
                named_video_path(root / ("中" * 250), "标题")


if __name__ == "__main__":
    unittest.main()
