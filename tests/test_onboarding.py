import unittest
from uuid import uuid4

from bot.onboarding import _callback_parts, _profile_photo_attachment, categories_keyboard, interests_keyboard, mvp_cities


class OnboardingKeyboardTests(unittest.TestCase):
    def test_callback_payload_is_scoped_to_onboarding(self):
        self.assertEqual(
            _callback_parts("onboarding:gender:female"),
            ("onboarding", "gender", "female"),
        )
        self.assertIsNone(_callback_parts("other:gender:female"))

    def test_interest_keyboard_marks_selected_tag(self):
        selected_id = uuid4()

        class Tag:
            id = selected_id
            code = "concert"
            name = "Concert"

        attachment = interests_keyboard([Tag()], {selected_id}, "music_stage")[0]
        button = attachment.payload.buttons[0][0]
        self.assertEqual(button.text, "✓ Concert")
        self.assertEqual(button.payload, f"onboarding:interest:{selected_id}")

    def test_categories_show_selected_count(self):
        tag_id = uuid4()

        class Tag:
            id = tag_id
            code = "concert"
            name = "Concert"

        attachment = categories_keyboard([Tag()], {tag_id})[0]
        self.assertTrue(attachment.payload.buttons[0][0].text.endswith(" · 1"))

    def test_interest_group_shows_continue_after_three_choices(self):
        tags = []
        selected = set()
        for _ in range(3):
            tag_id = uuid4()
            selected.add(tag_id)
            tags.append(type("Tag", (), {"id": tag_id, "code": "concert", "name": "Concert"})())
        attachment = interests_keyboard(tags, selected, "music_stage")[0]
        self.assertIn("onboarding:interest:finish", [button.payload for row in attachment.payload.buttons for button in row])

    def test_mvp_cities_only_returns_moscow(self):
        moscow = type("City", (), {"name": "Москва"})()
        other = type("City", (), {"name": "Санкт-Петербург"})()
        self.assertEqual(mvp_cities([other, moscow]), [moscow])

    def test_profile_photo_attachment_uses_the_saved_url(self):
        attachment = _profile_photo_attachment(
            None,
            {"photo_id": 1, "token": "photo-token", "url": "https://example.test/profile.jpg"},
        )
        self.assertEqual(attachment.payload.url, "https://example.test/profile.jpg")


if __name__ == "__main__":
    unittest.main()
