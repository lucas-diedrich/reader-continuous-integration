# Continuous testing of AlphaBase readers against ProteoBench submissions

## Goal

Continuously regression-test every feature level in alphapepttools.io.read_psm_table reader against real-world search engine reports, using the growing corpus of community submissions held by [ProteoBench](https://proteobench.cubimed.rub.de) and report potential bugs etc. 


Most important search engines:

- [ ] AlphaDIA
- [ ] Spectronaut
- [ ] Dia-NN
- [ ] MSFragger/FragPipe

For DDA + DIA data. 

## Feasibility verdict

**Yes — the original uploaded search engine reports are publicly downloadable**, with no
authentication, and the `proteobench` Python package already ships helpers to fetch them in
bulk. The limiting factor is not access; it is that ProteoBench asks submitters for
*quantification-level* files while most AlphaBase readers target *PSM/precursor-level* files.
Five readers map cleanly; the rest do not.

---

## 1. Where the data lives

ProteoBench splits every submission across two locations.

### Metrics and parameters — GitHub

One JSON per submission, in 12 `Proteobench/Results_*` repositories (one per module).
Small and cheap to clone (~1 MB each). Each JSON carries `software_name`,
`software_version`, all search settings, the computed metrics, and an `intermediate_hash`.

### Uploaded files — static server, keyed by hash

```
https://proteobench.cubimed.rub.de/datasets/<intermediate_hash>/<intermediate_hash>_data.zip
```

Written by `write_intermediate_raw()` in `proteobench/modules/*/`. Contents:

| Entry | Meaning |
|---|---|
| `input_file.<ext>` | **the original, unmodified search engine report** |
| `input_file_secondary.<ext>` | second file where the tool needs one (AlphaDIA) |
| `result_performance.csv` | ProteoBench's own parse of that file |
| `param_<i>.<ext>` | the tool's parameter / config file |
| `comment.txt` | submitter comment |

The same files are exposed in the web app under *"Download all data from a submitted workflow"*.

---

## 2. Programmatic access

`proteobench.utils.server_io` already implements the fetch:

```python
from proteobench.utils.server_io import get_merged_json, get_raw_data

df = get_merged_json(
    "https://github.com/Proteobench/Results_quant_ion_DDA/archive/refs/heads/main.zip"
)
paths = get_raw_data(df)          # -> {intermediate_hash: extracted_dir}
```
Code is locally available in `PreteoBench/`


There is also `dataset_folder_exists(hash)` for pre-flight checks.

**Recommendation:** do not use `get_raw_data` as-is in CI. It scrapes the Apache/nginx
autoindex listing with BeautifulSoup, which is fragile. The zip path is fully deterministic
from the hash, so construct the URL directly from the GitHub JSONs and skip the listing
entirely.

---

## 3. Corpus size

243 public datapoints across 12 modules, every one carrying an `intermediate_hash`.

| Tool | Submissions | Modules |
|---|---|---|
| DIA-NN | 92 | 8 |
| AlphaDIA | 27 | 6 |
| FragPipe (DIA-NN quant) | 21 | 5 |
| Spectronaut | 19 | 4 |
| MaxQuant | 19 | 4 |
| PEAKS | 19 | 6 |
| i2MassChroQ | 13 | 2 |
| FragPipe | 9 | 2 |
| WOMBAT | 8 | 2 |
| AlphaPept | 4 | 1 |
| Sage | 2 | 2 |
| de novo tools, MSAngel, ProlineStudio, quantms | 1 each | 1 |

---

## 4. Reader coverage

ProteoBench's expected upload vs. `alphabase.psm_reader.psm_reader_provider`:

| ProteoBench tool | Stored file | AlphaBase reader | Fit |
|---|---|---|---|
| DIA-NN | `report.tsv` / `report.parquet` | `diann` | ✅ |
| FragPipe (DIA-NN quant) | `report.tsv` | `diann` | ✅ |
| MaxQuant | `evidence.txt` | `maxquant` | ✅ |
| Spectronaut | `*_Report.tsv` | `spectronaut`, `spectronaut_report` | ✅ |
| AlphaDIA | `precursors.tsv` (+ matrix) / `precursors.parquet` | `alphadia`, `alphadia_parquet` | ✅ |
| Sage | `lfq.tsv` | `sage_tsv` wants `results.sage.tsv` | ❌ wrong file |
| FragPipe | `combined_ion.tsv` | `msfragger_*` want `psm.tsv` / pepXML | ❌ wrong file |
| AlphaPept | `results.csv` | `alphapept` wants `.ms_data.hdf` | ❌ wrong file |
| MetaMorpheus | `AllQuantifiedPeaks.tsv` | — | ❌ |
| PEAKS, i2MassChroQ, WOMBAT, MSAngel, ProlineStudio, quantms, MSAID | various | — | ❌ |
| de novo tools (Casanovo, InstaNovo, …) | various | — | ❌ |

Roughly 160 of the 243 submissions are usable, covering five readers with genuine
version drift and real user-produced files.

Not covered by any AlphaBase reader today: `pfind`, `openswath`, `swath`, `speclib_tsv`,
`msfragger_pepxml`, `msfragger_psm_tsv`, `sage_parquet`.

---

## 5. Proposed pipeline

1. **Nightly:** clone the 12 `Results_*` repos (shallow, ~1 MB each) and merge the JSONs
   into one table of `(intermediate_hash, software_name, software_version, module)`.
2. **Filter** to the five mappable tools.
3. **Diff against a local cache.** Hashes are content-derived and stable, so only genuinely
   new submissions need downloading. This keeps CI off the ProteoBench server.
4. **Download** `<hash>_data.zip` by deterministic URL; skip (do not fail) on 404, since
   older submissions predate the storage feature.
5. **Run the matching AlphaBase reader** on `input_file.<ext>`, passing
   `input_file_secondary` where present.
6. Check that parsing works across all feature levels (proteins, precursors, genes). 

## 7. Risks and caveats

| Risk | Mitigation |
|---|---|
| Autoindex scraping breaks | Build zip URLs deterministically from hashes |
| Missing dataset folders for older hashes | Treat 404 as skip, not failure |
| Server load / flaky CI | Local hash-keyed cache; only fetch new hashes |
| Licensing of third-party submissions | Fetch at CI time or cache privately; check submission terms before vendoring files into a public test repo |
| AlphaDIA two-file inputs | Handle `input_file_secondary` explicitly |
| Hash definition changes upstream | Hash derives from intermediate content and formatting; existing hashes are stable, but pin expectations to hashes already seen |

*Not yet verified end-to-end:* an actual download from `cubimed.rub.de` — the mechanism was
confirmed from source and documentation only. Run one manual `curl` before building on it.

---

## 8. Complement: ProteoRunners

For the readers ProteoBench cannot feed
([Sage](https://github.com/Proteobench/ProteoRunners), FragPipe/MSFragger, AlphaPept,
MetaMorpheus), [ProteoRunners](https://github.com/Proteobench/ProteoRunners) — a Nextflow
pipeline from the same organisation — runs DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant and
MetaMorpheus in Docker on ProteoBench benchmark data. It produces complete output
directories, including `results.sage.tsv` and `psm.tsv`, at the cost of compute.

**Focus on breadth of acquistion modes and search engines**
- DDA AND DIA
- As many search engines as possible.
suggested data: Results_quant_ion_DDA_Astral, Results_quant_ion_DIA_Astral

**Suggested split:**

- **Nightly, cheap:** ProteoBench downloads → `diann`, `maxquant`, `spectronaut`, `alphadia`, and their parquet variants.
- **Weekly or on release, expensive:** ProteoRunners → `sage_tsv`, `sage_parquet`,
  `msfragger_psm_tsv`, `msfragger_pepxml`, `alphapept`.

---

## References

- Web app: <https://proteobench.cubimed.rub.de>
- Source: <https://github.com/Proteobench/ProteoBench>
- Docs: <https://proteobench.readthedocs.io>
- Results repositories: <https://github.com/orgs/Proteobench/repositories>
- ProteoRunners: <https://github.com/Proteobench/ProteoRunners>
- Relevant source files: `proteobench/utils/server_io.py`,
  `proteobench/modules/quant/quant_base_module.py` (`write_intermediate_raw`),
  `proteobench/io/parsing/io_parse_settings/parse_settings_files.toml`