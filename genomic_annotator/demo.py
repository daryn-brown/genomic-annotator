"""Deterministic, explicitly fictional data for the local workspace preview."""

import pandas as pd

from genomic_annotator.annotator import ANNOTATED_COLUMNS
from genomic_annotator.parser import CHROMOSOME_ORDER


def demo_annotations() -> pd.DataFrame:
    """Build illustrative rows without HTTP or touching the annotation cache."""
    rows = []
    labels = ["Likely benign", "Uncertain significance", None, "Other", "Benign", None]
    calls = ["AG", "CC", "CT", "AA", "GT", "TT", "GG", "AC"]
    for chrom_index, chromosome in enumerate(CHROMOSOME_ORDER):
        count = 40 - chrom_index
        span = 240_000_000 - chrom_index * 6_000_000 if chromosome != "MT" else 16_500
        for index in range(count):
            label = labels[(index + chrom_index) % len(labels)]
            available = label is not None
            rows.append(
                [
                    f"rs{900000000 + chrom_index * 1000 + index}",
                    chromosome,
                    max(1, int(span * (index + 1) / (count + 1))),
                    calls[index % len(calls)] if chromosome not in {"Y", "MT"} else "A",
                    f"DEMO{chrom_index + 1}" if available else None,
                    label,
                    "Fictional annotation for exploring the interface. Not a biological finding."
                    if available else None,
                    "annotated" if available else "offline_cache_miss",
                    "synthetic" if available else "none",
                    "Synthetic example, not a real RSID interpretation."
                    if available else "Example of a missing annotation; no lookup was made.",
                ]
            )
    frame = pd.DataFrame(rows, columns=ANNOTATED_COLUMNS)
    frame.attrs["annotation_warnings"] = [
        "Demo only: every genotype, coordinate and annotation is fictional. "
        "The demo never queries MyVariant.info or writes to your cache."
    ]
    frame.attrs["offline"] = True
    return frame
