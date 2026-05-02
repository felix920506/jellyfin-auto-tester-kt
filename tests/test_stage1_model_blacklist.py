import unittest

from stage1_model_blacklist import is_stage1_model_blacklisted


class Stage1ModelBlacklistTests(unittest.TestCase):
    def test_blocks_gemini_31_family_models(self):
        self.assertTrue(is_stage1_model_blacklisted("openrouter/gemini-3.1-pro"))
        self.assertTrue(is_stage1_model_blacklisted("gemini 3.1 flash lite"))
        self.assertTrue(is_stage1_model_blacklisted("google/gemini-3.1-pro-preview"))

    def test_allows_other_gemini_models(self):
        self.assertFalse(is_stage1_model_blacklisted("openrouter/gemini-2.5-pro"))
        self.assertFalse(is_stage1_model_blacklisted("gemini-3.0-flash"))
        self.assertFalse(is_stage1_model_blacklisted("openrouter/gemini-3.1-flash"))


if __name__ == "__main__":
    unittest.main()
