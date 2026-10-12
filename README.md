# law_crawl

A crawler for the catalogue and full text of Chinese laws, regulations and ministry rules. It runs on GitHub
Actions on a schedule and publishes the data as assets of this repository's Release.

The code, logs and documentation are in English. The collected data is not translated: titles, texts and field
values such as category, issuer, region and validity status are stored in Chinese, exactly as the sources publish
them.

## Sources

| `source` | Source | What is collected | How |
| --- | --- | --- | --- |
| `flk` | National Database of Laws and Regulations (国家法律法规数据库, flk.npc.gov.cn) | The constitution, laws, administrative regulations, supervisory regulations, judicial interpretations and local regulations — about 31,000 documents | The whole catalogue is downloaded and diffed on every run; texts come from the official docx |
| `mee` | Ministry of Ecology and Environment (生态环境部) | Rules in force in the ministry's rule library | Paginated static list; text from the docx or the detail page |
| `mot` | Ministry of Transport (交通运输部) | Same | Same |
| `mem` | Ministry of Emergency Management (应急管理部) | Same | Same |
| `mohurd` | Ministry of Housing and Urban-Rural Development (住房和城乡建设部) | Same | The content endpoint called by the section page; text from the docx or the detail page |
| `samr` | State Administration for Market Regulation (市场监管总局) | Same, from its three rule sections | Same |
| `nea` | National Energy Administration (国家能源局) | The rules and normative documents linked from its "energy laws and regulations" page | The page itself; it gives neither publication date nor validity status, so the status is stored as `时效待核实` (unverified) |

Not collected: the rules of other ministries, and the rules of local governments.

## Data files

Published under the Release tagged `data` and overwritten after every crawl:

```
https://github.com/martian2035-dev/law_crawl/releases/download/data/laws.db
https://github.com/martian2035-dev/law_crawl/releases/download/data/laws_content.db
```

`manifest.json` in the repository describes the data after the latest crawl: snapshot date, row count per source,
number of documents with text, and the most recent changes.

### `laws.db` — catalogue (SQLite)

`laws`, one row per document:

| Column | Meaning |
| --- | --- |
| `id` | 32 hex digits. For `flk` it is the database's own id; for ministry rules it is the MD5 of the detail page address |
| `title`, `title_key` | Title, and the title normalised for searching (punctuation and whitespace removed, lower case) |
| `top_category` | `宪法` (constitution), `法律` (law), `行政法规` (administrative regulation), `监察法规` (supervisory regulation), `司法解释` (judicial interpretation), `地方法规` (local regulation), `部门规章` (ministry rule), `规范性文件` (normative document) |
| `category` | Finer category, as named by the source |
| `issuer`, `region` | Issuing body; province-level region for local regulations |
| `publish_date`, `effective_date` | ISO dates; empty when the source gives none |
| `status` | `有效` (in force), `尚未生效` (not yet in force), `已修改` (amended), `已废止` (repealed), `时效待核实` (unverified); empty when the source gives none |
| `is_listed` | `0` when the source no longer lists the document; such rows are kept, never deleted |
| `source` | Source key from the table above |
| `source_url` | Address of the official page, for ministry rules; empty for `flk` |

`law_changes` records every change a crawl detects. `change_type` is `added`, `status` (validity status changed),
`removed` (no longer listed) or `restored` (listed again); `old_value` and `new_value` hold the status before and
after; `detected_on` is the date of the crawl.

`meta` holds key-value notes: the snapshot date, the name of the national database, total row counts, and the
snapshot date and row count per ministry.

### `laws_content.db` — texts (SQLite)

- `contents`: one row per document. `body` is the zlib-compressed text, one paragraph per line (UTF-8);
  `paragraph_count` and `fetched_at` describe it.
- `unreadable`: documents whose text file cannot be parsed, for example when the source only offers a legacy
  `.doc`. `reason` says why, in Chinese.

Reading a text in Python:

```python
import sqlite3, zlib

connection = sqlite3.connect("laws_content.db")
body, = connection.execute("SELECT body FROM contents WHERE id = ?", (law_id,)).fetchone()
paragraphs = zlib.decompress(body).decode("utf-8").split("\n")
```

## Pipeline

- `.github/workflows/crawl.yml` ("Crawl laws and rules") runs every day at 20:30 Beijing time and can be started by
  hand from the Actions page. Steps: download the published data → probe the sources → diff the catalogue, fetch
  the missing texts and sync the ministry rules → publish the data → write back `manifest.json`. A run lasts up to
  about 4.5 hours and is cut into 45-minute chunks; the data is published after every chunk, so a cancelled job
  loses one chunk at most. Texts not fetched in one run are fetched in the next; once everything is fetched, a run
  only handles new documents.
- `.github/workflows/test.yml` ("Tests") runs the linter and the tests when the code changes.

To deploy a copy of this repository:

1. Create a public repository on GitHub and push this directory. The workflow declares the `contents: write`
   permission it needs to publish the Release and write back the manifest.
2. Start "Crawl laws and rules" by hand once and read the output of the "Probe data sources" step.

If you already have data from a local run, upload it first and the pipeline will continue from there:

```bash
gh release create data --title "Law data" --notes "Data files maintained by the pipeline"
gh release upload data /path/to/laws.db /path/to/laws_content.db --clobber
```

### Rate limiting

- The national database answers with a browser challenge page and refuses service when it is hit too often. The
  crawler stops as soon as it sees that page and does not continue that day; it neither retries nor works around
  the challenge. The delays between requests (1 second for the catalogue, 2 seconds for texts) and the rule of one
  crawl at a time exist to avoid the challenge. Do not raise the request rate.
- GitHub-hosted runners are outside mainland China. If "Probe data sources" reports a site as unreachable, that
  site is not open to them; switch to a self-hosted runner on a machine that can reach it by changing `runs-on` in
  `crawl.yml` to `self-hosted`.

## Running locally

Python 3.12 or later.

```bash
pip install -r requirements.txt
python -m law_crawl probe                 # can this network reach each source?
python -m law_crawl update --content-limit 200 --budget-seconds 600
python -m law_crawl manifest              # the data manifest
```

Data is written to `data/` (not committed); set `LAW_CRAWL_DATA_DIR` to put it elsewhere.

Tests:

```bash
pip install pytest ruff
python -m pytest -q
ruff check law_crawl tests
```

## Code layout

| File | Purpose |
| --- | --- |
| `law_crawl/sync.py` | Command entry point: catalogue diff, bulk text fetching, ministry rule sync, reachability probe, data manifest |
| `law_crawl/flk_client.py` | Client for the national database |
| `law_crawl/ministry_rules.py` | Fetching and parsing the ministry rule libraries; `SOURCES` lists the ministries covered |
| `law_crawl/docx_text.py`, `html_text.py` | Extracting text paragraphs from a docx or a detail page, and classifying headings and articles |
| `law_crawl/repository.py` | Reading and writing the two SQLite files |
| `law_crawl/service.py` | Read helpers: title search, document details |
| `scripts/crawl-loop.sh`, `release-data.sh` | The chunked crawl loop and the data publishing used by the pipeline |

Adding a ministry: when its rule library has the same structure as one already in `SOURCES`, add an entry;
otherwise write a new fetch function. Check locally with `python -m law_crawl ministries` that the number of rules
fetched matches the number the official site states.

## About the data

The texts are official documents published by the sources listed above; every row keeps what is needed to find its
official page, so it can be checked against the source. This repository does not translate the documents and makes
no claim about the legal validity of any of them — consult the official source before relying on one.
