# Local genomic annotator

A privacy-first Python CLI for reading a **local 23andMe raw TSV**, looking up
public RSID annotations through a local cache and, optionally, MyVariant.info,
and producing a terminal table or a standalone searchable HTML report.

**Research only; not diagnostic.** An RSID can describe multiple alleles, with
different clinical interpretations. This tool does **not** match the user's
genotype, strand or reference build to an annotated allele. Do not infer disease,
personal risk, carrier status or treatment decisions from an RSID match.
Personal medical conclusions require allele-aware clinical confirmation.
Colors describe public annotation labels, not the individual's health.

## Installation

Use Python 3.10 or newer with an OpenSSL-backed HTTPS implementation. Create a
local virtual environment and install the five declared dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py --help
```

Substitute an appropriate interpreter if your system `python3` is older.
The prepared environment in this workspace uses **Python 3.14.6**, created with
`python3.14 -m venv .venv`. On Windows, use `.venv\Scripts\python.exe`.

Runtime dependencies: pandas, requests, Typer, Rich and Jinja2. SQLite and the
test runner come with Python. Node is optional and used only by a test that
executes the report's native JavaScript; it is not needed to use the application.

## Usage

```sh
# Strict cache-only operation: zero HTTP, including when annotations are missing.
.venv/bin/python main.py annotate /path/to/raw-genome.txt --offline

# Cache first, then online lookup for distinct uncached RSIDs.
.venv/bin/python main.py annotate /path/to/raw-genome.txt

# Create a new private HTML report, never overwrite an existing file.
.venv/bin/python main.py annotate /path/to/raw-genome.txt --offline --export /path/to/new-report.html

# Remove cached annotation rows; this does not delete raw files or HTML reports.
.venv/bin/python main.py clear-cache
```

Online mode is the default. For no identifier disclosure, explicitly use
`--offline`. A fresh offline cache produces useful per-row `offline_cache_miss`
labels, **not** medical findings. Both offline and online runs retain the order,
duplicate records, chromosome, position and genotype of every called input row.

HTML reports contain **sensitive local genotype and position data**. New reports
and cache files use owner-only permissions (`0600` on POSIX). Keep them private;
do not commit, upload or share a report merely because it has no external assets.
Reports contain no external scripts, fonts, stylesheets, images, links or CDNs.
Search and intersecting status/classification filters operate locally through
static inline JavaScript. A restrictive Content Security Policy blocks network
access and permits only the exact bundled script. Without JavaScript all rows
remain readable, but filtering is unavailable.

An export always requires a **new** filename and an existing parent directory.
Existing files, directories and symlinks are refused, including raw input,
cache files and SQLite sidecars. A failed export raises an error and removes
its newly created partial file when possible.

### Input format

`parse_23andme(file_path: str) -> pandas.DataFrame` returns exactly:

```text
rsid  chromosome  position  genotype
```

Files are UTF-8 (a BOM is accepted), tab-separated, normally headerless with a
commented header. Blank lines and whitespace-prefixed `#` comments are skipped;
surrounding whitespace in each field is stripped. An optional **first**
noncomment row may contain the exact plain header
`rsid<TAB>chromosome<TAB>position<TAB>genotype`.

Accepted values:

- Identifiers: ASCII `rs[0-9]+` or `i[0-9]+`; `i` identifiers stay local.
- Chromosomes: strings `1` through `22`, `X`, `Y`, `MT`.
- Positions: positive integers up to 2,147,483,647, without build conversion.
- Genotypes: one or two uppercase `A`, `C`, `G`, `T` bases, or one or two `I`/`D`
  indel symbols. Haploid and indel calls are retained literally.
- Uncalled `--` and `00` rows are excluded **after** validating other fields.

Missing/extra columns, unknown chromosome labels, malformed identifiers,
nonintegral positions and unsupported calls raise a line-numbered
`GenomeParseError`. Unreadable/invalid paths and non-UTF-8 input raise
`GenomeFileError`; empty, comment-only and uncalled-only files raise
`EmptyGenomeError`. The parser never silently drops a malformed record.
Chromosome numeric aliases such as `23`/`24` and ambiguous calls such as `NN`
are deliberately not guessed or converted.

## Privacy and network behavior

The only genome-derived request values are validated ASCII **RSIDs**, joined in
the form field `q`. Online mode exposes those queried RSIDs **and normal
connection metadata (including the connecting IP address)** to MyVariant.info.
The query list itself can be sensitive; RSID-only querying is **not anonymity**.
A large genome may result in many batches revealing its queried identifier set.

The application never sends raw file contents, genotype calls, input coordinates,
paths, names, personal metadata or local timestamps. Requests are built from an
RSID allowlist, never from a dataframe or a raw row. There is no telemetry.
Only this fixed endpoint and these fixed controls are used:

```text
POST https://myvariant.info/v1/query
Content-Type: application/x-www-form-urlencoded
q=rs101,rs102
scopes=dbsnp.rsid
fields=dbnsfp.genename,clinvar.gene.symbol,clinvar.rcv.clinical_significance,clinvar.rcv.conditions.name,clinvar.clnsig
```

There is no email parameter, input-derived URL or assumption that
`GET /variant/{rsid}` accepts RSIDs. Queries contain at most **50 distinct
uncached RSIDs**; cached keys and duplicate input rows do not add requests.
Responses are associated by their `query` field, never by position. Multiple
hits are combined deterministically; annotation fields may be dicts or lists.

TLS verification is enabled. Redirects are never followed. The application
disables environment-provided proxy/netrc credentials and clears response cookies
before each request. Connect/read timeouts are **3.05/15 seconds**, with **zero
automatic retries**. These are per-operation timeouts, not a total runtime
deadline. An unavailable service, bad HTTP status, malformed batch or missing
query response stops further batches for that invocation. Already cached and
validly fetched annotations are retained and failures are labeled, not hidden.

`--offline` does not even construct an HTTP session. It is the recommended mode
when identifier sharing is unacceptable, connectivity is unreliable or you need
a repeatable cache-only run.

## Cache and result contracts

The default cache is `~/.genomic_annotator_cache.db`. It contains one table,
`rsid_annotations`, with `rsid TEXT PRIMARY KEY`, `gene`,
`clinical_significance`, `trait_summary`, `raw_json` and `last_updated`.
Only **public API annotations** and a locally generated UTC cache timestamp
are stored, not genotype rows or personal input metadata. Public response IDs
can themselves include reference coordinates; these are not input coordinates.
Cached RSID keys may reveal what was queried, so the cache also deserves privacy.

```python
from genomic_annotator.database import AnnotationCache

with AnnotationCache("/an/existing/private/directory/annotations.db") as cache:
    record = cache.get_annotation("rs101")  # None on a miss
    # save_annotation("rs101", {...}) atomically inserts or replaces a public annotation.
    # clear_cache() returns the number of removed rows.
```

`get_annotation` returns `None` or a dictionary with all six columns:
`rsid`, the three nullable text fields, **decoded** `raw_json` (dict/list), and an
ISO-8601 UTC `last_updated`. `save_annotation` accepts a dictionary containing
the three optional nullable text fields plus a required decoded `raw_json`
dict/list and returns `None`. Other top-level fields are rejected.
Use only public annotation payloads as `raw_json`, never input records.
Methods use parameterized SQL and transactional upserts; invalid or corrupt
data and storage failures raise `CacheError` (invalid arguments use its
`CacheValidationError` subclass). Use a context manager or explicitly `close()`.
Parent directories must exist, and symbolic cache paths are rejected.

Positive hits, including legitimate `_id`-only hits without annotation details,
are cached. Not-found results, network failures and malformed responses are
**not** cached. There is no automatic cache expiry: public annotations can
become stale. Clear the cache before an online refresh. `clear-cache` purges
rows, not the database file, and is **not guaranteed secure erasure**, especially
on SSDs or in backups. Existing file permissions are not changed.

`VariantAnnotator(cache, offline=False).annotate(dataframe)` appends `gene`,
`clinical_significance`, `trait_summary`, `annotation_status`,
`annotation_source` and `annotation_message`, without changing input order,
index or duplicate rows. Unavailable fields are `None`. Aggregate warnings are
available on `annotator.warnings` and
`result.attrs["annotation_warnings"]`; library callers should surface them.
Sources are `cache`, `myvariant` or `none`.

| Status | Meaning |
| --- | --- |
| `annotated` | At least one requested public RSID annotation field is available; **not an allele/genotype match**. |
| `no_details` | A valid variant record was found, but requested details are absent. |
| `not_found` | The service explicitly returned `notfound: true`. |
| `unsupported_id` | Not a queryable RSID (including 23andMe `i` IDs); retained locally. |
| `offline_cache_miss` | No cached annotation, and HTTP is prohibited. |
| `fetch_failed` | A query failed, was malformed/omitted, or was not attempted after an outage. |

Missing annotation is never interpreted as benign. `likely benign` is green;
known pathogenic labels are red and risk labels orange. Conflicting, mixed,
uncertain, negated or partly unknown compound labels are not shown as a
definitive benign/pathogenic/risk category.

CLI exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Command completed. Inspect warnings/statuses: offline misses, unsupported IDs, not-found hits and missing details may remain. |
| `1` | Cache, storage, report or export failure. |
| `2` | Invalid input or command-line usage. |
| `3` | Online fetch failure produced a **partial result**; available rows and requested HTML are still returned when export succeeds. |

## Synthetic verification

All fixtures are deliberately synthetic or public API response shapes. No test
uses a personal genome, the real user cache or real HTTP. The minimal API
fixture records a public documentation-example response shape, not a clinical
expectation; biological annotations are not assumed stable.

```sh
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -t . -v
.venv/bin/python -m tests.synthetic_workflow
.venv/bin/python -m pip check
```

The smoke workflow runs the actual `main.py` entry point in subprocesses:
empty-cache offline, mocked online, populated-cache offline and `clear-cache`.
It uses a disposable `HOME`, verifies that duplicate calls survive and cached
annotations persist, checks generated private HTML, then removes its artifacts.
The online HTTP edge is mocked and verifies the exact allowed form. This is
also a safe copy-paste demonstration: it cannot query the example RSIDs live.

Tests cover parser validation, cache lifecycle/raw JSON/upserts, 0/1/50/51
batching, query-only privacy, cache/deduplication, multiple response shapes,
outages, no-hit/no-details distinctions, no-network offline operation,
escaping/classification/report permissions, native JavaScript filtering and CLI
help/annotation/export/cache/error paths.

## Files

`genomic_annotator/parser.py`, `database.py`, `annotator.py`, `reporter.py` and
`cli.py` contain the implementation; `__init__.py` defines the package.
`main.py` is the runner, `requirements.txt` declares dependencies and `tests/`
contains only synthetic test code/fixtures. `.gitignore` excludes environments,
caches, common raw-genome formats, generated reports and common secret files.
Never rely solely on filename exclusions: inspect staged files before committing.
