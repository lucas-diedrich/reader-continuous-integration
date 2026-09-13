"""Run ``read_psm_table`` across submissions, readers and levels, and classify the outcome."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .fetch import (
    DOWNLOAD_FAILED,
    INPUT_FILE_STEM,
    NOT_FOUND,
    SECONDARY_FILE_STEM,
    ensure_zip,
    extracted_inputs,
    remote_size,
)
from .inventory import Submission
from .mapping import (
    LEVELS,
    NO_READER,
    ReaderCandidate,
    candidates_for,
    predicted_level_support,
)

logger = logging.getLogger(__name__)

#: A parse yielding fewer samples than this is almost certainly wrong: every ProteoBench
#: module is a multi-run benchmark.
MIN_EXPECTED_SAMPLES = 2
#: Above this share of missing values the matrix carries no usable signal.
MAX_ACCEPTABLE_NAN_FRACTION = 0.999


def _level_intensity_column(level: str) -> str | None:
    """The alphabase column a level needs, used to tell a missing level from a broken read."""
    from alphapepttools.io.reader_columns import FEATURE_LEVEL_CONFIG

    config = FEATURE_LEVEL_CONFIG.get(level)
    return config["intensity_column"] if config else None


def _degeneracy_reason(metrics: dict) -> str | None:
    """Why a successful parse should still be treated as a finding, if at all."""
    # Most severe first: a non-numeric X means downstream analysis is impossible even
    # though the call returned an AnnData of plausible shape.
    if not metrics.get("x_is_numeric", True):
        return f"intensity matrix is non-numeric (dtype={metrics.get('x_dtype')})"
    if metrics["n_obs"] == 0 or metrics["n_var"] == 0:
        return f"empty matrix ({metrics['n_obs']} x {metrics['n_var']})"
    if metrics["nan_fraction"] >= MAX_ACCEPTABLE_NAN_FRACTION:
        return f"matrix is {metrics['nan_fraction']:.1%} missing values"
    if metrics["n_obs"] < MIN_EXPECTED_SAMPLES:
        return f"only {metrics['n_obs']} sample(s) parsed from a multi-run benchmark"
    return None


def _is_missing_level_column(raw: dict, level_column: str | None) -> bool:
    """Whether the failure is exactly "this reader has no data for this level".

    Such a failure is a ``KeyError`` naming the level's own intensity column. Any other
    exception means the reader could not handle the file at all, which is level
    independent and must not be excused by a level-support prediction.
    """
    if level_column is None or raw.get("error_type") != "KeyError":
        return False
    return (raw.get("error_message") or "").strip().strip("\"'") == level_column


def _classify(raw: dict, predicted: bool | None, level_column: str | None = None) -> tuple[str, str | None]:
    """Assign a status to one level outcome, given what psm_reader.yaml predicted."""
    if raw["outcome"] == "returned":
        reason = _degeneracy_reason(raw)
        if reason is not None:
            return "suspicious", reason
        if predicted is False:
            return "unexpected_ok", "succeeded although psm_reader.yaml maps no such column"
        return "ok", None

    if _is_missing_level_column(raw, level_column):
        if predicted is True:
            return "bug", "psm_reader.yaml maps this level, but the column is absent after load"
        return "unsupported", None

    # The reader did not merely lack this level; it could not read the file.
    return "load_failure", "reader raised before the requested level could be resolved"


def _run_worker(file_paths: list[Path], reader: str, levels: tuple[str, ...], timeout: int) -> list[dict]:
    """Execute one file/reader combination out of process."""
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "spec.json"
        out_path = Path(tmp) / "out.json"
        spec_path.write_text(
            json.dumps({"file_paths": [str(p) for p in file_paths], "reader": reader, "levels": list(levels)})
        )

        command = [sys.executable, "-m", "pbreadertest.worker", "--spec", str(spec_path), "--out", str(out_path)]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return [
                {"level": level, "outcome": "raised", "error_type": "Timeout",
                 "error_message": f"reader did not finish within {timeout}s", "traceback": ""}
                for level in levels
            ]

        if out_path.exists():
            return json.loads(out_path.read_text())

        # No output file means the interpreter itself died (segfault, OOM kill).
        stderr = completed.stderr.decode(errors="replace")[-2000:]
        return [
            {"level": level, "outcome": "raised", "error_type": "WorkerCrash",
             "error_message": f"worker exited with code {completed.returncode}", "traceback": stderr}
            for level in levels
        ]


def probe_submission(
    submission: Submission,
    cache_dir: Path,
    *,
    levels: tuple[str, ...] = LEVELS,
    timeout: int = 1800,
    max_size_bytes: int | None = None,
) -> list[dict]:
    """Fetch one submission and probe every applicable reader at every level."""
    base = submission.as_dict() | {"probed_at": datetime.now(timezone.utc).isoformat()}

    if submission.software_name in NO_READER:
        return [base | {"status": "no_reader", "detail": NO_READER[submission.software_name]}]

    if max_size_bytes is not None:
        size = remote_size(submission.intermediate_hash)
        if size is not None and size > max_size_bytes:
            return [base | {"status": "skipped_too_large", "detail": f"{size / 1e6:.0f} MB exceeds limit", "zip_size": size}]

    zip_path, reason = ensure_zip(submission.intermediate_hash, cache_dir / "datasets")
    if zip_path is None:
        status = "skipped_no_dataset" if reason == NOT_FOUND else "download_failed"
        detail = (
            "no dataset zip stored upstream"
            if reason == NOT_FOUND
            else "download did not complete; the dataset exists and should be retried"
        )
        return [base | {"status": status, "detail": detail, "fetch_reason": reason or DOWNLOAD_FAILED}]

    base["zip_size"] = zip_path.stat().st_size
    records: list[dict] = []

    with extracted_inputs(zip_path) as inputs:
        primary = inputs.get(INPUT_FILE_STEM)
        if primary is None:
            return [base | {"status": "skipped_no_input", "detail": f"no {INPUT_FILE_STEM}.* in dataset zip"}]

        extension = primary.suffix.lower()
        file_paths = [primary]
        if SECONDARY_FILE_STEM in inputs:
            file_paths.append(inputs[SECONDARY_FILE_STEM])

        base |= {"input_extension": extension, "n_input_files": len(file_paths)}

        candidates: list[ReaderCandidate] = candidates_for(submission.software_name, extension)
        if not candidates:
            return [base | {"status": "no_reader_for_extension",
                            "detail": f"no reader mapped for {submission.software_name} with {extension}"}]

        for candidate in candidates:
            logger.info(
                "Probing %s %s (%s) with reader %s",
                submission.software_name, submission.software_version, extension, candidate.reader,
            )
            predictions = predicted_level_support(candidate.reader)
            for raw in _run_worker(file_paths, candidate.reader, levels, timeout):
                status, detail = _classify(
                    raw, predictions.get(raw["level"]), _level_intensity_column(raw["level"])
                )
                records.append(
                    base
                    | raw
                    | {
                        "reader": candidate.reader,
                        "input_mismatch": candidate.input_mismatch,
                        "mismatch_note": candidate.note,
                        "predicted_supported": predictions.get(raw["level"]),
                        "status": status,
                        "detail": detail,
                    }
                )

    return records
