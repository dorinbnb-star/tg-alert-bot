from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler import dispatch  # noqa: E402

TOKEN = "ghs_EPHEMERAL_TEST_TOKEN"
INPUTS = {"wait_env": "scan-wait-10m", "not_before": "2026-09-28T10:10:00Z", "ticks": ""}


class FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class Opener:
    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.requests: list = []

    def __call__(self, request, timeout: int):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("redacted", code, "error", {}, None)


class DispatchTests(unittest.TestCase):
    def call(self, outcomes: list) -> tuple[int, Opener, list[float]]:
        opener, slept = Opener(outcomes), []
        attempts = dispatch.dispatch(
            "owner/repo", "ai-crypto-trader.yml", "main", INPUTS, TOKEN,
            opener=opener, sleeper=slept.append, log=lambda _: None,
        )
        return attempts, opener, slept

    def test_first_attempt_success_sends_expected_request(self) -> None:
        attempts, opener, slept = self.call([204])
        self.assertEqual(1, attempts)
        self.assertEqual([], slept)
        request = opener.requests[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual("https://api.github.com/repos/owner/repo/actions/workflows/ai-crypto-trader.yml/dispatches", request.full_url)
        self.assertEqual({"ref": "main", "inputs": INPUTS}, json.loads(request.data))
        self.assertEqual(f"Bearer {TOKEN}", request.get_header("Authorization"))

    def test_transient_errors_are_retried_with_bounded_backoff(self) -> None:
        attempts, opener, slept = self.call([http_error(503), urllib.error.URLError("reset"), 204])
        self.assertEqual(3, attempts)
        self.assertEqual([5, 15], slept)

    def test_rate_limit_is_retried(self) -> None:
        attempts, _, slept = self.call([http_error(429), 204])
        self.assertEqual(2, attempts)
        self.assertEqual([5], slept)

    def test_permanent_error_fails_immediately_without_retry(self) -> None:
        with self.assertRaises(dispatch.DispatchError) as caught:
            self.call([http_error(422)])
        self.assertIn("HTTP 422", str(caught.exception))

    def test_retries_are_exhausted_and_error_never_contains_token(self) -> None:
        opener, slept = Opener([http_error(502)] * 4), []
        with self.assertRaises(dispatch.DispatchError) as caught:
            dispatch.dispatch(
                "owner/repo", "ai-crypto-trader.yml", "main", INPUTS, TOKEN,
                opener=opener, sleeper=slept.append, log=lambda _: None,
            )
        self.assertEqual(4, len(opener.requests))
        self.assertEqual([5, 15, 30], slept)
        self.assertIn("HTTP 502", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_total_retry_pause_is_short(self) -> None:
        self.assertLessEqual(sum(dispatch.RETRY_DELAYS), 60)


if __name__ == "__main__":
    unittest.main()
