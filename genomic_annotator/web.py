"""Loopback-only browser workspace; genome rows remain in process memory."""

import io
import logging
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote

import pandas as pd
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler, make_server

from genomic_annotator.annotator import (
    ANNOTATED_COLUMNS,
    STATUS_LABELS,
    AnnotationInputError,
    VariantAnnotator,
)
from genomic_annotator.database import AnnotationCache, CacheError, default_cache_path
from genomic_annotator.demo import demo_annotations
from genomic_annotator.parser import CHROMOSOME_ORDER, GENOME_COLUMNS, GenomeParseError, parse_23andme_stream
from genomic_annotator.reporter import (
    CATEGORY_LABELS,
    ReportError,
    classify_significance,
    export_html_report,
    validate_export_path,
)


MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_DATASETS = 3
MAP_BINS = 48
LOGGER = logging.getLogger(__name__)


class WorkspaceError(ValueError):
    """An explicit, user-correctable workspace operation failure."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Dataset:
    id: str
    name: str
    synthetic: bool = False
    phase: str = "loading"
    error: Optional[str] = None
    frame: Optional[pd.DataFrame] = None
    search: Optional[pd.Series] = None
    summary: Dict[str, Any] = field(default_factory=dict)
    revision: int = 0

    def metadata(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "synthetic": self.synthetic,
            "phase": self.phase,
            "error": self.error,
            "revision": self.revision,
            **self.summary,
        }


def _prepare_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, Dict[str, Any]]:
    """Precompute bounded map bins and a literal search index, not DOM-sized rows."""
    frame = frame.copy()
    categories = {
        value: classify_significance(value)
        for value in frame["clinical_significance"].unique()
    }
    frame["category"] = frame["clinical_significance"].map(categories)
    frame["row_id"] = range(len(frame))
    search = pd.Series(
        [
            " ".join("" if pd.isna(value) else str(value) for value in values).casefold()
            for values in frame[ANNOTATED_COLUMNS].itertuples(index=False, name=None)
        ],
        index=frame.index,
    )
    chromosomes = []
    groups = {str(chromosome): group for chromosome, group in frame.groupby("chromosome")}
    for chromosome in CHROMOSOME_ORDER:
        if chromosome not in groups:
            continue
        group = groups[chromosome]
        maximum = int(group["position"].max())
        bins = ((group["position"] - 1) * MAP_BINS // maximum).value_counts()
        chromosomes.append(
            {
                "name": chromosome,
                "count": len(group),
                "annotated": int((group["annotation_status"] == "annotated").sum()),
                "max_position": maximum,
                "bins": [int(bins.get(index, 0)) for index in range(MAP_BINS)],
            }
        )
    summary = {
        "row_count": len(frame),
        "annotated_count": int((frame["annotation_status"] == "annotated").sum()),
        "offline": bool(frame.attrs.get("offline", True)),
        "chromosomes": chromosomes,
        "statuses": {str(key): int(value) for key, value in frame["annotation_status"].value_counts().items()},
        "categories": {str(key): int(value) for key, value in frame["category"].value_counts().items()},
        "warnings": list(frame.attrs.get("annotation_warnings", [])),
    }
    return frame, search, summary


class Workspace:
    """Bounded datasets shared by tabs in one local server, never saved as genomes."""

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path.expanduser().absolute()
        self.datasets: Dict[str, Dataset] = {}
        self.lock = threading.RLock()
        self.busy = False
        self.worker: Optional[threading.Thread] = None

    def get(self, dataset_id: str) -> Dataset:
        if dataset_id not in self.datasets:
            raise WorkspaceError("This file is no longer open. Import it again.", 404)
        return self.datasets[dataset_id]

    def _reserve(self, name: str, synthetic: bool = False) -> Dataset:
        if len(self.datasets) >= MAX_DATASETS:
            raise WorkspaceError("Close an open file before adding another (maximum 3 files).", 409)
        dataset = Dataset(secrets.token_hex(12), name, synthetic=synthetic)
        self.datasets[dataset.id] = dataset
        return dataset

    def _publish(self, dataset: Dataset, frame: pd.DataFrame) -> None:
        prepared, search, summary = _prepare_frame(frame)
        with self.lock:
            dataset.frame, dataset.search, dataset.summary = prepared, search, summary
            dataset.revision += 1
            dataset.phase = "ready"
            dataset.error = None

    def demo(self) -> Dataset:
        with self.lock:
            for dataset in self.datasets.values():
                if dataset.synthetic:
                    return dataset
            dataset = self._reserve("Example genome", synthetic=True)
            self._publish(dataset, demo_annotations())
            return dataset

    def start(self, *, name: Optional[str] = None, payload: Optional[bytes] = None,
              dataset_id: Optional[str] = None) -> Dataset:
        with self.lock:
            if self.busy:
                raise WorkspaceError("Another import or lookup is running. Wait for it to finish.", 409)
            if dataset_id is not None:
                dataset = self.get(dataset_id)
                if dataset.synthetic:
                    raise WorkspaceError("Online lookup is disabled for fictional demo identifiers.")
                if dataset.frame is None:
                    raise WorkspaceError("Wait for this file to finish loading.", 409)
                dataset.phase = "annotating"
            else:
                if name is None or payload is None:
                    raise WorkspaceError("Choose a local 23andMe file.")
                dataset = self._reserve(name)
            dataset.error = None
            self.busy = True
            self.worker = threading.Thread(
                target=self._process, args=(dataset, payload), daemon=True, name="local-annotation"
            )
            self.worker.start()
            return dataset

    def _process(self, dataset: Dataset, payload: Optional[bytes]) -> None:
        try:
            if payload is not None:
                with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8-sig", newline="") as lines:
                    variants = parse_23andme_stream(lines, max_rows=MAX_ROWS)
            else:
                # Published frames are immutable; browsing keeps the previous snapshot.
                if dataset.frame is None:
                    raise WorkspaceError("The file has no parsed variants.")
                variants = dataset.frame[GENOME_COLUMNS]
            with AnnotationCache(self.cache_path) as cache:
                annotator = VariantAnnotator(cache, offline=payload is not None)
                frame = annotator.annotate(variants)
            self._publish(dataset, frame)
        except (GenomeParseError, CacheError, AnnotationInputError, WorkspaceError, UnicodeError) as exc:
            with self.lock:
                dataset.error = (
                    "Cannot read input file as UTF-8." if isinstance(exc, UnicodeError) else str(exc)
                )
                dataset.phase = "error"
        finally:
            with self.lock:
                if dataset.phase in {"loading", "annotating"}:
                    dataset.phase = "error"
                    dataset.error = "The local operation failed unexpectedly. See the terminal for details."
                    LOGGER.error("Unexpected failure in local annotation worker.")
                self.busy = False


def _integer_parameter(name: str, default: int, minimum: int, maximum: int) -> int:
    value = request.args.get(name, str(default))
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        raise WorkspaceError(f"{name} must be an integer between {minimum} and {maximum}.")
    result = int(value)
    if not minimum <= result <= maximum:
        raise WorkspaceError(f"{name} must be between {minimum} and {maximum}.")
    return result


def create_app(cache_path: Optional[Path] = None) -> Flask:
    """Create an authenticated same-origin UI, with no file-path reading endpoint."""
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
        WORKSPACE_TOKEN=secrets.token_urlsafe(32),
    )
    workspace = Workspace(default_cache_path() if cache_path is None else Path(cache_path))
    app.extensions["workspace"] = workspace

    @app.before_request
    def guard_local_request() -> Optional[tuple[Response, int]]:
        origin = request.headers.get("Origin")
        if origin is not None and origin != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin access to local genome data is forbidden."), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="Cross-site access to this local workspace is forbidden."), 403
        if request.path.startswith("/api/"):
            token = request.headers.get("X-Workspace-Token", "")
            if not secrets.compare_digest(
                token.encode("utf-8"), app.config["WORKSPACE_TOKEN"].encode("ascii")
            ):
                return jsonify(error="Workspace access expired. Reload this page."), 403
        return None

    @app.after_request
    def private_headers(response: Response) -> Response:
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "img-src 'self'; connect-src 'self'; font-src 'self'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    @app.errorhandler(WorkspaceError)
    def workspace_error(error: WorkspaceError) -> tuple[Response, int]:
        return jsonify(error=str(error)), error.status

    @app.errorhandler(ReportError)
    def report_error(error: ReportError) -> tuple[Response, int]:
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> tuple[Response, int]:
        message = (
            "File exceeds the 32 MiB browser limit. Use the CLI for larger files."
            if error.code == 413 else error.description
        )
        return jsonify(error=message), error.code or 500

    @app.get("/")
    def index() -> str:
        return render_template("workspace.html", token=app.config["WORKSPACE_TOKEN"])

    @app.get("/api/workspace")
    def workspace_state() -> Response:
        with workspace.lock:
            return jsonify(
                datasets=[dataset.metadata() for dataset in workspace.datasets.values()],
                busy=workspace.busy,
                status_labels=STATUS_LABELS,
                category_labels=CATEGORY_LABELS,
                max_upload_bytes=MAX_UPLOAD_BYTES,
            )

    @app.post("/api/demo")
    def open_demo() -> Response:
        return jsonify(workspace.demo().metadata())

    @app.post("/api/datasets")
    def import_file() -> tuple[Response, int]:
        if request.mimetype not in {"application/octet-stream", "text/plain", "text/tab-separated-values"}:
            raise WorkspaceError("Send the file as plain UTF-8 TSV, not a multipart upload.", 415)
        try:
            name = unquote(request.headers.get("X-Genome-Name", "genome.txt"), errors="strict")
        except UnicodeError as exc:
            raise WorkspaceError("The file name is not valid UTF-8.") from exc
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not name.strip() or len(name) > 200 or any(ord(char) < 32 for char in name):
            raise WorkspaceError("Choose a file with a printable name of at most 200 characters.")
        payload = request.get_data(cache=False)
        if not payload:
            raise WorkspaceError("The selected file is empty.")
        dataset = workspace.start(name=name, payload=payload)
        return jsonify(dataset.metadata()), 202

    @app.post("/api/datasets/<dataset_id>/annotate")
    def annotate_online(dataset_id: str) -> tuple[Response, int]:
        if not request.is_json:
            raise WorkspaceError("Explicit consent to share uncached RSIDs is required.")
        consent = request.get_json()
        if not isinstance(consent, dict) or set(consent) != {"allow_online"} or consent["allow_online"] is not True:
            raise WorkspaceError("Explicit consent to share uncached RSIDs is required.")
        dataset = workspace.start(dataset_id=dataset_id)
        return jsonify(dataset.metadata()), 202

    @app.delete("/api/datasets/<dataset_id>")
    def close_dataset(dataset_id: str) -> Response:
        with workspace.lock:
            dataset = workspace.get(dataset_id)
            if dataset.phase in {"loading", "annotating"}:
                raise WorkspaceError("Wait for this file's operation to finish before closing it.", 409)
            del workspace.datasets[dataset_id]
        return jsonify(closed=True)

    @app.get("/api/datasets/<dataset_id>/variants")
    def variants(dataset_id: str) -> Response:
        with workspace.lock:
            dataset = workspace.get(dataset_id)
            frame, search = dataset.frame, dataset.search
            revision = dataset.revision
        if frame is None or search is None:
            raise WorkspaceError("This file has not finished loading.", 409)
        chromosome = request.args.get("chromosome", "")
        status = request.args.get("status", "")
        category = request.args.get("category", "")
        query = request.args.get("query", "").strip().casefold()
        if chromosome and chromosome not in CHROMOSOME_ORDER:
            raise WorkspaceError("Unknown chromosome filter.")
        if status and status not in STATUS_LABELS:
            raise WorkspaceError("Unknown annotation status filter.")
        if category and category not in CATEGORY_LABELS:
            raise WorkspaceError("Unknown classification filter.")
        if len(query) > 200:
            raise WorkspaceError("Search is limited to 200 characters.")
        offset = _integer_parameter("offset", 0, 0, MAX_ROWS)
        limit = _integer_parameter("limit", 30, 1, 100)
        mask = pd.Series(True, index=frame.index)
        for column, value in (("chromosome", chromosome), ("annotation_status", status), ("category", category)):
            if value:
                mask &= frame[column] == value
        if query:
            mask &= search.str.contains(query, regex=False, na=False)
        matches = frame.loc[mask]
        rows = matches.iloc[offset:offset + limit][[*ANNOTATED_COLUMNS, "category", "row_id"]]
        return jsonify(
            total=len(matches), offset=offset, limit=limit, revision=revision,
            rows=rows.astype(object).where(rows.notna(), None).to_dict("records"),
        )

    @app.post("/api/datasets/<dataset_id>/export")
    def export(dataset_id: str) -> Response:
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"path"} or not isinstance(body["path"], str):
            raise WorkspaceError("Specify a new local HTML report path.")
        with workspace.lock:
            dataset = workspace.get(dataset_id)
            frame = dataset.frame
            if dataset.phase in {"loading", "annotating"}:
                raise WorkspaceError("Wait for annotation to finish before saving a report.", 409)
        if frame is None:
            raise WorkspaceError("There are no results to export.", 409)
        protected = [workspace.cache_path, *(
            Path(str(workspace.cache_path) + suffix) for suffix in ("-journal", "-wal", "-shm")
        )]
        target = validate_export_path(body["path"], protected)
        export_html_report(frame, str(target))
        return jsonify(path=str(target), row_count=len(frame))

    return app


class LocalRequestHandler(WSGIRequestHandler):
    """Do not log URLs, query strings, filenames or other browsing activity."""

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        pass


def serve_workspace(port: int = 8765, open_browser: bool = True) -> None:
    """Serve only IPv4 loopback, without a debugger, reloader or remote bind option."""
    app = create_app()
    with make_server("127.0.0.1", port, app, threaded=True, request_handler=LocalRequestHandler) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Local genome workspace: {url}", flush=True)
        print("Offline by default. Genome rows stay in memory. Press Ctrl+C to stop.", flush=True)
        if open_browser and not webbrowser.open(url):
            print("The browser could not be opened automatically. Open the local URL above.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nLocal workspace stopped.", flush=True)
