"""Reproducible CLI smoke example: synthetic calls, disposable HOME and mocked API."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from genomic_annotator.database import AnnotationCache


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "synthetic_23andme.tsv"
MOCKED_RUNNER = """
import json
import runpy
import sys
from unittest.mock import patch

import requests

from genomic_annotator.annotator import API_FIELDS, API_URL, HTTP_TIMEOUT

def synthetic_post(session, url, **kwargs):
    assert url == API_URL
    assert kwargs == {
        "data": {
            "q": "rs101,rs102,rs103,rs104",
            "scopes": "dbsnp.rsid",
            "fields": API_FIELDS,
        },
        "timeout": HTTP_TIMEOUT,
        "allow_redirects": False,
        "verify": True,
    }
    payload = [
        {"query": "rs104", "notfound": True},
        {
            "query": "rs102", "_id": "synthetic-x-hit",
            "clinvar": {
                "gene": {"symbol": "SYNTHETIC_EXAMPLE"},
                "rcv": {
                    "clinical_significance": "Likely pathogenic",
                    "conditions": {"name": "Invented fixture; not a medical finding"},
                },
            },
        },
        {"query": "rs103", "_id": "synthetic-mt-hit"},
        {
            "query": "rs101", "_id": "synthetic-autosomal-hit",
            "dbnsfp": {"genename": "SYNTHETIC_EXAMPLE"},
            "clinvar": {"clnsig": "Benign"},
        },
    ]
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload).encode("utf-8")
    response._content_consumed = True
    response.encoding = "utf-8"
    return response

sys.argv = ["main.py", *sys.argv[1:]]
with patch("requests.sessions.Session.request", side_effect=AssertionError("Real HTTP forbidden")):
    with patch("requests.Session.post", autospec=True, side_effect=synthetic_post) as post:
        try:
            runpy.run_path("main.py", run_name="__main__")
        except SystemExit as exc:
            assert exc.code in (None, 0), f"Mocked CLI failed with exit {exc.code}"
        post.assert_called_once()
"""


def _invoke(
    arguments: List[str], environment: Dict[str, str], mocked: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the actual main.py entry point in a subprocess; never use personal HOME."""
    command = (
        [sys.executable, "-c", MOCKED_RUNNER, *arguments]
        if mocked
        else [sys.executable, "main.py", *arguments]
    )
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )


def run_synthetic_workflow() -> List[str]:
    """Verify real-runner offline/mock/cache/export/clear flows, cleaning all artifacts."""
    results: List[str] = []
    with tempfile.TemporaryDirectory(prefix="genomic-annotator-synthetic-") as directory:
        home = Path(directory)
        environment = dict(os.environ, HOME=directory, NO_COLOR="1", COLUMNS="180")
        outputs = [
            ("empty-cache offline", False, True, "offline_cache_miss=5"),
            ("mocked online", True, False, "annotated=3"),
            ("populated-cache offline", False, True, "offline_cache_miss=1"),
        ]
        for index, (label, mocked, offline, expected) in enumerate(outputs):
            report = home / f"synthetic-{index}.html"
            arguments = ["annotate", str(FIXTURE), "--export", str(report)]
            if offline:
                arguments.append("--offline")
            completed = _invoke(arguments, environment, mocked=mocked)
            if completed.returncode != 0:
                raise AssertionError(f"{label} failed:\n{completed.stdout}\n{completed.stderr}")
            if expected not in completed.stdout:
                raise AssertionError(f"{label}: expected {expected} in:\n{completed.stdout}")
            html = report.read_text(encoding="utf-8")
            if html.count("<td>rs101</td>") != 2 or html.count("<tr data-status=") != 6:
                raise AssertionError(f"{label}: report did not retain every called row.")
            if "not diagnostic" not in html or "connect-src 'none'" not in html:
                raise AssertionError(f"{label}: report lacks medical/privacy protections.")
            results.append(f"PASS: {label}; 6 called rows retained; private HTML generated")
        with AnnotationCache(home / ".genomic_annotator_cache.db") as cache:
            if cache.get_annotation("rs101")["gene"] != "SYNTHETIC_EXAMPLE":
                raise AssertionError("The mocked online annotation did not persist.")
            if cache.get_annotation("rs104") is not None:
                raise AssertionError("A not-found result must not be cached.")
        cleared = _invoke(["clear-cache"], environment)
        if cleared.returncode != 0 or "Cleared 3 cached annotation" not in cleared.stdout:
            raise AssertionError(f"clear-cache failed:\n{cleared.stdout}\n{cleared.stderr}")
        with AnnotationCache(home / ".genomic_annotator_cache.db") as cache:
            if cache.get_annotation("rs101") is not None:
                raise AssertionError("clear-cache did not persist.")
        results.append("PASS: actual clear-cache command removed 3 public annotations")
    results.append("PASS: temporary HOME, cache and HTML artifacts removed; no real HTTP or personal data")
    return results


def main() -> None:
    """Print reproducible synthetic workflow outcomes for a developer."""
    print("\n".join(run_synthetic_workflow()))


if __name__ == "__main__":
    main()
