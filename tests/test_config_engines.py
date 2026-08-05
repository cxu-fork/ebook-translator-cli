import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ebook_translator.config import EngineConfig, load_config
from ebook_translator.engines.anthropic_engine import AnthropicEngine
from ebook_translator.engines import get_engine
from ebook_translator.engines.openai_engine import DeepSeekEngine, OpenAIEngine


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

    def test_full_endpoints_preserve_query_strings(self):
        openai = OpenAIEngine(EngineConfig(
            api_key="x",
            base_url="https://gateway/v1/chat/completions?api-version=1",
        ), "English", "Chinese")
        anthropic = AnthropicEngine(EngineConfig(
            api_key="x", base_url="https://gateway/v1?api-version=1",
        ), "English", "Chinese")

        self.assertEqual(
            "https://gateway/v1/chat/completions?api-version=1",
            openai.chat_url,
        )
        self.assertEqual(
            "https://gateway/v1/messages?api-version=1",
            anthropic.messages_url,
        )

    def test_engines_reject_invalid_base_urls_early(self):
        for engine_cls in (OpenAIEngine, AnthropicEngine):
            with self.subTest(engine=engine_cls.__name__), self.assertRaisesRegex(
                ValueError, "base_url 无效"
            ):
                engine_cls(
                    EngineConfig(api_key="x", base_url="gateway/v1"),
                    "English", "Chinese",
                )

    def test_deepseek_has_its_own_defaults(self):
        engine_cls = get_engine("deepseek")
        engine = engine_cls(EngineConfig(api_key="x"), "English", "Chinese")

        self.assertIs(engine_cls, DeepSeekEngine)
        self.assertEqual("https://api.deepseek.com/v1/chat/completions", engine.chat_url)
        self.assertEqual("deepseek-chat", engine.model)

        without_v1 = engine_cls(
            EngineConfig(api_key="x", base_url="https://api.deepseek.com"),
            "English", "Chinese",
        )
        self.assertEqual("deepseek-chat", without_v1.model)

    def test_example_config_keys_are_consumed_not_sent_to_api(self):
        cfg = load_config(Path(__file__).parents[1] / "config.example.json")
        engine = cfg.engines["openai"]
        self.assertEqual("temperature", engine.sampling)
        self.assertIsNone(engine.prompt)
        self.assertNotIn("sampling", engine.extra)
        self.assertNotIn("prompt", engine.extra)

    def test_anthropic_extra_supports_max_tokens_and_rejects_reserved_fields(self):
        engine = AnthropicEngine(EngineConfig(
            api_key="x", extra={"max_tokens": 123, "metadata": {"user_id": "u"}},
        ), "English", "Chinese")
        body = engine._body("hi", "prompt", stream=False)
        self.assertEqual(123, body["max_tokens"])
        self.assertEqual({"user_id": "u"}, body["metadata"])

        bad = AnthropicEngine(EngineConfig(
            api_key="x", extra={"messages": []},
        ), "English", "Chinese")
        with self.assertRaises(ValueError):
            bad._body("hi", "prompt", stream=False)

        default_body = AnthropicEngine(
            EngineConfig(api_key="x"), "English", "Chinese"
        )._body("hi", "prompt", stream=False)
        self.assertEqual(64_000, default_body["max_tokens"])
        self.assertIn("temperature", default_body)
        self.assertNotIn("top_p", default_body)

        top_p_body = AnthropicEngine(
            EngineConfig(api_key="x", sampling="top_p"), "English", "Chinese"
        )._body("hi", "prompt", stream=False)
        self.assertNotIn("temperature", top_p_body)
        self.assertIn("top_p", top_p_body)

    def test_auto_source_language_uses_detected_language(self):
        engine = OpenAIEngine(EngineConfig(api_key="x"), "Auto", "Chinese")
        prompt = engine.build_prompt("Translate from <slang> to <tlang>")
        self.assertEqual("Translate from detected language to Chinese", prompt)

    def test_engines_reject_truncated_responses(self):
        openai = OpenAIEngine(EngineConfig(api_key="x"), "English", "Chinese")
        openai_response = mock.Mock(status_code=200)
        openai_response.json.return_value = {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "partial"},
            }]
        }
        openai_response.raise_for_status.return_value = None
        with mock.patch.object(openai, "_get_client") as get_client:
            get_client.return_value.post.return_value = openai_response
            with self.assertRaisesRegex(RuntimeError, "截断"):
                openai.translate("hi", "prompt")

        anthropic = AnthropicEngine(EngineConfig(api_key="x"), "English", "Chinese")
        anthropic_response = mock.Mock(status_code=200)
        anthropic_response.json.return_value = {
            "stop_reason": "max_tokens",
            "content": [{"type": "text", "text": "partial"}],
        }
        anthropic_response.raise_for_status.return_value = None
        with mock.patch.object(anthropic, "_get_client") as get_client:
            get_client.return_value.post.return_value = anthropic_response
            with self.assertRaisesRegex(RuntimeError, "截断"):
                anthropic.translate("hi", "prompt")

    def test_deepseek_retries_resource_shortage_instead_of_caching_partial(self):
        engine = DeepSeekEngine(EngineConfig(api_key="x"), "English", "Chinese")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "choices": [{
                "finish_reason": "insufficient_system_resource",
                "message": {"content": "partial"},
            }]
        }
        response.raise_for_status.return_value = None
        with mock.patch.object(engine, "_get_client") as get_client:
            get_client.return_value.post.return_value = response
            with self.assertRaisesRegex(RuntimeError, "资源暂时不足"):
                engine.translate("hi", "prompt")

    def test_openai_refusal_is_not_treated_as_empty_translation(self):
        engine = OpenAIEngine(EngineConfig(api_key="x"), "English", "Chinese")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": None, "refusal": "blocked"},
            }]
        }
        response.raise_for_status.return_value = None
        with mock.patch.object(engine, "_get_client") as get_client:
            get_client.return_value.post.return_value = response
            with self.assertRaisesRegex(RuntimeError, "内容过滤"):
                engine.translate("hi", "prompt")

    def test_sampling_parameters_can_be_omitted(self):
        config = EngineConfig(api_key="x", temperature=None, top_p=None)

        self.assertNotIn(
            "temperature", OpenAIEngine(config, "en", "zh")._body("x", "p", False))
        self.assertNotIn(
            "top_p", AnthropicEngine(config, "en", "zh")._body("x", "p", False))

    def test_streams_reject_malformed_or_incomplete_events(self):
        class StreamResponse:
            status_code = 200

            def __init__(self, lines):
                self.lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                pass

            def iter_lines(self):
                return iter(self.lines)

        openai = OpenAIEngine(EngineConfig(api_key="x", stream=True), "en", "zh")
        with mock.patch.object(openai, "_get_client") as get_client:
            get_client.return_value.stream.return_value = StreamResponse([
                'data: {"choices":[{"delta":{"content":"partial"}}]}',
            ])
            with self.assertRaisesRegex(RuntimeError, "未完整结束"):
                openai.translate("hi", "prompt")

        anthropic = AnthropicEngine(EngineConfig(api_key="x", stream=True), "en", "zh")
        with mock.patch.object(anthropic, "_get_client") as get_client:
            get_client.return_value.stream.return_value = StreamResponse([
                "data: not-json",
            ])
            with self.assertRaisesRegex(RuntimeError, "JSON 无效"):
                anthropic.translate("hi", "prompt")

        deepseek = DeepSeekEngine(
            EngineConfig(api_key="x", stream=True), "en", "zh")
        with mock.patch.object(deepseek, "_get_client") as get_client:
            get_client.return_value.stream.return_value = StreamResponse([
                'data: {"choices":[{"delta":{"content":"partial"},'
                '"finish_reason":"insufficient_system_resource"}]}',
            ])
            with self.assertRaisesRegex(RuntimeError, "资源暂时不足"):
                deepseek.translate("hi", "prompt")

        refused = OpenAIEngine(
            EngineConfig(api_key="x", stream=True), "en", "zh")
        with mock.patch.object(refused, "_get_client") as get_client:
            get_client.return_value.stream.return_value = StreamResponse([
                'data: {"choices":[{"delta":{"refusal":"blocked"},'
                '"finish_reason":"stop"}]}',
            ])
            with self.assertRaisesRegex(RuntimeError, "内容过滤"):
                refused.translate("hi", "prompt")

    def test_anthropic_concatenates_all_text_blocks(self):
        engine = AnthropicEngine(EngineConfig(api_key="x"), "English", "Chinese")
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ],
        }
        response.raise_for_status.return_value = None
        with mock.patch.object(engine, "_get_client") as get_client:
            get_client.return_value.post.return_value = response
            self.assertEqual("hello world", engine.translate("hi", "prompt"))

    def test_load_config_rejects_invalid_shapes_and_ranges(self):
        cases = [
            [],
            {"engines": []},
            {"engine": "google"},
            {"cache_enabled": "false"},
            {"engines": {"openai": {"request_timeout": 0}}},
            {"engines": {"openai": {"extra": []}}},
            {"engines": {"openai": {"retry_delay": float("nan")}}},
            {"engines": {"openai": {"top_p": 1.1}}},
            {"engines": {"openai": {"request_timeout": None}}},
            {"engines": {"openai": {"concurrency": 257}}},
            {"engines": {"openai": {"temprature": 0.2}}},
            {"engines": {"claude": {"temperature": 1.1}}},
        ]
        for i, value in enumerate(cases):
            path = self.tmp / f"bad-{i}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_config(path)

    def test_openai_reuses_httpx_client(self):
        cfg = EngineConfig(api_key="x", base_url="http://gateway/v1", stream=False)
        engine = OpenAIEngine(cfg, "English", "Chinese")
        c1 = engine._get_client()
        c2 = engine._get_client()
        self.assertIs(c1, c2)
        engine.close()
        self.assertIsNone(engine._client)
        c3 = engine._get_client()
        self.assertIsNot(c1, c3)
        engine.close()

    def test_anthropic_reuses_httpx_client(self):
        cfg = EngineConfig(api_key="x", stream=False)
        engine = AnthropicEngine(cfg, "English", "Chinese")
        c1 = engine._get_client()
        c2 = engine._get_client()
        self.assertIs(c1, c2)
        engine.close()
        self.assertIsNone(engine._client)


if __name__ == "__main__":
    unittest.main()
