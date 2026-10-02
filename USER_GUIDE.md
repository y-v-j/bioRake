# bioRake User Guide

bioRake indexes a folder of biomedical documents (PDF, DOCX, PPTX, Markdown,
TXT) and ranks them against a search term using a domain-tuned RAKE keyword
scorer and a built-in medical taxonomy. The taxonomy covers every NCI cancer type.

## 1. Install

```bash
git clone https://github.com/y-v-j/bioRake.git
cd bioRake
python -m venv .venv && source .venv/bin/activate
pip install .                                         # installs dependencies + the biorake command
python -m nltk.downloader punkt punkt_tab stopwords   # one time only
```

Check the install:

```bash
biorake --help
```

Without installing, you can run `pip install -r requirements.txt` and then use
`python biorake.py` wherever this guide says `biorake`.

## 2. Index a folder of papers

Each folder is indexed into a named **session**. Results from different sessions
are never mixed.

```bash
biorake index ~/papers/leukemia --session leukemia_2026
```

```text
Indexing '/home/me/papers/leukemia' as session 'leukemia_2026'...
Indexing ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 6/6 files 0:00:00
Done. 6 file(s) indexed/updated, 0 unchanged skipped.
```

Run the same command again after adding papers. Only new or changed files are
re-parsed.

List your sessions:

```bash
biorake sessions
```

## 3. Search

```bash
biorake search --session leukemia_2026 "acute myeloid leukemia"
```

Abbreviations, synonyms and spelling variants resolve to the same term, so these
searches all find acute myeloid leukemia papers:

```bash
biorake search --session leukemia_2026 AML
biorake search --session leukemia_2026 "acute myelogenous leukemia"
biorake search --session leukemia_2026 "Sézary syndrome"     # same as "sezary syndrome"
```

To search a whole taxonomy category, use the category name:

```bash
biorake search --session leukemia_2026 "hematologic oncology"
```

Results open in an interactive table:

| Key | Action |
|-----|--------|
| ↑ / ↓ or k / j | Move the highlight |
| Space | Show/hide the synopsis of the highlighted paper |
| n / p | Next / previous page |
| q | Quit the results view |

## 4. Interactive menu

Run with no arguments to pick a session, index a folder and search from a menu:

```bash
biorake
```

## 5. Add your own terminology

To add terms to the saved taxonomy permanently, use a JSON or CSV file:

```bash
biorake add-terminology custom_cancer_terminology_example.json
biorake add-terminology custom_cancer_terminology_example.csv
biorake index ~/papers/leukemia --session leukemia_2026   # re-index to apply
```

To use extra terms for one indexing run only, leaving the saved taxonomy
unchanged:

```bash
biorake index ~/papers/trial_x --session trial_x --taxonomy-file my_terms.csv
```

## 6. Where data is stored

The database and taxonomy are stored in `~/.biorake/`. To keep them somewhere
else, for example one store per project:

```bash
export BIORAKE_HOME=~/projects/oncology/.biorake
biorake index ~/projects/oncology/papers --session onc
```

Or use a separate database file for a single command:

```bash
biorake --db /tmp/scratch.db index ~/papers/misc --session misc
biorake --db /tmp/scratch.db search --session misc "melanoma"
```
