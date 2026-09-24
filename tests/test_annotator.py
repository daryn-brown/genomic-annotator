"""Mock-only batching, response-shape, failure and request-privacy tests."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, patch

import pandas as pd
import requests

from genomic_annotator.annotator import (
    ANNOTATED_COLUMNS,
    API_FIELDS,
    API_URL,
    HTTP_TIMEOUT,
    AnnotationInputError,
    VariantAnnotator,
    _request_batch,
)
from genomic_annotator.database import AnnotationCache, CacheError
from genomic_annotator.parser import GENOME_COLUMNS
from tests.test_database import sample_annotation


def genome(rsids: List[Any]) -> pd.DataFrame:
    """Return invented calls with unique local coordinates for the given IDs."""
    return pd.DataFrame(
        [
            {
                "rsid": rsid,
                "chromosome": "X",
                "position": 987_654 + index,
                "genotype": "DI",
            }
            for index, rsid in enumerate(rsids)
        ],
        columns=GENOME_COLUMNS,
    )


def hit(rsid: str) -> Dict[str, Any]:
    """Return a synthetic service hit with no biological assertions."""
    return {
        "query": rsid,
        "_id": f"synthetic-{rsid}",
        "dbnsfp": {"genename": "SYNTHETIC_GENE"},
        "clinvar": {
            "rcv": {
                "clinical_significance": "Benign",
                "conditions": {"name": "Synthetic condition"},
            }
        },
    }


def response(payload: Any, status: int = 200) -> Mock:
    """Build a requests-response-shaped mock without any real HTTP."""
    result = Mock(spec=requests.Response)
    result.status_code = status
    result.json.return_value = payload
    return result


class AnnotatorTests(unittest.TestCase):
    """Exercise annotation using isolated SQLite and a forbidden real transport."""

    def setUp(self) -> None:
        """Create disposable data storage and intercept all possible real HTTP."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache = AnnotationCache(Path(self.temp_dir.name) / "annotations.db")
        self.addCleanup(self.cache.close)
        guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Real HTTP is forbidden in synthetic tests"),
        )
        guard.start()
        self.addCleanup(guard.stop)
        factory = patch("genomic_annotator.annotator.requests.Session", autospec=True)
        self.session_factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = self.session_factory.return_value.__enter__.return_value
        self.client.post.side_effect = self.reply_with_hits

    def reply_with_hits(self, url: str, **kwargs: Any) -> Mock:
        """Generate one mock hit for every RSID in the actual allowlisted form."""
        return response([hit(rsid) for rsid in kwargs["data"]["q"].split(",")])

    def test_zero_one_fifty_and_fifty_one_batches(self) -> None:
        """Respect exact 50-distinct-RSID boundaries, including no work at all."""
        for count, expected_sizes in [(0, []), (1, [1]), (50, [50]), (51, [50, 1])]:
            with self.subTest(count=count):
                self.cache.clear_cache()
                self.session_factory.reset_mock()
                original = genome([f"rs{index + 1}" for index in range(count)])
                result = VariantAnnotator(self.cache).annotate(original)
                sizes = [
                    len(call.kwargs["data"]["q"].split(","))
                    for call in self.client.post.call_args_list
                ]
                self.assertEqual(sizes, expected_sizes)
                self.assertEqual(result.columns.tolist(), ANNOTATED_COLUMNS)
                pd.testing.assert_frame_equal(result[GENOME_COLUMNS], original)
                self.assertEqual(result["annotation_status"].tolist(), ["annotated"] * count)
                if count == 0:
                    self.session_factory.assert_not_called()

    def test_query_allowlist_contains_no_personal_data(self) -> None:
        """Only RSIDs plus fixed controls reach a fixed HTTPS URL."""
        original = genome(["rs101", "i888", "rs102", "rs101"])
        original["file_path"] = "/private/SYNTHETIC_PERSON/raw-genome.txt"
        original["name"] = "SYNTHETIC_PERSON"
        original["metadata"] = [{"private": "DO_NOT_SEND"}] * len(original)
        original.attrs["timestamp"] = "PRIVATE_TIMESTAMP"
        result = VariantAnnotator(self.cache).annotate(original)
        self.client.post.assert_called_once_with(
            API_URL,
            data={"q": "rs101,rs102", "scopes": "dbsnp.rsid", "fields": API_FIELDS},
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
            verify=True,
        )
        serialized = repr(self.client.post.call_args)
        for private in ["DI", "987654", "i888", "SYNTHETIC_PERSON", "DO_NOT_SEND", "PRIVATE_TIMESTAMP"]:
            self.assertNotIn(private, serialized)
        self.assertFalse(self.client.trust_env)
        self.client.cookies.clear.assert_called_once()
        adapter = self.client.mount.call_args.args[1]
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertEqual(result["genotype"].tolist(), original["genotype"].tolist())
        cached = self.cache.get_annotation("rs101")
        self.assertNotIn("genotype", cached)
        self.assertNotIn("DO_NOT_SEND", json.dumps(cached))
        self.assertNotIn("SYNTHETIC_PERSON", json.dumps(cached))

    def test_deduplicated_cache_mixed_batches_preserve_duplicate_rows(self) -> None:
        """Cache lookup and HTTP deduplicate keys, never the input dataframe."""
        self.cache.save_annotation("rs1", sample_annotation("CACHED"))
        rsids = ["rs1"] + [f"rs{i}" for i in range(2, 53)] + ["rs2", "rs1", "i100"]
        original = genome(rsids)
        original.index = [7] * len(original)
        with patch.object(self.cache, "get_annotation", wraps=self.cache.get_annotation) as get:
            result = VariantAnnotator(self.cache).annotate(original)
        self.assertEqual(get.call_count, 52)
        self.assertEqual(self.client.post.call_count, 2)
        queries = [
            call.kwargs["data"]["q"].split(",") for call in self.client.post.call_args_list
        ]
        self.assertEqual([len(query) for query in queries], [50, 1])
        self.assertNotIn("rs1", queries[0])
        self.assertEqual(sum(query.count("rs2") for query in queries), 1)
        pd.testing.assert_frame_equal(result[GENOME_COLUMNS], original)
        self.assertEqual(result.iloc[0]["annotation_source"], "cache")
        self.assertEqual(result.iloc[-2]["gene"], "CACHED")
        self.assertEqual(result.iloc[-1]["annotation_status"], "unsupported_id")

    def test_all_cached_rows_require_no_session(self) -> None:
        """Even online mode never constructs a session when all RSIDs are cached."""
        self.cache.save_annotation("rs1", sample_annotation())
        result = VariantAnnotator(self.cache).annotate(genome(["rs1", "rs1"]))
        self.session_factory.assert_not_called()
        self.assertEqual(result["annotation_source"].tolist(), ["cache", "cache"])

    def test_offline_forbids_all_http_and_preserves_cached_rows(self) -> None:
        """Offline is a hard no-network switch, including cache misses."""
        self.session_factory.side_effect = AssertionError("Offline must not create a session")
        self.cache.save_annotation("rs2", sample_annotation("CACHED"))
        annotator = VariantAnnotator(self.cache, offline=True)
        original = genome(["rs1", "i100", "rs2", "rs1"])
        result = annotator.annotate(original)
        self.assertEqual(
            result["annotation_status"].tolist(),
            ["offline_cache_miss", "unsupported_id", "annotated", "offline_cache_miss"],
        )
        self.assertEqual(result.iloc[2]["gene"], "CACHED")
        self.assertEqual(result.attrs["annotation_warnings"], annotator.warnings)
        self.assertTrue(any("no HTTP" in warning for warning in annotator.warnings))
        pd.testing.assert_frame_equal(result[GENOME_COLUMNS], original)
        self.session_factory.assert_not_called()

    def test_unsupported_and_malicious_ids_stay_local(self) -> None:
        """Defend the network boundary even if a caller bypasses the strict parser."""
        values = ["i123", "rs1,rs2", "rs1 OR *", "https://other.example", "rs١", None, 101, ["rs1"]]
        result = VariantAnnotator(self.cache).annotate(genome(values))
        self.assertEqual(result["annotation_status"].tolist(), ["unsupported_id"] * len(values))
        self.session_factory.assert_not_called()

    def test_request_function_revalidates_its_only_input(self) -> None:
        """Even private helper misuse cannot send a malformed query."""
        for values in [[], ["i1"], ["rs1,rs2"], ["rs1", "rs1"], [f"rs{i}" for i in range(51)]]:
            with self.subTest(values=values), self.assertRaises(AnnotationInputError):
                _request_batch(self.client, values)
        self.client.post.assert_not_called()

    def test_minimal_official_sample_is_valid_not_fabricated(self) -> None:
        """A _id-only hit has no details; notfound is distinct and not cached."""
        fixture = Path(__file__).parent / "fixtures" / "myvariant_minimal.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        self.client.post.side_effect = None
        self.client.post.return_value = response(payload)
        result = VariantAnnotator(self.cache).annotate(genome(["rs2500", "rs58991260"]))
        self.assertEqual(result["annotation_status"].tolist(), ["not_found", "no_details"])
        self.assertTrue(result["clinical_significance"].isna().all())
        self.assertIsNone(self.cache.get_annotation("rs2500"))
        self.assertIsNotNone(self.cache.get_annotation("rs58991260"))
        cached = VariantAnnotator(self.cache, offline=True).annotate(genome(["rs58991260"]))
        self.assertEqual(cached.iloc[0]["annotation_status"], "no_details")
        self.assertEqual(cached.iloc[0]["annotation_source"], "cache")

    def test_multiple_hits_nested_dict_list_fallback_and_order(self) -> None:
        """Merge all allele hits deterministically while respecting field priority."""
        first = {
            "query": "rs1",
            "_id": "synthetic-one",
            "dbnsfp": [{"genename": ["GENE_B", ["GENE_A", "GENE_B"]]}],
            "clinvar": {
                "gene": [{"symbol": "SHOULD_NOT_REPLACE_DBNSFP"}],
                "clnsig": "SHOULD_NOT_REPLACE_RCV",
                "rcv": [
                    {
                        "clinical_significance": {"description": ["Likely benign"]},
                        "conditions": [{"name": "Trait B"}, {"name": ["Trait A", "Trait B"]}],
                    },
                    {"clinical_significance": "Benign", "conditions": {"name": None}},
                ],
            },
        }
        second = {
            "query": "rs1",
            "_id": "synthetic-two",
            "clinvar": [
                {"gene": {"symbol": ["GENE_C"]}, "clnsig": {"value": "Pathogenic"}},
                {"rcv": {"conditions": {"name": {"value": "Trait C"}}}},
            ],
        }
        self.client.post.side_effect = None
        self.client.post.return_value = response([hit("rs2"), second, first])
        result = VariantAnnotator(self.cache).annotate(genome(["rs1", "rs2", "rs1"]))
        self.assertEqual(result.iloc[0]["gene"], "GENE_A; GENE_B; GENE_C")
        self.assertEqual(result.iloc[0]["clinical_significance"], "Benign; Likely benign; Pathogenic")
        self.assertEqual(result.iloc[0]["trait_summary"], "Trait A; Trait B; Trait C")
        self.assertEqual(result.iloc[0]["gene"], result.iloc[2]["gene"])
        self.assertEqual(len(self.cache.get_annotation("rs1")["raw_json"]), 2)
        self.cache.clear_cache()
        self.client.post.return_value = response([first, hit("rs2"), second])
        reordered = VariantAnnotator(self.cache).annotate(genome(["rs1", "rs2", "rs1"]))
        pd.testing.assert_frame_equal(result, reordered)

    def test_absent_and_empty_annotation_fields_are_not_malformed(self) -> None:
        """Legitimate absent/null/empty fields represent unavailable details."""
        self.client.post.side_effect = None
        self.client.post.return_value = response(
            [
                {"query": "rs1", "_id": "synthetic", "dbnsfp": None, "clinvar": []},
                {"query": "rs2", "_id": "synthetic", "clinvar": {"rcv": [{}], "clnsig": []}},
                {"query": "rs3", "_id": "synthetic", "dbnsfp": {"genename": ""}},
            ]
        )
        result = VariantAnnotator(self.cache).annotate(genome(["rs1", "rs2", "rs3"]))
        self.assertEqual(result["annotation_status"].tolist(), ["no_details"] * 3)

    def test_malformed_fields_and_contradictions_are_not_cached(self) -> None:
        """Invalid values never become textified annotations or valid cache rows."""
        cases = [
            [{"query": "rs1"}],
            [{"query": "rs1", "clinvar": None}],
            [{"query": "rs1", "_id": 5}],
            [{"query": "rs1", "notfound": "true"}],
            [{"query": "rs1", "notfound": True, "_id": "synthetic"}],
            [{"query": "rs1", "notfound": True}, hit("rs1")],
            [{"query": "rs1", "error": "synthetic upstream error"}],
            [{**hit("rs1"), "dbnsfp": {"genename": 12}}],
            [{**hit("rs1"), "clinvar": "bad"}],
            [{**hit("rs1"), "clinvar": {"rcv": [{"clinical_significance": ["Benign", 7]}]}}],
            [{**hit("rs1"), "clinvar": {"clnsig": {"unexpected": "Pathogenic"}}}],
            [{**hit("rs1"), "clinvar": {"rcv": {"conditions": {"name": True}}}}],
            [{**hit("rs1"), "_score": float("nan")}],
            [hit("rs1"), {"query": "rs1", "_id": "synthetic", "dbnsfp": 10}],
        ]
        self.client.post.side_effect = None
        for payload in cases:
            with self.subTest(payload=payload):
                self.client.post.return_value = response(payload)
                annotator = VariantAnnotator(self.cache)
                result = annotator.annotate(genome(["rs1"]))
                self.assertEqual(result.iloc[0]["annotation_status"], "fetch_failed")
                self.assertIsNone(self.cache.get_annotation("rs1"))
                self.assertTrue(annotator.warnings)

    def test_unexpected_json_and_identity_errors_stop_further_batches(self) -> None:
        """Broken batch contracts must not cause thousands of doomed requests."""
        self.client.post.side_effect = None
        for payload in [None, {}, {"hits": [hit("rs1")]}, "bad", [], [None], [hit("rs999")], [{"_id": "no-query"}]]:
            with self.subTest(payload=payload):
                self.client.post.reset_mock()
                self.client.post.return_value = response(payload)
                result = VariantAnnotator(self.cache).annotate(genome([f"rs{i + 1}" for i in range(151)]))
                self.client.post.assert_called_once()
                self.assertEqual(result["annotation_status"].tolist(), ["fetch_failed"] * 151)
                self.assertIsNone(self.cache.get_annotation("rs1"))

    def test_partial_batch_retains_valid_rows_but_never_caches_malformed_rows(self) -> None:
        """A bad RSID response does not discard another query's usable hit."""
        self.client.post.side_effect = None
        self.client.post.return_value = response([hit("rs1"), {"query": "rs2", "clinvar": 10}])
        result = VariantAnnotator(self.cache).annotate(genome(["rs1", "rs2", "rs3"]))
        self.assertEqual(result["annotation_status"].tolist(), ["annotated", "fetch_failed", "fetch_failed"])
        self.assertIsNotNone(self.cache.get_annotation("rs1"))
        self.assertIsNone(self.cache.get_annotation("rs2"))
        self.assertIsNone(self.cache.get_annotation("rs3"))

    def test_timeouts_network_http_and_redirect_failures_keep_all_cache_hits(self) -> None:
        """Cache-first scanning plus a circuit breaker preserves data during outages."""
        self.cache.save_annotation("rs999", sample_annotation("CACHED_AFTER_MISSES"))
        failures = [
            requests.Timeout("synthetic timeout"),
            requests.ConnectionError("synthetic connection failure"),
            requests.HTTPError("synthetic HTTP failure"),
            response([], 302),
            response([], 307),
            response([], 429),
            response([], 503),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                self.client.post.reset_mock()
                if isinstance(failure, Exception):
                    self.client.post.side_effect = failure
                else:
                    self.client.post.side_effect = None
                    self.client.post.return_value = failure
                original = genome([f"rs{i + 1}" for i in range(151)] + ["rs999", "i100"])
                annotator = VariantAnnotator(self.cache)
                result = annotator.annotate(original)
                self.client.post.assert_called_once()
                self.assertFalse(self.client.post.call_args.kwargs["allow_redirects"])
                self.assertEqual(result.iloc[-2]["gene"], "CACHED_AFTER_MISSES")
                self.assertEqual(result["annotation_status"].tolist()[:151], ["fetch_failed"] * 151)
                self.assertEqual(len(result), len(original))
                self.assertTrue(any("stopped" in warning for warning in annotator.warnings))
                self.assertIsNone(self.cache.get_annotation("rs1"))

    def test_later_batch_outage_keeps_successes_and_skips_rest(self) -> None:
        """Do not lose the first batch or attempt another after the second fails."""
        first_response = response([hit(f"rs{i + 1}") for i in range(50)])
        self.client.post.side_effect = [first_response, requests.Timeout("synthetic timeout")]
        result = VariantAnnotator(self.cache).annotate(genome([f"rs{i + 1}" for i in range(151)]))
        self.assertEqual(self.client.post.call_count, 2)
        self.assertEqual(result["annotation_status"].tolist(), ["annotated"] * 50 + ["fetch_failed"] * 101)
        self.assertIsNotNone(self.cache.get_annotation("rs50"))
        self.assertIsNone(self.cache.get_annotation("rs51"))
        first_response.close.assert_called_once()

    def test_invalid_json_is_an_explicit_uncached_fetch_failure(self) -> None:
        """Malformed HTTP bodies fail clearly and close the response."""
        bad_response = response(None)
        bad_response.json.side_effect = ValueError("synthetic invalid JSON")
        self.client.post.side_effect = None
        self.client.post.return_value = bad_response
        result = VariantAnnotator(self.cache).annotate(genome(["rs1"]))
        self.assertEqual(result.iloc[0]["annotation_status"], "fetch_failed")
        self.assertIn("invalid JSON", result.iloc[0]["annotation_message"])
        self.assertIsNone(self.cache.get_annotation("rs1"))
        bad_response.close.assert_called_once()

    def test_not_found_is_not_negative_cached(self) -> None:
        """A future online run may discover annotations for previously missing IDs."""
        self.client.post.side_effect = None
        self.client.post.return_value = response([{"query": "rs1", "notfound": True}])
        first = VariantAnnotator(self.cache).annotate(genome(["rs1"]))
        self.assertEqual(first.iloc[0]["annotation_status"], "not_found")
        self.client.post.return_value = response([hit("rs1")])
        second = VariantAnnotator(self.cache).annotate(genome(["rs1"]))
        self.assertEqual(second.iloc[0]["annotation_status"], "annotated")
        self.assertEqual(self.client.post.call_count, 2)

    def test_cache_errors_are_not_hidden_as_network_misses(self) -> None:
        """Storage failures propagate instead of returning success-shaped results."""
        with patch.object(self.cache, "save_annotation", side_effect=CacheError("synthetic disk failure")):
            with self.assertRaisesRegex(CacheError, "synthetic disk"):
                VariantAnnotator(self.cache).annotate(genome(["rs1"]))
        self.cache.close()
        self.session_factory.reset_mock()
        with self.assertRaisesRegex(CacheError, "closed"):
            VariantAnnotator(self.cache).annotate(genome(["rs1"]))
        self.session_factory.assert_not_called()

    def test_invalid_dataframe_schema(self) -> None:
        """Reject missing or duplicate columns with a typed input error."""
        for value in [None, pd.DataFrame(), pd.DataFrame(columns=["rsid", "rsid"])]:
            with self.subTest(value=value), self.assertRaises(AnnotationInputError):
                VariantAnnotator(self.cache).annotate(value)

    def test_warning_state_resets_between_calls(self) -> None:
        """Warnings reflect only the current invocation."""
        annotator = VariantAnnotator(self.cache, offline=True)
        annotator.annotate(genome(["i100"]))
        self.assertTrue(annotator.warnings)
        annotator.annotate(genome([]))
        self.assertEqual(annotator.warnings, [])


if __name__ == "__main__":
    unittest.main()
