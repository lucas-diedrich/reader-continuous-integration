"""Map ProteoBench software names onto alphabase PSM readers and predict level support."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Feature levels accepted by ``alphapepttools.io.read_psm_table``.
LEVELS = ("proteins", "precursors", "peptides", "genes")


@dataclass(frozen=True)
class ReaderCandidate:
    """An alphabase reader worth trying on a given tool's uploaded file."""

    reader: str
    #: File extensions this reader can consume; the uploaded file's extension selects the variant.
    extensions: tuple[str, ...]
    #: True when ProteoBench stores a different file than the reader was written for.
    input_mismatch: bool = False
    note: str = ""


SOFTWARE_READERS: dict[str, tuple[ReaderCandidate, ...]] = {
    "MaxQuant": (ReaderCandidate("maxquant", (".txt",)),),
    "DIA-NN": (ReaderCandidate("diann", (".tsv", ".parquet")),),
    "FragPipe (DIA-NN quant)": (ReaderCandidate("diann", (".tsv", ".parquet")),),
    "AlphaDIA": (
        ReaderCandidate("alphadia", (".tsv",)),
        ReaderCandidate("alphadia_parquet", (".parquet",)),
    ),
    # Both Spectronaut readers are plausible for a Spectronaut report; try each.
    "Spectronaut": (
        ReaderCandidate("spectronaut", (".tsv",)),
        ReaderCandidate("spectronaut_report", (".tsv",)),
    ),
    "Sage": (
        ReaderCandidate(
            "sage_tsv",
            (".tsv",),
            input_mismatch=True,
            note="ProteoBench stores lfq.tsv; sage_tsv expects results.sage.tsv",
        ),
    ),
    "FragPipe": (
        ReaderCandidate(
            "msfragger_psm_tsv",
            (".tsv", ".txt"),
            input_mismatch=True,
            note="ProteoBench stores combined_ion.tsv; msfragger_psm_tsv expects psm.tsv",
        ),
    ),
    "AlphaPept": (
        ReaderCandidate(
            "alphapept",
            (".txt", ".csv"),
            input_mismatch=True,
            note="ProteoBench stores results.csv; alphapept expects .ms_data.hdf",
        ),
    ),
}

#: Tools ProteoBench collects that no alphabase reader addresses today.
NO_READER = {
    "PEAKS": "no alphabase reader",
    "i2MassChroQ": "no alphabase reader",
    "WOMBAT": "no alphabase reader",
    "MSAngel": "no alphabase reader",
    "ProlineStudio": "no alphabase reader",
    "quantms": "no alphabase reader",
    "MetaMorpheus": "no alphabase reader",
}

#: Readers registered with the provider but sharing another reader's psm_reader.yaml entry.
_YAML_ALIASES = {
    "alphadia_parquet": "alphadia",
    "sage_tsv": "sage",
    "sage_parquet": "sage",
}


def candidates_for(software_name: str, extension: str) -> list[ReaderCandidate]:
    """Readers to try for a tool, filtered to those that accept the uploaded file's extension."""
    return [c for c in SOFTWARE_READERS.get(software_name, ()) if extension in c.extensions]


def predicted_level_support(reader: str) -> dict[str, bool | None]:
    """Predict, per level, whether a reader can supply the required columns.

    Reads ``psm_reader.yaml``'s static column mapping. This is a heuristic, not ground
    truth: readers such as spectronaut translate columns dynamically at load time, so a
    level predicted unsupported may still succeed. Runs that contradict the prediction
    are reported rather than hidden.

    Returns ``None`` per level when no prediction can be made.
    """
    from alphabase.psm_reader.psm_reader import psm_reader_yaml
    from alphapepttools.io.reader_columns import (
        ALPHAPEPTTOOLS_FEATURE_ID_NAME,
        FEATURE_LEVEL_CONFIG,
    )

    config = psm_reader_yaml.get(_YAML_ALIASES.get(reader, reader), {})
    column_mapping = config.get("column_mapping") if isinstance(config, dict) else None

    if not column_mapping:
        return dict.fromkeys(LEVELS, None)

    support: dict[str, bool | None] = {}
    for level in LEVELS:
        level_config = FEATURE_LEVEL_CONFIG.get(level)
        if level_config is None:
            support[level] = None
            continue
        required = (
            level_config["intensity_column"],
            level_config[ALPHAPEPTTOOLS_FEATURE_ID_NAME],
            level_config["sample_id_column"],
        )
        support[level] = all(column in column_mapping for column in required)
    return support
