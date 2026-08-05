import unittest

from guard import extract_usage


class ExtractUsageTest(unittest.TestCase):
    def test_chat_completions_usage(self):
        events = [{
            "usage": {
                "completion_tokens": 96,
                "completion_tokens_details": {"reasoning_tokens": 12},
            }
        }]
        self.assertEqual(extract_usage(events), (96, 12))

    def test_responses_usage(self):
        events = [{"response": {"usage": {"output_tokens": 64}}}]
        self.assertEqual(extract_usage(events), (64, 0))


if __name__ == "__main__":
    unittest.main()
