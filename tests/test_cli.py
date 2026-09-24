"""End-to-end CLI tests with synthetic files, disposable HOME and no real HTTP."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import requests
from typer.testing import CliRunner

from genomic_annotator.annotator import API_FIELDS, API_URL, HTTP_TIMEOUT
from genomic_annotator.cli import app
from genomic_annotator.database import AnnotationCache, CacheError
from tests.test_annotator import hit, response
from tests.test_database import sample_annotation


class CLITests(unittest.TestCase):
    """Run real parse/cache/report commands while replacing only the HTTP edge."""

    def setUp(self) -> None:
        """Set up an isolated HOME, private cache path and synthetic input."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.home = Path(self.temp_dir.name)
        self.path = self.home / "SYNTHETIC_PERSON.tsv"
        self.path.write_text(
            "# Synthetic, not a personal genome\n"
            "rs101\t1\t123\tAG\n"
            "rs102\tX\t456\tA\n"
            "i900\tMT\t789\tD\n"
            "rs101\t1\t123\tAG\n",
            encoding="utf-8",
        )
        self.cache_path = self.home / ".genomic_annotator_cache.db"
        self.export_path = self.home / "synthetic.html"
        self.runner = CliRunner()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "COLUMNS": "180", "NO_COLOR": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        network_guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Real HTTP is forbidden in CLI tests"),
        )
        self.real_request_guard = network_guard.start()
        self.addCleanup(network_guard.stop)

    def test_help_for_app_and_commands(self) -> None:
        """Top-level and command help must work without reading files or networking."""
        for args in [["--help"], ["annotate", "--help"], ["clear-cache", "--help"]]:
            with self.subTest(args=args):
                result = self.runner.invoke(app, args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("Usage", result.output)
        result = self.runner.invoke(app, ["annotate", "--help"])
        self.assertIn("--offline", result.output)
        self.assertIn("--export", result.output)
        self.real_request_guard.assert_not_called()
        self.assertFalse(self.cache_path.exists())

    def test_offline_annotate_export_preserves_duplicate_rows(self) -> None:
        """The whole offline command works without even constructing an HTTP session."""
        with AnnotationCache(self.cache_path) as cache:
            cache.save_annotation("rs101", sample_annotation("SYNTHETIC_CACHED"))
        with patch("requests.Session", side_effect=AssertionError("Offline created HTTP session")):
            result = self.runner.invoke(app, ["annotate", str(self.path), "--offline", "--export", str(self.export_path)])
        self.assertEqual(result.exit_code, 0, result.output)
        for text in ["Offline mode", "no HTTP", "SYNTHETIC_CACHED", "offline_cache_miss", "unsupported_id", "4 called input row", "Research only", "not diagnostic"]:
            self.assertIn(text, result.output)
        self.assertIn("Warning:", result.output)
        self.assertNotIn("fully annotated", result.output)
        html = self.export_path.read_text(encoding="utf-8")
        self.assertEqual(html.count("<td>rs101</td>"), 2)
        self.assertIn("<td>AG</td>", html)
        self.assertIn("<td>123</td>", html)
        self.assertIn("Partial-data warnings", html)
        self.real_request_guard.assert_not_called()

    def test_online_annotate_uses_documented_safe_form_and_caches(self) -> None:
        """Run parse-to-report against a mocked transport with exact request assertions."""
        with patch("requests.Session.post", return_value=response([hit("rs102"), hit("rs101")])) as post:
            result = self.runner.invoke(app, ["annotate", str(self.path), "--export", str(self.export_path)])
        self.assertEqual(result.exit_code, 0, result.output)
        post.assert_called_once_with(
            API_URL,
            data={"q": "rs101,rs102", "scopes": "dbsnp.rsid", "fields": API_FIELDS},
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
            verify=True,
        )
        self.assertIn("normal connection metadata", result.output)
        self.assertIn("annotated=3", result.output)
        self.assertIn("unsupported_id=1", result.output)
        with AnnotationCache(self.cache_path) as cache:
            self.assertEqual(cache.get_annotation("rs101")["gene"], "SYNTHETIC_GENE")
        self.assertTrue(self.export_path.exists())
        self.real_request_guard.assert_not_called()

    def test_outage_retains_cached_rows_and_exports_partial_result_with_exit_three(self) -> None:
        """Partial output is usable but not reported as a successful full annotation."""
        with AnnotationCache(self.cache_path) as cache:
            cache.save_annotation("rs101", sample_annotation("SYNTHETIC_CACHED"))
        with patch("requests.Session.post", side_effect=requests.Timeout("synthetic offline")) as post:
            result = self.runner.invoke(app, ["annotate", str(self.path), "--export", str(self.export_path)])
        self.assertEqual(result.exit_code, 3, result.output)
        post.assert_called_once()
        self.assertIn("Partial result", result.output)
        self.assertIn("fetch_failed=1", result.output)
        self.assertIn("SYNTHETIC_CACHED", result.output)
        html = self.export_path.read_text(encoding="utf-8")
        self.assertIn("Fetch failed", html)
        self.assertIn("SYNTHETIC_CACHED", html)
        self.assertIn("timed out", html)

    def test_clear_cache_is_offline_and_preserves_other_artifacts(self) -> None:
        """Purging annotations changes neither the synthetic genome nor reports."""
        original = self.path.read_bytes()
        self.export_path.write_text("SYNTHETIC_REPORT", encoding="utf-8")
        with AnnotationCache(self.cache_path) as cache:
            cache.save_annotation("rs101", sample_annotation())
        result = self.runner.invoke(app, ["clear-cache"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Cleared 1 cached annotation", result.output)
        with AnnotationCache(self.cache_path) as cache:
            self.assertIsNone(cache.get_annotation("rs101"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.export_path.read_text(), "SYNTHETIC_REPORT")
        self.real_request_guard.assert_not_called()

    def test_missing_malformed_and_empty_input_errors(self) -> None:
        """Input errors are concise, typed, nonzero and never cause HTTP."""
        missing = self.home / "missing.tsv"
        result = self.runner.invoke(app, ["annotate", str(missing), "--offline"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Cannot open input", result.output)
        for content, expected in [
            ("# synthetic\n", "no called variants"),
            ("rs101\t1\tbad\tAG\n", "position"),
            ("rs101\t1\t1\tAG\textra\n", "columns"),
        ]:
            self.path.write_text(content, encoding="utf-8")
            result = self.runner.invoke(app, ["annotate", str(self.path)])
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertIn(expected, result.output)
            self.assertNotIn("Traceback", result.output)
        self.assertFalse(self.cache_path.exists())
        self.real_request_guard.assert_not_called()

    def test_invalid_export_paths_fail_before_network_or_cache_creation(self) -> None:
        """Input/cache/report collisions cannot overwrite or trigger online work."""
        original = self.path.read_bytes()
        for path in [
            self.path,
            self.cache_path,
            Path(str(self.cache_path) + "-journal"),
            self.home / "missing-parent" / "report.html",
        ]:
            with self.subTest(path=path):
                result = self.runner.invoke(app, ["annotate", str(self.path), "--export", str(path)])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Error:", result.output)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.cache_path.exists())
        self.real_request_guard.assert_not_called()

    def test_existing_export_is_not_replaced(self) -> None:
        """Existing user reports remain untouched without an overwrite option."""
        self.export_path.write_text("PRESERVE", encoding="utf-8")
        result = self.runner.invoke(app, ["annotate", str(self.path), "--offline", "--export", str(self.export_path)])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(self.export_path.read_text(), "PRESERVE")
        self.assertFalse(self.cache_path.exists())

    def test_export_permission_failure_is_nonzero(self) -> None:
        """Write errors are explicit, not followed by a success message."""
        with patch("genomic_annotator.reporter.os.open", side_effect=PermissionError("synthetic denied")):
            # Prepare the cache first so this test fails specifically on HTML creation.
            with patch("genomic_annotator.cli.AnnotationCache") as cache_class:
                cache_class.return_value.__enter__.return_value.get_annotation.return_value = None
                result = self.runner.invoke(app, ["annotate", str(self.path), "--offline", "--export", str(self.export_path)])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Cannot export HTML", result.output)
        self.assertNotIn("report written", result.output)
        self.assertFalse(self.export_path.exists())

    def test_cache_open_and_clear_errors_are_nonzero(self) -> None:
        """Storage errors are surfaced instead of disguised as empty caches."""
        self.cache_path.mkdir()
        for args in [["annotate", str(self.path), "--offline"], ["clear-cache"]]:
            with self.subTest(args=args):
                result = self.runner.invoke(app, args)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("cache", result.output)
                self.assertNotIn("Traceback", result.output)
        self.real_request_guard.assert_not_called()

    def test_cache_write_failure_is_nonzero(self) -> None:
        """A successful HTTP response cannot conceal a failed persistent cache write."""
        with patch("requests.Session.post", return_value=response([hit("rs101"), hit("rs102")])):
            with patch.object(AnnotationCache, "save_annotation", side_effect=CacheError("synthetic storage failure")):
                result = self.runner.invoke(app, ["annotate", str(self.path)])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("synthetic storage failure", result.output)

    def test_input_cannot_be_the_cache_or_a_hard_link_to_it(self) -> None:
        """Never read and modify the same user file as both genome and cache."""
        self.cache_path.write_bytes(self.path.read_bytes())
        original = self.cache_path.read_bytes()
        result = self.runner.invoke(app, ["annotate", str(self.cache_path), "--offline"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("must not be the annotation cache", result.output)
        linked_input = self.home / "linked-input.tsv"
        os.link(self.cache_path, linked_input)
        result = self.runner.invoke(app, ["annotate", str(linked_input), "--offline"])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(self.cache_path.read_bytes(), original)

    def test_usage_errors_and_missing_arguments(self) -> None:
        """Typer rejects missing paths and unknown options without side effects."""
        for args in [["annotate"], ["unknown-command"], ["annotate", str(self.path), "--not-an-option"]]:
            with self.subTest(args=args):
                result = self.runner.invoke(app, args)
                self.assertEqual(result.exit_code, 2)
        self.assertFalse(self.cache_path.exists())
        self.real_request_guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
