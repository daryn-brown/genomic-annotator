"""Cache-first RSID annotation with an explicit, RSID-only network boundary."""

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

from genomic_annotator.database import ANNOTATION_FIELDS, AnnotationCache
from genomic_annotator.parser import GENOME_COLUMNS, is_valid_rsid


API_URL = "https://myvariant.info/v1/query"
API_FIELDS = (
    "dbnsfp.genename,clinvar.gene.symbol,clinvar.rcv.clinical_significance,"
    "clinvar.rcv.conditions.name,clinvar.clnsig"
)
BATCH_SIZE = 50
HTTP_TIMEOUT = (3.05, 15.0)
RESULT_COLUMNS = [
    *ANNOTATION_FIELDS,
    "annotation_status",
    "annotation_source",
    "annotation_message",
]
ANNOTATED_COLUMNS = [*GENOME_COLUMNS, *RESULT_COLUMNS]
STATUS_LABELS = {
    "annotated": "RSID annotation available",
    "no_details": "Variant found; details unavailable",
    "not_found": "RSID not found",
    "unsupported_id": "Unsupported ID (local only)",
    "offline_cache_miss": "Offline cache miss",
    "fetch_failed": "Fetch failed / not attempted",
}


class AnnotationInputError(ValueError):
    """The input is not a dataframe with the required genome columns."""


class AnnotationResponseError(ValueError):
    """The service response is malformed, incomplete or contradictory."""


class AnnotationFetchError(RuntimeError):
    """The service is unavailable or returned an unusable batch response."""


@dataclass
class _Result:
    """A local annotation outcome, separate from the user's genome record."""

    status: str
    source: str
    message: str
    data: Optional[Dict[str, Any]] = None

    def values(self) -> List[Any]:
        """Return fields in RESULT_COLUMNS order without any raw genome data."""
        data = self.data if self.data is not None else {}
        return [
            *(data.get(field) for field in ANNOTATION_FIELDS),
            self.status,
            self.source,
            self.message,
        ]


def _leaf_text(value: Any, aliases: Tuple[str, ...], field: str) -> List[str]:
    """Read text/list/recognized nested text leaves without stringifying objects."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [text for item in value for text in _leaf_text(item, aliases, field)]
    if isinstance(value, dict):
        if not value:
            return []
        keys = [key for key in aliases if key in value]
        if not keys:
            raise AnnotationResponseError(f"Unexpected object in {field}.")
        return [text for key in keys for text in _leaf_text(value[key], aliases, field)]
    raise AnnotationResponseError(f"Expected text or a text list in {field}.")


def _path_text(
    node: Any, path: Tuple[str, ...], aliases: Tuple[str, ...], field: str
) -> List[str]:
    """Traverse dictionaries and arbitrarily nested lists along a known path."""
    if not path:
        return _leaf_text(node, aliases, field)
    if node is None:
        return []
    if isinstance(node, list):
        return [text for item in node for text in _path_text(item, path, aliases, field)]
    if not isinstance(node, dict):
        raise AnnotationResponseError(f"Unexpected structure in {field}.")
    if path[0] not in node:
        return []
    return _path_text(node[path[0]], path[1:], aliases, field)


def _merged_text(values: Sequence[str]) -> Optional[str]:
    """Join distinct public text deterministically, preserving its original case."""
    unique = sorted(set(values), key=lambda text: (text.casefold(), text))
    return "; ".join(unique) if unique else None


def _extract_hit(hit: Dict[str, Any]) -> Dict[str, List[str]]:
    """Extract supported fields from one validated, allele-unspecific API hit."""
    if "error" in hit:
        raise AnnotationResponseError("Service returned a per-query error.")
    if "_id" in hit and (not isinstance(hit["_id"], str) or not hit["_id"].strip()):
        raise AnnotationResponseError("Variant hit has an invalid _id.")
    if "_id" not in hit and not any(field in hit for field in ("dbnsfp", "clinvar")):
        raise AnnotationResponseError("Variant hit contains neither an _id nor annotation fields.")
    try:
        json.dumps(hit, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise AnnotationResponseError("Variant hit contains non-JSON values.") from exc

    gene_aliases = ("symbol", "genename", "name", "value")
    significance_aliases = ("description", "clinical_significance", "name", "value")
    db_genes = _path_text(hit, ("dbnsfp", "genename"), gene_aliases, "dbnsfp.genename")
    cv_genes = _path_text(hit, ("clinvar", "gene", "symbol"), gene_aliases, "clinvar.gene.symbol")
    rcv_significance = _path_text(
        hit,
        ("clinvar", "rcv", "clinical_significance"),
        significance_aliases,
        "clinvar.rcv.clinical_significance",
    )
    clnsig = _path_text(hit, ("clinvar", "clnsig"), significance_aliases, "clinvar.clnsig")
    traits = _path_text(
        hit,
        ("clinvar", "rcv", "conditions", "name"),
        ("name", "value"),
        "clinvar.rcv.conditions.name",
    )
    extracted = {
        "gene": db_genes or cv_genes,
        "clinical_significance": rcv_significance or clnsig,
        "trait_summary": traits,
    }
    if "_id" not in hit and not any(extracted.values()):
        raise AnnotationResponseError("Variant hit has no identity or usable annotation fields.")
    return extracted


def _available_result(data: Dict[str, Any], source: str) -> _Result:
    """Describe a validated hit, including legitimate hits with no selected fields."""
    if any(data.get(field) for field in ANNOTATION_FIELDS):
        return _Result(
            "annotated",
            source,
            "Public RSID annotations; not matched to this genotype or allele.",
            data,
        )
    return _Result(
        "no_details",
        source,
        "Variant record found, but the requested annotation details are unavailable.",
        data,
    )


def _query_result(hits: List[Dict[str, Any]]) -> _Result:
    """Combine all hits for one RSID; reject contradictions or malformed fields."""
    if not hits:
        raise AnnotationResponseError("The batch response omitted this RSID.")
    for hit in hits:
        if "notfound" in hit and not isinstance(hit["notfound"], bool):
            raise AnnotationResponseError("Invalid notfound flag.")
    misses = [hit for hit in hits if hit.get("notfound") is True]
    if misses:
        if len(misses) != len(hits) or any(
            key in miss for miss in misses for key in ("_id", "dbnsfp", "clinvar", "error")
        ):
            raise AnnotationResponseError("Contradictory found/not-found results.")
        return _Result("not_found", "myvariant", "MyVariant.info returned no hit for this RSID.")
    extracted = [_extract_hit(hit) for hit in hits]
    data: Dict[str, Any] = {}
    for field in ANNOTATION_FIELDS:
        values = [value for entry in extracted for value in entry[field]]
        data[field] = _merged_text(values)
    data["raw_json"] = hits
    return _available_result(data, "myvariant")


def _batch_results(payload: Any, rsids: List[str]) -> Dict[str, _Result]:
    """Associate flat POST hits by query, never by list index or allele identifier."""
    if not isinstance(payload, list):
        raise AnnotationResponseError("Expected a flat JSON list of batch query results.")
    grouped: Dict[str, List[Dict[str, Any]]] = {rsid: [] for rsid in rsids}
    for hit in payload:
        if not isinstance(hit, dict):
            raise AnnotationResponseError("Batch entries must be JSON objects.")
        query = hit.get("query")
        if not is_valid_rsid(query) or query not in grouped:
            raise AnnotationResponseError("Batch entry has a missing or unrequested query RSID.")
        grouped[query].append(hit)
    outcomes: Dict[str, _Result] = {}
    for rsid in rsids:
        try:
            outcomes[rsid] = _query_result(grouped[rsid])
        except (AnnotationResponseError, RecursionError) as exc:
            outcomes[rsid] = _Result(
                "fetch_failed", "myvariant", f"Unusable annotation response: {exc}"
            )
    return outcomes


def _request_batch(session: requests.Session, rsids: List[str]) -> Dict[str, _Result]:
    """POST an allowlisted form made exclusively from distinct, validated RSIDs.

    No dataframe, genome row, coordinate, genotype, path, timestamp, name or
    input-derived URL reaches this function. Redirects, environment credentials,
    proxy discovery, cookies and automatic retries are not used.
    """
    if not 1 <= len(rsids) <= BATCH_SIZE or any(not is_valid_rsid(rsid) for rsid in rsids):
        raise AnnotationInputError("Network batches must contain 1-50 valid RSIDs.")
    if len(set(rsids)) != len(rsids):
        raise AnnotationInputError("Network batch RSIDs must be distinct.")
    payload = {"q": ",".join(rsids), "scopes": "dbsnp.rsid", "fields": API_FIELDS}
    session.cookies.clear()
    try:
        response = session.post(
            API_URL,
            data=payload,
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
            verify=True,
        )
    except requests.Timeout as exc:
        raise AnnotationFetchError("MyVariant.info timed out.") from exc
    except requests.RequestException as exc:
        raise AnnotationFetchError(
            f"MyVariant.info request failed ({type(exc).__name__})."
        ) from exc
    try:
        if not 200 <= response.status_code < 300:
            suffix = " Redirects are disabled." if 300 <= response.status_code < 400 else ""
            raise AnnotationFetchError(f"MyVariant.info returned HTTP {response.status_code}.{suffix}")
        try:
            response_payload = response.json()
        except (ValueError, RecursionError) as exc:
            raise AnnotationFetchError("MyVariant.info returned invalid JSON.") from exc
        try:
            return _batch_results(response_payload, rsids)
        except AnnotationResponseError as exc:
            raise AnnotationFetchError(f"MyVariant.info returned an unusable response: {exc}") from exc
    finally:
        response.close()


class VariantAnnotator:
    """Annotate every input row locally, consulting the cache before the network.

    ``annotate`` preserves input order, index, duplicate rows and all input
    columns, appending RESULT_COLUMNS. Status codes are documented in
    STATUS_LABELS. Missing fields are None, never inferred from a genotype.
    ``warnings`` and ``result.attrs['annotation_warnings']`` contain aggregate
    human-readable partial-data warnings for callers to display.

    Offline mode never even creates an HTTP session. Online mode sends up to
    50 distinct uncached RSIDs per POST, with fixed connect/read timeouts and
    zero retries. A failed or malformed batch stops further network work for
    that run; available cache hits and valid returned rows remain available.
    Only validated positive hits are cached, including hits without details.
    """

    def __init__(self, cache: AnnotationCache, offline: bool = False) -> None:
        """Use an explicitly supplied cache and an optional cache-only policy."""
        self.cache = cache
        self.offline = offline
        self.warnings: List[str] = []

    def _fetch_uncached(self, rsids: List[str], outcomes: Dict[str, _Result]) -> None:
        """Fill uncached outcomes, stopping subsequent batches on service failures."""
        with requests.Session() as session:
            session.trust_env = False
            session.headers.update(
                {"Accept": "application/json", "User-Agent": "genomic-annotator/1.0"}
            )
            session.mount("https://", HTTPAdapter(max_retries=0))
            for offset in range(0, len(rsids), BATCH_SIZE):
                batch = rsids[offset : offset + BATCH_SIZE]
                try:
                    batch_outcomes = _request_batch(session, batch)
                except AnnotationFetchError as exc:
                    reason = str(exc)
                    for rsid in rsids[offset:]:
                        outcomes[rsid] = _Result(
                            "fetch_failed", "none", f"{reason} Online lookup stopped for this run."
                        )
                    self.warnings.append(
                        f"{reason} Online lookup stopped; remaining uncached RSIDs were not queried."
                    )
                    break
                outcomes.update(batch_outcomes)
                for rsid, result in batch_outcomes.items():
                    if result.data is not None:
                        self.cache.save_annotation(rsid, result.data)
                if any(result.status == "fetch_failed" for result in batch_outcomes.values()):
                    reason = "MyVariant.info returned incomplete or malformed annotations."
                    for rsid in rsids[offset + BATCH_SIZE :]:
                        outcomes[rsid] = _Result(
                            "fetch_failed", "none", f"{reason} Not queried after this failure."
                        )
                    self.warnings.append(
                        f"{reason} Further online batches were stopped; valid rows were retained."
                    )
                    break

    def annotate(self, variants: pd.DataFrame) -> pd.DataFrame:
        """Return annotations and explicit statuses without dropping any input row.

        Raises AnnotationInputError for an invalid dataframe schema and
        CacheError for storage failures. Network/service failures are returned
        as fetch_failed rows with warnings, not raised in place of cached data.
        """
        if not isinstance(variants, pd.DataFrame) or not variants.columns.is_unique:
            raise AnnotationInputError("Expected a dataframe with unique genome column names.")
        if any(column not in variants.columns for column in GENOME_COLUMNS):
            raise AnnotationInputError("Input must contain rsid, chromosome, position and genotype.")
        self.warnings = []
        input_rsids = variants["rsid"].tolist()
        queryable = list(dict.fromkeys(rsid for rsid in input_rsids if is_valid_rsid(rsid)))
        outcomes: Dict[str, _Result] = {}
        uncached: List[str] = []
        for rsid in queryable:
            cached = self.cache.get_annotation(rsid)
            if cached is None:
                uncached.append(rsid)
            else:
                outcomes[rsid] = _available_result(cached, "cache")
        if self.offline:
            for rsid in uncached:
                outcomes[rsid] = _Result(
                    "offline_cache_miss", "none", "No cached annotation; offline mode forbids HTTP."
                )
        elif uncached:
            self._fetch_uncached(uncached, outcomes)

        unsupported = _Result(
            "unsupported_id", "none", "Only rs[0-9]+ IDs can be queried; this identifier stayed local."
        )
        row_results = [
            outcomes[rsid] if is_valid_rsid(rsid) else unsupported for rsid in input_rsids
        ]
        result = variants.copy(deep=True)
        values = [row.values() for row in row_results]
        for index, column in enumerate(RESULT_COLUMNS):
            result[column] = [row[index] for row in values]
        counts = Counter(row.status for row in row_results)
        warning_labels = {
            "unsupported_id": "have unsupported IDs and stayed local",
            "offline_cache_miss": "lack a cached annotation (offline; no HTTP was attempted)",
            "not_found": "had no matching RSID in MyVariant.info",
            "no_details": "have variant hits but no requested annotation details",
            "fetch_failed": "have unavailable annotations after an online lookup failure",
        }
        for status, label in warning_labels.items():
            if counts[status]:
                self.warnings.append(f"Partial data: {counts[status]} input row(s) {label}.")
        result.attrs["annotation_warnings"] = list(self.warnings)
        result.attrs["offline"] = self.offline
        return result
