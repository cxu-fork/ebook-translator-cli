import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from ebook_translator.config import EngineConfig, load_config
from ebook_translator.engines.anthropic_engine import AnthropicEngine
from ebook_translator.engines.openai_engine import OpenAIEngine


class ConfigEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="et_config_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_expands_user_paths(self):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({
            "cache_dir": "~/.cache/ebook-translator-test",
            "glossary_path": "~/glossary.txt",
            "log_file": "~/ebook-translator.log",
        }), encoding="utf-8")

        cfg = load_config(cfg_path)

        self.assertTrue(cfg.cache_dir.startswith(os.path.expanduser("~")))
        self.assertTrue(cfg.glossary_path.startswith(os.path.expanduser("~")))
        self.assertTrue(cfg.log_file.startswith(os.path.expanduser("~")))

    def test_default_temperature_is_conservative_for_translation(self):
        self.assertEqual(0.3, EngineConfig().temperature)

    def test_openai_full_completion_endpoint_is_not_duplicated(self):
        cfg = EngineConfig(
            api_key="x",
            base_url="http://gateway/v1/chat/completions",
            stream=False,
        )
        engine = OpenAIEngine(cfg, "English", "Chinese")

        self.assertEqual(
            "http://gateway/v1/chat/completions",
            engine.chat_url,
        )

    def test_openai_custom_endpoint_allows_empty_model(self):
        cfg = EngineConfig(
            api_key="x",
            base_url="http://gateway/v1",
            model="",
            stream=False,
        )
        engine = OpenAIEngine(cfg, "English", "Chinese")

        self.assertEqual("", engine.model)
        self.assertNotIn("model", engine._body("hi", "prompt", stream=False))

    def test_anthropic_full_messages_endpoint_is_not_duplicated(self):
        cfg = EngineConfig(
            api_key="x",
            base_url="http://gateway/v1/messages",
            stream=False,
        )
        engine = AnthropicEngine(cfg, "English", "Chinese")

        self.assertEqual("http://gateway/v1/messages", engine.messages_url)


if __name__ == "__main__":
    unittest.main()
