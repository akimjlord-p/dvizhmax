import os
import unittest
from unittest.mock import patch

from workers.catalog_worker import CatalogWorkerSettings, _tag_definitions


class CatalogWorkerTests(unittest.TestCase):
    def test_default_schedule_runs_twice_a_day(self):
        with patch("workers.catalog_worker.load_dotenv", return_value=None):
            with patch.dict(os.environ, {}, clear=True):
                settings = CatalogWorkerSettings.from_env()
        self.assertEqual(settings.interval_seconds, 12 * 60 * 60)
        self.assertEqual(settings.cities[0].location, "msk")

    def test_cities_can_be_configured_as_a_json_list(self):
        with patch("workers.catalog_worker.load_dotenv", return_value=None):
            with patch.dict(
                os.environ,
                {
                    "KUDAGO_CITIES": (
                        '[{"location":"msk","name":"Москва","timezone":"Europe/Moscow"},'
                        '{"location":"spb","name":"Санкт-Петербург","timezone":"Europe/Moscow"}]'
                    )
                },
                clear=True,
            ):
                settings = CatalogWorkerSettings.from_env()
        self.assertEqual([city.location for city in settings.cities], ["msk", "spb"])

    def test_dictionary_uses_current_tags_only(self):
        definitions = {code: kind for code, _, kind in _tag_definitions()}
        self.assertEqual(definitions["comedy"], "secondary")
        for code in ("cultural", "evening", "night", "small_group", "large_group"):
            self.assertNotIn(code, definitions)


if __name__ == "__main__":
    unittest.main()
