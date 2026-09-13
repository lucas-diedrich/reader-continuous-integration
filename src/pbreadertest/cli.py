"""Command line entry point for the regression sweep."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .inventory import fetch_inventory, select_latest
from .mapping import LEVELS
from .probe import probe_submission
from .report import write_report

logger = logging.getLogger("pbreadertest")

# Relative to the working directory, not to __file__: once the package is installed the
# module lives in site-packages, where a derived path would put caches somewhere unexpected.
DEFAULT_CACHE_DIR = Path("cache")
DEFAULT_RESULTS_DIR = Path("results")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--scope",
        choices=("latest-per-tool-mode", "latest-per-tool", "all"),
        default="latest-per-tool-mode",
        help="Which submissions to probe. The default keeps one submission per tool per "
        "acquisition mode, which preserves DDA coverage for tools whose newest upload is DIA.",
    )
    parser.add_argument("--software", action="append", help="Restrict to these software names (repeatable)")
    parser.add_argument("--levels", nargs="+", default=list(LEVELS), choices=list(LEVELS))
    parser.add_argument("--max-size-mb", type=float, default=None, help="Skip datasets larger than this")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-file/reader timeout in seconds")
    parser.add_argument("--limit", type=int, default=None, help="Probe at most this many submissions")
    parser.add_argument("--refresh-inventory", action="store_true")
    parser.add_argument("--stem", default=None, help="Base name for the output files")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    submissions = fetch_inventory(args.cache_dir, refresh=args.refresh_inventory)
    if args.scope == "latest-per-tool-mode":
        submissions = select_latest(submissions, ("software_name", "acquisition_mode"))
    elif args.scope == "latest-per-tool":
        submissions = select_latest(submissions, ("software_name",))

    if args.software:
        wanted = set(args.software)
        submissions = [s for s in submissions if s.software_name in wanted]
    if args.limit:
        submissions = submissions[: args.limit]

    logger.info("Probing %d submissions at levels %s", len(submissions), args.levels)

    max_size = int(args.max_size_mb * 1e6) if args.max_size_mb else None
    records: list[dict] = []
    for index, submission in enumerate(submissions, start=1):
        logger.info(
            "[%d/%d] %s %s (%s)",
            index, len(submissions), submission.software_name,
            submission.software_version, submission.acquisition_mode,
        )
        try:
            records.extend(
                probe_submission(
                    submission,
                    args.cache_dir,
                    levels=tuple(args.levels),
                    timeout=args.timeout,
                    max_size_bytes=max_size,
                )
            )
        except Exception:
            # The sweep must survive an unexpected failure in its own plumbing.
            logger.exception("Harness error while probing %s", submission.intermediate_hash)
            records.append(submission.as_dict() | {"status": "harness_error"})

    jsonl, markdown = write_report(records, args.results_dir, stem=args.stem)
    logger.info("Wrote %s and %s", jsonl, markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
