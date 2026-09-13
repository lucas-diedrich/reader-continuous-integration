"""Build the ProteoBench submission inventory from the public ``Results_*`` repositories.

Each submission is one JSON file named after its ``intermediate_hash``, which is also
the key under which the original uploaded search engine report is stored.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GITHUB_ARCHIVE_URL = "https://github.com/Proteobench/{repo}/archive/refs/heads/main.zip"

RESULTS_REPOS = (
    "Results_quant_ion_DDA",
    "Results_quant_ion_DDA_Astral",
    "Results_quant_ion_DIA_Astral",
    "Results_quant_ion_DIA_AIF",
    "Results_quant_ion_DIA_diaPASEF",
    "Results_quant_ion_DIA_lowinput",
    "Results_quant_ion_DIA_plasma",
    "Results_quant_ion_DIA_ZenoTOF",
    "Results_quant_peptidoform_DDA",
    "Results_quant_peptidoform_DIA",
    "Results_quant_proteingroup_DIA_Astral",
    "Results_entrapment_ion_DIA_Astral",
)


@dataclass(frozen=True)
class Submission:
    """One ProteoBench datapoint and the uploaded report behind it."""

    intermediate_hash: str
    submission_id: str
    software_name: str
    software_version: str
    module: str
    acquisition_mode: str

    def as_dict(self) -> dict:
        return asdict(self)


def _acquisition_mode(module: str) -> str:
    if "DDA" in module:
        return "DDA"
    if "DIA" in module:
        return "DIA"
    return "unknown"


def _fetch_repo_zip(repo: str, cache_dir: Path, refresh: bool) -> bytes | None:
    """Return the repo archive bytes, caching on disk. ``None`` if the repo is unavailable."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    local = cache_dir / f"{repo}.zip"

    if local.exists() and not refresh:
        return local.read_bytes()

    url = GITHUB_ARCHIVE_URL.format(repo=repo)
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Could not fetch %s: %s", repo, exc)
        return None

    # Empty repositories return a valid response that is not a zip.
    if not response.content.startswith(b"PK"):
        logger.warning("Repository %s has no downloadable main branch", repo)
        return None

    local.write_bytes(response.content)
    return response.content


def fetch_inventory(cache_dir: Path, *, refresh: bool = False) -> list[Submission]:
    """Collect every datapoint across all ``Results_*`` repositories."""
    submissions: list[Submission] = []

    for repo in RESULTS_REPOS:
        payload = _fetch_repo_zip(repo, cache_dir / "repos", refresh)
        if payload is None:
            continue

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                if not name.endswith(".json"):
                    continue
                try:
                    record = json.loads(archive.read(name))
                except json.JSONDecodeError:
                    logger.warning("Malformed JSON %s in %s", name, repo)
                    continue
                if not isinstance(record, dict) or "intermediate_hash" not in record:
                    continue

                submissions.append(
                    Submission(
                        intermediate_hash=record["intermediate_hash"],
                        submission_id=record.get("id") or "",
                        software_name=record.get("software_name") or "unknown",
                        software_version=str(record.get("software_version")),
                        module=repo,
                        acquisition_mode=_acquisition_mode(repo),
                    )
                )

    logger.info("Collected %d submissions across %d repositories", len(submissions), len(RESULTS_REPOS))
    return submissions


def select_latest(
    submissions: list[Submission],
    group_by: tuple[str, ...] = ("software_name", "acquisition_mode"),
) -> list[Submission]:
    """Keep the most recent submission per group.

    ``submission_id`` is ``Tool_YYYYMMDD_HHMMSS``, so lexical order is chronological.
    Grouping by acquisition mode as well as software matters: for several tools the
    globally latest submission is DIA, which would silently drop DDA coverage.
    """
    latest: dict[tuple, Submission] = {}
    for submission in submissions:
        key = tuple(getattr(submission, field) for field in group_by)
        if key not in latest or submission.submission_id > latest[key].submission_id:
            latest[key] = submission
    return sorted(latest.values(), key=lambda s: (s.software_name, s.acquisition_mode))
