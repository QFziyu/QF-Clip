"""Provider switching and settings tests; no paid APIs or real credentials."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.config import load_config
from core.llm_settings import save_settings, cache_identity
from core.llm_client import LLMClient, LLMError
from core.codex_cli import environment, login_status, request, CodexCLIError

SCHEMA = {"type":"object", "additionalProperties":False, "required":["clips"],
          "properties":{"clips":{"type":"array", "items":{"type":"string"}}}}


class ProfileFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/".env").write_text("OPENAI_API_KEY=old-test-key\nOPENAI_BASE_URL=https://old.invalid/v1\nOPENAI_MODEL=old-model\n", encoding="utf-8")
        self.patches = [patch("core.config.ROOT", self.root), patch.dict(os.environ, {k:v for k,v in os.environ.items() if k in ("USERPROFILE","HOMEDRIVE","HOMEPATH","SystemRoot","TEMP","TMP","PATH")}, clear=True)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

class ProfileTests(ProfileFixture, unittest.TestCase):
    def test_switch_preserves_api_and_separate_model_effort(self):
        cfg = load_config()
        original = (self.root/".env").read_bytes()
        save_settings(cfg, "codex_cli", {"CODEX_MODEL":"codex-test-model", "CODEX_REASONING_EFFORT":"high"})
        cfg = load_config()
        self.assertEqual(cfg["openai"]["provider"], "codex_cli")
        self.assertEqual(cfg["openai"]["model"], "codex-test-model")
        self.assertEqual(cfg["openai"]["reasoning_effort"], "high")
        self.assertEqual(cfg["openai"]["api_key"], "")
        save_settings(cfg, "api", {"OPENAI_API_KEY":"", "OPENAI_MODEL":"api-new", "OPENAI_REASONING_EFFORT":"low"})
        cfg = load_config()
        self.assertEqual((cfg["openai"]["model"], cfg["openai"]["reasoning_effort"]), ("api-new","low"))
        self.assertEqual(cfg["openai"]["api_key"], "old-test-key")
        self.assertEqual(cfg["_llm_codex_profile"]["model"], "codex-test-model")
        self.assertEqual((self.root/".env").read_bytes(), original)

    def test_ui_override_beats_stale_launcher_environment(self):
        cfg = load_config()
        save_settings(cfg, "api", {"OPENAI_BASE_URL":"https://new.invalid/v1", "OPENAI_MODEL":"new-model", "OPENAI_API_KEY":"new-test-key"})
        with patch.dict(os.environ, {"OPENAI_API_KEY":"stale", "OPENAI_MODEL":"stale", "OPENAI_BASE_URL":"https://stale.invalid"}):
            options = load_config()["openai"]
        self.assertEqual(options["api_key"], "new-test-key")
        self.assertEqual(options["model"], "new-model")
        self.assertEqual(options["base_url"], "https://new.invalid/v1")

    def test_invalid_fields_do_not_publish_partial_settings(self):
        cfg = load_config()
        for fields in ({"OPENAI_BASE_URL":"file:///tmp/model"}, {"OPENAI_MODEL":"model\nTOKEN=bad"},
                       {"OPENAI_REASONING_EFFORT":"imaginary"}, {"OPENAI_BASE_URL":"https://user:pass@host/v1"}):
            with self.assertRaises(ValueError):
                save_settings(cfg, "api", fields)
        self.assertFalse((self.root/".env.llm").exists())

    def test_cache_identity_keeps_default_api_but_separates_effort(self):
        self.assertEqual(cache_identity({"model":"same"}), {})
        low = cache_identity({"provider":"api", "model":"same", "reasoning_effort":"low"})
        high = cache_identity({"provider":"api", "model":"same", "reasoning_effort":"high"})
        self.assertNotEqual(low, high)
        self.assertNotEqual(high, cache_identity({"provider":"codex_cli", "model":"same", "reasoning_effort":"high"}))

    def test_codex_default_needs_no_api_key_or_model(self):
        cfg = load_config()
        save_settings(cfg, "codex_cli", {"CODEX_MODEL":"", "CODEX_REASONING_EFFORT":"default"})
        cfg = load_config()
        self.assertEqual(cfg["openai"]["model"], "")
        self.assertEqual(cfg["openai"]["api_key"], "")
        self.assertEqual(cfg["openai"]["input_cost_per_million"], 0)

    def test_api_reasoning_parameter_is_optional_and_forwarded(self):
        for effort in ("default", "high"):
            seen = []
            def transport(payload):
                seen.append(payload)
                return {"choices":[{"message":{"content":'{"clips":[]}'}}]}
            client = LLMClient({"model":"exact-model","reasoning_effort":effort}, self.root/(effort+".json"), transport)
            client.complete([], SCHEMA)
            self.assertEqual(seen[0]["model"], "exact-model")
            if effort == "default": self.assertNotIn("reasoning_effort", seen[0])
            else: self.assertEqual(seen[0]["reasoning_effort"], effort)

    def test_codex_budget_is_separate_from_exhausted_api_ledger(self):
        path = self.root/"api_budget.json"
        old = {"version":1,"attempts":[{"id":0,"status":"success","cost_usd":100}]}
        path.write_text(json.dumps(old), encoding="utf-8")
        opts = {"provider":"codex_cli","max_calls":2,"budget_usd":0.01,"model":""}
        with patch("core.codex_cli.request", return_value={"choices":[{"message":{"content":'{"clips":[]}'}}]}) as call:
            LLMClient(opts,path).complete([], SCHEMA)
        self.assertEqual(json.loads(path.read_text()), old)
        ledger = json.loads((self.root/"api_budget_codex.json").read_text())
        self.assertEqual(ledger["attempts"][0]["cost_usd"], 0)
        call.assert_called_once()

    def test_codex_selection_and_subtitle_review_do_not_require_key(self):
        from core.clip_selector import select_clips
        from core.subtitle_review import proofread_cues
        cfg = load_config()
        cfg["openai"].update(provider="codex_cli",model="",api_key="")
        cfg["prompt_path"] = str(Path(__file__).resolve().parents[1]/"prompts"/"clip_selection.md")
        cfg["selection"].update(mode="semantic", audio_events=False)
        transcript = {"segments":[{"id":"s1","start":1,"end":5,"text":"本场比赛开始了"}]}
        with patch("core.llm_client.LLMClient.complete", return_value={"clips":[]}):
            self.assertEqual(select_clips(transcript,{"duration":10},cfg,self.root/"project"), [])
        cfg["subtitle_review"]["enabled"] = True
        with patch("core.llm_client.LLMClient.complete", return_value={"edits":[]}) as call:
            _, report = proofread_cues([{"start":1,"end":5,"text":"本场比赛开始了"}],cfg,self.root/"project",request=True)
        self.assertEqual(report["status"], "complete")
        call.assert_called_once()


class CLITransportTests(unittest.TestCase):
    def test_official_login_home_is_detected_without_reading_credentials(self):
        from core.codex_cli import detected_home
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root/'.codex-official'
            official.mkdir()
            (official/'auth.json').touch()
            with patch('core.codex_cli.Path.home', return_value=root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(detected_home({}), str(official))
                self.assertEqual(environment({})['CODEX_HOME'], str(official))
                self.assertEqual(detected_home({'codex_home': str(root/'explicit')}), str(root/'explicit'))
                (root/'.codex').mkdir()
                (root/'.codex/auth.json').touch()
                self.assertEqual(detected_home({}), '')
            with patch.dict(os.environ, {'CODEX_HOME': str(root/'inherited')}):
                self.assertEqual(detected_home({}), str(root/'inherited'))

    def test_environment_removes_keys_and_proxy_endpoint(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY":"secret","CODEX_API_KEY":"other","OPENAI_BASE_URL":"https://proxy.invalid","CODEX_HOME":"original"}):
            env = environment({})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertEqual(env["CODEX_HOME"], "original")

    def test_api_key_login_is_rejected_before_model_execution(self):
        completed = subprocess.CompletedProcess([],0,"","Logged in using an API key")
        with patch("core.codex_cli.executable",return_value=["fake"]), patch("core.codex_cli.subprocess.run",return_value=completed):
            self.assertFalse(login_status({})["ready"])
            with self.assertRaises(CodexCLIError):
                request({}, {}, 5)

    def test_real_subprocess_receives_exact_model_effort_schema_and_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root/"fake_codex.py"
            record = root/"record.json"
            fake.write_text("""
import json, os, pathlib, sys
args=sys.argv[1:]
if args[:2]==['login','status']:
    print('Logged in using ChatGPT');sys.exit(0)
assert 'OPENAI_API_KEY' not in os.environ
assert 'CODEX_API_KEY' not in os.environ
assert 'OPENAI_BASE_URL' not in os.environ
schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
assert schema['required']==['clips']
assert args[args.index('--model')+1]=='exact-model'
assert 'model_reasoning_effort="high"' in args
assert args[args.index('--sandbox')+1]=='read-only'
assert '--ignore-user-config' in args
assert '--ephemeral' in args
assert pathlib.Path(args[args.index('--image')+1]).read_bytes()==b'fake-image'
prompt=sys.stdin.read()
assert 'quoted input' in prompt
assert '--run-command' not in args
pathlib.Path(os.environ['FAKE_RECORD']).write_text(json.dumps(args))
pathlib.Path(args[args.index('--output-last-message')+1]).write_text('{"clips":[]}')
""", encoding="utf-8")
            payload = {"messages":[{"role":"user","content":[{"type":"text","text":"quoted input --run-command"},
                {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,ZmFrZS1pbWFnZQ=="}}]}],
                "response_format":{"json_schema":{"schema":SCHEMA}}}
            with patch("core.codex_cli.executable", return_value=[sys.executable,str(fake)]), patch.dict(os.environ,{"FAKE_RECORD":str(record),"OPENAI_API_KEY":"secret"}):
                result = request({"model":"exact-model","reasoning_effort":"high"},payload,10)
            self.assertEqual(json.loads(result["choices"][0]["message"]["content"]), {"clips":[]})
            self.assertTrue(record.is_file())

    def test_cli_configuration_error_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("core.codex_cli.request",side_effect=CodexCLIError("unsupported model")) as call:
                with self.assertRaises(LLMError):
                    LLMClient({"provider":"codex_cli","max_retries":2},Path(temporary)/"ledger.json",sleeper=lambda _:None).complete([],SCHEMA)
            call.assert_called_once()


class SettingsUITests(ProfileFixture, unittest.TestCase):
    def test_settings_form_switches_and_saves_profiles(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_string("from core.config import load_config\nfrom ui.llm_settings import panel\npanel(load_config())",default_timeout=15).run()
        self.assertEqual(len(app.exception),0,str(app.exception))
        app.radio(key="llm-provider").set_value("codex_cli").run()
        app.text_input(key="llm-codex_cli-model").set_value("codex-ui-model")
        app.selectbox(key="llm-codex_cli-effort").set_value("high")
        next(b for b in app.button if b.label=="保存并使用此来源").click().run()
        self.assertEqual(len(app.exception),0,str(app.exception))
        self.assertEqual(len(app.error),0,str(app.error))
        cfg=load_config()
        self.assertEqual(cfg["openai"]["provider"], "codex_cli")
        self.assertEqual(cfg["openai"]["model"], "codex-ui-model")
        self.assertEqual(cfg["openai"]["reasoning_effort"], "high")
        self.assertTrue(any("codex-ui-model" in c.value for c in app.caption))
        app.radio(key="llm-provider").set_value("api").run()
        self.assertEqual(app.text_input(key="llm-api-model").value,"old-model")
        app.selectbox(key="llm-api-effort").set_value("low")
        next(b for b in app.button if b.label=="保存并使用此来源").click().run()
        self.assertEqual(len(app.exception),0,str(app.exception))
        self.assertEqual(load_config()["openai"]["api_key"],"old-test-key")
        self.assertEqual(load_config()["_llm_codex_profile"]["reasoning_effort"],"high")


if __name__ == "__main__":
    unittest.main()
