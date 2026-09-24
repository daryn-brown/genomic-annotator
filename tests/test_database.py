"""Tests of private, annotation-only SQLite storage in temporary directories."""

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from genomic_annotator.database import (
    CACHE_COLUMNS,
    AnnotationCache,
    CacheError,
    CacheValidationError,
    default_cache_path,
)


def sample_annotation(gene: str = "SYNTHETIC_GENE") -> Dict[str, Any]:
    """Return invented, nonmedical public-annotation-shaped data."""
    return {
        "gene": gene,
        "clinical_significance": "Benign",
        "trait_summary": "Synthetic condition <not a clinical assertion>",
        "raw_json": [
            {
                "query": "rs101",
                "_id": "synthetic-variant",
                "dbnsfp": {"genename": gene},
                "clinvar": {"rcv": [{"clinical_significance": "Benign"}]},
            }
        ],
    }


class CacheTests(unittest.TestCase):
    """Verify lifecycle, round trips, validation, transactions and permissions."""

    def setUp(self) -> None:
        """Open an isolated cache and register cleanup before each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "annotations.db"
        self.cache = AnnotationCache(self.path)
        self.addCleanup(self.cache.close)

    def test_roundtrip_raw_json_and_utc_timestamp(self) -> None:
        """Persist fields exactly, including decoded structured raw JSON."""
        data = sample_annotation()
        self.assertIsNone(self.cache.get_annotation("rs101"))
        self.assertIsNone(self.cache.save_annotation("rs101", data))
        result = self.cache.get_annotation("rs101")
        self.assertIsNotNone(result)
        self.assertEqual(set(result), set(CACHE_COLUMNS))
        for key, value in data.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["rsid"], "rs101")
        self.assertEqual(datetime.fromisoformat(result["last_updated"]).utcoffset(), timedelta(0))
        with closing(sqlite3.connect(self.path)) as connection:
            raw = connection.execute("SELECT raw_json FROM rsid_annotations").fetchone()[0]
        self.assertEqual(json.loads(raw), data["raw_json"])

    def test_upsert_reopen_and_clear(self) -> None:
        """A second save replaces the same key; all changes survive reopening."""
        self.cache.save_annotation("rs101", sample_annotation("FIRST"))
        self.cache.save_annotation("rs101", sample_annotation("SECOND"))
        self.cache.save_annotation("rs102", sample_annotation("THIRD"))
        self.cache.close()
        with AnnotationCache(self.path) as reopened:
            self.assertEqual(reopened.get_annotation("rs101")["gene"], "SECOND")
            self.assertEqual(reopened.clear_cache(), 2)
            self.assertIsNone(reopened.get_annotation("rs101"))
            self.assertEqual(reopened.clear_cache(), 0)
        with AnnotationCache(self.path) as reopened:
            self.assertIsNone(reopened.get_annotation("rs102"))

    def test_null_annotation_details_are_valid(self) -> None:
        """A real API hit with no selected details can be cached as unavailable."""
        self.cache.save_annotation("rs101", {"raw_json": {"query": "rs101", "_id": "synthetic"}})
        result = self.cache.get_annotation("rs101")
        self.assertIsNone(result["gene"])
        self.assertIsNone(result["clinical_significance"])
        self.assertIsNone(result["trait_summary"])

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_new_cache_permissions_are_private(self) -> None:
        """New databases are owner-readable/writable regardless of normal umask."""
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_default_path_can_be_isolated_with_home(self) -> None:
        """The default path follows HOME; never touch the actual user's cache."""
        with patch.dict(os.environ, {"HOME": self.temp_dir.name}):
            self.assertEqual(default_cache_path(), Path(self.temp_dir.name) / ".genomic_annotator_cache.db")
            with AnnotationCache() as cache:
                cache.save_annotation("rs101", sample_annotation())
                self.assertIsNotNone(cache.get_annotation("rs101"))

    def test_parameterized_values_preserve_quotes(self) -> None:
        """SQL-like text is stored as data without altering the table."""
        gene = "SYNTHETIC'); DROP TABLE rsid_annotations; --"
        self.cache.save_annotation("rs101", sample_annotation(gene))
        self.assertEqual(self.cache.get_annotation("rs101")["gene"], gene)
        self.assertEqual(self.cache.clear_cache(), 1)

    def test_bad_keys_and_personal_fields_are_rejected(self) -> None:
        """Cache operations reject query injection and whole genome records."""
        for rsid in ["i101", "rs", "rs1,rs2", "rs١", "rs1' OR 1=1", "", None]:
            with self.subTest(rsid=rsid):
                with self.assertRaises(CacheValidationError):
                    self.cache.save_annotation(rsid, sample_annotation())
                with self.assertRaises(CacheValidationError):
                    self.cache.get_annotation(rsid)
        for key in ["genotype", "chromosome", "position", "file_path", "name", "last_updated"]:
            with self.subTest(key=key), self.assertRaisesRegex(CacheValidationError, "only"):
                self.cache.save_annotation("rs101", {**sample_annotation(), key: "private"})

    def test_bad_payload_does_not_replace_cached_record(self) -> None:
        """Invalid input is rejected before an upsert can overwrite valid data."""
        original = sample_annotation()
        self.cache.save_annotation("rs101", original)
        for data in [
            [],
            {},
            {"raw_json": "{}"},
            {"raw_json": {"not_json": {1, 2}}},
            {"raw_json": {"number": float("nan")}},
            {"raw_json": {}, "gene": ["SYNTHETIC"]},
            {"raw_json": {}, "gene": 12},
            {"raw_json": {}, "clinical_significance": ""},
        ]:
            with self.subTest(data=data), self.assertRaises(CacheValidationError):
                self.cache.save_annotation("rs101", data)
        result = self.cache.get_annotation("rs101")
        for key, value in original.items():
            self.assertEqual(result[key], value)

    def test_transaction_failure_preserves_previous_record(self) -> None:
        """A SQLite write error is surfaced and cannot partially update a row."""
        self.cache.save_annotation("rs101", sample_annotation("ORIGINAL"))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "CREATE TRIGGER reject_update BEFORE UPDATE ON rsid_annotations "
                "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
        with self.assertRaisesRegex(CacheError, "Cannot save"):
            self.cache.save_annotation("rs101", sample_annotation("REPLACEMENT"))
        self.assertEqual(self.cache.get_annotation("rs101")["gene"], "ORIGINAL")

    def test_corrupt_json_and_timestamp_are_explicit_errors(self) -> None:
        """Corrupt stored records are never reported as valid cache hits."""
        for field, value in [
            ("raw_json", "{bad"),
            ("raw_json", "null"),
            ("raw_json", '{"value": NaN}'),
            ("last_updated", "yesterday"),
            ("last_updated", "2026-01-01T00:00:00"),
            ("last_updated", "2026-01-01T00:00:00+01:00"),
        ]:
            self.cache.save_annotation("rs101", sample_annotation())
            with closing(sqlite3.connect(self.path)) as connection:
                with connection:
                    if field == "raw_json":
                        connection.execute(
                            "UPDATE rsid_annotations SET raw_json = ? WHERE rsid = ?", (value, "rs101")
                        )
                    else:
                        connection.execute(
                            "UPDATE rsid_annotations SET last_updated = ? WHERE rsid = ?", (value, "rs101")
                        )
            with self.subTest(field=field, value=value), self.assertRaisesRegex(CacheError, "corrupt"):
                self.cache.get_annotation("rs101")

    def test_context_close_and_idempotence(self) -> None:
        """Closed handles fail clearly, and context managers close on errors."""
        self.cache.close()
        self.cache.close()
        with self.assertRaisesRegex(CacheError, "closed"):
            self.cache.get_annotation("rs101")
        with self.assertRaisesRegex(CacheError, "closed"):
            self.cache.save_annotation("rs101", sample_annotation())
        with self.assertRaisesRegex(CacheError, "closed"):
            self.cache.clear_cache()
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with AnnotationCache(self.path) as cache:
                raise RuntimeError("synthetic")
        with self.assertRaisesRegex(CacheError, "closed"):
            cache.get_annotation("rs101")

    def test_open_and_schema_failures(self) -> None:
        """Invalid paths, non-databases and incompatible schemas fail explicitly."""
        invalid_file = Path(self.temp_dir.name) / "not-a-database"
        invalid_file.write_text("synthetic non-SQLite content", encoding="utf-8")
        for path in ["", "\x00", self.temp_dir.name, self.path / "child.db", invalid_file]:
            with self.subTest(path=path), self.assertRaises(CacheError):
                AnnotationCache(path)
        self.assertEqual(invalid_file.read_text(), "synthetic non-SQLite content")
        incompatible = Path(self.temp_dir.name) / "incompatible.db"
        with closing(sqlite3.connect(incompatible)) as connection:
            connection.execute("CREATE TABLE rsid_annotations (wrong TEXT)")
        with self.assertRaisesRegex(CacheError, "schema"):
            AnnotationCache(incompatible)

    def test_symlink_path_is_not_followed(self) -> None:
        """Reject symbolic cache paths instead of modifying their target."""
        link = Path(self.temp_dir.name) / "linked.db"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(CacheError, "symbolic link"):
            AnnotationCache(link)


if __name__ == "__main__":
    unittest.main()
