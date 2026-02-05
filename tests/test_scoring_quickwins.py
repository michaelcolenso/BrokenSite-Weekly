import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from src.scoring import evaluate_website, ScoringConfig


class _Elapsed:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def total_seconds(self) -> float:
        return self._seconds


class _Response:
    def __init__(
        self,
        *,
        status_code: int = 200,
        url: str = "https://example.com",
        text: str = "",
        headers: dict | None = None,
        history: list | None = None,
        elapsed_seconds: float = 0.1,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = headers or {}
        self.history = history or []
        self.elapsed = _Elapsed(elapsed_seconds)


BASE_HTML = f"""
<html>
  <head>
    <title>Example</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://cdn.example.com/bootstrap.css">
  </head>
  <body>
    <h1>Example</h1>
    <p>{"x" * 200}</p>
  </body>
</html>
"""


class ScoringQuickWinTests(unittest.TestCase):
    def test_scores_slow_response(self) -> None:
        config = ScoringConfig(
            slow_response_ms_threshold=200,
            weight_slow_response=20,
        )
        response = _Response(text=BASE_HTML, elapsed_seconds=0.5)
        result = evaluate_website("https://example.com", config=config, response=response)
        self.assertIn("slow_response_500ms", result.reasons)
        self.assertGreaterEqual(result.score, config.weight_slow_response)

    def test_scores_redirect_chain(self) -> None:
        config = ScoringConfig(
            redirect_chain_length_threshold=3,
            weight_redirect_chain=15,
        )
        response = _Response(text=BASE_HTML, history=[object(), object(), object()])
        result = evaluate_website("https://example.com", config=config, response=response)
        self.assertIn("redirect_chain_3", result.reasons)
        self.assertGreaterEqual(result.score, config.weight_redirect_chain)

    def test_scores_last_modified_age(self) -> None:
        config = ScoringConfig(
            last_modified_years_threshold=2,
            weight_last_modified_stale=20,
        )
        headers = {"Last-Modified": "Wed, 01 Jan 2020 00:00:00 GMT"}
        response = _Response(text=BASE_HTML, headers=headers)
        result = evaluate_website("https://example.com", config=config, response=response)
        self.assertTrue(any(reason.startswith("last_modified_") for reason in result.reasons))
        self.assertGreaterEqual(result.score, config.weight_last_modified_stale)

    def test_scores_missing_meta_description(self) -> None:
        config = ScoringConfig(weight_missing_meta_description=10)
        html = BASE_HTML.replace('<meta name="viewport"', '<meta name="viewport"')  # no description
        response = _Response(text=html)
        result = evaluate_website("https://example.com", config=config, response=response)
        self.assertIn("missing_meta_description", result.reasons)
        self.assertGreaterEqual(result.score, config.weight_missing_meta_description)

    def test_scores_missing_h1(self) -> None:
        config = ScoringConfig(weight_missing_h1=8)
        html = BASE_HTML.replace("<h1>Example</h1>", "<div>No H1</div>")
        response = _Response(text=html)
        result = evaluate_website("https://example.com", config=config, response=response)
        self.assertIn("missing_h1", result.reasons)
        self.assertGreaterEqual(result.score, config.weight_missing_h1)

    def test_scores_generic_title(self) -> None:
        config = ScoringConfig(weight_generic_title=10)
        html = BASE_HTML.replace("<title>Example</title>", "<title>Home</title>")
        response = _Response(text=html)
        result = evaluate_website("https://example.com", config=config, response=response)
        self.assertIn("generic_title", result.reasons)
        self.assertGreaterEqual(result.score, config.weight_generic_title)

    def test_scores_under_construction(self) -> None:
        config = ScoringConfig(weight_under_construction=70)
        html = BASE_HTML.replace("<h1>Example</h1>", "<h1>Under Construction</h1>")
        response = _Response(text=html)
        result = evaluate_website("https://example.com", config=config, response=response)
        self.assertIn("under_construction", result.reasons)
        self.assertGreaterEqual(result.score, config.weight_under_construction)


if __name__ == "__main__":
    unittest.main()
