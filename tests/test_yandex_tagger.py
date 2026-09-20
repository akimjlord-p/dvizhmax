import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from integrations.yandex_tagger import (
    YandexTagger,
    YandexTaggerConfigError,
    YandexTaggerError,
    YandexTaggerSettings,
)


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.configure_kwargs = None
        self.messages = None
        self.timeout = None

    def configure(self, **kwargs):
        self.configure_kwargs = kwargs
        return self

    async def run(self, messages, *, timeout):
        self.messages = messages
        self.timeout = timeout
        return self.response


class FakeSDK:
    def __init__(self, response):
        self.model = FakeModel(response)
        self.requested_model_uri = None
        self.models = SimpleNamespace(completions=self.completions)

    def completions(self, model_uri):
        self.requested_model_uri = model_uri
        return self.model


class YandexTaggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tag_text_uses_sdk_and_structured_schema(self):
        response = SimpleNamespace(
            text='{"primary":["party","board_games"],"secondary":["interactive"]}',
            usage=SimpleNamespace(
                input_text_tokens=120,
                completion_tokens=12,
                total_tokens=132,
            ),
            model_version="test-version",
        )
        sdk = FakeSDK(response)
        settings = YandexTaggerSettings(
            api_key="test-key",
            folder_id="folder",
            model_uri="gpt://folder/yandexgpt-5-lite",
        )

        async with YandexTagger(settings, sdk=sdk) as tagger:
            result = await tagger.tag_text(
                title="Настольная вечеринка",
                description="Играем командами и знакомимся.",
                categories=("игры",),
                source_tags=("вечеринка",),
            )

        self.assertEqual(sdk.requested_model_uri, "gpt://folder/yandexgpt-5-lite")
        self.assertEqual(sdk.model.configure_kwargs["temperature"], 0)
        self.assertEqual(sdk.model.configure_kwargs["max_tokens"], 256)
        self.assertEqual(sdk.model.configure_kwargs["response_format"]["name"], "event_tags")
        self.assertEqual(sdk.model.messages[0]["role"], "system")
        self.assertIn("Настольная вечеринка", sdk.model.messages[1]["text"])
        self.assertEqual(result.primary, ("party", "board_games"))
        self.assertEqual(result.secondary, ("interactive",))
        self.assertEqual(result.usage.total_tokens, 132)
        self.assertEqual(result.model_version, "test-version")

    def test_from_env_requires_folder_or_model_uri(self):
        with patch("integrations.yandex_tagger.load_dotenv", return_value=None):
            with patch.dict(os.environ, {"YANDEX_API_KEY": "test-key"}, clear=True):
                with self.assertRaises(YandexTaggerConfigError):
                    YandexTaggerSettings.from_env()

    async def test_unknown_tag_is_rejected(self):
        response = SimpleNamespace(
            text='{"primary":["unknown"],"secondary":[]}',
            usage=None,
            model_version="test-version",
        )
        sdk = FakeSDK(response)
        settings = YandexTaggerSettings(
            api_key="test-key",
            folder_id="folder",
            model_uri="gpt://folder/yandexgpt-5-lite",
        )
        async with YandexTagger(settings, sdk=sdk) as tagger:
            with self.assertRaisesRegex(YandexTaggerError, "unknown primary tag"):
                await tagger.tag_text(title="Событие")


if __name__ == "__main__":
    unittest.main()
