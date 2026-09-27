"""Strict parsing of local, tab-separated 23andMe raw genotype files."""

import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import pandas as pd


GENOME_COLUMNS: List[str] = ["rsid", "chromosome", "position", "genotype"]
CHROMOSOME_ORDER = tuple([str(number) for number in range(1, 23)] + ["X", "Y", "MT"])
CHROMOSOMES = frozenset(CHROMOSOME_ORDER)
MAX_POSITION = 2_147_483_647
IDENTIFIER_PATTERN = re.compile(r"(?:rs|i)[0-9]+")
RSID_PATTERN = re.compile(r"rs[0-9]+")
GENOTYPE_PATTERN = re.compile(r"(?:[ACGT]{1,2}|[DI]{1,2})")


class GenomeParseError(ValueError):
    """A genome file contains invalid or unsupported TSV data."""


class GenomeFileError(GenomeParseError):
    """A genome file cannot be opened or decoded as UTF-8."""


class EmptyGenomeError(GenomeParseError):
    """A genome file contains no called variants."""


def is_valid_rsid(value: object) -> bool:
    """Return whether a value is an ASCII RSID safe for the query allowlist."""
    return isinstance(value, str) and RSID_PATTERN.fullmatch(value) is not None


def _parse_position(value: str, line_number: int) -> int:
    """Return a bounded positive integer position or raise a parse error."""
    message = (
        f"Line {line_number}: position must be an integer between "
        f"1 and {MAX_POSITION}."
    )
    if re.fullmatch(r"[0-9]+", value) is None:
        raise GenomeParseError(message)
    try:
        position = int(value)
    except ValueError as exc:
        raise GenomeParseError(message) from exc
    if not 1 <= position <= MAX_POSITION:
        raise GenomeParseError(message)
    return position


def parse_23andme(file_path: str) -> pd.DataFrame:
    """Parse a UTF-8 23andMe TSV, retaining every valid called input row.

    The returned columns are exactly ``rsid, chromosome, position, genotype``.
    Chromosomes and calls remain strings; positions are integers. Duplicate
    rows are preserved. Blank lines and whitespace-prefixed ``#`` comments
    are ignored. An optional first noncomment row may be the exact column
    names. Surrounding field whitespace is stripped.

    Accepted identifiers are ``rs[0-9]+`` and local-only ``i[0-9]+``. Accepted
    chromosomes are 1-22, X, Y and MT. Calls are one or two uppercase A/C/G/T
    bases, or one or two I/D indel symbols. Uncalled ``--`` and ``00`` rows
    are filtered only after validating their other fields.

    Raises:
        GenomeFileError: The path is invalid/unreadable or the file is not UTF-8.
        EmptyGenomeError: There are no called variants after filtering.
        GenomeParseError: Any record has an invalid field or not four columns.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise GenomeFileError("Input file path must be a nonempty string.")
    try:
        path = Path(file_path).expanduser()
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, ValueError, RuntimeError) as exc:
        raise GenomeFileError(f"Cannot open input file: {exc}") from exc

    try:
        with handle:
            return parse_23andme_stream(handle)
    except (OSError, UnicodeError) as exc:
        raise GenomeFileError(f"Cannot read input file as UTF-8: {exc}") from exc


def parse_23andme_stream(
    lines: Iterable[str], *, max_rows: Optional[int] = None
) -> pd.DataFrame:
    """Apply the same strict parser to decoded lines without saving an upload.

    ``max_rows`` optionally bounds called rows for memory-limited consumers.
    The caller owns the stream and its decoding/IO lifecycle.
    """
    if max_rows is not None and (type(max_rows) is not int or max_rows < 1):
        raise ValueError("max_rows must be a positive integer.")
    rows: List[Tuple[str, str, int, str]] = []
    first_record = True
    for line_number, raw_line in enumerate(lines, start=1):
        if line_number == 1:
            raw_line = raw_line.removeprefix("\ufeff")
        line = raw_line.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split("\t")]
        if first_record and fields == GENOME_COLUMNS:
            first_record = False
            continue
        first_record = False
        if len(fields) != 4:
            raise GenomeParseError(
                f"Line {line_number}: expected 4 tab-separated columns; "
                f"found {len(fields)}."
            )
        rsid, chromosome, position_text, genotype = fields
        if IDENTIFIER_PATTERN.fullmatch(rsid) is None:
            raise GenomeParseError(
                f"Line {line_number}: identifier must match rs[0-9]+ or i[0-9]+."
            )
        if chromosome not in CHROMOSOMES:
            raise GenomeParseError(
                f"Line {line_number}: chromosome must be 1-22, X, Y or MT."
            )
        position = _parse_position(position_text, line_number)
        if genotype in {"--", "00"}:
            continue
        if GENOTYPE_PATTERN.fullmatch(genotype) is None:
            raise GenomeParseError(
                f"Line {line_number}: genotype must be one or two "
                "uppercase A/C/G/T bases or I/D indel symbols "
                "(uncalled values: --, 00)."
            )
        rows.append((rsid, chromosome, position, genotype))
        if max_rows is not None and len(rows) > max_rows:
            raise GenomeParseError(f"Input exceeds the browser limit of {max_rows:,} called rows.")
    if not rows:
        raise EmptyGenomeError("Input contains no called variants.")
    return pd.DataFrame(rows, columns=GENOME_COLUMNS)
