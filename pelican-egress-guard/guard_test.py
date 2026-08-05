import unittest

from guard import extract_usage
from pelican_pool_guard import accounts


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


class AccountSelectionTest(unittest.TestCase):
    class Client:
        def __init__(self, items):
            self.items = items

        def admin_request(self, method, path):
            return {"items": self.items}

    def test_falls_back_when_all_active_accounts_are_waiting_reset(self):
        waiting = {
            "id": 7,
            "enabled": True,
            "authStatus": "active",
            "failureCount": 0,
            "quota": {"remaining": 0, "status": "waitingReset"},
        }
        self.assertEqual(accounts(self.Client([waiting])), [waiting])

    def test_prefers_strictly_healthy_accounts(self):
        waiting = {"id": 7, "enabled": True, "authStatus": "active", "quota": {"remaining": 0}}
        healthy = {"id": 8, "enabled": True, "authStatus": "active", "quota": {"remaining": 1}}
        self.assertEqual(accounts(self.Client([waiting, healthy])), [healthy])


if __name__ == "__main__":
    unittest.main()
