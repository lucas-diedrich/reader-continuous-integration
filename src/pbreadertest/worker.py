"""Subprocess entry point: run ``read_psm_table`` for one file/reader across all levels.

Runs out of process so that a segfault, OOM or runaway parse in one reader cannot take
down the whole sweep, and so that large intermediate DataFrames are reclaimed between
submissions. All levels for one file share a process because importing alphapepttools
costs several seconds and re-parsing per level is already the expensive part.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path


def _quality_metrics(adata) -> dict:
    """Shape, dtype and sparsity of the parsed matrix, used to spot silent misparses.

    A non-numeric ``X`` is treated as data rather than as an error: the reader returned
    successfully, so the defect is that it returned something unusable.
    """
    import numpy as np
    import pandas as pd

    n_obs, n_var = int(adata.n_obs), int(adata.n_vars)
    values = np.asarray(adata.X)
    is_numeric = bool(np.issubdtype(values.dtype, np.number))

    if not is_numeric:
        coerced = pd.to_numeric(pd.Series(values.ravel()), errors="coerce").to_numpy(dtype=float)
        values = coerced.reshape(n_obs, n_var) if n_obs * n_var else coerced
    values = values.astype(float, copy=False)

    missing = np.isnan(values)
    return {
        "n_obs": n_obs,
        "n_var": n_var,
        "x_dtype": str(np.asarray(adata.X).dtype),
        "x_is_numeric": is_numeric,
        "nan_fraction": float(missing.mean()) if values.size else 1.0,
        "n_all_nan_features": int(missing.all(axis=0).sum()) if values.size else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text())
    results = []

    import alphabase
    import alphapepttools as at

    versions = {
        "alphapepttools_version": at.__version__,
        "alphabase_version": alphabase.__version__,
    }

    file_paths = spec["file_paths"]

    for level in spec["levels"]:
        record = {"level": level, **versions}
        started = time.perf_counter()
        try:
            adata = at.io.read_psm_table(
                file_paths if len(file_paths) > 1 else file_paths[0],
                search_engine=spec["reader"],
                level=level,
            )
        except BaseException as exc:  # noqa: BLE001 - any failure mode is a finding
            record.update(
                outcome="raised",
                error_type=type(exc).__name__,
                error_message=str(exc)[:2000],
                traceback=traceback.format_exc()[-4000:],
            )
        else:
            try:
                record.update(outcome="returned", **_quality_metrics(adata))
            except BaseException as exc:  # noqa: BLE001 - inspecting the result must not abort the sweep
                record.update(
                    outcome="returned",
                    metrics_error=f"{type(exc).__name__}: {exc}",
                    n_obs=int(adata.n_obs),
                    n_var=int(adata.n_vars),
                    x_is_numeric=False,
                    x_dtype="unknown",
                    nan_fraction=0.0,
                    n_all_nan_features=0,
                )
            del adata
        record["duration_s"] = round(time.perf_counter() - started, 2)
        results.append(record)

    args.out.write_text(json.dumps(results))


if __name__ == "__main__":
    main()
