import unittest
from unittest import mock

from guard import extract_usage, http_sse
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

    def test_http_sse_reads_chat_events_by_line(self):
        class Response:
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.lines = iter([
                    b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',
                    b'data: {"usage":{"completion_tokens":42}}\n',
                    b'data: [DONE]\n',
                ])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def readline(self, _limit):
                return next(self.lines, b"")

            def read(self, _limit):
                raise AssertionError("streaming responses must not use buffered read")

        with mock.patch("guard.urllib.request.urlopen", return_value=Response()):
            result = http_sse("GET", "/probe", timeout=10)
        self.assertEqual(result["text_parts"], ["hello"])
        self.assertIsNotNone(result["first_token_at"])
        self.assertEqual(extract_usage(result["events"]), (42, 0))


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
