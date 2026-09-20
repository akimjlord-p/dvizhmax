import unittest
from uuid import uuid4

from bot.onboarding import _callback_parts, categories_keyboard, interests_keyboard


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


if __name__ == "__main__":
    unittest.main()
