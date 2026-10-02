# bioRake

A domain-tuned RAKE search engine for ranking large batches of medical / pharma
/ biopharma papers (PDF, DOCX, PPTX, Markdown, TXT) by relevance to a search
term, with a clean terminal UI.

For a quick walkthrough with terminal commands, see [USER_GUIDE.md](USER_GUIDE.md).

## 1\. Installation

Requires Python 3.9 or newer.

**Install the package** (this also installs the dependencies and adds a
`biorake` command to your environment):

```bash
git clone https://github.com/y-v-j/bioRake.git
cd bioRake
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .
```

Or install straight from GitHub without cloning:

```bash
pip install git+https://github.com/y-v-j/bioRake.git
```

**Dependencies only** (to run the script as `python biorake.py ...` without
installing the package):

```bash
pip install -r requirements.txt
```

The dependencies are `nltk`, `rich`, `pypdf`, `python-docx` and `python-pptx`.

**NLTK data** (one time, needs internet). bioRake tries to download this
automatically on first run; to do it yourself:

```bash
python -m nltk.downloader punkt punkt_tab stopwords
```

Check that it works:

```bash
biorake --help
```

## 2\. Index your papers

Point it at any folder — it recurses into subfolders and picks up `.pdf`, `.docx`,
`.pptx`, `.md`, `.markdown`, `.txt` (legacy `.doc`/`.ppt` files are picked up but
usually yield no text). Handles 200+ files fine; only new/changed files are
re-parsed on subsequent runs (tracked by modification time), so re-indexing a
daily-updated folder is fast.

```bash
biorake index /path/to/papers --session oncology_batch1
```
**Sessions keep folders separate.** Every document and every extracted phrase is
tagged with a `session_id` in SQLite. Indexing a different folder into a
different session name means the two never mix in search results — you can have
`oncology_batch1`, `cardiology_2026`, etc. all in the same database file without
cross-contamination. Omit `--session` and it defaults to the folder name.

```bash
biorake sessions   # see all sessions + doc counts
```
## 3\. Search + browse

```bash
biorake search --session oncology_batch1 "glomerulonephritis"
```
This opens an interactive, paginated table (20 results/page):

- **↑ / ↓** (or `k` / `j`) — move the highlight
- **Space** — expand/collapse a synopsis (first few lines) of the highlighted
  paper, right under the table
- **n** / **p** — next / previous page
- **q** — back out to search

Or just run the script with no arguments for a full menu (pick a session, index
a new folder, search, repeat):

```bash
biorake
```
## 4\. How ranking works

Each document gets a composite score built from every matching phrase it
contains, weighted by a relevance tier so that specificity wins:


|Match type|Example (query: "glomerulonephritis")|Weight|
|-|-|-|
|Exact term|doc contains "glomerulonephritis"|1000x|
|Compound/partial phrase|"acute glomerulonephritis"|300x|
|Known synonym|"gn"|150x|
|Same broad category (taxonomy)|"kidney stone" (both under `renal`)|50x + similarity bonus|
|Fuzzy fallback (spelling variants)|only used when results are thin|5x|

That's why a search for **glomerulonephritis** puts the paper that actually
discusses glomerulonephritis first, and a paper about **kidney stones** (same
`renal` taxonomy category) ranks ahead of unrelated papers. Within a tier,
documents are further ordered by their RAKE phrase score and mention frequency.

You can also search for a whole category by name, e.g.
`"hematologic oncology"` or `hematologic_oncology`.

## 5\. Built-in taxonomy, including the NCI cancer types

The taxonomy (`~/.biorake/taxonomy.json` by default) ships with these categories:

- **General medical:** `ocular`, `renal`, `cardiac`, `gastrointestinal`,
  `pharmacology`, `neurology`
- **Oncology:** `oncology` (general terms, metastatic/recurrent cancer, carcinoma
  of unknown primary), `hematologic_oncology`, `neuro_oncology`,
  `pediatric_oncology`, `thoracic_oncology`, `breast_oncology`,
  `gynecologic_oncology`, `genitourinary_oncology`, `gastrointestinal_oncology`,
  `head_neck_oncology`, `endocrine_oncology`, `skin_oncology`,
  `bone_soft_tissue_oncology`, `ocular_oncology`

Every cancer type in the National Cancer Institute (NCI) A–Z list of cancer
types at <https://www.cancer.gov/types> is included and grouped by subspecialty
(see *Data sources* below), so a search
for "rectal cancer" also brings up papers on other GI cancers rather than every
cancer paper. NCI's alternate forms are folded into one canonical term plus
synonyms:

- abbreviations: "Acute Myeloid Leukemia (AML)" → `acute myeloid leukemia` + `aml`
- inverted index entries: "Ependymoma, Childhood" → `ependymoma` + `childhood ependymoma`
- duplicate entries: "Kidney (Renal Cell) Cancer" / "Renal Cell (Kidney) Cancer" →
  `kidney cancer` + `renal cell cancer`

Terms, synonyms, document text and queries are all normalized the same way
(lowercased, accents folded, apostrophes dropped), so "Sézary syndrome",
"sezary syndrome" and "Hodgkin's lymphoma" / "hodgkins lymphoma" match each other.

If you already have a saved `taxonomy.json`, any new default terms are added to it
automatically the next time bioRake runs. Your own additions stay as they are.

## 6\. Adding your own terminology (e.g. industry cancer nomenclature)

Extend the taxonomy with an external file (JSON or CSV) without touching the
code:

```bash
biorake add-terminology custom_cancer_terminology_example.json
```
**JSON format:**

```json
{
  "oncology": {
    "her2-positive breast cancer": ["her2+ breast cancer"],
    "immunotherapy": ["checkpoint inhibitor"]
  }
}
```
**CSV format** (synonyms pipe-separated):

```csv
category,term,synonyms
oncology,her2-positive breast cancer,her2+ breast cancer|her2 positive breast carcinoma
```
New terms merge into existing categories (or create new ones) and are used by
every future index/search. Re-run `index` afterward so already-indexed
documents pick up the new taxonomy boost in their RAKE scores.

You can also merge in a one-off terminology file for a single indexing run
without touching your saved default:

```bash
biorake index /path/to/papers --taxonomy-file my_terms.csv
```
## 7\. Medical/pharma-specific tuning already built in

- **Hyphen/slash-safe tokenizer:** compound clinical notation like `IL-6`,
  `HER2/neu`, `beta-blocker`, `5-FU` stays intact instead of being shredded by
  generic punctuation splitting.
- **Journal boilerplate filtering:** words like `fig`, `doi`, `et al`, `abstract`,
  `methods` are stripped so they don't pollute extracted phrases.
- **Taxonomy-boosted RAKE scores:** any phrase that matches a known medical
  taxonomy term/synonym gets a 3x score boost at indexing time, so clinically
  meaningful phrases naturally outrank generic prose.
- **Synopsis extraction:** the first few clean sentences of each document are
  stored at index time so the spacebar-expand preview is instant (no re-parsing
  the file at query time).

## 8\. Data location

The database (`biorake.db`) and `taxonomy.json` live in `~/.biorake/`. Set
`BIORAKE_HOME` to keep them somewhere else, or pass `--db` to use a different
database file.

## Files

- `biorake.py`: the full engine + CLI
- `pyproject.toml`: package metadata (installs the `biorake` command)
- `requirements.txt`: pip dependencies
- `custom_cancer_terminology_example.json` / `.csv`: sample external
  terminology files showing the format for section 6
- `USER_GUIDE.md`: short walkthrough with terminal commands
- `LICENSE`: MIT license

## Data sources

The cancer type names in the built-in taxonomy come from the National Cancer
Institute's "Cancer Types" A–Z list, <https://www.cancer.gov/types> (National
Cancer Institute, U.S. National Institutes of Health). The names were grouped into
categories, and abbreviations and alternate forms were added as synonyms, for
use in bioRake. NCI does not endorse this project.

## License

bioRake is released under the [MIT License](LICENSE).
