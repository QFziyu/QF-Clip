import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from core.audio_events import analyze_audio
from core.clip_selector import CLIP_SCHEMA, build_windows, select_clips, validate_clip_payload
from core.llm_client import BudgetExceeded, HTTPStatusError, LLMClient, LLMError, extract_json
from core.timeline import normalize_candidates


def sample_clip(**updates):
    value = {"start": 10.0, "end": 20.0, "category": "tactical_analysis", "title": "这次换人带来变化",
             "description": "主播讨论换人的影响", "reason": "有完整观点", "score": 75,
             "tags": ["换人"], "evidence": ["这次换人非常关键"]}
    value.update(updates)
    return value


def response(value, usage=None):
    return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}], "usage": usage}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.cfg = {"selection": {"window_seconds": 60, "overlap_seconds": 15, "max_window_chars": 2000,
                                  "context_before": 2, "context_after": 3, "min_seconds": 5,
                                  "max_seconds": 60, "dedup_iou": 0.65, "max_candidates": 60},
                    "openai": {"api_key": "fake-test-key", "model": "test", "max_retries": 1, "max_calls": 20,
                               "budget_usd": 1, "max_output_tokens": 500, "response_format": "auto"}}
        self.transcript = {"segments": [{"id": "a", "start": 10, "end": 20, "text": "我觉得这次换人非常关键。"},
                                        {"id": "b", "start": 28, "end": 38, "text": "他的跑位拉开了空间。"}]}

    def tearDown(self):
        self.directory.cleanup()

    def test_grounding_rejects_fiction_and_nonfinite_times(self):
        values = [sample_clip(), sample_clip(evidence=["曼城已经进球了"]), sample_clip(start=float("nan")),
                  sample_clip(end=200), sample_clip(start=29, end=38)]
        result = normalize_candidates(values, self.transcript, 100, self.cfg)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start"], result[0]["end"]), (8, 23))
        self.assertEqual(result[0]["segment_ids"], ["a"])
        self.assertTrue(result[0]["needs_human_review"])

    def test_duplicate_merge_preserves_distinct_overlapping_viewpoints(self):
        transcript = {"segments": [{"start": 10, "end": 20, "text": "这次换人非常关键。他的跑位拉开了空间。"}]}
        values = [sample_clip(), sample_clip(title="换人非常关键", score=65),
                  sample_clip(title="跑位创造空间", evidence=["他的跑位拉开了空间"])]
        result = normalize_candidates(values, transcript, 100, self.cfg)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["duplicate_count"], 1)

    def test_overlap_windows_keep_boundary_speech_and_bound_text(self):
        transcript = {"segments": [{"start": i * 5, "end": i * 5 + 4, "text": "观点" * 100} for i in range(50)]}
        windows = build_windows(transcript, 250, self.cfg)
        seen = [segment["id"] for window in windows for segment in window["segments"]]
        self.assertTrue(all(str(i) in seen for i in range(50)))
        self.assertGreater(seen.count("11"), 1)
        self.assertTrue(all(len(json.dumps(window["segments"], ensure_ascii=False)) <= 2000 for window in windows))

    def test_strict_schema_and_fenced_fallback(self):
        payload = {"clips": [sample_clip()]}
        self.assertEqual(extract_json("回答如下：\n```json\n" + json.dumps(payload) + "\n```"), payload)
        invalid = sample_clip(start=True)
        with self.assertRaises(ValueError):
            validate_clip_payload({"clips": [invalid]})
        with self.assertRaises(LLMError):
            extract_json('{"clips": [{"start": NaN}]}')

    def test_api_format_fallback_counts_every_attempt(self):
        seen = []
        def transport(payload):
            seen.append(payload)
            if len(seen) < 3:
                raise HTTPStatusError(400, "unsupported response_format")
            return response({"clips": [sample_clip()]}, {"prompt_tokens": 20, "completion_tokens": 40})
        client = LLMClient(self.cfg["openai"], self.root / "budget.json", transport, sleeper=lambda _: None)
        output = client.complete([{"role": "user", "content": "JSON"}], CLIP_SCHEMA, validate_clip_payload)
        self.assertEqual(len(output["clips"]), 1)
        self.assertNotIn("response_format", seen[-1])
        ledger = json.loads((self.root / "budget.json").read_text())
        self.assertEqual(len(ledger["attempts"]), 3)
        self.assertEqual([a["status"] for a in ledger["attempts"]], ["failed", "failed", "success"])

    def test_budget_survives_restart_and_reserves_before_transport(self):
        options = {**self.cfg["openai"], "max_calls": 1}
        transport = lambda _: response({"clips": []})
        for iteration in range(2):
            client = LLMClient(options, self.root / "budget.json", transport, sleeper=lambda _: None)
            if not iteration:
                client.complete([], CLIP_SCHEMA)
            else:
                with self.assertRaises(BudgetExceeded):
                    client.complete([], CLIP_SCHEMA)
        options = {**options, "budget_usd": 0.00000001}
        with self.assertRaises(BudgetExceeded):
            LLMClient(options, self.root / "tiny.json", lambda _: self.fail("request must not run")).complete([], CLIP_SCHEMA)

    def test_reasoning_gateway_token_parameter_fallback(self):
        seen = []
        def transport(payload):
            seen.append(payload)
            if "max_tokens" in payload:
                raise HTTPStatusError(400, "max_tokens not supported; use max_completion_tokens")
            return response({"clips": []})
        LLMClient(self.cfg["openai"], self.root / "budget.json", transport).complete([], CLIP_SCHEMA)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["max_completion_tokens"], 500)

    def test_finite_retries_and_failure_accounting(self):
        calls = []
        def broken(payload):
            calls.append(payload)
            return {"choices": [{"message": {"content": "not json"}}]}
        client = LLMClient(self.cfg["openai"], self.root / "budget.json", broken, sleeper=lambda _: None)
        with self.assertRaises(LLMError):
            client.complete([], CLIP_SCHEMA, validate_clip_payload)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(json.loads((self.root / "budget.json").read_text())["attempts"]), 2)

    def test_window_cache_avoids_second_api_request(self):
        with patch("core.clip_selector.LLMClient.complete", return_value={"clips": [sample_clip()]}) as complete:
            first = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root)
            second = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root)
        self.assertEqual(first, second)
        self.assertEqual(complete.call_count, 1)
        self.assertTrue((self.root / "selection" / "decisions.json").exists())

    def test_cached_windows_work_without_api_key(self):
        with patch("core.clip_selector.LLMClient.complete", return_value={"clips": [sample_clip()]}):
            expected = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root)
        self.cfg["openai"]["api_key"] = ""
        with patch("core.clip_selector.LLMClient.complete", side_effect=AssertionError("must use cache")):
            actual = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root)
        self.assertEqual(actual, expected)

    def test_failure_keeps_finished_windows_without_global_plan(self):
        transcript = {"segments": self.transcript["segments"] + [{"start": 100, "end": 110, "text": "后半段真实观点。"}]}
        with patch("core.clip_selector.LLMClient.complete", side_effect=[{"clips": [sample_clip()]}, LLMError("offline")]):
            with self.assertRaises(LLMError):
                select_clips(transcript, {"duration": 150}, self.cfg, self.root)
        self.assertEqual(len(list((self.root / "selection").glob("window_*.json"))), 1)
        self.assertFalse((self.root / "selection" / "decisions.json").exists())

    def test_mock_is_explicit_and_uses_provided_times(self):
        result = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root, mock=True)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(c["source"] == "mock" and c["title"].startswith("[模拟]") for c in result))
        self.assertTrue(all(0 <= c["start"] < c["end"] <= 100 for c in result))

    def test_audio_energy_detects_injected_burst_without_goal_claim(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy is not installed")
        sample_rate = 16000
        path = self.root / "音量测试.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
            for second in range(15):
                volume = 0.3 if 7 <= second < 9 else 0.008
                signal = np.sin(np.arange(sample_rate) * 2 * np.pi * 440 / sample_rate) * volume
                output.writeframes((signal * 32767).astype("<i2").tobytes())
        events = analyze_audio(path, self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["start"], events[0]["end"]), (7, 9))
        self.assertEqual(events[0]["kind"], "energy_rise")

    def test_semantic_mode_ignores_audio_hints_and_extra_audio_candidates(self):
        self.cfg["selection"]["mode"] = "semantic"
        events = [{"start": 80, "end": 82, "strength": 4, "kind": "energy_rise"}]
        with patch("core.clip_selector.LLMClient.complete", return_value={"clips": [sample_clip()]}) as call:
            clips = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root, audio_events=events)
        self.assertEqual(len(clips), 1)
        self.assertTrue(all(clip["source"] == "llm" for clip in clips))
        sent = json.loads(call.call_args.args[0][1]["content"])
        self.assertEqual(sent["audio_hints"], [])

    def test_corrupt_budget_is_never_reset_or_used_to_allow_more_requests(self):
        examples = [[], {"attempts": [None]}, {"attempts": [{"cost_usd": -1, "status": "success"}]},
                    {"attempts": [{"cost_usd": float("nan"), "status": "success"}]},
                    {"attempts": [{"cost_usd": 0.1, "status": "unknown"}]}]
        for invalid in examples:
            with self.subTest(ledger=invalid):
                path = self.root / "budget.json"
                path.write_text(json.dumps(invalid), encoding="utf-8")
                original = path.read_bytes()
                client = LLMClient(self.cfg["openai"], path, lambda _: self.fail("must not call API"))
                with self.assertRaisesRegex(LLMError, "拒绝重置"):
                    client.complete([], CLIP_SCHEMA)
                self.assertEqual(path.read_bytes(), original)

    def test_corrupt_window_cache_is_quarantined_and_rebuilt(self):
        with patch("core.clip_selector.LLMClient.complete", return_value={"clips": [sample_clip()]}) as call:
            expected = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root)
            cached = next((self.root / "selection").glob("window_*.json"))
            cached.write_text("[]", encoding="utf-8")
            actual = select_clips(self.transcript, {"duration": 100}, self.cfg, self.root)
        self.assertEqual(expected, actual)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(list(cached.parent.glob(cached.name + ".corrupt-*"))), 1)


if __name__ == "__main__":
    unittest.main()
