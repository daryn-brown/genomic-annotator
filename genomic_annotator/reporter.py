"""Literal-text terminal tables and private, self-contained HTML reports."""

import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

import pandas as pd
from jinja2 import Environment, StrictUndefined, TemplateError
from rich.table import Table
from rich.text import Text

from genomic_annotator.annotator import ANNOTATED_COLUMNS, STATUS_LABELS
from genomic_annotator.database import default_cache_path


MEDICAL_NOTE = (
    "Research only; not diagnostic. RSID annotations may describe multiple alleles "
    "and are not genotype-specific. Do not infer disease, risk, or carrier status; "
    "personal medical conclusions require allele-aware clinical confirmation."
)
PRIVACY_NOTE = (
    "Online queries disclose queried RSIDs and normal connection metadata to MyVariant.info. "
    "Whole genome files, genotypes, coordinates, paths and personal metadata are never uploaded. "
    "Use --offline for zero HTTP."
)
SENSITIVE_REPORT_NOTE = (
    "Local output and HTML reports contain sensitive genotype and position data. Keep them private."
)
CATEGORY_LABELS = {
    "benign": "Benign / likely benign label",
    "pathogenic": "Pathogenic / likely pathogenic label",
    "risk": "Risk label (not personal risk)",
    "uncertain": "Conflicting, mixed or uncertain labels",
    "other": "Other / unclassified label",
    "unavailable": "No classification available",
}
CATEGORY_STYLES = {
    "benign": "green",
    "pathogenic": "bold red",
    "risk": "dark_orange",
    "uncertain": "yellow",
    "other": "",
    "unavailable": "dim",
}

SEARCH_SCRIPT = """
"use strict";
(() => {
  const search = document.getElementById("search");
  const status = document.getElementById("status-filter");
  const category = document.getElementById("category-filter");
  const counter = document.getElementById("visible-count");
  const noMatches = document.getElementById("no-matches");
  const rows = Array.from(document.querySelectorAll("#findings tbody tr[data-status]"))
    .map(row => ({row, text: row.textContent.toLowerCase()}));
  function applyFilters() {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    for (const entry of rows) {
      const matches = entry.text.includes(query) &&
        (!status.value || entry.row.dataset.status === status.value) &&
        (!category.value || entry.row.dataset.category === category.value);
      entry.row.hidden = !matches;
      if (matches) visible += 1;
    }
    counter.textContent = `${visible} of ${rows.length} rows shown`;
    noMatches.hidden = visible !== 0 || rows.length === 0;
  }
  search.addEventListener("input", applyFilters);
  status.addEventListener("change", applyFilters);
  category.addEventListener("change", applyFilters);
  applyFilters();
})();
"""

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-{{ script_hash }}'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'">
  <meta name="referrer" content="no-referrer">
  <title>Local RSID research annotations</title>
  <style>
    :root { color-scheme: light dark; font: 16px/1.5 system-ui, sans-serif; }
    body { max-width: 1600px; margin: 0 auto; padding: 1.5rem; }
    h1 { line-height: 1.2; }
    .notice { padding: 1rem; border: 2px solid #777; border-radius: .4rem; }
    .notice p { margin: .4rem 0; }
    .controls { display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }
    label { display: grid; gap: .25rem; }
    input, select { font: inherit; padding: .4rem; max-width: 100%; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border: 1px solid #999; padding: .6rem; text-align: left; vertical-align: top; }
    th { background: #e9edf0; color: #17212b; }
    td { overflow-wrap: anywhere; min-width: 3rem; }
    .badge { display: inline-block; padding: .1rem .4rem; border-radius: .25rem; font-weight: 600; }
    .benign { color: #14532d; background: #dcfce7; }
    .pathogenic { color: #991b1b; background: #fee2e2; }
    .risk { color: #9a3412; background: #ffedd5; }
    .uncertain { color: #713f12; background: #fef9c3; }
    .other, .unavailable { color: #334155; background: #e2e8f0; }
    .detail { display: block; font-size: .875rem; margin-top: .25rem; }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <h1>Local RSID research annotations</h1>
  <aside class="notice" aria-label="Important limitations and privacy">
    <p><strong>Not a diagnosis.</strong> {{ medical_note }}</p>
    <p><strong>Sensitive local data.</strong> {{ sensitive_note }}</p>
    <p>{{ privacy_note }}</p>
    <p>This HTML is self-contained. Search and filters run in your browser without network access.
       Colors describe public annotation labels, not your health or risk.</p>
  </aside>
  <p>{{ row_count }} input row(s). Every called input row is retained, including duplicates.</p>
  {% if warnings %}
  <aside aria-label="Annotation warnings">
    <h2>Partial-data warnings</h2>
    <ul>{% for warning in warnings %}<li>{{ warning }}</li>{% endfor %}</ul>
  </aside>
  {% endif %}
  <div class="controls">
    <label for="search">Search all displayed fields
      <input id="search" type="search" placeholder="RSID, gene, trait, genotype..." autocomplete="off">
    </label>
    <label for="status-filter">Annotation status
      <select id="status-filter">
        <option value="">All statuses</option>
        {% for code, label in statuses.items() %}<option value="{{ code }}">{{ label }}</option>{% endfor %}
      </select>
    </label>
    <label for="category-filter">Reported classification
      <select id="category-filter">
        <option value="">All classifications</option>
        {% for code, label in categories.items() %}<option value="{{ code }}">{{ label }}</option>{% endfor %}
      </select>
    </label>
  </div>
  <p id="visible-count" role="status" aria-live="polite">{{ row_count }} of {{ row_count }} rows shown</p>
  <p id="no-matches" hidden>No rows match these filters.</p>
  <noscript>JavaScript is disabled: all rows remain visible, but search and filters are unavailable.</noscript>
  <div class="table-wrap">
    <table id="findings">
      <thead><tr>
        <th scope="col">RSID</th><th scope="col">Chromosome</th><th scope="col">Position</th>
        <th scope="col">Genotype</th><th scope="col">Gene</th>
        <th scope="col">Reported significance (not genotype-specific)</th>
        <th scope="col">Reported trait(s)</th><th scope="col">Annotation status</th>
      </tr></thead>
      <tbody>
      {% for row in rows %}
        <tr data-status="{{ row.annotation_status }}" data-category="{{ row.category }}">
          <td>{{ row.rsid }}</td><td>{{ row.chromosome }}</td><td>{{ row.position }}</td>
          <td>{{ row.genotype }}</td><td>{{ row.gene }}</td>
          <td><span class="badge {{ row.category }}">{{ row.clinical_significance }}</span>
              <span class="detail">{{ row.category_label }}</span></td>
          <td>{{ row.trait_summary }}</td>
          <td>{{ row.status_label }}
              <span class="detail">Source: {{ row.annotation_source }}</span>
              <span class="detail">{{ row.annotation_message }}</span></td>
        </tr>
      {% else %}
        <tr><td colspan="8">No findings to display.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <script>""" + SEARCH_SCRIPT + """</script>
</body>
</html>
"""


class ReportError(RuntimeError):
    """A dataframe cannot be safely rendered as a local annotation report."""


class ReportExportError(ReportError):
    """An HTML output path cannot be safely created or written."""


def _display(value: Any) -> str:
    """Format a scalar as literal data, with an explicit missing-value label."""
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return "Not available"
    text = str(value)
    return text if text.strip() else "Not available"


def classify_significance(significance: Any) -> str:
    """Conservatively classify annotation labels, not the individual's genotype.

    Only exact known terms receive benign/pathogenic/risk colors. Mixed,
    conflicting, uncertain, negated or partly unknown compound labels never
    become a definitive benign/pathogenic/risk category.
    """
    text = _display(significance)
    if text == "Not available":
        return "unavailable"
    normalized = re.sub(r"[_\s]+", " ", text.casefold()).strip()
    if re.search(
        r"\b(conflict\w*|uncertain|uncertainty|vus|unknown|unclassified)\b"
        r"|\b(not|non|no)\b",
        normalized,
    ):
        return "uncertain"
    terms = [term.strip() for term in re.split(r"[/;,|]|\band\b", normalized) if term.strip()]
    vocabulary = {
        "benign": "benign",
        "likely benign": "benign",
        "pathogenic": "pathogenic",
        "likely pathogenic": "pathogenic",
        "risk factor": "risk",
        "risk allele": "risk",
        "established risk allele": "risk",
        "likely risk allele": "risk",
    }
    categories = {vocabulary.get(term, "other") for term in terms}
    if len(categories) == 1:
        return next(iter(categories))
    return "uncertain" if categories else "other"


def _validate_frame(annotated_df: pd.DataFrame) -> None:
    """Reject missing report fields or unknown statuses before creating output."""
    if not isinstance(annotated_df, pd.DataFrame) or not annotated_df.columns.is_unique:
        raise ReportError("Report input must be a dataframe with unique columns.")
    if any(column not in annotated_df.columns for column in ANNOTATED_COLUMNS):
        raise ReportError("Report input is missing required annotation columns.")
    if any(
        not isinstance(status, str) or status not in STATUS_LABELS
        for status in annotated_df["annotation_status"]
    ):
        raise ReportError("Report input contains an unknown annotation status.")
    _report_warnings(annotated_df)


def _report_warnings(annotated_df: pd.DataFrame) -> List[str]:
    """Accept only literal warning strings, stripping any trusted-markup subtype."""
    warnings = annotated_df.attrs.get("annotation_warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
        raise ReportError("Report annotation_warnings must be a list of strings.")
    return [str(warning) for warning in warnings]


def _report_rows(annotated_df: pd.DataFrame) -> Iterator[Dict[str, str]]:
    """Yield a fixed local report projection; never embed arbitrary dataframe metadata."""
    for values in annotated_df[ANNOTATED_COLUMNS].itertuples(index=False, name=None):
        row = {column: _display(value) for column, value in zip(ANNOTATED_COLUMNS, values)}
        row["category"] = classify_significance(row["clinical_significance"])
        row["category_label"] = CATEGORY_LABELS[row["category"]]
        row["status_label"] = STATUS_LABELS[row["annotation_status"]]
        yield row


def generate_terminal_table(annotated_df: pd.DataFrame) -> Table:
    """Return a Rich table with literal data, cautious colors and medical limitations.

    All called rows are shown. Missing findings get an explicit empty caption.
    Invalid dataframe schemas or status codes raise ReportError.
    """
    _validate_frame(annotated_df)
    caption = MEDICAL_NOTE + " " + SENSITIVE_REPORT_NOTE + " " + PRIVACY_NOTE
    if annotated_df.empty:
        caption = "No findings to display. " + caption
    table = Table(
        title=Text("RSID research annotations (not genotype-specific)"),
        caption=Text(caption),
        show_lines=True,
    )
    for label in ("RSID", "Chr", "Position", "Genotype", "Gene", "Reported significance", "Reported trait(s)", "Annotation status"):
        table.add_column(label, overflow="fold")
    for row in _report_rows(annotated_df):
        table.add_row(
            Text(row["rsid"]),
            Text(row["chromosome"]),
            Text(row["position"]),
            Text(row["genotype"]),
            Text(row["gene"]),
            Text(row["clinical_significance"], style=CATEGORY_STYLES[row["category"]]),
            Text(row["trait_summary"]),
            Text(
                f"{row['status_label']}\nSource: {row['annotation_source']}\n"
                f"{row['annotation_message']}"
            ),
        )
    return table


def validate_export_path(output_path: str, protected_paths: Iterable[Path] = ()) -> Path:
    """Preflight a new report path without creating it or modifying existing files.

    Existing files, directories and symlinks are never overwritten. The default
    cache and its SQLite sidecars are protected even if not yet created. Callers
    may also protect their input file and any custom cache paths.
    """
    try:
        if not isinstance(output_path, str) or not output_path.strip():
            raise ValueError("HTML output path must be a nonempty string.")
        target = Path(output_path).expanduser().absolute()
        cache = default_cache_path()
        protected = [cache, *(Path(str(cache) + suffix) for suffix in ("-journal", "-wal", "-shm"))]
        protected.extend(protected_paths)
        if target.resolve() in {path.resolve() for path in protected}:
            raise ReportExportError("Refusing to export over an input file or annotation cache.")
        if target.exists() or target.is_symlink():
            raise ReportExportError("HTML output already exists; choose a new path. Nothing was overwritten.")
        if not target.parent.is_dir():
            raise ReportExportError("HTML output parent directory does not exist.")
        return target
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ReportExportError):
            raise
        raise ReportExportError(f"Invalid HTML output path: {exc}") from exc


def export_html_report(annotated_df: pd.DataFrame, output_path: str) -> None:
    """Stream a standalone, autoescaped HTML report into a new mode-0600 file.

    Browser search/filter uses only static inline JavaScript and DOM text; no
    annotation data is interpolated into executable script. A restrictive CSP
    allows only that script and inline styles, with all network access blocked.
    Refuses overwrites and cleans up a newly created partial file on export
    errors. Raises ReportError/ReportExportError rather than returning success.
    """
    _validate_frame(annotated_df)
    target = validate_export_path(output_path)
    environment = Environment(autoescape=True, undefined=StrictUndefined)
    script_hash = base64.b64encode(hashlib.sha256(SEARCH_SCRIPT.encode("utf-8")).digest()).decode("ascii")
    created = False
    descriptor = None
    try:
        template = environment.from_string(HTML_TEMPLATE)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None
        with handle:
            stream = template.stream(
                rows=_report_rows(annotated_df),
                row_count=len(annotated_df),
                warnings=_report_warnings(annotated_df),
                statuses=STATUS_LABELS,
                categories=CATEGORY_LABELS,
                medical_note=MEDICAL_NOTE,
                privacy_note=PRIVACY_NOTE,
                sensitive_note=SENSITIVE_REPORT_NOTE,
                script_hash=script_hash,
            )
            stream.enable_buffering(100)
            stream.dump(handle)
    except (OSError, ValueError, TemplateError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                target.unlink()
            except OSError as cleanup_exc:
                raise ReportExportError(
                    f"HTML export failed and its partial file could not be removed: {cleanup_exc}"
                ) from exc
        raise ReportExportError(f"Cannot export HTML report: {exc}") from exc
