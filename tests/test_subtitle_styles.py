"""User-selected subtitle styles and intro-relative timestamp regression checks."""
from pathlib import Path
import tempfile
import unittest

from core.subtitle import parse_srt, write_subtitles
from core.subtitle_styles import SUBTITLE_PRESETS, ass_color, resolve_subtitle_settings


class SubtitleStyleTests(unittest.TestCase):
    def test_default_style_is_compatible_and_presets_are_independent(self):
        default = resolve_subtitle_settings({})
        self.assertEqual(default["primary_color"], "#FFFFFF")
        self.assertEqual(default["outline_color"], "#000000")
        self.assertEqual(default["font_size"], 34 * .75)
        self.assertEqual(default["outline"], 3)
        self.assertFalse(default["bold"])
        self.assertEqual(set(SUBTITLE_PRESETS), {"classic", "gold", "cyan", "comic", "boxed"})
        for name in SUBTITLE_PRESETS:
            settings = resolve_subtitle_settings({"preset": name})
            self.assertEqual(settings["preset"], name)
            self.assertNotIn("label", settings)
        customized = resolve_subtitle_settings({"preset": "gold", "primary_color": "#a1B2c3", "bold": False})
        self.assertEqual(customized["primary_color"], "#A1B2C3")
        self.assertFalse(customized["bold"])
        self.assertEqual(SUBTITLE_PRESETS["gold"]["primary_color"], "#FFD34E")

    def test_ass_color_converts_rgb_order_and_alpha(self):
        self.assertEqual(ass_color("#1234AB"), "&H00AB3412")
        self.assertEqual(ass_color("#aBcD09", 64), "&H4009CDAB")

    def test_invalid_values_cannot_inject_ass_or_emit_invalid_numbers(self):
        invalid = [
            {"preset": "untrusted"}, {"preset": []},
            {"primary_color": "red"}, {"outline_color": "#0011"},
            {"back_color": "#001122\n[Events]"},
            {"font": "font\n[Events]"}, {"font": "Arial,72"}, {"font": "font\u2028Events"},
            {"font": r"{\pos(0,0)}"}, {"font": ""}, {"font": "X" * 101},
            {"font_size": 0}, {"vertical_font_size": float("nan")},
            {"font_size": True}, {"outline": float("inf")},
            {"shadow": -1}, {"spacing": 100}, {"bold": "false"},
            {"italic": 1}, {"border_style": 2}, {"alignment": 0},
            {"back_alpha": 256}, {"max_chars": 2}, {"margin_v": -1},
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                resolve_subtitle_settings(fields)

    def test_presets_and_overrides_reach_ass_style(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in SUBTITLE_PRESETS:
                result = write_subtitles({}, {"start": 20, "end": 22}, root / f"{name}.srt", root / f"{name}.ass",
                                         {"subtitle": {"preset": name}},
                                         edited_srt="1\n00:00:00,100 --> 00:00:01,500\n曼城！")
                ass = Path(result["ass"]).read_text(encoding="utf-8")
                fields = next(line for line in ass.splitlines() if line.startswith("Style: Default,")).split(",")
                self.assertEqual(fields[3], ass_color(SUBTITLE_PRESETS[name]["primary_color"]))
                self.assertEqual(fields[8], "0")  # Italic
                self.assertEqual(fields[15], str(SUBTITLE_PRESETS[name]["border_style"]))
                if name == "boxed":
                    self.assertEqual(fields[5], "&H40271811")
                    self.assertEqual(fields[6], "&H40271811")
            result = write_subtitles({}, {"start": 0, "end": 2}, root / "custom.srt", root / "custom.ass",
                                     {"subtitle": {"font": "Arial", "font_size": 60, "vertical_font_size": 72,
                                                   "primary_color": "#112233", "outline_color": "#445566",
                                                   "outline": 4.5, "shadow": 2, "bold": True, "italic": True,
                                                   "spacing": 1.5}}, orientation="vertical",
                                     edited_srt="1\n00:00:00,100 --> 00:00:01,500\n测试")
            custom = Path(result["ass"]).read_text(encoding="utf-8")
            self.assertIn("Style: Default,Arial,72,&H00332211,&H000000FF,&H00665544,", custom)
            self.assertIn(",-1,-1,0,0,100,100,1.5,0,1,4.5,2,", custom)

    def test_intro_offset_shifts_edited_and_automatic_cues_only_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual = "1\n00:00:00,250 --> 00:00:01,750\n开场后的字幕"
            result = write_subtitles({}, {"start": 100, "end": 102}, root / "edited.srt", root / "edited.ass", {},
                                     edited_srt=manual, time_offset=3.5)
            cues = parse_srt(Path(result["srt"]).read_text(encoding="utf-8-sig"), 5.5)
            self.assertEqual(cues[0], {"start": 3.75, "end": 5.25, "text": "开场后的字幕"})
            self.assertIn("Dialogue: 0,0:00:03.75,0:00:05.25,", Path(result["ass"]).read_text(encoding="utf-8"))
            self.assertEqual(result["time_offset"], 3.5)
            original = {"segments": [{"start": 100.5, "end": 101.5, "text": "进球"}]}
            result = write_subtitles(original, {"start": 100, "end": 102}, root / "auto.srt", root / "auto.ass", {}, time_offset=4)
            cues = parse_srt(Path(result["srt"]).read_text(encoding="utf-8-sig"), 6)
            self.assertEqual((cues[0]["start"], cues[0]["end"]), (4.5, 5.5))
            self.assertEqual(original["segments"][0]["start"], 100.5)
            # An intro must not make invalid body-relative edits appear valid.
            with self.assertRaises(ValueError):
                write_subtitles({}, {"start": 0, "end": 2}, root / "bad.srt", root / "bad.ass", {},
                                edited_srt="1\n00:00:00,000 --> 00:00:03,000\n越界", time_offset=10)

    def test_offset_rejects_nonfinite_negative_and_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for offset in (-1, float("inf"), float("nan"), True, "3"):
                with self.subTest(offset=offset), self.assertRaises(ValueError):
                    write_subtitles({}, {"start": 0, "end": 2}, root / "x.srt", root / "x.ass", {}, time_offset=offset)


if __name__ == "__main__":
    unittest.main()
