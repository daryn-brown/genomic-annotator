"""Unit tests for strict 23andMe TSV parsing with disposable dummy files."""

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from genomic_annotator.parser import (
    GENOME_COLUMNS,
    MAX_POSITION,
    EmptyGenomeError,
    GenomeFileError,
    GenomeParseError,
    parse_23andme,
)


class ParserTests(unittest.TestCase):
    """Exercise headers, every accepted call shape and malformed records."""

    def setUp(self) -> None:
        """Create an isolated directory for synthetic TSV files."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "synthetic.tsv"

    def parse(self, text: str) -> pd.DataFrame:
        """Write dummy TSV text and parse it."""
        self.path.write_text(text, encoding="utf-8")
        return parse_23andme(str(self.path))

    def test_comments_whitespace_and_uncalled_filtering(self) -> None:
        """Ignore comments/blanks and strip fields without losing string X/Y/MT."""
        frame = self.parse(
            "  # Completely synthetic data\n"
            "# rsid\tchromosome\tposition\tgenotype\n\n"
            " rs101 \t 1 \t 00123 \t AG \n"
            "rs102\tX\t456\tA\n"
            "rs103\tY\t789\tD\n"
            "i104\tMT\t900\tII\n"
            "rs105\t22\t91\t--\n"
            "rs106\t2\t92\t00\n"
        )
        self.assertEqual(frame.columns.tolist(), GENOME_COLUMNS)
        self.assertEqual(
            frame.to_dict("records"),
            [
                {"rsid": "rs101", "chromosome": "1", "position": 123, "genotype": "AG"},
                {"rsid": "rs102", "chromosome": "X", "position": 456, "genotype": "A"},
                {"rsid": "rs103", "chromosome": "Y", "position": 789, "genotype": "D"},
                {"rsid": "i104", "chromosome": "MT", "position": 900, "genotype": "II"},
            ],
        )
        self.assertTrue(pd.api.types.is_integer_dtype(frame["position"]))
        self.assertTrue(all(isinstance(value, str) for value in frame["chromosome"]))

    def test_plain_header_bom_and_crlf(self) -> None:
        """Accept a BOM and an optional exact plain schema header."""
        frame = self.parse(
            "\ufeff# synthetic\r\n"
            " rsid\t chromosome\tposition\tgenotype \r\n"
            "rs101\tMT\t1\tT\r\n"
        )
        self.assertEqual(frame.iloc[0].tolist(), ["rs101", "MT", 1, "T"])

    def test_all_base_and_indel_calls(self) -> None:
        """Accept diploid, haploid and I/D calls without allele inference."""
        calls = list("ACGTID")
        calls += [first + second for first in "ACGT" for second in "ACGT"]
        calls += ["II", "DD", "ID", "DI"]
        frame = self.parse(
            "\n".join(f"rs{index + 1}\t1\t{index + 1}\t{call}" for index, call in enumerate(calls))
        )
        self.assertEqual(frame["genotype"].tolist(), calls)

    def test_duplicate_rows_and_positions_preserved(self) -> None:
        """Do not deduplicate the user's called input rows."""
        row = "rs101\t1\t123\tAG\n"
        frame = self.parse(row + row + "rs101\tX\t124\tA\n")
        self.assertEqual(len(frame), 3)
        self.assertEqual(frame["position"].tolist(), [123, 123, 124])

    def test_all_supported_chromosomes(self) -> None:
        """Keep autosomes and sex/mitochondrial chromosome labels as strings."""
        chromosomes = [str(number) for number in range(1, 23)] + ["X", "Y", "MT"]
        frame = self.parse(
            "\n".join(f"rs{i + 1}\t{chrom}\t1\tAA" for i, chrom in enumerate(chromosomes))
        )
        self.assertEqual(frame["chromosome"].tolist(), chromosomes)

    def test_empty_comment_header_and_uncalled_only_files(self) -> None:
        """Raise a typed error instead of pretending an empty genome succeeded."""
        for text in [
            "",
            " \n\t \n",
            "# synthetic\n  # comment\n",
            "rsid\tchromosome\tposition\tgenotype\n",
            "rs101\t1\t1\t--\nrs102\tX\t2\t00\n",
        ]:
            with self.subTest(text=text), self.assertRaisesRegex(EmptyGenomeError, "no called"):
                self.parse(text)

    def test_column_count_errors(self) -> None:
        """Reject missing, extra, empty trailing and space-separated columns."""
        for row in [
            "rs101\t1\t1",
            "rs101\t1",
            "rs101",
            "rs101\t1\t1\tAA\textra",
            "rs101\t1\t1\tAA\t",
            "rs101 1 1 AA",
        ]:
            with self.subTest(row=row), self.assertRaisesRegex(GenomeParseError, "Line 2.*columns"):
                self.parse("# synthetic\n" + row + "\n")

    def test_invalid_identifiers(self) -> None:
        """Reject malformed, query-like and non-ASCII identifiers."""
        for value in ["", "rs", "RS101", "rs-1", "rs1 OR rs2", "rs1,rs2", "i", "abc", "rs١", "rs1\x00"]:
            with self.subTest(value=value), self.assertRaisesRegex(GenomeParseError, "identifier"):
                self.parse(f"{value}\t1\t1\tAA\n")

    def test_invalid_chromosomes(self) -> None:
        """Reject unsupported labels rather than guessing a reference build."""
        for value in ["", "0", "23", "chr1", "x", "M", "1.0", "-1"]:
            with self.subTest(value=value), self.assertRaisesRegex(GenomeParseError, "chromosome"):
                self.parse(f"rs101\t{value}\t1\tAA\n")

    def test_invalid_positions(self) -> None:
        """Reject invalid syntax, zero, negative and implausibly large positions."""
        for value in ["", "0", "-1", "1.1", "1e6", "+1", "NaN", "inf", "١", str(MAX_POSITION + 1), "9" * 5000]:
            with self.subTest(value=value[:30]), self.assertRaisesRegex(GenomeParseError, "position"):
                self.parse(f"rs101\t1\t{value}\tAA\n")

    def test_invalid_genotypes(self) -> None:
        """Do not silently drop unknown, quoted or mixed base/indel calls."""
        for value in ["", "aa", "N", "NN", "A/G", "AGT", "AI", "A-", "-", "0", '"AA"', "AA\x00"]:
            with self.subTest(value=value), self.assertRaisesRegex(GenomeParseError, "genotype"):
                self.parse(f"rs101\t1\t1\t{value}\n")

    def test_uncalled_rows_still_validate_other_fields(self) -> None:
        """Uncalled rows must not conceal malformed identifiers or coordinates."""
        for row in ["bad\t1\t1\t--", "rs101\tbad\t1\t00", "rs101\t1\t0\t--"]:
            with self.subTest(row=row), self.assertRaises(GenomeParseError):
                self.parse("rs102\t1\t1\tAA\n" + row)

    def test_repeated_and_inexact_headers_rejected(self) -> None:
        """Only the optional first exact schema header is supported."""
        for text in [
            "RSID\tchromosome\tposition\tgenotype\n",
            "rsid\tposition\tchromosome\tgenotype\n",
            "rs101\t1\t1\tAA\nrsid\tchromosome\tposition\tgenotype\n",
            "rsid\tchromosome\tposition\tgenotype\nrsid\tchromosome\tposition\tgenotype\n",
        ]:
            with self.subTest(text=text), self.assertRaises(GenomeParseError):
                self.parse(text)

    def test_late_malformed_row_is_not_silently_dropped(self) -> None:
        """Any invalid record rejects the parse, even after valid records."""
        with self.assertRaisesRegex(GenomeParseError, "Line 3.*genotype"):
            self.parse("rs101\t1\t1\tAA\n# synthetic\nrs102\t1\t2\t???\n")

    def test_invalid_and_unreadable_paths(self) -> None:
        """Wrap missing files, directories and invalid paths in GenomeFileError."""
        for path in ["", " ", "\x00", str(self.path), self.temp_dir.name]:
            with self.subTest(path=path), self.assertRaises(GenomeFileError):
                parse_23andme(path)

    def test_non_utf8_file(self) -> None:
        """Decode failures use a clear typed error."""
        self.path.write_bytes(b"rs101\t1\t1\tAA\n\xff")
        with self.assertRaisesRegex(GenomeFileError, "UTF-8"):
            parse_23andme(str(self.path))


if __name__ == "__main__":
    unittest.main()
