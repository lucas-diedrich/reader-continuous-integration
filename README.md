# Continuous testing of `read_psm_table` against ProteoBench submissions

Regression-tests `alphapepttools.io.read_psm_table` at every feature level against real,
user-produced search engine reports, using the public corpus of
[ProteoBench](https://proteobench.cubimed.rub.de) community submissions, and turns
failures into grouped bug reports.

## Why ProteoBench

236 public datapoints across 12 `Results_*` repositories, spanning 14 search engines in
both DDA and DIA, with genuine version drift. Every submission stores the **original,
unmodified** search engine report, keyed by a content-derived `intermediate_hash`:

```
https://proteobench.cubimed.rub.de/datasets/<hash>/<hash>_data.zip
```

All 236 were verified reachable; the URL is built deterministically from the hash rather
than scraped from the nginx autoindex.

## What it does

For each submission it downloads the dataset zip, extracts only `input_file.*` (and
`input_file_secondary.*` where present), and calls `read_psm_table` once per feature level
(`proteins`, `precursors`, `peptides`, `genes`) with the matching alphabase reader.

Each trial is classified against a prediction derived from `alphabase`'s
`psm_reader.yaml`: a level is *predicted supported* when the reader maps the intensity,
feature-id and sample-id columns that `FEATURE_LEVEL_CONFIG` requires.

| Status | Meaning |
|---|---|
| `ok` | Parsed, non-degenerate |
| `unexpected_ok` | Parsed although no column mapping was predicted (readers that translate columns dynamically) |
| `suspicious` | Returned without error, but the result is unusable: non-numeric `X`, empty matrix, all-missing, or a single sample from a multi-run benchmark |
| `bug` | `psm_reader.yaml` maps the level, but the column is absent after load |
| `load_failure` | The reader could not read the file at all; fails at every level |
| `unsupported` | Raised a `KeyError` naming the level's own intensity column, and no mapping exists |

`suspicious` is the class that matters most: a silent misparse is worse than a crash, and
nothing else catches it.

A failure is only `unsupported` when the error is exactly a `KeyError` naming that
level's intensity column. Any other exception means the reader failed before the level was
consulted, which is level independent and is reported as `load_failure` regardless of what
the prediction said — otherwise a genuine incompatibility hides behind a prediction that
only ever described level coverage.

`unsupported` is not a defect, but it is still reported — under *error quality*. A user who
asks for `level="precursors"` and gets `KeyError: 'precursor_intensity'` receives an
internal column name they never mentioned instead of a statement that the level is
unavailable for that reader.

## Design notes

- **Out-of-process probes.** Each (file, reader) pair runs in a subprocess, so a segfault,
  OOM or runaway parse cannot take down the sweep, and large DataFrames are reclaimed
  between submissions. All levels share one process because importing alphapepttools costs
  several seconds.
- **Cache zips, extract to temp.** Hashes are content-derived, so a cached zip never goes
  stale. Files reach 516 MB uncompressed, so extraction is temporary and deleted after each
  probe.
- **Latest per (tool, acquisition mode).** Taking the globally latest submission per tool
  skews to DIA — MaxQuant, DIA-NN, AlphaDIA and Spectronaut all have DIA as their most
  recent upload. Grouping by mode as well preserves DDA coverage at negligible cost.
- **404 is a skip; a failed transfer is not.** A missing dataset is a permanent gap
  (`skipped_no_dataset`); a transfer that dies part-way is `download_failed` and is retried
  three times. Conflating them would silently shrink coverage on every subsequent run.

## Reader coverage

ProteoBench collects *quantification*-level files; several alphabase readers target
*PSM*-level files. Where those disagree the reader is still attempted, but tagged
`input_mismatch` and excluded from bug reports — it documents the gap without adding noise.

| Tool | Reader | Fit |
|---|---|---|
| DIA-NN, FragPipe (DIA-NN quant) | `diann` | clean |
| MaxQuant | `maxquant` | clean |
| Spectronaut | `spectronaut`, `spectronaut_report` | clean |
| AlphaDIA | `alphadia`, `alphadia_parquet` | clean |
| Sage | `sage_tsv` | mismatch: stores `lfq.tsv`, reader wants `results.sage.tsv` |
| FragPipe | `msfragger_psm_tsv` | mismatch: stores `combined_ion.tsv`, reader wants `psm.tsv` |
| AlphaPept | `alphapept` | mismatch: stores `results.csv`, reader wants `.ms_data.hdf` |
| PEAKS, i2MassChroQ, WOMBAT, MSAngel, ProlineStudio, quantms | — | no alphabase reader |

For the mismatched readers, [ProteoRunners](https://github.com/Proteobench/ProteoRunners)
can generate the expected files in Docker. That is deliberately out of scope here.

## Usage

Requires an environment with `alphapepttools` and `alphabase` installed.

```
pip install -e .
```

```bash
export PYTHONPATH=src

# Default sweep: latest submission per tool per acquisition mode (17 submissions, ~0.64 GB)
python -m pbreadertest --stem nightly

# Single tool, for iterating
python -m pbreadertest --software MaxQuant --software DIA-NN

# Whole corpus (~15.6 GB of mappable submissions)
python -m pbreadertest --scope all --max-size-mb 300
```

| Flag | Purpose |
|---|---|
| `--scope` | `latest-per-tool-mode` (default), `latest-per-tool`, `all` |
| `--software` | Restrict to given software names, repeatable |
| `--levels` | Subset of feature levels |
| `--max-size-mb` | Skip datasets above this size (checked by HTTP HEAD, before download) |
| `--timeout` | Per file/reader timeout in seconds, default 1800 |
| `--limit` | Probe at most N submissions |
| `--refresh-inventory` | Re-download the `Results_*` repositories |

## Output

`results/<stem>.jsonl` — one record per (submission, reader, level) trial, carrying the
submission identity, outcome, matrix metrics, timing and the `alphapepttools`/`alphabase`
versions.

`results/<stem>.md` — coverage matrix, findings grouped by defect signature (so one bug hit
by ten submissions is one report listing ten submissions, not ten reports), error-quality
table, and everything that could not be tested with the reason why.

## Layout

```
pyproject.toml
src/pbreadertest/
  inventory.py   Results_* repos -> submission table; latest-per-group selection
  mapping.py     software name + file extension -> reader; level-support prediction
  fetch.py       deterministic zip URL, hash-keyed cache, temp extraction
  worker.py      subprocess: one file/reader across all levels
  probe.py       orchestration and outcome classification
  report.py      JSONL log and grouped markdown report
  cli.py         command line entry point
cache/           downloaded repo and dataset zips (gitignored)
results/         run logs and reports
```

## References

> Devreese, R. et al. ProteoBench: the community-curated platform for comparing proteomics data analysis workflows. 2025.12.09.692895 Preprint at https://doi.org/10.64898/2025.12.09.692895 (2025).

