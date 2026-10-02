#!/usr/bin/env python3
"""
bioRake
=======
A domain-tuned RAKE search engine for biomedical literature. Indexes a
folder of medical/pharma/biopharma documents (PDF, DOCX, PPTX,
Markdown, TXT), scores them with a domain-tuned RAKE variant, stores
everything in SQLite (partitioned by "session" so different folders never
mix results), and lets you search + browse ranked documents in a clean,
interactive terminal UI (arrow keys to move, SPACE to expand a synopsis).

Usage
-----
    # one-time NLTK data download (needs internet, run once)
    python -m nltk.downloader punkt punkt_tab stopwords

    # index a folder of papers into a named session
    biorake index /path/to/papers --session oncology_batch1

    # search within that session (interactive, paginated, arrow+space UI)
    biorake search --session oncology_batch1 "glomerulonephritis"

    # no arguments -> full interactive menu (pick/create a session, then search loop)
    biorake

    # merge in an external, industry-specific terminology file
    biorake add-terminology custom_cancer_terms.json

    # list existing sessions
    biorake sessions
"""

import os
import sys
import re
import io
import json
import math
import string
import sqlite3
import hashlib
import argparse
import difflib
import platform
import unicodedata
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Set, Optional

import nltk
from nltk.tokenize import sent_tokenize

# ---- optional document parsers -------------------------------------------
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import docx
except ImportError:
    docx = None

try:
    import pptx
except ImportError:
    pptx = None

# ---- terminal UI ------------------------------------------------------------
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt

# ---- cross-platform single-keypress reader ---------------------------------
IS_WINDOWS = platform.system() == "Windows"
if IS_WINDOWS:
    import msvcrt
else:
    import tty
    import termios

console = Console()

APP_DIR = Path(os.environ.get("BIORAKE_HOME", Path.home() / ".biorake"))
APP_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_DB_PATH = APP_DIR / "biorake.db"
DEFAULT_TAXONOMY_PATH = APP_DIR / "taxonomy.json"

SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".md", ".markdown", ".txt"}
PAGE_SIZE = 20
SYNOPSIS_CHARS = 500


# ==============================================================================
# 0. NLTK BOOTSTRAP
# ==============================================================================
def ensure_nltk_data():
    """Make sure tokenizer/stopword data is present; try a silent download."""
    for pkg, path in [("punkt", "tokenizers/punkt"), ("punkt_tab", "tokenizers/punkt_tab"),
                       ("stopwords", "corpora/stopwords")]:
        try:
            nltk.data.find(path)
        except LookupError:
            try:
                nltk.download(pkg, quiet=True)
            except Exception:
                pass


ensure_nltk_data()
try:
    from nltk.corpus import stopwords as _nltk_stopwords
    BASE_STOPWORDS = set(_nltk_stopwords.words("english"))
except Exception:
    # minimal fallback if corpora truly unavailable
    BASE_STOPWORDS = {
        "the", "a", "an", "and", "or", "of", "in", "on", "for", "to", "is",
        "are", "was", "were", "be", "been", "with", "as", "by", "at", "this",
        "that", "it", "from", "we", "our", "their", "these", "those",
    }

# Boilerplate noise common in journals/papers that isn't a real stopword
# but pollutes RAKE phrases in medical literature.
PAPER_NOISE_WORDS = {
    "et", "al", "fig", "figure", "table", "doi", "https", "http", "www",
    "vol", "pp", "issn", "isbn", "supplementary", "eq", "ref", "refs",
    "ci", "n", "p", "abstract", "introduction", "conclusion", "discussion",
    "methods", "results", "background", "objective", "objectives",
    "copyright", "elsevier", "springer", "wiley",
}

# Keep hyphens/slashes inside tokens so compound clinical/pharma terms
# (e.g. "IL-6", "HER2/neu", "beta-blocker", "5-FU") don't get shredded by
# generic punctuation-based tokenizers.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-/]*")


def normalize_text(text: str) -> str:
    """Lowercase, fold accents ("Sézary" -> "sezary") and drop apostrophes
    ("Hodgkin's" -> "hodgkins") so document text, taxonomy terms and queries
    all tokenize the same way."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"['’]", "", text)


def normalize_term(term: str) -> str:
    """Canonical phrase form of a taxonomy term or query: the exact string
    RAKE would produce for it if it appeared in a document."""
    return " ".join(_TOKEN_RE.findall(normalize_text(term)))


# ==============================================================================
# 1. TAXONOMY (broad category -> {term: [synonyms]})
# ==============================================================================
#
# Cancer types: every name in the NCI "Cancer Types" A-Z list
# (https://www.cancer.gov/types) is included below,
# grouped into oncology subspecialty categories so that the "same broad
# category" ranking tier relates e.g. "rectal cancer" to "anal cancer" rather
# than to every cancer. NCI's inverted index forms ("Ependymoma, Childhood"),
# parenthetical alternates ("Kidney (Renal Cell) Cancer") and abbreviations
# ("(AML)") are folded into a canonical term + synonyms. Each term/synonym
# lives in exactly one category.
DEFAULT_TAXONOMY: Dict[str, Dict[str, List[str]]] = {
    "oncology": {
        "cancer": [], "benign": [], "malignant": ["malignancy"],
        "malignant tumor": ["malignant neoplasm"],
        "chemotherapy": ["chemo"], "carcinoma": [], "tumor": ["neoplasm"],
        "oncology": [], "metastasis": ["metastatic disease"], "sarcoma": [],
        "radiotherapy": ["radiation therapy"], "biopsy": [],
        "metastatic cancer": ["metastatic carcinoma"],
        "recurrent cancer": ["cancer recurrence", "recurrent disease"],
        "carcinoma of unknown primary": ["cup", "cancer of unknown primary",
                                         "unknown primary carcinoma"],
    },
    "hematologic_oncology": {
        "leukemia": ["leukaemia"],
        "acute lymphoblastic leukemia": ["all", "acute lymphocytic leukemia"],
        "acute myeloid leukemia": ["aml", "acute myelogenous leukemia"],
        "chronic lymphocytic leukemia": ["cll"],
        "chronic myelogenous leukemia": ["cml", "chronic myeloid leukemia"],
        "hairy cell leukemia": ["hcl"],
        "lymphoma": [],
        "hodgkin lymphoma": ["hodgkin's lymphoma", "hodgkin disease", "hodgkin's disease"],
        "non-hodgkin lymphoma": ["nhl", "non-hodgkin's lymphoma"],
        "aids-related lymphoma": ["hiv-associated lymphoma", "hiv-related lymphoma"],
        "primary cns lymphoma": ["primary central nervous system lymphoma", "pcnsl"],
        "hodgkin lymphoma during pregnancy": ["pregnancy and hodgkin lymphoma"],
        "non-hodgkin lymphoma during pregnancy": ["pregnancy and non-hodgkin lymphoma"],
        "mycosis fungoides": ["cutaneous t-cell lymphoma", "ctcl"],
        "sezary syndrome": ["sézary syndrome"],
        "multiple myeloma": ["mm", "myeloma", "plasma cell neoplasm", "plasma cell neoplasms",
                             "multiple myeloma/plasma cell neoplasms",
                             "plasma cell neoplasm/multiple myeloma"],
        "myelodysplastic syndromes": ["mds", "myelodysplastic syndrome"],
        "myeloproliferative neoplasms": ["mpn", "myeloproliferative neoplasm"],
        "myelodysplastic/myeloproliferative neoplasms": ["mds/mpn"],
        "langerhans cell histiocytosis": ["lch"],
    },
    "neuro_oncology": {
        "brain tumor": ["brain tumors", "brain cancer"],
        "astrocytoma": ["astrocytomas", "childhood astrocytoma", "childhood astrocytomas"],
        "atypical teratoid/rhabdoid tumor": ["at/rt", "atrt",
                                             "childhood atypical teratoid/rhabdoid tumor"],
        "cns germ cell tumor": ["cns germ cell tumors",
                                "childhood central nervous system germ cell tumors"],
        "craniopharyngioma": ["childhood craniopharyngioma"],
        "diffuse intrinsic pontine glioma": ["dipg"],
        "cns embryonal tumor": ["cns embryonal tumors", "embryonal tumors"],
        "medulloblastoma": ["childhood medulloblastoma"],
        "ependymoma": ["childhood ependymoma"],
    },
    "pediatric_oncology": {
        "childhood cancer": ["childhood cancers", "pediatric cancer", "pediatric cancers"],
        "rare childhood cancers": ["rare cancers of childhood"],
        "neuroblastoma": [],
        "retinoblastoma": [],
        "wilms tumor": ["nephroblastoma", "childhood kidney tumors"],
        "pleuropulmonary blastoma": ["ppb"],
        "childhood cardiac tumors": ["childhood heart tumors", "cardiac tumors", "heart tumors"],
        "childhood extracranial germ cell tumor": ["childhood extracranial germ cell tumors",
                                                  "extracranial germ cell tumor"],
        "childhood germ cell tumor": ["childhood germ cell tumors"],
        "childhood vascular tumors": ["vascular tumors", "vascular tumor"],
        "laryngeal papillomatosis": ["childhood laryngeal papillomatosis",
                                     "recurrent respiratory papillomatosis", "papillomatosis"],
    },
    "thoracic_oncology": {
        "lung cancer": ["lung carcinoma"],
        "non-small cell lung cancer": ["nsclc"],
        "small cell lung cancer": ["sclc"],
        "mesothelioma": ["malignant mesothelioma"],
        "thymoma": [], "thymic carcinoma": [],
        "thymus cancer": ["thymic cancer"],
        "bronchial tumors": ["bronchial tumor"],
        "tracheobronchial tumors": ["tracheobronchial tumor"],
        "pulmonary inflammatory myofibroblastic tumor": ["inflammatory myofibroblastic tumor"],
    },
    "breast_oncology": {
        "breast cancer": ["breast carcinoma"],
        "male breast cancer": [],
        "ductal carcinoma in situ": ["dcis"],
        "breast cancer during pregnancy": ["pregnancy and breast cancer",
                                           "pregnancy-associated breast cancer"],
    },
    "gynecologic_oncology": {
        "cervical cancer": [],
        "endometrial cancer": ["uterine cancer"],
        "uterine sarcoma": [],
        "ovarian cancer": [],
        "ovarian germ cell tumors": ["ovarian germ cell tumor"],
        "fallopian tube cancer": [],
        "primary peritoneal cancer": [],
        "vaginal cancer": [],
        "vulvar cancer": [],
        "gestational trophoblastic disease": ["gtd", "gestational trophoblastic neoplasia"],
    },
    "genitourinary_oncology": {
        "bladder cancer": [],
        "kidney cancer": ["renal cell cancer", "renal cell carcinoma", "rcc"],
        "transitional cell cancer of the renal pelvis and ureter": [
            "renal pelvis and ureter cancer", "transitional cell carcinoma"],
        "urethral cancer": [],
        "prostate cancer": [],
        "testicular cancer": [],
        "penile cancer": [],
        "extragonadal germ cell tumors": ["extragonadal germ cell tumor"],
    },
    "gastrointestinal_oncology": {
        "anal cancer": [],
        "bile duct cancer": ["cholangiocarcinoma"],
        "colorectal cancer": ["crc", "colon cancer"],
        "rectal cancer": [],
        "esophageal cancer": [],
        "gallbladder cancer": [],
        "stomach cancer": ["gastric cancer"],
        "gastrointestinal neuroendocrine tumors": ["gastrointestinal carcinoid tumors",
                                                   "neuroendocrine tumors"],
        "gastrointestinal stromal tumors": ["gist", "gastrointestinal stromal tumor"],
        "liver cancer": ["hepatocellular cancer", "hepatocellular carcinoma", "hcc"],
        "pancreatic cancer": [],
        "pancreatic neuroendocrine tumors": ["islet cell tumors", "islet cell tumor", "pnets"],
        "small intestine cancer": ["small bowel cancer"],
    },
    "head_neck_oncology": {
        "head and neck cancer": ["head and neck squamous cell carcinoma", "hnscc"],
        "hypopharyngeal cancer": [],
        "laryngeal cancer": [],
        "lip and oral cavity cancer": ["oral cancer", "mouth cancer", "oral cavity cancer"],
        "oropharyngeal cancer": [],
        "pharyngeal cancer": ["throat cancer"],
        "nasopharyngeal cancer": [],
        "nasal cavity and paranasal sinus cancer": ["paranasal sinus and nasal cavity cancer",
                                                    "sinonasal cancer"],
        "salivary gland cancer": [],
        "metastatic squamous neck cancer with occult primary": ["squamous neck cancer"],
        "midline tract carcinoma with nut gene changes": ["nut carcinoma",
                                                          "nut midline carcinoma"],
        "esthesioneuroblastoma": ["olfactory neuroblastoma"],
    },
    "endocrine_oncology": {
        "adrenocortical carcinoma": ["adrenocortical cancer", "adrenal cortex cancer"],
        "thyroid cancer": [],
        "parathyroid cancer": [],
        "pheochromocytoma": [],
        "paraganglioma": [],
        "pituitary tumor": ["pituitary tumors", "pituitary adenoma"],
        "multiple endocrine neoplasia syndromes": ["multiple endocrine neoplasia",
                                                   "men1", "men2"],
    },
    "skin_oncology": {
        "skin cancer": [],
        "melanoma": ["cutaneous melanoma"],
        "merkel cell carcinoma": [],
        "kaposi sarcoma": ["kaposi's sarcoma"],
    },
    "bone_soft_tissue_oncology": {
        "bone cancer": [],
        "ewing sarcoma": [],
        "osteosarcoma": [],
        "undifferentiated pleomorphic sarcoma of bone": ["undifferentiated pleomorphic sarcoma"],
        "chordoma": ["childhood chordoma"],
        "soft tissue sarcoma": [],
        "rhabdomyosarcoma": ["childhood rhabdomyosarcoma"],
    },
    "ocular_oncology": {
        "intraocular melanoma": ["eye melanoma", "uveal melanoma", "ocular melanoma"],
    },
    "ocular": {
        "ocular": ["occular"], "optic nerve": ["ocular nerve"],
        "vision impairment": ["visual impairment"], "cataract": [],
        "glaucoma": [], "retina": ["retinal"], "cornea": ["corneal"],
        "ophthalmology": [], "macular degeneration": [],
    },
    "renal": {
        "renal": [], "renal failure": ["kidney failure"],
        "glomerulonephritis": ["gn"], "nephron": [],
        "kidney stone": ["nephrolithiasis", "renal calculi", "renal calculus"],
        "chronic kidney disease": ["ckd"], "dialysis": [],
        "nephrology": [], "acute kidney injury": ["aki"],
        "proteinuria": [], "hematuria": [],
    },
    "cardiac": {
        "cardiac": ["cardiovascular"], "myocardial infarction": ["heart attack", "mi"],
        "arrhythmia": [], "heart failure": ["cardiac failure"],
        "hypertension": ["high blood pressure"], "atherosclerosis": [],
        "cardiomyopathy": [], "coronary artery disease": ["cad"],
    },
    "gastrointestinal": {
        "gastrointestinal": ["gi"], "endoscopy": [], "gastritis": [],
        "irritable bowel syndrome": ["ibs", "irritable bowel"],
        "colitis": [], "esophagus": ["esophageal"], "duodenum": [],
        "cirrhosis": [], "crohns disease": ["crohn's disease"],
    },
    "pharmacology": {
        "pharmacokinetics": ["pk"], "pharmacodynamics": ["pd"],
        "bioavailability": [], "half-life": [], "dosage": ["dose", "dosing"],
        "adverse event": ["adverse drug reaction", "side effect"],
        "clinical trial": ["rct", "randomized controlled trial"],
        "monoclonal antibody": ["mab"], "biologic": ["biologics"],
        "efficacy": [], "contraindication": [],
    },
    "neurology": {
        "epilepsy": ["seizure disorder"], "parkinsons disease": ["parkinson's disease"],
        "neuropathy": [], "alzheimers disease": ["alzheimer's disease"],
        "multiple sclerosis": ["ms"], "stroke": ["cerebrovascular accident", "cva"],
    },
}


class Taxonomy:
    """Loads / merges / persists the category -> term -> synonym map, and
    builds fast reverse lookups used for query-time relatedness scoring."""

    def __init__(self, path: Path = DEFAULT_TAXONOMY_PATH):
        self.path = path
        self.data: Dict[str, Dict[str, List[str]]] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}
        if not self.data:
            self.data = json.loads(json.dumps(DEFAULT_TAXONOMY))  # deep copy
            self.save()
        elif self._add_missing_defaults():
            # a taxonomy saved by an older version picks up newly shipped
            # default terms; user-added terms/synonyms are left untouched
            self.save()
        self._build_indexes()

    def _add_missing_defaults(self) -> bool:
        known = {normalize_term(t) for terms in self.data.values() for t in terms}
        changed = False
        for category, terms in DEFAULT_TAXONOMY.items():
            for term, synonyms in terms.items():
                if normalize_term(term) not in known:
                    self.data.setdefault(category, {})[term] = list(synonyms)
                    changed = True
        return changed

    def _build_indexes(self):
        self.term_to_category: Dict[str, str] = {}
        self.synonym_to_term: Dict[str, str] = {}
        self.category_terms: Dict[str, Set[str]] = {}
        for category, terms in self.data.items():
            cat = category.lower()
            all_terms_in_cat = set()
            for term, synonyms in terms.items():
                t = normalize_term(term)
                self.term_to_category[t] = cat
                all_terms_in_cat.add(t)
                for syn in synonyms:
                    s = normalize_term(syn)
                    self.synonym_to_term[s] = t
                    self.term_to_category[s] = cat
                    all_terms_in_cat.add(s)
            self.category_terms[cat] = all_terms_in_cat

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def merge_external_file(self, file_path: str, persist: bool = True) -> int:
        """Merge an external terminology file (JSON or CSV) into the taxonomy.

        JSON shape:  {"category": {"term": ["synonym1", "synonym2"], ...}, ...}
        CSV shape:   category,term,synonyms   (synonyms pipe-separated, optional)
        Returns number of terms added/updated.
        """
        ext = Path(file_path).suffix.lower()
        added = 0
        if ext == ".json":
            incoming = json.loads(Path(file_path).read_text(encoding="utf-8"))
        elif ext == ".csv":
            import csv
            incoming = {}
            with open(file_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    cat = row.get("category", "").strip().lower()
                    term = row.get("term", "").strip().lower()
                    syns_raw = row.get("synonyms", "") or ""
                    syns = [s.strip() for s in syns_raw.split("|") if s.strip()]
                    if not cat or not term:
                        continue
                    incoming.setdefault(cat, {})[term] = syns
        else:
            raise ValueError("Unsupported taxonomy file type (use .json or .csv)")

        for category, terms in incoming.items():
            cat = category.lower()
            bucket = self.data.setdefault(cat, {})
            for term, synonyms in terms.items():
                t = term.lower()
                existing = set(x.lower() for x in bucket.get(t, []))
                existing.update(s.lower() for s in synonyms)
                bucket[t] = sorted(existing)
                added += 1

        self._build_indexes()
        if persist:
            self.save()
        return added

    def resolve_query(self, query: str) -> Tuple[Optional[str], Optional[str], Set[str]]:
        """Given a raw query, return (canonical_term_or_None, category_or_None,
        related_terms_in_that_category)."""
        cat_key = "_".join(query.strip().lower().split())  # "hematologic oncology" ok too
        if cat_key in self.category_terms:
            return None, cat_key, self.category_terms[cat_key]
        q = normalize_term(query)
        canonical = self.synonym_to_term.get(q, q if q in self.term_to_category else None)
        category = self.term_to_category.get(q)
        related = self.category_terms.get(category, set()) if category else set()
        return canonical, category, related


# ==============================================================================
# 2. DOCUMENT PARSERS
# ==============================================================================
class DocumentParser:
    """Extracts raw text from PDF, Word, PowerPoint, Markdown, and text files."""

    @staticmethod
    def parse_pdf(file_path: str) -> str:
        if not PdfReader:
            return ""
        try:
            reader = PdfReader(file_path)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""

    @staticmethod
    def parse_docx(file_path: str) -> str:
        if not docx:
            return ""
        try:
            doc = docx.Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text)
        except Exception:
            return ""

    @staticmethod
    def parse_pptx(file_path: str) -> str:
        if not pptx:
            return ""
        try:
            prs = pptx.Presentation(file_path)
            runs = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        runs.append(shape.text)
            return "\n".join(runs)
        except Exception:
            return ""

    @staticmethod
    def parse_txt(file_path: str) -> str:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            return ""

    @classmethod
    def extract_text(cls, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        if ext == ".pdf":
            return cls.parse_pdf(file_path)
        if ext in (".docx", ".doc"):
            return cls.parse_docx(file_path)
        if ext in (".pptx", ".ppt"):
            return cls.parse_pptx(file_path)
        if ext in (".md", ".markdown", ".txt", ".log"):
            return cls.parse_txt(file_path)
        return ""

    @staticmethod
    def make_synopsis(text: str, max_chars: int = SYNOPSIS_CHARS) -> str:
        """First few clean sentences of a document, used as a quick preview."""
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return "(no extractable text)"
        try:
            sentences = sent_tokenize(cleaned)
        except Exception:
            sentences = [cleaned]
        out = ""
        for s in sentences:
            if len(out) + len(s) > max_chars:
                break
            out += (" " if out else "") + s
        if not out:
            out = cleaned[:max_chars]
        if len(cleaned) > len(out):
            out += " ..."
        return out


# ==============================================================================
# 3. DOMAIN-TUNED RAKE
# ==============================================================================
class BioRake:
    def __init__(self, stop_words: Optional[Set[str]] = None, taxonomy: Optional[Taxonomy] = None,
                 taxonomy_boost: float = 3.0):
        self.stop_words = set(stop_words) if stop_words else (BASE_STOPWORDS | PAPER_NOISE_WORDS)
        self.punctuations = set(string.punctuation)
        self.taxonomy = taxonomy
        self.taxonomy_boost = taxonomy_boost
        self._taxonomy_phrase_set = set()
        if taxonomy:
            self._taxonomy_phrase_set = set(taxonomy.term_to_category.keys())

    def _tokenize(self, sentence: str) -> List[str]:
        return _TOKEN_RE.findall(sentence)

    def _candidate_phrases(self, text: str) -> List[List[str]]:
        text = normalize_text(text)
        try:
            sentences = sent_tokenize(text)
        except Exception:
            sentences = re.split(r"(?<=[.!?])\s+", text)

        phrase_list = []
        for sentence in sentences:
            tokens = self._tokenize(sentence)
            phrase = []
            for token in tokens:
                if token in self.stop_words or token.isnumeric():
                    if phrase:
                        phrase_list.append(phrase)
                        phrase = []
                else:
                    phrase.append(token)
            if phrase:
                phrase_list.append(phrase)
        return phrase_list

    def extract_phrases_with_frequencies(self, text: str) -> Dict[str, Tuple[float, int]]:
        """Returns {phrase: (rake_score, frequency)} with a boost applied to
        phrases that are recognized medical taxonomy terms/synonyms - these
        are the phrases that matter most for downstream ranking."""
        phrase_list = self._candidate_phrases(text)

        word_freq, word_degree = defaultdict(int), defaultdict(int)
        for phrase in phrase_list:
            degree = len(phrase) - 1
            for word in phrase:
                word_freq[word] += 1
                word_degree[word] += degree

        word_scores = {
            w: (word_degree[w] + word_freq[w]) / word_freq[w] for w in word_freq
        }

        phrase_metrics: Dict[str, List] = defaultdict(lambda: [0.0, 0])
        for phrase in phrase_list:
            phrase_str = " ".join(phrase)
            score = sum(word_scores[w] for w in phrase)
            if phrase_str in self._taxonomy_phrase_set:
                score *= self.taxonomy_boost
            phrase_metrics[phrase_str][0] = score
            phrase_metrics[phrase_str][1] += 1

        return {p: (v[0], v[1]) for p, v in phrase_metrics.items()}


# ==============================================================================
# 4. DATABASE + SEARCH ENGINE (session-partitioned)
# ==============================================================================
class BioRakeEngine:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH, taxonomy: Optional[Taxonomy] = None):
        self.db_path = str(db_path)
        self.taxonomy = taxonomy or Taxonomy()
        self.rake = BioRake(taxonomy=self.taxonomy)
        self._init_db()

    # ---- schema -------------------------------------------------------
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT UNIQUE,      -- resolved folder path or explicit name
                    display_name TEXT,
                    folder_path TEXT,
                    created_at TEXT,
                    last_synced TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    file_path TEXT,
                    file_hash TEXT,
                    last_modified REAL,
                    synopsis TEXT,
                    UNIQUE(session_id, file_path),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phrase_index (
                    doc_id INTEGER,
                    phrase TEXT,
                    rake_score REAL,
                    frequency INTEGER,
                    FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_phrase ON phrase_index(phrase)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_session ON documents(session_id)")
            conn.commit()

    # ---- session management -------------------------------------------
    def get_or_create_session(self, folder_path: str, session_name: Optional[str] = None) -> int:
        resolved = str(Path(folder_path).expanduser().resolve())
        session_key = session_name.strip() if session_name else resolved
        display_name = session_name.strip() if session_name else Path(resolved).name

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE sessions SET folder_path = ? WHERE session_id = ?",
                    (resolved, row[0]),
                )
                conn.commit()
                return row[0]
            cur = conn.execute(
                "INSERT INTO sessions (session_key, display_name, folder_path, created_at, last_synced) "
                "VALUES (?, ?, ?, ?, NULL)",
                (session_key, display_name, resolved, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return cur.lastrowid

    def find_session_by_key(self, session_key: str) -> Optional[int]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE session_key = ? OR display_name = ?",
                (session_key, session_key),
            ).fetchone()
            return row[0] if row else None

    def list_sessions(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT s.session_id, s.display_name, s.folder_path, s.last_synced,
                       COUNT(d.doc_id) as doc_count
                FROM sessions s LEFT JOIN documents d ON s.session_id = d.session_id
                GROUP BY s.session_id ORDER BY s.last_synced DESC
            """).fetchall()
        return [
            {"session_id": r[0], "display_name": r[1], "folder_path": r[2],
             "last_synced": r[3], "doc_count": r[4]}
            for r in rows
        ]

    # ---- indexing -------------------------------------------------------
    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def sync_folder(self, folder_path: str, session_name: Optional[str] = None,
                     show_progress: bool = True) -> Tuple[int, int]:
        """Incrementally indexes every supported file in folder_path into its
        own session. Only new/modified files are re-parsed. Returns
        (files_added_or_updated, files_skipped_unchanged)."""
        folder = Path(folder_path).expanduser()
        if not folder.exists():
            raise FileNotFoundError(f"Folder not found: {folder_path}")

        session_id = self.get_or_create_session(folder_path, session_name)
        files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in SUPPORTED_EXTS)

        updated, skipped = 0, 0

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.cursor()

            def process_one(file_path: Path):
                nonlocal updated, skipped
                str_path = str(file_path.resolve())
                mtime = file_path.stat().st_mtime

                row = cursor.execute(
                    "SELECT doc_id, last_modified FROM documents WHERE session_id = ? AND file_path = ?",
                    (session_id, str_path),
                ).fetchone()

                if row and row[1] == mtime:
                    skipped += 1
                    return

                text = DocumentParser.extract_text(str_path)
                if not text.strip():
                    return

                synopsis = DocumentParser.make_synopsis(text)
                phrases = self.rake.extract_phrases_with_frequencies(text)

                if row:
                    doc_id = row[0]
                    cursor.execute(
                        "UPDATE documents SET last_modified = ?, synopsis = ? WHERE doc_id = ?",
                        (mtime, synopsis, doc_id),
                    )
                    cursor.execute("DELETE FROM phrase_index WHERE doc_id = ?", (doc_id,))
                else:
                    cursor.execute(
                        "INSERT INTO documents (session_id, file_path, last_modified, synopsis) "
                        "VALUES (?, ?, ?, ?)",
                        (session_id, str_path, mtime, synopsis),
                    )
                    doc_id = cursor.lastrowid

                phrase_rows = [
                    (doc_id, phrase, score, freq)
                    for phrase, (score, freq) in phrases.items()
                    if len(phrase) > 2  # drop noise tokens
                ]
                cursor.executemany("INSERT INTO phrase_index VALUES (?, ?, ?, ?)", phrase_rows)
                updated += 1

            if show_progress:
                with Progress(
                    TextColumn("[bold blue]Indexing[/bold blue]"),
                    BarColumn(),
                    TextColumn("{task.completed}/{task.total} files"),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task("index", total=len(files))
                    for file_path in files:
                        process_one(file_path)
                        progress.advance(task)
            else:
                for file_path in files:
                    process_one(file_path)

            cursor.execute(
                "UPDATE sessions SET last_synced = ? WHERE session_id = ?",
                (datetime.now(timezone.utc).isoformat(), session_id),
            )
            conn.commit()

        return updated, skipped

    # ---- ranked search ---------------------------------------------------
    # Tiered relevance weights: exact match beats a compound/partial match,
    # which beats a known synonym, which beats "same broad category"
    # (e.g. "kidney stone" ranking under a "glomerulonephritis" query
    # because both live in the renal taxonomy), which beats a loose fuzzy
    # fallback for spelling variants not yet catalogued.
    TIER_EXACT = 1000.0
    TIER_COMPOUND = 300.0
    TIER_SYNONYM = 150.0
    TIER_CATEGORY = 50.0
    TIER_FUZZY = 5.0

    def search(self, query: str, session_id: int, fuzzy_fallback: bool = True) -> List[Dict]:
        query_clean = normalize_term(query)
        canonical, category, related_terms = self.taxonomy.resolve_query(query_clean)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            candidate_terms = set(related_terms) | {query_clean}
            if canonical:
                candidate_terms.add(canonical)

            placeholders = ",".join("?" for _ in candidate_terms) or "''"
            sql = f"""
                SELECT d.doc_id, d.file_path, d.synopsis, p.phrase, p.rake_score, p.frequency
                FROM phrase_index p
                JOIN documents d ON p.doc_id = d.doc_id
                WHERE d.session_id = ?
                  AND (
                        p.phrase IN ({placeholders})
                        OR p.phrase LIKE ?
                        OR p.phrase LIKE ?
                        OR ? LIKE '%' || p.phrase || '%'
                      )
            """
            # canonical containment lets a synonym query ("acute myelogenous
            # leukemia") hit longer RAKE phrases built around the canonical term
            # ("acute myeloid leukemia relapse rates")
            canonical_like = f"%{canonical}%" if canonical else f"%{query_clean}%"
            params = ([session_id] + list(candidate_terms)
                      + [f"%{query_clean}%", canonical_like, query_clean])
            rows = cursor.execute(sql, params).fetchall()

            doc_meta = {}
            doc_scores = defaultdict(float)
            doc_matches = defaultdict(list)

            def weight_for(phrase: str) -> float:
                if phrase == query_clean or phrase == canonical:
                    return self.TIER_EXACT
                if query_clean in phrase or phrase in query_clean or (canonical and canonical in phrase):
                    return self.TIER_COMPOUND
                if phrase in self.taxonomy.synonym_to_term and self.taxonomy.synonym_to_term[phrase] == canonical:
                    return self.TIER_SYNONYM
                if category and phrase in related_terms:
                    # closer terms (higher string similarity to the query) rank
                    # higher within the same category tier
                    sim = difflib.SequenceMatcher(None, query_clean, phrase).ratio()
                    return self.TIER_CATEGORY + (sim * 20)
                return 0.0

            for doc_id, file_path, synopsis, phrase, rake_score, freq in rows:
                w = weight_for(phrase)
                if w <= 0:
                    continue
                contribution = w * rake_score * (1 + math.log1p(freq))
                doc_scores[doc_id] += contribution
                doc_matches[doc_id].append((phrase, contribution))
                doc_meta[doc_id] = (file_path, synopsis)

            # Fuzzy fallback for spelling variants / terms not in the taxonomy,
            # only used to enrich thin result sets so we don't drown genuine
            # matches in noise.
            if fuzzy_fallback and len(doc_scores) < 5:
                distinct_phrases = cursor.execute(
                    """SELECT DISTINCT p.phrase FROM phrase_index p
                       JOIN documents d ON p.doc_id = d.doc_id
                       WHERE d.session_id = ? AND LENGTH(p.phrase) > 3
                       LIMIT 5000""",
                    (session_id,),
                ).fetchall()
                close = [
                    ph[0] for ph in distinct_phrases
                    if difflib.SequenceMatcher(None, query_clean, ph[0]).ratio() > 0.82
                ]
                if close:
                    ph_placeholders = ",".join("?" for _ in close)
                    fuzzy_rows = cursor.execute(
                        f"""SELECT d.doc_id, d.file_path, d.synopsis, p.phrase, p.rake_score, p.frequency
                            FROM phrase_index p JOIN documents d ON p.doc_id = d.doc_id
                            WHERE d.session_id = ? AND p.phrase IN ({ph_placeholders})""",
                        [session_id] + close,
                    ).fetchall()
                    for doc_id, file_path, synopsis, phrase, rake_score, freq in fuzzy_rows:
                        sim = difflib.SequenceMatcher(None, query_clean, phrase).ratio()
                        contribution = self.TIER_FUZZY * sim * rake_score * (1 + math.log1p(freq))
                        doc_scores[doc_id] += contribution
                        doc_matches[doc_id].append((phrase, contribution))
                        doc_meta[doc_id] = (file_path, synopsis)

        results = []
        for doc_id, score in doc_scores.items():
            file_path, synopsis = doc_meta[doc_id]
            top_matches = [m[0] for m in sorted(doc_matches[doc_id], key=lambda x: x[1], reverse=True)[:4]]
            results.append({
                "doc_id": doc_id,
                "file_name": Path(file_path).name,
                "file_path": file_path,
                "synopsis": synopsis,
                "score": round(score, 2),
                "matched_terms": top_matches,
            })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results


# ==============================================================================
# 5. TERMINAL UI: single-keypress reader + interactive results browser
# ==============================================================================
def get_key() -> str:
    """Blocks for one keypress, returns a normalized token:
    'UP','DOWN','LEFT','RIGHT','SPACE','ENTER','QUIT', or a raw lowercase char."""
    if IS_WINDOWS:
        ch = msvcrt.getch()
        if ch in (b"\xe0", b"\x00"):
            ch2 = msvcrt.getch()
            return {b"H": "UP", b"P": "DOWN", b"K": "LEFT", b"M": "RIGHT"}.get(ch2, "")
        if ch == b"\r":
            return "ENTER"
        if ch == b" ":
            return "SPACE"
        try:
            c = ch.decode("utf-8", errors="ignore").lower()
        except Exception:
            return ""
        return "QUIT" if c == "q" else c
    else:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                rest = sys.stdin.read(2)
                return {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT"}.get(rest, "")
            if ch in ("\r", "\n"):
                return "ENTER"
            if ch == " ":
                return "SPACE"
            if ch.lower() == "q":
                return "QUIT"
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def build_results_table(page_items: List[Dict], start_idx: int, highlighted: int,
                         page: int, total_pages: int, total: int, query: str) -> Table:
    table = Table(
        title=f"Ranked results for '{query}'  (Page {page}/{total_pages} - {total} document(s))",
        show_header=True, header_style="bold magenta", expand=True,
    )
    table.add_column("Rank", style="dim", width=5, justify="center")
    table.add_column("Document", style="bold cyan", ratio=3, overflow="fold")
    table.add_column("Score", style="bold green", width=10, justify="right")
    table.add_column("Matched Terms", style="white", ratio=3, overflow="fold")

    for idx, item in enumerate(page_items, start=start_idx + 1):
        is_hl = (idx - 1 - start_idx) == highlighted
        rank_str = str(idx)
        name = item["file_name"]
        score_str = f"{item['score']:.2f}"
        matches = ", ".join(item["matched_terms"])
        if is_hl:
            style = "reverse bold"
            table.add_row(Text(rank_str, style=style), Text("▶ " + name, style=style),
                          Text(score_str, style=style), Text(matches, style=style))
        else:
            table.add_row(rank_str, name, score_str, matches)
    return table


def browse_results(results: List[Dict], query: str, page_size: int = PAGE_SIZE):
    if not results:
        console.print("[bold red]No matching documents found for that term.[/bold red]")
        return

    total = len(results)
    total_pages = max(1, math.ceil(total / page_size))
    page = 1
    highlighted = 0
    expanded = set()  # doc_ids currently expanded on screen

    while True:
        start = (page - 1) * page_size
        end = min(start + page_size, total)
        page_items = results[start:end]

        console.clear()
        console.print(build_results_table(page_items, start, highlighted, page, total_pages, total, query))

        current = page_items[highlighted] if page_items else None
        if current and current["doc_id"] in expanded:
            console.print(Panel(
                current["synopsis"],
                title=f"Synopsis - {current['file_name']}",
                subtitle=current["file_path"],
                border_style="yellow",
            ))

        console.print(
            "[bold yellow]Controls:[/bold yellow] "
            "[↑/↓ or k/j] Move   [SPACE] Expand/Collapse synopsis   "
            "\\[n] Next page   \\[p] Prev page   \\[q] Back to search"
        )

        key = get_key()
        if key in ("UP", "k"):
            highlighted = max(0, highlighted - 1)
        elif key in ("DOWN", "j"):
            highlighted = min(len(page_items) - 1, highlighted + 1)
        elif key == "SPACE":
            if current:
                if current["doc_id"] in expanded:
                    expanded.discard(current["doc_id"])
                else:
                    expanded.add(current["doc_id"])
        elif key == "n":
            if page < total_pages:
                page += 1
                highlighted = 0
        elif key == "p":
            if page > 1:
                page -= 1
                highlighted = 0
        elif key == "QUIT":
            break


# ==============================================================================
# 6. CLI
# ==============================================================================
def cmd_index(args):
    taxonomy = Taxonomy(DEFAULT_TAXONOMY_PATH)
    if args.taxonomy_file:
        # one-off terminology for this run only; the saved default is untouched
        taxonomy.merge_external_file(args.taxonomy_file, persist=False)
    engine = BioRakeEngine(Path(args.db), taxonomy)
    console.print(f"[bold blue]Indexing '{args.folder}' as session "
                  f"'{args.session or Path(args.folder).name}'...[/bold blue]")
    updated, skipped = engine.sync_folder(args.folder, args.session)
    console.print(f"[bold green]Done.[/bold green] {updated} file(s) indexed/updated, "
                  f"{skipped} unchanged skipped.")


def cmd_sessions(args):
    engine = BioRakeEngine(Path(args.db))
    sessions = engine.list_sessions()
    if not sessions:
        console.print("[yellow]No sessions yet. Run the 'index' command first.[/yellow]")
        return
    table = Table(title="Indexed Sessions", header_style="bold magenta")
    table.add_column("Session", style="bold cyan")
    table.add_column("Folder")
    table.add_column("Documents", justify="right")
    table.add_column("Last Synced")
    for s in sessions:
        table.add_row(s["display_name"], s["folder_path"], str(s["doc_count"]), s["last_synced"] or "-")
    console.print(table)


def cmd_add_terminology(args):
    taxonomy = Taxonomy(DEFAULT_TAXONOMY_PATH)
    added = taxonomy.merge_external_file(args.file, persist=True)
    console.print(f"[bold green]Merged {added} term(s) from '{args.file}' into "
                  f"{DEFAULT_TAXONOMY_PATH}[/bold green]")


def cmd_search(args):
    engine = BioRakeEngine(Path(args.db))
    session_id = engine.find_session_by_key(args.session) if args.session else None
    if session_id is None:
        sessions = engine.list_sessions()
        if not sessions:
            console.print("[bold red]No sessions found. Index a folder first with 'index'.[/bold red]")
            return
        if args.session:
            console.print(f"[bold red]Session '{args.session}' not found.[/bold red]")
        console.print("[yellow]Available sessions:[/yellow]")
        for s in sessions:
            console.print(f"  - {s['display_name']} ({s['doc_count']} docs)")
        return

    results = engine.search(args.query, session_id)
    browse_results(results, args.query)


def interactive_menu(args):
    engine = BioRakeEngine(Path(args.db))
    while True:
        console.clear()
        sessions = engine.list_sessions()
        console.print(Panel.fit("[bold]bioRake[/bold] - biomedical literature search", border_style="blue"))
        if sessions:
            table = Table(header_style="bold magenta")
            table.add_column("#", width=4)
            table.add_column("Session")
            table.add_column("Documents", justify="right")
            table.add_column("Folder")
            for i, s in enumerate(sessions, 1):
                table.add_row(str(i), s["display_name"], str(s["doc_count"]), s["folder_path"])
            console.print(table)
        else:
            console.print("[yellow]No sessions yet.[/yellow]")

        console.print(
            "\n[bold]Options:[/bold] \\[i] Index a new folder   \\[number] Search a session   \\[q] Quit"
        )
        choice = Prompt.ask("Select an option").strip().lower()

        if choice == "q":
            break
        elif choice == "i":
            folder = Prompt.ask("Folder path to index").strip()
            name = Prompt.ask("Session name (blank = derive from folder)", default="").strip() or None
            try:
                updated, skipped = engine.sync_folder(folder, name)
                console.print(f"[bold green]Indexed {updated} file(s), skipped {skipped} unchanged.[/bold green]")
            except FileNotFoundError as e:
                console.print(f"[bold red]{e}[/bold red]")
            Prompt.ask("Press Enter to continue", default="")
        elif choice.isdigit() and 1 <= int(choice) <= len(sessions):
            session = sessions[int(choice) - 1]
            while True:
                query = Prompt.ask(
                    f"\nSearch '{session['display_name']}' (blank to go back)"
                ).strip()
                if not query:
                    break
                results = engine.search(query, session["session_id"])
                browse_results(results, query)


def main():
    parser = argparse.ArgumentParser(
        prog="biorake",
        description="bioRake: domain-tuned RAKE search engine for medical/pharma/biopharma literature."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to the SQLite database file")
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="Index (or re-sync) a folder of documents into a session")
    p_index.add_argument("folder", help="Folder containing the papers/documents")
    p_index.add_argument("--session", default=None, help="Session name (defaults to folder name)")
    p_index.add_argument("--taxonomy-file", default=None,
                          help="Merge this terminology JSON/CSV file in for this run only (saved taxonomy untouched)")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search within a session and browse ranked results")
    p_search.add_argument("query", help="Medical term or category to search for")
    p_search.add_argument("--session", required=True, help="Session name to search within")
    p_search.set_defaults(func=cmd_search)

    p_sessions = sub.add_parser("sessions", help="List all indexed sessions")
    p_sessions.set_defaults(func=cmd_sessions)

    p_tax = sub.add_parser("add-terminology", help="Merge an external terminology file into the taxonomy")
    p_tax.add_argument("file", help="Path to a .json or .csv terminology file")
    p_tax.set_defaults(func=cmd_add_terminology)

    args = parser.parse_args()

    if not args.command:
        interactive_menu(args)
        return

    args.func(args)


if __name__ == "__main__":
    main()
