"""Turn probe records into a JSONL log and a grouped, human-readable bug report."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .mapping import LEVELS

#: Statuses that represent a defect in alphapepttools/alphabase rather than a known gap.
FINDING_STATUSES = ("bug", "suspicious", "load_failure")

STATUS_SYMBOL = {
    "ok": "OK",
    "unexpected_ok": "OK*",
    "suspicious": "SUSP",
    "bug": "BUG",
    "unsupported": "n/a",
    "load_failure": "LOAD",
}

_PATH_PATTERN = re.compile(r"(/[^\s'\"]+)+")
_NUMBER_PATTERN = re.compile(r"\b\d+\b")


def write_jsonl(records: list[dict], path: Path) -> Path:
    """Append-friendly structured log, one record per (submission, reader, level) trial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _signature(record: dict) -> tuple:
    """Collapse a failure to its identity so the same defect is reported once.

    Paths and counts are stripped: the same bug hit by ten submissions differs only in
    those, and should produce one report listing ten affected submissions.
    """
    message = record.get("error_message") or record.get("detail") or ""
    message = _PATH_PATTERN.sub("<path>", message)
    message = _NUMBER_PATTERN.sub("<n>", message)
    # A load failure happens before the level is consulted, so it is one defect, not one
    # per level; keying on level would report the same traceback four times.
    level = None if record.get("status") == "load_failure" else record.get("level")
    return (
        record.get("reader"),
        level,
        record.get("status"),
        record.get("error_type", "-"),
        " ".join(message.split())[:200],
    )


def _coverage_table(records: list[dict]) -> str:
    """Status of every (tool, acquisition mode, reader) against every level."""
    grid: dict[tuple, dict[str, str]] = defaultdict(dict)
    for record in records:
        if "level" not in record:
            continue
        key = (record["software_name"], record["acquisition_mode"], record["software_version"], record["reader"])
        symbol = STATUS_SYMBOL.get(record["status"], record["status"])
        if record.get("input_mismatch") and record["status"] != "ok":
            symbol = "mism"
        grid[key][record["level"]] = symbol

    lines = [
        "| Software | Mode | Version | Reader | " + " | ".join(LEVELS) + " |",
        "|---|---|---|---|" + "---|" * len(LEVELS),
    ]
    for (software, mode, version, reader), levels in sorted(grid.items()):
        cells = " | ".join(levels.get(level, "-") for level in LEVELS)
        lines.append(f"| {software} | {mode} | {version[:24]} | `{reader}` | {cells} |")
    return "\n".join(lines)


def _findings_section(records: list[dict]) -> str:
    """One section per distinct defect signature, listing every affected submission."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in records:
        # A mismatched input file is a known corpus limitation, not an alphapepttools defect.
        if record.get("status") in FINDING_STATUSES and not record.get("input_mismatch"):
            groups[_signature(record)].append(record)

    if not groups:
        return "No defects found.\n"

    blocks = []
    for index, (signature, hits) in enumerate(
        sorted(groups.items(), key=lambda item: -len(item[1])), start=1
    ):
        reader, level, status, error_type, message = signature
        scope = f"level `{level}`" if status != "load_failure" else "all levels"
        sample = hits[0]
        block = [
            f"### {index}. `{reader}` / {scope} - {status}: {error_type}",
            "",
            f"**Message:** `{message}`",
            "",
            f"**Affected submissions ({len(hits)}):**",
            "",
            "| Software | Version | Mode | Module | Result | Hash |",
            "|---|---|---|---|---|---|",
        ]
        for hit in hits:
            # Shape and dtype make a degenerate parse diagnosable without re-running it.
            shape = (
                f"{hit['n_obs']} x {hit['n_var']} ({hit.get('x_dtype', '?')})"
                if hit.get("n_obs") is not None
                else "raised"
            )
            block.append(
                f"| {hit['software_name']} | {hit['software_version']} | {hit['acquisition_mode']} "
                f"| {hit['module']} | {shape} | `{hit['intermediate_hash'][:12]}` |"
            )
        if sample.get("detail"):
            block += ["", f"**Detail:** {sample['detail']}"]
        if sample.get("traceback"):
            block += ["", "<details><summary>Traceback</summary>", "", "```", sample["traceback"].strip(), "```", "", "</details>"]
        blocks.append("\n".join(block))

    return "\n\n".join(blocks) + "\n"


#: Exceptions that name a level as unsupported in terms a user can act on.
_ACTIONABLE_ERRORS = ("ValueError", "NotImplementedError", "KeyError_is_not_actionable_placeholder")


def _usability_section(records: list[dict]) -> str:
    """Levels a reader legitimately cannot serve, judged on the quality of the error.

    An unsupported level is not a defect, but raising a bare ``KeyError`` naming an
    internal column the caller never mentioned is: the user asked for a level, and the
    error should say that level is unavailable for that reader.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in records:
        if record.get("status") == "unsupported" and not record.get("input_mismatch"):
            groups[(record.get("reader"), record.get("error_type"), record.get("error_message", "")[:120])].append(record)

    if not groups:
        return "None.\n"

    lines = ["| Reader | Levels | Error | Message | Actionable |", "|---|---|---|---|---|"]
    for (reader, error_type, message), hits in sorted(groups.items()):
        levels = ", ".join(sorted({h["level"] for h in hits}))
        actionable = "yes" if error_type in _ACTIONABLE_ERRORS else "**no**"
        lines.append(f"| `{reader}` | {levels} | `{error_type}` | `{message}` | {actionable} |")
    return "\n".join(lines) + "\n"


def _gaps_section(records: list[dict]) -> str:
    """Everything the sweep could not test, and why."""
    rows = []
    for record in records:
        status = record.get("status")
        if status in ("no_reader", "no_reader_for_extension", "skipped_no_dataset",
                      "skipped_no_input", "skipped_too_large"):
            rows.append((record["software_name"], record["acquisition_mode"], status, record.get("detail", "")))
        elif record.get("input_mismatch") and record.get("level") == LEVELS[0]:
            rows.append((record["software_name"], record["acquisition_mode"], "input_mismatch", record.get("mismatch_note", "")))

    if not rows:
        return "None.\n"

    lines = ["| Software | Mode | Reason | Detail |", "|---|---|---|---|"]
    lines += [f"| {s} | {m} | `{r}` | {d} |" for s, m, r, d in sorted(set(rows))]
    return "\n".join(lines) + "\n"


def build_markdown(records: list[dict]) -> str:
    """Assemble the full report."""
    versioned = next((r for r in records if "alphapepttools_version" in r), {})
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[record.get("status", "?")] += 1

    n_submissions = len({r["intermediate_hash"] for r in records})
    summary = " - ".join(f"{status}: {count}" for status, count in sorted(counts.items()))

    return f"""# read_psm_table regression report

Generated {datetime.now(timezone.utc).isoformat(timespec="seconds")}

- **alphapepttools** `{versioned.get("alphapepttools_version", "?")}`
- **alphabase** `{versioned.get("alphabase_version", "?")}`
- **Submissions probed:** {n_submissions}
- **Trials:** {len(records)} ({summary})

Legend: `OK` parsed - `OK*` parsed although unpredicted - `SUSP` parsed but degenerate -
`BUG` raised where support was predicted - `n/a` raised, no column mapping for this level -
`LOAD` reader could not read the file at all - `mism` reader was given a file it was not written for.

## Coverage

{_coverage_table(records)}

## Findings

{_findings_section(records)}

## Unsupported levels: error quality

{_usability_section(records)}

## Not covered

{_gaps_section(records)}
"""


def write_report(records: list[dict], directory: Path, *, stem: str | None = None) -> tuple[Path, Path]:
    """Write both the structured log and the markdown report."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = stem or datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    jsonl = write_jsonl(records, directory / f"{stem}.jsonl")
    markdown = directory / f"{stem}.md"
    markdown.write_text(build_markdown(records))
    return jsonl, markdown
