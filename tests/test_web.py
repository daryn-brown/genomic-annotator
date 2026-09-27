"""Local UI contracts with synthetic uploads, disposable caches and forbidden HTTP."""

import json
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from genomic_annotator.annotator import API_FIELDS, API_URL, HTTP_TIMEOUT, VariantAnnotator
from genomic_annotator.database import AnnotationCache
from genomic_annotator.web import MAX_UPLOAD_BYTES, create_app
from tests.test_annotator import hit, response
from tests.test_database import sample_annotation


SYNTHETIC_INPUT = (
    b"# Completely synthetic UI fixture\n"
    b"rs101\t1\t123\tAG\n"
    b"rs102\tX\t456\tA\n"
    b"i900\tMT\t789\tD\n"
    b"rs101\t1\t123\tAG\n"
    b"rs103\t2\t987\t--\n"
)


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.cache_path = self.directory / "annotations.db"
        self.app = create_app(self.cache_path)
        self.app.testing = True
        self.client = self.app.test_client()
        self.workspace = self.app.extensions["workspace"]
        self.headers = {"X-Workspace-Token": self.app.config["WORKSPACE_TOKEN"]}
        guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Real HTTP is forbidden in UI tests."),
        )
        self.network_guard = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.finish_worker)

    def finish_worker(self) -> None:
        worker = self.workspace.worker
        if worker is not None:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "A local UI worker did not finish.")

    def post(self, path: str, **kwargs):
        return self.client.post(path, headers=self.headers, **kwargs)

    def get(self, path: str, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def upload(self, data: bytes = SYNTHETIC_INPUT, name: str = "synthetic.tsv") -> dict:
        result = self.client.post(
            "/api/datasets", data=data,
            content_type="application/octet-stream",
            headers={**self.headers, "X-Genome-Name": name},
        )
        self.assertEqual(result.status_code, 202, result.json)
        self.finish_worker()
        return self.workspace.datasets[result.json["id"]].metadata()

    def rows(self, dataset: dict, query: str = "") -> dict:
        result = self.get(f"/api/datasets/{dataset['id']}/variants{query}")
        self.assertEqual(result.status_code, 200, result.json)
        json.dumps(result.json, allow_nan=False)
        return result.json

    def test_page_and_assets_are_bundled_private_and_do_not_open_a_cache(self) -> None:
        for path, content_type in [
            ("/", "text/html"),
            ("/static/workspace.css", "text/css"),
            ("/static/workspace.mjs", "javascript"),
            ("/static/model.mjs", "javascript"),
            ("/static/mark.svg", "image/svg+xml"),
        ]:
            with self.subTest(path=path):
                result = self.client.get(path)
                self.assertEqual(result.status_code, 200)
                self.assertIn(content_type, result.content_type)
                self.assertEqual(result.headers["Cache-Control"], "no-store")
                self.assertIn("connect-src 'self'", result.headers["Content-Security-Policy"])
                self.assertIn("frame-ancestors 'none'", result.headers["Content-Security-Policy"])
                self.assertEqual(result.headers["Referrer-Policy"], "no-referrer")
                result.close()
        html = self.client.get("/").text
        self.assertIn(self.app.config["WORKSPACE_TOKEN"], html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<style>", html)
        self.assertNotIn('src="http', html)
        self.assertNotIn('href="http', html)
        self.assertFalse(self.cache_path.exists())
        self.network_guard.assert_not_called()

    def test_all_api_reads_and_writes_require_the_process_token(self) -> None:
        self.assertEqual(self.client.get("/api/workspace").status_code, 403)
        self.assertEqual(self.client.post("/api/demo").status_code, 403)
        self.assertEqual(self.client.delete("/api/datasets/missing").status_code, 403)
        self.assertEqual(self.client.get("/api/workspace", headers={"X-Workspace-Token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.get("/api/workspace", headers={"X-Workspace-Token": "\u00e9"}).status_code, 403)
        self.assertEqual(self.get("/api/workspace").status_code, 200)

    def test_origin_fetch_site_and_host_guards(self) -> None:
        for headers in [
            {"Origin": "https://untrusted.example"},
            {"Origin": "http://localhost:9999"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ]:
            with self.subTest(headers=headers):
                result = self.client.get("/api/workspace", headers={**self.headers, **headers})
                self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "untrusted.example"}).status_code, 400)
        self.assertEqual(self.client.get("/api/workspace", headers={**self.headers, "Origin": "http://localhost"}).status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", self.get("/api/workspace").headers)

    def test_demo_is_deterministic_labeled_and_never_uses_cache_or_network(self) -> None:
        with patch("requests.Session", side_effect=AssertionError("Demo constructed an HTTP session")):
            first = self.post("/api/demo").json
            second = self.post("/api/demo").json
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["synthetic"])
        self.assertTrue(first["offline"])
        self.assertEqual(first["row_count"], 700)
        self.assertEqual(len(first["chromosomes"]), 25)
        self.assertEqual(sum(item["count"] for item in first["chromosomes"]), 700)
        self.assertTrue(all(sum(item["bins"]) == item["count"] for item in first["chromosomes"]))
        self.assertTrue(all(len(item["bins"]) == 48 for item in first["chromosomes"]))
        self.assertIn("fictional", first["warnings"][0])
        page = self.rows(first)
        self.assertEqual(len(page["rows"]), 30)
        self.assertIsNone(page["rows"][2]["clinical_significance"])
        self.assertEqual(page["rows"][2]["category"], "unavailable")
        self.assertFalse(self.cache_path.exists())
        result = self.post(f"/api/datasets/{first['id']}/annotate", json={"allow_online": True})
        self.assertEqual(result.status_code, 400)
        self.assertIn("fictional", result.json["error"])

    def test_offline_upload_uses_existing_pipeline_and_preserves_every_called_row(self) -> None:
        with AnnotationCache(self.cache_path) as cache:
            cache.save_annotation("rs101", sample_annotation("SYNTHETIC_CACHED"))
        with patch("requests.Session", side_effect=AssertionError("Offline constructed an HTTP session")):
            dataset = self.upload()
        self.assertEqual(dataset["phase"], "ready")
        self.assertTrue(dataset["offline"])
        self.assertEqual(dataset["row_count"], 4)
        self.assertEqual(dataset["annotated_count"], 2)
        self.assertEqual(dataset["statuses"], {"annotated": 2, "offline_cache_miss": 1, "unsupported_id": 1})
        page = self.rows(dataset)
        self.assertEqual([row["row_id"] for row in page["rows"]], [0, 1, 2, 3])
        self.assertEqual([row["rsid"] for row in page["rows"]], ["rs101", "rs102", "i900", "rs101"])
        self.assertEqual([row["genotype"] for row in page["rows"]], ["AG", "A", "D", "AG"])
        self.assertEqual([row["position"] for row in page["rows"]], [123, 456, 789, 123])
        self.assertEqual(page["rows"][0]["gene"], "SYNTHETIC_CACHED")
        self.assertNotIn("raw_json", page["rows"][0])
        self.assertFalse(any(path.suffix in {".txt", ".tsv"} for path in self.directory.iterdir()))
        self.network_guard.assert_not_called()

    def test_case_insensitive_literal_search_filters_and_paging_intersect(self) -> None:
        with AnnotationCache(self.cache_path) as cache:
            cache.save_annotation("rs101", sample_annotation("SYNTHETIC_CACHED"))
        dataset = self.upload()
        result = self.rows(dataset, "?chromosome=1&status=annotated&category=benign&query=synthetic")
        self.assertEqual(result["total"], 2)
        self.assertEqual([row["row_id"] for row in result["rows"]], [0, 3])
        second = self.rows(dataset, "?offset=3&limit=1")
        self.assertEqual(second["rows"][0]["row_id"], 3)
        self.assertEqual(second["total"], 4)
        self.assertEqual(self.rows(dataset, "?query=RS102")["rows"][0]["rsid"], "rs102")
        self.assertEqual(self.rows(dataset, "?query=.*")["total"], 0)
        self.assertEqual(self.rows(dataset, "?chromosome=X&status=annotated")["total"], 0)
        self.assertEqual(self.rows(dataset, "?query=nan")["total"], 0)
        self.assertEqual(self.rows(dataset, "?offset=100")["rows"], [])

    def test_invalid_filters_and_page_sizes_are_explicit_errors(self) -> None:
        dataset = self.upload()
        for query in [
            "limit=0", "limit=101", "offset=-1", "offset=1.2",
            "offset=1000001", "limit=true", "chromosome=23",
            "status=benign", "category=annotated", "query=" + "x" * 201,
        ]:
            with self.subTest(query=query):
                result = self.get(f"/api/datasets/{dataset['id']}/variants?{query}")
                self.assertEqual(result.status_code, 400)
                self.assertIn("error", result.json)

    def test_online_lookup_requires_consent_uses_exact_safe_form_and_replaces_snapshot(self) -> None:
        dataset = self.upload()
        path = f"/api/datasets/{dataset['id']}/annotate"
        for body in [
            {}, [], {"allow_online": False}, {"allow_online": "true"},
            {"allow_online": 1}, {"allow_online": True, "extra": "no"},
        ]:
            self.assertEqual(self.post(path, json=body).status_code, 400)
        self.network_guard.assert_not_called()
        with patch("requests.Session.post", return_value=response([hit("rs101"), hit("rs102")])) as post:
            self.assertEqual(self.post(path, json={"allow_online": True}).status_code, 202)
            self.finish_worker()
        post.assert_called_once_with(
            API_URL, data={"q": "rs101,rs102", "scopes": "dbsnp.rsid", "fields": API_FIELDS},
            timeout=HTTP_TIMEOUT, allow_redirects=False, verify=True,
        )
        metadata = self.workspace.get(dataset["id"]).metadata()
        self.assertFalse(metadata["offline"])
        self.assertEqual(metadata["revision"], 2)
        self.assertEqual(metadata["annotated_count"], 3)
        self.assertEqual(len(self.rows(dataset)["rows"]), 4)
        with AnnotationCache(self.cache_path) as cache:
            self.assertEqual(cache.get_annotation("rs101")["gene"], "SYNTHETIC_GENE")

    def test_outage_publishes_partial_rows_and_warnings_not_successful_annotations(self) -> None:
        dataset = self.upload()
        with patch("requests.Session.post", side_effect=requests.Timeout("synthetic")):
            self.post(f"/api/datasets/{dataset['id']}/annotate", json={"allow_online": True})
            self.finish_worker()
        metadata = self.workspace.get(dataset["id"]).metadata()
        self.assertEqual(metadata["statuses"]["fetch_failed"], 3)
        self.assertTrue(any("timed out" in warning for warning in metadata["warnings"]))
        self.assertEqual(metadata["annotated_count"], 0)
        self.assertEqual(self.rows(dataset)["total"], 4)

    def test_invalid_input_never_opens_cache_and_has_line_numbered_error(self) -> None:
        for payload, message in [
            (b"rs101\t1\t1\tAA\nrs102\t1\tbad\tCC\n", "Line 2"),
            (b"rs101\t1\t1\tAA\n\xff", "UTF-8"),
            (b"# no calls\n", "no called variants"),
        ]:
            with self.subTest(payload=payload):
                dataset = self.upload(payload)
                self.assertEqual(dataset["phase"], "error")
                self.assertIn(message, dataset["error"])
                self.assertNotIn("row_count", dataset)
                self.assertFalse(self.cache_path.exists())
                self.client.delete(f"/api/datasets/{dataset['id']}", headers=self.headers)

    def test_empty_oversized_and_nonraw_uploads_are_rejected(self) -> None:
        self.assertEqual(self.post("/api/datasets", data=b"", content_type="text/plain").status_code, 400)
        self.assertEqual(self.post("/api/datasets", data=SYNTHETIC_INPUT, content_type="multipart/form-data").status_code, 415)
        with patch.dict(self.app.config, {"MAX_CONTENT_LENGTH": 4}):
            result = self.post("/api/datasets", data=SYNTHETIC_INPUT, content_type="text/plain")
            self.assertEqual(result.status_code, 413)
            self.assertIn("32 MiB", result.json["error"])
        with patch("genomic_annotator.web.MAX_ROWS", 2):
            dataset = self.upload()
            self.assertEqual(dataset["phase"], "error")
            self.assertIn("2 called rows", dataset["error"])
        self.assertFalse(self.cache_path.exists())

    def test_cache_failure_on_import_is_visible(self) -> None:
        self.cache_path.mkdir()
        dataset = self.upload()
        self.assertEqual(dataset["phase"], "error")
        self.assertIn("cache", dataset["error"])
        self.assertNotIn("row_count", dataset)

    def test_failed_online_storage_keeps_the_previous_browsable_snapshot(self) -> None:
        dataset = self.upload()
        previous = self.rows(dataset)
        self.cache_path.unlink()
        self.cache_path.mkdir()
        result = self.post(f"/api/datasets/{dataset['id']}/annotate", json={"allow_online": True})
        self.assertEqual(result.status_code, 202)
        self.finish_worker()
        metadata = self.workspace.get(dataset["id"]).metadata()
        self.assertEqual(metadata["phase"], "error")
        self.assertIn("cache", metadata["error"])
        self.assertEqual(metadata["revision"], 1)
        self.assertEqual(self.rows(dataset), previous)
        self.network_guard.assert_not_called()

    def test_exact_32_mib_boundary_is_accepted_and_one_extra_byte_is_rejected(self) -> None:
        payload = b"#" + b" " * (MAX_UPLOAD_BYTES - len(SYNTHETIC_INPUT) - 2) + b"\n" + SYNTHETIC_INPUT
        self.assertEqual(len(payload), 32 * 1024 * 1024)
        dataset = self.upload(payload)
        self.assertEqual(dataset["phase"], "ready")
        self.assertEqual(dataset["row_count"], 4)
        result = self.post("/api/datasets", data=payload + b"\n", content_type="text/plain")
        self.assertEqual(result.status_code, 413)
        self.assertEqual(len(self.workspace.datasets), 1)

    def test_import_jobs_are_bounded_and_a_running_file_cannot_be_closed(self) -> None:
        entered, release = threading.Event(), threading.Event()
        annotate = VariantAnnotator.annotate

        def blocked(annotator, frame):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release the worker.")
            return annotate(annotator, frame)

        with patch.object(VariantAnnotator, "annotate", blocked):
            result = self.post("/api/datasets", data=SYNTHETIC_INPUT, content_type="text/plain")
            self.assertEqual(result.status_code, 202)
            try:
                self.assertTrue(entered.wait(5))
                another = self.post("/api/datasets", data=SYNTHETIC_INPUT, content_type="text/plain")
                self.assertEqual(another.status_code, 409)
                self.assertEqual(len(self.workspace.datasets), 1)
                closed = self.client.delete(f"/api/datasets/{result.json['id']}", headers=self.headers)
                self.assertEqual(closed.status_code, 409)
                self.assertTrue(self.get("/api/workspace").json["busy"])
            finally:
                release.set()
                self.finish_worker()
        self.assertFalse(self.get("/api/workspace").json["busy"])

    def test_three_file_limit_and_close_remove_data_from_all_endpoints(self) -> None:
        datasets = [self.upload(name=f"synthetic-{index}.tsv") for index in range(3)]
        self.assertEqual(self.post("/api/datasets", data=SYNTHETIC_INPUT, content_type="text/plain").status_code, 409)
        self.assertEqual(self.post("/api/demo").status_code, 409)
        first = datasets[0]
        self.assertEqual(self.client.delete(f"/api/datasets/{first['id']}", headers=self.headers).status_code, 200)
        self.assertEqual(self.get(f"/api/datasets/{first['id']}/variants").status_code, 404)
        self.assertEqual(self.post(f"/api/datasets/{first['id']}/export", json={"path": str(self.directory / "unused.html")}).status_code, 404)
        self.assertEqual(len(self.get("/api/workspace").json["datasets"]), 2)
        self.assertEqual(self.post("/api/demo").status_code, 200)

    def test_private_export_includes_duplicates_and_refuses_existing_or_cache_paths(self) -> None:
        dataset = self.upload()
        path = self.directory / "synthetic-report.html"
        result = self.post(f"/api/datasets/{dataset['id']}/export", json={"path": str(path)})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["row_count"], 4)
        html = path.read_text()
        self.assertEqual(html.count("<td>rs101</td>"), 2)
        self.assertIn("not diagnostic", html)
        self.assertIn("connect-src 'none'", html)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for protected in [path, self.cache_path, Path(str(self.cache_path) + "-wal"), self.directory / "missing" / "report.html"]:
            with self.subTest(protected=protected):
                denied = self.post(f"/api/datasets/{dataset['id']}/export", json={"path": str(protected)})
                self.assertEqual(denied.status_code, 400)
                self.assertIn("error", denied.json)
        self.assertEqual(path.read_text(), html)

    def test_annotation_text_remains_literal_in_json_and_escaped_in_reports(self) -> None:
        injection = '<img src="https://untrusted.example" onerror="alert(1)">'
        with AnnotationCache(self.cache_path) as cache:
            cache.save_annotation("rs101", sample_annotation(injection))
        dataset = self.upload(name="%3Cscript%3ESYNTHETIC%3C%2Fscript%3E.tsv")
        self.assertEqual(dataset["name"], "script>.tsv")
        self.assertEqual(self.rows(dataset)["rows"][0]["gene"], injection)
        path = self.directory / "literal.html"
        self.assertEqual(self.post(f"/api/datasets/{dataset['id']}/export", json={"path": str(path)}).status_code, 200)
        self.assertNotIn(injection, path.read_text())
        self.assertIn("&lt;img", path.read_text())

    @unittest.skipUnless(shutil.which("node"), "Node is optional; needed for native UI model tests")
    def test_native_ui_geometry_and_display_contracts(self) -> None:
        result = subprocess.run(
            ["node", "--test", "tests/ui_model.test.mjs"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
