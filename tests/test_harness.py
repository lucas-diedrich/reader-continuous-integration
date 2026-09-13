"""Tests for the sweep's own logic: submission selection, classification and grouping.

These are the parts that decide what gets reported as a bug, so a mistake here silently
corrupts every report. Nothing in this file touches the network.
"""

from __future__ import annotations

import pytest

from pbreadertest.inventory import Submission, _acquisition_mode, select_latest
from pbreadertest.mapping import candidates_for
from pbreadertest.probe import _classify, _degeneracy_reason
from pbreadertest.report import _signature


def make_submission(**overrides) -> Submission:
    defaults = {
        "intermediate_hash": "abc123",
        "submission_id": "Tool_20250101_000000",
        "software_name": "DIA-NN",
        "software_version": "2.0",
        "module": "Results_quant_ion_DDA",
        "acquisition_mode": "DDA",
    }
    return Submission(**(defaults | overrides))


def healthy_metrics(**overrides) -> dict:
    defaults = {
        "n_obs": 6,
        "n_var": 5000,
        "x_dtype": "float64",
        "x_is_numeric": True,
        "nan_fraction": 0.2,
        "n_all_nan_features": 0,
    }
    return defaults | overrides


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("Results_quant_ion_DDA", "DDA"),
        ("Results_quant_ion_DDA_Astral", "DDA"),
        ("Results_quant_ion_DIA_Astral", "DIA"),
        ("Results_entrapment_ion_DIA_Astral", "DIA"),
        ("Results_something_else", "unknown"),
    ],
)
def test_acquisition_mode_from_module_name(module, expected):
    assert _acquisition_mode(module) == expected


def test_select_latest_keeps_newest_per_group():
    submissions = [
        make_submission(
            intermediate_hash="old", submission_id="DIA-NN_20250101_000000"
        ),
        make_submission(
            intermediate_hash="new", submission_id="DIA-NN_20260101_000000"
        ),
    ]

    selected = select_latest(submissions, ("software_name", "acquisition_mode"))

    assert [s.intermediate_hash for s in selected] == ["new"]


def test_select_latest_preserves_dda_when_newest_submission_is_dia():
    """The reason for grouping by mode: a tool's newest upload is often DIA."""
    submissions = [
        make_submission(
            intermediate_hash="dda",
            submission_id="MaxQuant_20250101_000000",
            acquisition_mode="DDA",
        ),
        make_submission(
            intermediate_hash="dia",
            submission_id="MaxQuant_20260101_000000",
            acquisition_mode="DIA",
        ),
    ]

    by_mode = select_latest(submissions, ("software_name", "acquisition_mode"))
    by_tool = select_latest(submissions, ("software_name",))

    assert {s.intermediate_hash for s in by_mode} == {"dda", "dia"}
    assert {s.intermediate_hash for s in by_tool} == {"dia"}


@pytest.mark.parametrize(
    ("metrics", "expected_fragment"),
    [
        (healthy_metrics(), None),
        (healthy_metrics(x_is_numeric=False, x_dtype="object"), "non-numeric"),
        (healthy_metrics(n_var=0), "empty matrix"),
        (healthy_metrics(n_obs=0), "empty matrix"),
        (healthy_metrics(nan_fraction=1.0), "missing values"),
        (healthy_metrics(n_obs=1), "sample(s) parsed"),
    ],
)
def test_degeneracy_detection(metrics, expected_fragment):
    reason = _degeneracy_reason(metrics)

    if expected_fragment is None:
        assert reason is None
    else:
        assert expected_fragment in reason


def test_non_numeric_matrix_outranks_other_degeneracies():
    """A non-numeric X is the most actionable signal and must not be masked."""
    metrics = healthy_metrics(
        x_is_numeric=False, x_dtype="object", n_obs=1, nan_fraction=1.0
    )

    assert "non-numeric" in _degeneracy_reason(metrics)


@pytest.mark.parametrize(
    ("predicted", "expected_status"),
    [(True, "ok"), (False, "unexpected_ok"), (None, "ok")],
)
def test_successful_parse_classification(predicted, expected_status):
    raw = {"outcome": "returned", **healthy_metrics()}

    status, _ = _classify(raw, predicted, "precursor_intensity")

    assert status == expected_status


@pytest.mark.parametrize(
    ("predicted", "expected_status"),
    [(True, "bug"), (False, "unsupported"), (None, "unsupported")],
)
def test_missing_level_column_classification(predicted, expected_status):
    """A KeyError naming the level's own intensity column means the level is absent."""
    raw = {
        "outcome": "raised",
        "error_type": "KeyError",
        "error_message": "'precursor_intensity'",
    }

    status, _ = _classify(raw, predicted, "precursor_intensity")

    assert status == expected_status


@pytest.mark.parametrize(
    ("error_type", "error_message"),
    [
        ("KeyError", "Index([None, 'PrecursorCharge'], dtype='object')"),
        ("KeyError", "None"),
        ("ValueError", "could not convert string to float"),
        ("Timeout", "reader did not finish within 1800s"),
    ],
)
@pytest.mark.parametrize("predicted", [True, False, None])
def test_reader_failures_are_not_excused_by_the_prediction(
    error_type, error_message, predicted
):
    """A reader that cannot read the file fails at every level.

    Bucketing that as "level unsupported" would hide a real incompatibility behind a
    prediction that only ever described level coverage.
    """
    raw = {
        "outcome": "raised",
        "error_type": error_type,
        "error_message": error_message,
    }

    status, _ = _classify(raw, predicted, "precursor_intensity")

    assert status == "load_failure"


def test_degenerate_result_is_suspicious_regardless_of_prediction():
    raw = {
        "outcome": "returned",
        **healthy_metrics(x_is_numeric=False, x_dtype="object"),
    }

    assert _classify(raw, True, "intensity")[0] == "suspicious"
    assert _classify(raw, False, "intensity")[0] == "suspicious"


def test_signature_groups_same_defect_across_submissions():
    """Paths and counts differ per submission; the defect identity must not."""
    first = {
        "reader": "maxquant",
        "level": "precursors",
        "status": "bug",
        "error_type": "KeyError",
        "error_message": "column missing in /tmp/aaa/input_file.txt after 1234 rows",
    }
    second = first | {
        "error_message": "column missing in /tmp/bbb/input_file.txt after 9876 rows"
    }

    assert _signature(first) == _signature(second)


def test_load_failures_collapse_across_levels():
    """One unreadable file is one defect, not one per level."""
    base = {
        "reader": "spectronaut",
        "status": "load_failure",
        "error_type": "KeyError",
        "error_message": "Index([None, 'PrecursorCharge'], dtype='object')",
    }
    signatures = {
        _signature(base | {"level": level})
        for level in ("proteins", "precursors", "peptides", "genes")
    }

    assert len(signatures) == 1


def test_signature_separates_different_levels():
    base = {
        "reader": "maxquant",
        "status": "bug",
        "error_type": "KeyError",
        "error_message": "x",
    }

    assert _signature(base | {"level": "precursors"}) != _signature(
        base | {"level": "genes"}
    )


@pytest.mark.parametrize(
    ("software", "extension", "expected_readers"),
    [
        ("AlphaDIA", ".parquet", ["alphadia_parquet"]),
        ("AlphaDIA", ".tsv", ["alphadia"]),
        ("Spectronaut", ".tsv", ["spectronaut", "spectronaut_report"]),
        ("DIA-NN", ".parquet", ["diann"]),
        ("PEAKS", ".tsv", []),
        ("MaxQuant", ".parquet", []),
    ],
)
def test_reader_selection_by_software_and_extension(
    software, extension, expected_readers
):
    candidates = candidates_for(software, extension)

    assert [c.reader for c in candidates] == expected_readers


def test_mismatched_input_tools_are_flagged():
    """Sage/FragPipe/AlphaPept are probed anyway, but must not pollute bug reports."""
    assert all(c.input_mismatch for c in candidates_for("Sage", ".tsv"))
    assert all(c.note for c in candidates_for("AlphaPept", ".txt"))
    assert not any(c.input_mismatch for c in candidates_for("DIA-NN", ".tsv"))


def test_missing_dataset_is_distinguished_from_failed_download(tmp_path, monkeypatch):
    """A transient transfer failure must not be recorded as a permanent upstream gap.

    Conflating the two silently shrinks coverage on every subsequent run, because the
    submission looks like one that never had data rather than one worth retrying.
    """
    from pbreadertest import probe as probe_module
    from pbreadertest.fetch import DOWNLOAD_FAILED, NOT_FOUND

    submission = make_submission(software_name="DIA-NN")

    for reason, expected_status in [
        (NOT_FOUND, "skipped_no_dataset"),
        (DOWNLOAD_FAILED, "download_failed"),
    ]:
        monkeypatch.setattr(
            probe_module, "ensure_zip", lambda *a, _r=reason, **k: (None, _r)
        )

        records = probe_module.probe_submission(submission, tmp_path)

        assert len(records) == 1
        assert records[0]["status"] == expected_status
