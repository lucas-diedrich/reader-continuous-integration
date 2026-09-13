"""Download and unpack the original uploaded search engine reports from ProteoBench."""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DATASETS_BASE_URL = "https://proteobench.cubimed.rub.de/datasets"

#: Entry in the dataset zip holding the unmodified search engine report.
INPUT_FILE_STEM = "input_file"
#: Second input, used by tools that need one (e.g. AlphaDIA precursor + matrix).
SECONDARY_FILE_STEM = "input_file_secondary"


def dataset_url(intermediate_hash: str) -> str:
    """Deterministic zip URL for a submission.

    Built from the hash rather than scraped from the nginx autoindex, which is fragile.
    """
    return f"{DATASETS_BASE_URL}/{intermediate_hash}/{intermediate_hash}_data.zip"


#: Outcome of a fetch attempt, distinguishing a permanent gap from a transient failure.
NOT_FOUND = "not_found"
DOWNLOAD_FAILED = "download_failed"


def ensure_zip(
    intermediate_hash: str,
    cache_dir: Path,
    *,
    timeout: int = 600,
    attempts: int = 3,
) -> tuple[Path | None, str]:
    """Return the cached dataset zip, downloading it if absent.

    Returns ``(path, "")`` on success, ``(None, NOT_FOUND)`` when the dataset genuinely
    does not exist upstream, and ``(None, DOWNLOAD_FAILED)`` when the transfer failed.
    The two must not be conflated: a transient failure on a large file would otherwise be
    recorded as a permanent upstream gap and silently erode coverage on every run.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    local = cache_dir / f"{intermediate_hash}_data.zip"

    # Hashes are content-derived, so a cached zip never goes stale.
    if local.exists() and zipfile.is_zipfile(local):
        return local, ""

    url = dataset_url(intermediate_hash)
    partial = local.with_suffix(".part")

    for attempt in range(1, attempts + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                if response.status_code == 404:
                    logger.info("No dataset stored for %s", intermediate_hash)
                    return None, NOT_FOUND
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        handle.write(chunk)
        except requests.RequestException as exc:
            logger.warning("Download attempt %d/%d failed for %s: %s", attempt, attempts, intermediate_hash, exc)
            partial.unlink(missing_ok=True)
            continue

        if not zipfile.is_zipfile(partial):
            logger.warning("Attempt %d/%d for %s produced a truncated payload", attempt, attempts, intermediate_hash)
            partial.unlink(missing_ok=True)
            continue

        partial.replace(local)
        return local, ""

    return None, DOWNLOAD_FAILED


def remote_size(intermediate_hash: str, *, timeout: int = 30) -> int | None:
    """Size of the dataset zip without downloading it, for pre-flight size limits."""
    try:
        response = requests.head(dataset_url(intermediate_hash), timeout=timeout, allow_redirects=True)
        if response.status_code != 200:
            return None
        return int(response.headers["Content-Length"])
    except (requests.RequestException, KeyError, ValueError):
        return None


@contextlib.contextmanager
def extracted_inputs(zip_path: Path) -> Iterator[dict[str, Path]]:
    """Extract the input report(s) to a temporary directory, removed on exit.

    Only the input files are extracted; ``result_performance.csv`` and the parameter
    files can be several hundred megabytes and are not needed to exercise the reader.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="pbreadertest_"))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            inputs: dict[str, Path] = {}
            for name in archive.namelist():
                stem = Path(name).stem
                if stem not in (INPUT_FILE_STEM, SECONDARY_FILE_STEM):
                    continue
                archive.extract(name, tmpdir)
                inputs[stem] = tmpdir / name
        yield inputs
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
