"""Typer commands for private local annotation, reports and cache management."""

from pathlib import Path
from typing import NoReturn, Optional

import typer
from rich.console import Console
from rich.text import Text

from genomic_annotator.annotator import AnnotationInputError, VariantAnnotator
from genomic_annotator.database import AnnotationCache, CacheError, default_cache_path
from genomic_annotator.parser import GenomeFileError, GenomeParseError, parse_23andme
from genomic_annotator.reporter import (
    PRIVACY_NOTE,
    ReportError,
    export_html_report,
    generate_terminal_table,
    validate_export_path,
)


app = typer.Typer(
    name="genomic-annotator",
    help=(
        "Research-only local 23andMe annotation; not a diagnosis. Online mode sends only "
        "RSIDs to MyVariant.info. Use --offline on annotate to prohibit all HTTP."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
console = Console(markup=False, highlight=False, emoji=False)
error_console = Console(stderr=True, markup=False, highlight=False, emoji=False)


def _error(message: str, code: int) -> NoReturn:
    """Print a literal, local error and exit with the requested nonzero status."""
    error_console.print(Text(f"Error: {message}", style="red"))
    raise typer.Exit(code=code)


def _guard_input_cache_path(file_path: str, cache_path: Path) -> Path:
    """Keep the input distinct from the cache, including aliases and hard links."""
    try:
        input_path = Path(file_path).expanduser().absolute()
        same_path = input_path.resolve() == cache_path.resolve()
        same_file = (
            input_path.exists() and cache_path.exists() and input_path.samefile(cache_path)
        )
        if same_path or same_file:
            raise GenomeFileError("The raw input file must not be the annotation cache.")
        return input_path
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, GenomeFileError):
            raise
        raise GenomeFileError(f"Invalid input path: {exc}") from exc


@app.command("annotate")
def annotate(
    file_path: str = typer.Argument(..., help="Local UTF-8, tab-separated 23andMe raw file."),
    export: Optional[str] = typer.Option(
        None,
        "--export",
        help="Create a new private standalone HTML report; never overwrite existing files.",
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Use cached annotations only, with zero HTTP activity."
    ),
) -> None:
    """Parse, annotate and display all called rows, optionally exporting private HTML."""
    try:
        cache_path = default_cache_path()
        input_path = _guard_input_cache_path(file_path, cache_path)
        export_path = (
            validate_export_path(export, [input_path, cache_path]) if export is not None else None
        )
        variants = parse_23andme(file_path)
    except GenomeParseError as exc:
        _error(str(exc), 2)
    except ReportError as exc:
        _error(str(exc), 1)

    if offline:
        console.print(Text("Offline mode: cache-only annotation; no HTTP requests."))
    else:
        console.print(Text(PRIVACY_NOTE))
    try:
        with AnnotationCache(cache_path) as cache:
            annotator = VariantAnnotator(cache, offline=offline)
            annotated = annotator.annotate(variants)
        for warning in annotator.warnings:
            error_console.print(Text(f"Warning: {warning}", style="yellow"))
        console.print(generate_terminal_table(annotated))
        counts = annotated["annotation_status"].value_counts()
        summary = ", ".join(f"{status}={int(count)}" for status, count in sorted(counts.items()))
        console.print(Text(f"Processed {len(annotated)} called input row(s). Statuses: {summary}."))
        if export_path is not None:
            export_html_report(annotated, str(export_path))
            console.print(Text(f"Private HTML report written to {export_path}."))
    except (CacheError, ReportError, AnnotationInputError) as exc:
        _error(str(exc), 1)
    if (annotated["annotation_status"] == "fetch_failed").any():
        error_console.print(
            Text("Partial result: online lookup failed. Cached and available rows were retained.", style="yellow")
        )
        raise typer.Exit(code=3)


@app.command("clear-cache")
def clear_cache() -> None:
    """Delete cached public annotations, leaving genome files and reports untouched."""
    try:
        with AnnotationCache() as cache:
            count = cache.clear_cache()
    except CacheError as exc:
        _error(str(exc), 1)
    console.print(
        Text(
            f"Cleared {count} cached annotation(s). Raw input files and HTML reports were not changed. "
            "Cache clearing is not guaranteed secure erasure of disk backups."
        )
    )


def main() -> None:
    """Run the Typer app without enabling stack traces containing local data."""
    app()
