"""Private SQLite storage of public RSID annotations, never genotype rows."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, Optional, Type, Union

from genomic_annotator.parser import is_valid_rsid


ANNOTATION_FIELDS = ("gene", "clinical_significance", "trait_summary")
CACHE_COLUMNS = (
    "rsid",
    "gene",
    "clinical_significance",
    "trait_summary",
    "raw_json",
    "last_updated",
)


class CacheError(RuntimeError):
    """A cache cannot be opened, read, written, cleared or closed."""


class CacheValidationError(CacheError, ValueError):
    """A caller supplied an invalid RSID or non-annotation cache data."""


def default_cache_path() -> Path:
    """Return the local user's default cache path without opening it."""
    return Path.home() / ".genomic_annotator_cache.db"


def _validate_rsid(rsid: str) -> None:
    """Reject any cache key that is not an ASCII rs[0-9]+ identifier."""
    if not is_valid_rsid(rsid):
        raise CacheValidationError("Cache keys must match rs[0-9]+.")


def _encode_annotation(data: Dict[str, Any]) -> str:
    """Validate annotation-only fields and encode the decoded JSON payload."""
    if not isinstance(data, dict):
        raise CacheValidationError("Annotation data must be a dictionary.")
    allowed = set(ANNOTATION_FIELDS) | {"raw_json"}
    if set(data) - allowed:
        raise CacheValidationError(
            "Cache data may contain only gene, clinical_significance, "
            "trait_summary and raw_json; never supply genotype rows or metadata."
        )
    for field in ANNOTATION_FIELDS:
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise CacheValidationError(f"Annotation field {field} must be nonempty text or None.")
    if not isinstance(data.get("raw_json"), (dict, list)):
        raise CacheValidationError("raw_json must be a decoded JSON object or list.")
    try:
        return json.dumps(data["raw_json"], ensure_ascii=True, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CacheValidationError("raw_json must contain valid finite JSON data.") from exc


class AnnotationCache:
    """Store public annotations in an annotation-only, local SQLite database.

    ``db_path`` is injectable for tests; ``None`` uses
    ``~/.genomic_annotator_cache.db``. Parent directories must already exist.
    Newly created files have mode 0600. Symlinks are rejected. Use this class
    as a context manager or explicitly call ``close()``.

    ``get_annotation`` returns ``None`` for a miss or a dictionary with
    ``rsid``, three nullable text fields, decoded ``raw_json`` and a UTC
    ISO-8601 ``last_updated`` timestamp. ``save_annotation`` accepts the three
    optional nullable text fields and a required decoded ``raw_json`` object
    or list; it returns ``None`` and atomically upserts. Only public API
    annotation payloads belong in ``raw_json``, never input records.
    ``clear_cache`` returns the number of deleted annotations.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        """Open/create the cache, raising CacheError on path or schema errors."""
        self._connection: Optional[sqlite3.Connection] = None
        try:
            if db_path is not None and not str(db_path).strip():
                raise ValueError("Cache path must not be empty.")
            self.path = (
                default_cache_path() if db_path is None else Path(db_path).expanduser()
            ).absolute()
            if self.path.is_symlink():
                raise ValueError("Cache path must not be a symbolic link.")
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                if not self.path.is_file():
                    raise ValueError("Cache path must refer to a regular file.")
            else:
                os.close(descriptor)
            connection = sqlite3.connect(str(self.path), timeout=5.0)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS rsid_annotations (
                        rsid TEXT PRIMARY KEY,
                        gene TEXT,
                        clinical_significance TEXT,
                        trait_summary TEXT,
                        raw_json TEXT NOT NULL,
                        last_updated TEXT NOT NULL
                    )
                    """
                )
            columns = tuple(
                row["name"] for row in connection.execute("PRAGMA table_info(rsid_annotations)")
            )
            if columns != CACHE_COLUMNS:
                raise CacheError("Cache schema is incompatible; use a dedicated annotation cache.")
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if isinstance(exc, CacheError):
                raise
            raise CacheError(f"Cannot open annotation cache: {exc}") from exc

    def _open_connection(self) -> sqlite3.Connection:
        """Return the active connection or explicitly reject a closed cache."""
        if self._connection is None:
            raise CacheError("Annotation cache is closed.")
        return self._connection

    def get_annotation(self, rsid: str) -> Optional[Dict[str, Any]]:
        """Return the documented annotation dictionary, or None on a cache miss."""
        _validate_rsid(rsid)
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT rsid, gene, clinical_significance, trait_summary, raw_json, last_updated "
                "FROM rsid_annotations WHERE rsid = ?",
                (rsid,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise CacheError(f"Cannot read annotation cache: {exc}") from exc
        if row is None:
            return None
        record: Dict[str, Any] = dict(row)
        try:
            record["raw_json"] = json.loads(record["raw_json"])
            _encode_annotation(
                {field: record[field] for field in (*ANNOTATION_FIELDS, "raw_json")}
            )
            timestamp = datetime.fromisoformat(record["last_updated"])
            if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
                raise ValueError("Cache timestamp is not UTC.")
        except (TypeError, ValueError, CacheValidationError, RecursionError) as exc:
            raise CacheError(f"Cached annotation for {rsid} is corrupt: {exc}") from exc
        return record

    def save_annotation(self, rsid: str, data: Dict[str, Any]) -> None:
        """Transactionally upsert public annotation fields, assigning UTC time."""
        _validate_rsid(rsid)
        raw_json = _encode_annotation(data)
        connection = self._open_connection()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO rsid_annotations
                        (rsid, gene, clinical_significance, trait_summary, raw_json, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rsid) DO UPDATE SET
                        gene = excluded.gene,
                        clinical_significance = excluded.clinical_significance,
                        trait_summary = excluded.trait_summary,
                        raw_json = excluded.raw_json,
                        last_updated = excluded.last_updated
                    """,
                    (
                        rsid,
                        data.get("gene"),
                        data.get("clinical_significance"),
                        data.get("trait_summary"),
                        raw_json,
                        timestamp,
                    ),
                )
        except sqlite3.Error as exc:
            raise CacheError(f"Cannot save annotation cache: {exc}") from exc

    def clear_cache(self) -> int:
        """Delete cached rows transactionally; return their count, not secure erasure."""
        connection = self._open_connection()
        try:
            with connection:
                cursor = connection.execute("DELETE FROM rsid_annotations")
            return cursor.rowcount
        except sqlite3.Error as exc:
            raise CacheError(f"Cannot clear annotation cache: {exc}") from exc

    def close(self) -> None:
        """Close the connection; repeated calls are safe and perform no writes."""
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error as exc:
                raise CacheError(f"Cannot close annotation cache: {exc}") from exc
            self._connection = None

    def __enter__(self) -> "AnnotationCache":
        """Return this open cache for a with statement."""
        self._open_connection()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        """Close this cache without suppressing exceptions from the with block."""
        self.close()
