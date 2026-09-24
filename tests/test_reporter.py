"""Tests for cautious classifications, literal text and private local HTML."""

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pandas as pd
from markupsafe import Markup
from rich.console import Console
from rich.table import Table
from rich.text import Text

from genomic_annotator.annotator import ANNOTATED_COLUMNS, STATUS_LABELS
from genomic_annotator.reporter import (
    CATEGORY_STYLES,
    MEDICAL_NOTE,
    PRIVACY_NOTE,
    SEARCH_SCRIPT,
    SENSITIVE_REPORT_NOTE,
    ReportError,
    ReportExportError,
    classify_significance,
    export_html_report,
    generate_terminal_table,
    validate_export_path,
)


def report_frame() -> pd.DataFrame:
    """Return synthetic local calls and invented public classification labels."""
    return pd.DataFrame(
        [
            {
                "rsid": "rs101",
                "chromosome": "1",
                "position": 123,
                "genotype": "AG",
                "gene": "SYNTHETIC_GENE",
                "clinical_significance": "Likely benign",
                "trait_summary": "Synthetic condition",
                "annotation_status": "annotated",
                "annotation_source": "cache",
                "annotation_message": "Synthetic fixture; not a diagnosis.",
            },
            {
                "rsid": "i102",
                "chromosome": "X",
                "position": 456,
                "genotype": "D",
                "gene": None,
                "clinical_significance": None,
                "trait_summary": None,
                "annotation_status": "unsupported_id",
                "annotation_source": "none",
                "annotation_message": "Unsupported synthetic identifier stayed local.",
            },
        ],
        columns=ANNOTATED_COLUMNS,
    )


class ReportHTMLParser(HTMLParser):
    """Collect HTML structure, text and script without executing any code."""

    def __init__(self) -> None:
        """Initialize collections and enter the HTMLParser lifecycle."""
        super().__init__(convert_charrefs=True)
        self.tags: List[Tuple[str, Dict[str, Optional[str]]]] = []
        self.text: List[str] = []
        self.scripts: List[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """Record elements and capture exactly the contents of real script tags."""
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        """Leave a script when the parser sees its actual closing tag."""
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        """Record literal text, separately collecting static script contents."""
        self.text.append(data)
        if self._in_script:
            self.scripts[-1] += data


class ReporterTests(unittest.TestCase):
    """Exercise offline reports without creating any real user artifacts."""

    def setUp(self) -> None:
        """Choose an unused temporary report path for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "synthetic-report.html"
        home = patch.dict(os.environ, {"HOME": self.temp_dir.name})
        home.start()
        self.addCleanup(home.stop)

    def test_cautious_classification_of_compound_labels(self) -> None:
        """Only unambiguous known labels get green, red or orange styling."""
        cases = {
            "Benign": "benign",
            "likely benign": "benign",
            "BENIGN / LIKELY BENIGN": "benign",
            "Benign; likely_benign": "benign",
            "Likely benign and benign": "benign",
            "Pathogenic": "pathogenic",
            "Likely pathogenic / Pathogenic": "pathogenic",
            "risk factor": "risk",
            "established risk allele": "risk",
            "likely_risk_allele": "risk",
            "Conflicting classifications of pathogenicity": "uncertain",
            "conflicting_interpretations_of_pathogenicity": "uncertain",
            "Pathogenic; Benign": "uncertain",
            "Likely benign; risk factor": "uncertain",
            "Pathogenic, uncertain significance": "uncertain",
            "not pathogenic": "uncertain",
            "no risk": "uncertain",
            "non-pathogenic": "uncertain",
            "VUS": "uncertain",
            "Benign; drug response": "uncertain",
            "Uncertain risk allele": "uncertain",
            "drug response": "other",
            "protective": "other",
            "pathogenicity": "other",
            "nonpathogenic": "other",
            "": "unavailable",
            None: "unavailable",
        }
        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(classify_significance(label), expected)
        self.assertEqual(classify_significance(pd.NA), "unavailable")
        self.assertEqual(CATEGORY_STYLES["benign"], "green")
        self.assertIn("red", CATEGORY_STYLES["pathogenic"])
        self.assertIn("orange", CATEGORY_STYLES["risk"])

    def test_terminal_table_preserves_local_fields_and_treats_markup_as_data(self) -> None:
        """Rich markup supplied in annotations remains literal displayed text."""
        frame = report_frame()
        frame.loc[0, "gene"] = "[bold red]LITERAL_SYNTHETIC[/bold red]"
        table = generate_terminal_table(frame)
        self.assertIsInstance(table, Table)
        self.assertEqual(table.row_count, len(frame))
        cell = table.columns[4]._cells[0]
        self.assertIsInstance(cell, Text)
        self.assertEqual(cell.plain, frame.loc[0, "gene"])
        significance = table.columns[5]._cells[0]
        self.assertEqual(significance.style, "green")
        output = io.StringIO()
        Console(file=output, width=260, color_system=None, markup=False).print(table)
        rendered = output.getvalue()
        for expected in ["rs101", "123", "456", "AG", "[bold red]", "not diagnostic", "allele-aware"]:
            self.assertIn(expected, rendered)

    def test_html_escaping_csp_and_no_external_resources(self) -> None:
        """Untrusted annotations cannot create tags, links or executable JS."""
        frame = report_frame()
        injection = '</script><script>alert("SYNTHETIC")</script><img src="https://invalid.test/x" onerror="alert(1)">&'
        frame.loc[0, "trait_summary"] = injection
        frame.loc[0, "gene"] = "[bold]LITERAL[/bold]"
        frame.attrs["annotation_warnings"] = [injection]
        frame["unused_personal_metadata"] = "DO_NOT_INCLUDE"
        export_html_report(frame, str(self.path))
        html = self.path.read_text(encoding="utf-8")
        parser = ReportHTMLParser()
        parser.feed(html)
        self.assertIn(injection, "".join(parser.text))
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn(injection, html)
        self.assertNotIn("DO_NOT_INCLUDE", html)
        self.assertEqual(parser.scripts, [SEARCH_SCRIPT])
        self.assertNotIn("SYNTHETIC", parser.scripts[0])
        self.assertFalse(any(tag in {"img", "link", "iframe", "object", "embed"} for tag, _ in parser.tags))
        self.assertFalse(any("src" in attrs or "href" in attrs or any(k.startswith("on") for k in attrs) for _, attrs in parser.tags))
        digest = base64.b64encode(hashlib.sha256(SEARCH_SCRIPT.encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", html)
        self.assertIn("connect-src 'none'", html)
        for text in [MEDICAL_NOTE, PRIVACY_NOTE, SENSITIVE_REPORT_NOTE, "rs101", "AG", "123", "Source: cache"]:
            self.assertIn(text, html)
        rows = [attrs for tag, attrs in parser.tags if tag == "tr" and "data-status" in attrs]
        self.assertEqual(len(rows), len(frame))
        self.assertEqual(rows[0], {"data-status": "annotated", "data-category": "benign"})

    def test_empty_findings_are_explicit(self) -> None:
        """Empty results still produce useful, nonmisleading offline reports."""
        frame = pd.DataFrame(columns=ANNOTATED_COLUMNS)
        table = generate_terminal_table(frame)
        self.assertEqual(table.row_count, 0)
        self.assertIn("No findings", table.caption.plain)
        export_html_report(frame, str(self.path))
        html = self.path.read_text(encoding="utf-8")
        self.assertIn("No findings to display.", html)
        self.assertIn("0 of 0 rows shown", html)

    def test_all_annotation_statuses_are_labeled(self) -> None:
        """Every status has a visible label and a filter option."""
        original = report_frame().iloc[[0]]
        frames = []
        for status in STATUS_LABELS:
            row = original.copy()
            row["annotation_status"] = status
            frames.append(row)
        frame = pd.concat(frames, ignore_index=True)
        export_html_report(frame, str(self.path))
        html = self.path.read_text(encoding="utf-8")
        for status, label in STATUS_LABELS.items():
            self.assertIn(f'data-status="{status}"', html)
            self.assertIn(f'<option value="{status}">{label}</option>', html)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_new_html_has_owner_only_permissions(self) -> None:
        """Sensitive HTML is never temporarily created world-readable."""
        export_html_report(report_frame(), str(self.path))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_existing_file_and_symlink_are_never_overwritten(self) -> None:
        """Protect raw input, earlier reports and symbolic-link targets."""
        self.path.write_text("PRESERVE_SYNTHETIC_CONTENT", encoding="utf-8")
        with self.assertRaisesRegex(ReportExportError, "already exists"):
            export_html_report(report_frame(), str(self.path))
        self.assertEqual(self.path.read_text(), "PRESERVE_SYNTHETIC_CONTENT")
        link = Path(self.temp_dir.name) / "link.html"
        link.symlink_to(self.path)
        with self.assertRaises(ReportExportError):
            export_html_report(report_frame(), str(link))
        dangling = Path(self.temp_dir.name) / "dangling.html"
        dangling.symlink_to(Path(self.temp_dir.name) / "missing.html")
        with self.assertRaises(ReportExportError):
            export_html_report(report_frame(), str(dangling))
        self.assertEqual(self.path.read_text(), "PRESERVE_SYNTHETIC_CONTENT")

    def test_protected_paths_including_uncreated_cache_sidecars(self) -> None:
        """Protect paths even before SQLite creates its cache or journal."""
        cache = Path(self.temp_dir.name) / ".genomic_annotator_cache.db"
        for suffix in ["", "-journal", "-wal", "-shm"]:
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ReportExportError, "cache"):
                validate_export_path(str(cache) + suffix)
        custom = Path(self.temp_dir.name) / "custom-cache.db"
        with self.assertRaises(ReportExportError):
            validate_export_path(str(custom), [custom])
        self.assertFalse(cache.exists())
        self.assertFalse(custom.exists())

    def test_invalid_paths_and_missing_parent_fail_clearly(self) -> None:
        """Failures create neither an empty report nor hidden directories."""
        for path in ["", "\x00", self.temp_dir.name, str(self.path / "missing.html")]:
            with self.subTest(path=path), self.assertRaises(ReportExportError):
                export_html_report(report_frame(), path)
        self.assertFalse(self.path.exists())

    def test_race_after_preflight_cannot_overwrite(self) -> None:
        """Exclusive file creation protects a target that appears after preflight."""
        self.path.write_text("PRESERVE", encoding="utf-8")
        with patch("genomic_annotator.reporter.validate_export_path", return_value=self.path):
            with self.assertRaises(ReportExportError):
                export_html_report(report_frame(), str(self.path))
        self.assertEqual(self.path.read_text(), "PRESERVE")

    def test_write_failure_removes_partial_report(self) -> None:
        """A failing stream leaves an explicit error and no partial success file."""
        with patch("jinja2.environment.TemplateStream.dump", side_effect=OSError("synthetic disk full")):
            with self.assertRaisesRegex(ReportExportError, "synthetic disk full"):
                export_html_report(report_frame(), str(self.path))
        self.assertFalse(self.path.exists())

    def test_warning_strings_are_data_even_with_a_markup_subtype(self) -> None:
        """Autoescape caller-provided warning strings as well as annotation cells."""
        frame = report_frame()
        frame.attrs["annotation_warnings"] = [Markup("<script>synthetic()</script>")]
        export_html_report(frame, str(self.path))
        parser = ReportHTMLParser()
        parser.feed(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parser.scripts, [SEARCH_SCRIPT])
        self.assertIn("<script>synthetic()</script>", "".join(parser.text))

    def test_malformed_warning_metadata_fails_before_file_creation(self) -> None:
        """Do not leave a partial report after invalid warning metadata."""
        frame = report_frame()
        frame.attrs["annotation_warnings"] = 123
        with self.assertRaisesRegex(ReportError, "annotation_warnings"):
            export_html_report(frame, str(self.path))
        self.assertFalse(self.path.exists())

    def test_unknown_status_or_missing_schema_is_rejected(self) -> None:
        """Do not render an invalid annotation dataframe as a successful report."""
        invalid = report_frame()
        invalid.loc[0, "annotation_status"] = '<script>not-a-status</script>'
        for frame in [None, pd.DataFrame(), invalid, pd.DataFrame(columns=["rsid", "rsid"])]:
            with self.subTest(frame=frame):
                with self.assertRaises(ReportError):
                    generate_terminal_table(frame)
                with self.assertRaises(ReportError):
                    export_html_report(frame, str(self.path))
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(shutil.which("node"), "Node is optional; needed to execute native filter tests")
    def test_native_search_and_intersecting_filters_execute_without_network(self) -> None:
        """Run the real exported script on a minimal DOM with network APIs forbidden."""
        export_html_report(report_frame(), str(self.path))
        parser = ReportHTMLParser()
        parser.feed(self.path.read_text(encoding="utf-8"))
        script = parser.scripts[0]
        harness = """
const assert = require("node:assert/strict");
const vm = require("node:vm");
const script = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
function run(rows) {
  const elements = {};
  for (const id of ["search", "status-filter", "category-filter", "visible-count", "no-matches"]) {
    elements[id] = {
      value: "", textContent: "", hidden: false, listeners: {},
      addEventListener(event, handler) { this.listeners[event] = handler; }
    };
  }
  const forbidden = () => { throw new Error("Network activity forbidden"); };
  vm.runInNewContext(script, {
    document: {
      getElementById(id) { return elements[id]; },
      querySelectorAll(selector) {
        assert.equal(selector, "#findings tbody tr[data-status]");
        return rows;
      }
    },
    fetch: forbidden, XMLHttpRequest: forbidden, WebSocket: forbidden,
    navigator: {sendBeacon: forbidden}
  });
  function filter(id, value, event) {
    elements[id].value = value;
    elements[id].listeners[event]();
  }
  return {elements, filter};
}
const rows = [
  {textContent: "rs1 SYNTHETIC_ALPHA AG", dataset: {status: "annotated", category: "benign"}},
  {textContent: "rs2 SYNTHETIC_BETA II", dataset: {status: "annotated", category: "pathogenic"}},
  {textContent: "rs3 No details", dataset: {status: "offline_cache_miss", category: "unavailable"}}
];
const {elements, filter} = run(rows);
assert.equal(elements["visible-count"].textContent, "3 of 3 rows shown");
filter("search", "  synthetic_ALPHA ", "input");
assert.deepEqual(rows.map(r => r.hidden), [false, true, true]);
assert.equal(elements["visible-count"].textContent, "1 of 3 rows shown");
filter("category-filter", "pathogenic", "change");
assert.deepEqual(rows.map(r => r.hidden), [true, true, true]);
assert.equal(elements["no-matches"].hidden, false);
filter("search", "", "input");
assert.deepEqual(rows.map(r => r.hidden), [true, false, true]);
filter("status-filter", "offline_cache_miss", "change");
assert.deepEqual(rows.map(r => r.hidden), [true, true, true]);
filter("category-filter", "", "change");
assert.deepEqual(rows.map(r => r.hidden), [true, true, false]);
filter("status-filter", "", "change");
assert.deepEqual(rows.map(r => r.hidden), [false, false, false]);
filter("search", "[.*]+", "input");
assert.equal(elements["visible-count"].textContent, "0 of 3 rows shown");
const empty = run([]);
assert.equal(empty.elements["visible-count"].textContent, "0 of 0 rows shown");
assert.equal(empty.elements["no-matches"].hidden, true);
console.log("Native search/filter checks passed");
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            input=json.dumps(script),
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Native search/filter checks passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
