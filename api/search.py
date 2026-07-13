"""AccountSearch AI — Vercel Python serverless function.

POST /api/search  {"query": "...", "history": [{"role","content"}...], "model": "optional-id"}
->  {"answer", "sql", "reason", "rows", "columns", "model_used"}   or   {"error": "..."}

Stdlib only (urllib instead of requests) to keep the function bundle tiny.
Data lives in accounts.db (built by generate_synthetic_db.py): 500k accounts
plus 24 months of per-account booking/revenue history in monthly_activity.
The DB is opened read-only per request; months with zero bookings have no row.
"""

import csv
import json
import os
import pickle
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Vercel's Python runtime imports the entrypoint without putting api/ on
# sys.path, so sibling imports need it added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _turso  # noqa: E402

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Paid tier: fast, reliable, ~$0.00025/query at defaults. Override without a code
# change by setting the MODEL_IDS env var (comma-separated, primary first).
MODELS = [
    m.strip()
    for m in os.environ.get("MODEL_IDS", "openai/gpt-oss-120b,openai/gpt-4o-mini").split(",")
    if m.strip()
]
TABLES = ("accounts", "monthly_activity")
MAX_ROWS_TO_MODEL = 50  # hard cap on rows fetched/displayed
ANSWER_ROWS_TO_MODEL = 25  # rows sent to the answer model (smaller = faster)
MAX_HISTORY_MESSAGES = 20
RETRY_ROUNDS = 2
RETRY_DELAY_SECONDS = 4
CALL_TIMEOUT_SECONDS = 90

DB_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "accounts.db",
    Path.cwd() / "accounts.db",
]
CSV_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "Accounts-VadlamudiRamesh.csv",
    Path.cwd() / "Accounts-VadlamudiRamesh.csv",
]


def _local_db() -> Path | None:
    return next((p for p in DB_CANDIDATES if p.exists()), None)


def load_db():
    """Local accounts.db when present (dev); hosted Turso otherwise (Vercel)."""
    db_path = _local_db()
    if db_path is not None:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if _turso.available():
        return _turso.connect()
    raise FileNotFoundError(
        "accounts.db not found and TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set.")


def _month_bounds() -> tuple:
    try:
        conn = load_db()
        try:
            return conn.execute("SELECT MIN(month), MAX(month) FROM monthly_activity").fetchone()
        finally:
            conn.close()
    except Exception:  # data unreachable at import: fall back to generator defaults
        return "2024-07", "2026-06"


FIRST_MONTH, LATEST_MONTH = _month_bounds()
_LY, _LM = int(LATEST_MONTH[:4]), int(LATEST_MONTH[5:7])
_FY, _FM = int(FIRST_MONTH[:4]), int(FIRST_MONTH[5:7])
TOTAL_MONTHS = (_LY * 12 + _LM) - (_FY * 12 + _FM) + 1

SCHEMA_DESCRIPTION = f"""
Table: accounts — one row per account (500,000 rows)
- id (INTEGER, primary key, row number)
- account_id (INTEGER, unique) — the account number; JOIN key to monthly_activity
- account_name (TEXT) — the company / account name

Table: monthly_activity — one row per account per month IN WHICH IT BOOKED (~7.5M rows)
- account_id (INTEGER) — joins accounts.account_id
- month (TEXT, 'YYYY-MM') — from '{FIRST_MONTH}' to '{LATEST_MONTH}' ({TOTAL_MONTHS} months of history)
- bookings (INTEGER) — number of trips booked that month (always >= 1 when a row exists)
- revenue (REAL) — total booking revenue that month, in USD
- profit (REAL) — net profit on that month's bookings, in USD
- last_reservation_date (TEXT, 'YYYY-MM-DD') — date of that month's final booking

CRITICAL — monthly_activity is SPARSE: a month with zero bookings has NO row.
Therefore:
- Zero bookings in a month => no row; never assume {TOTAL_MONTHS} rows per account.
- Accounts that NEVER booked: WHERE NOT EXISTS (SELECT 1 FROM monthly_activity m WHERE m.account_id = a.account_id).
- "When did account X last book?": SELECT MAX(m.last_reservation_date) (or MAX(m.month)).
- "Months since last booking" (latest data month is {LATEST_MONTH}):
  ({_LY}*12 + {_LM}) - (CAST(substr(MAX(m.month),1,4) AS INTEGER)*12 + CAST(substr(MAX(m.month),6,2) AS INTEGER))
- TRUE average bookings per month over the whole history: SUM(m.bookings)/{TOTAL_MONTHS}.0
  (AVG(m.bookings) is the average over BOOKING months only — say which one you used in "reason").
- Customer-quality rule of thumb used by this business, per month: ~20 trips = good,
  ~30 = best, ~40+ = elite; useful as HAVING thresholds when asked about account quality.
"""

SQL_SYSTEM_PROMPT = f"""You are a database search planner for a travel-booking CRM. Given the
schema below and a user question, respond ONLY with a single JSON object of the form:
{{"sql": "<one SELECT query>", "reason": "<one sentence on how you interpreted the request>"}}

Rules:
- SELECT only, from the accounts and/or monthly_activity tables (JOIN on account_id
  when both are needed). No INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA/ATTACH.
- Exactly one statement, no semicolons. Subqueries, CTEs (WITH ... SELECT),
  JOINs, GROUP BY, HAVING, ORDER BY and aggregate functions are all allowed.
- Quarter bucketing (SQLite has no QUARTER()): sortable key
  substr(month,1,4) || '-Q' || ((CAST(substr(month,6,2) AS INTEGER) + 2) / 3)
  (e.g. '2025-Q1'). The data spans quarters 2024-Q3 through 2026-Q2.
- PIVOT / cross-tab requests ("quarter wise", "month wise", "as columns",
  "matrix", or any period-per-entity breakdown): return ONE ROW PER ENTITY and
  ONE COLUMN PER PERIOD via conditional aggregation, columns in chronological
  order, labelled like "Q1-2025". Example shape:
    SELECT a.account_name,
           ROUND(SUM(CASE WHEN m.month BETWEEN '2025-01' AND '2025-03' THEN m.revenue END), 2) AS "Q1-2025",
           ROUND(SUM(CASE WHEN m.month BETWEEN '2025-04' AND '2025-06' THEN m.revenue END), 2) AS "Q2-2025"
    FROM accounts a JOIN monthly_activity m ON m.account_id = a.account_id
    WHERE ... GROUP BY a.account_id, a.account_name ORDER BY ... LIMIT 50
  Prefer this pivoted shape whenever the user wants periods across specific
  entities — it renders as the table they expect.
- Add LIMIT 50 unless the query is a pure COUNT/aggregation.
- For per-account rankings/aggregations ("top accounts by revenue"), GROUP BY the
  account, ORDER BY the aggregate, and still add LIMIT 50.
- Use LOWER(account_name) LIKE '%...%' with lowercase values for fuzzy text matching.
- Correct obvious typos in the user's words before building the LIKE pattern (e.g.
  "companise" -> "companies", "capitl managment" -> "capital management").
- For conceptual/category queries with no literal keyword given (e.g. "law firms",
  "wealth management", "universities"), expand to the relevant industry keywords you'd expect
  in a company name (e.g. law firms -> '%law%' OR '%llp%' OR '%partners%') and combine with
  OR conditions, explaining your interpretation in "reason".
- For counting questions ("how many..."), use SELECT COUNT(*) AS count ... and do not add LIMIT.
- Money amounts are USD; round money in SELECT with ROUND(x, 2) when aggregating.
- Never invent column names not in the schema. Respect the SPARSE rules below exactly.

{SCHEMA_DESCRIPTION}
"""

ANSWER_SYSTEM_PROMPT = f"""You are AccountSearch AI, a friendly, concise assistant in the style of
Claude, answering questions over a travel-booking CRM (500,000 accounts with {TOTAL_MONTHS} months of
booking history, {FIRST_MONTH} to {LATEST_MONTH}; a month absent from the data means zero bookings).
Using ONLY the rows provided below (never invent data), answer the user's question
conversationally. Cite account_id values when referencing specific accounts, mention the total
count of matching records, format money as USD, and call out notable patterns (booking trends,
revenue concentration, gaps since last reservation). If the rows are empty, say so plainly and
suggest a broader query. Keep the answer focused and skimmable - short paragraphs or a brief
bulleted list."""

FORBIDDEN_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "attach", "detach", "pragma",
    "replace", "truncate", "grant", "revoke", "vacuum", "reindex", "sqlite_master",
}


# --------------------------------------------------------------------------
# Instant in-memory fuzzy index (no AI call, ~ms). Built once per cold start.
# Handles literal/typo'd/concept name searches; complex questions still go
# through the AI SQL pipeline.
# --------------------------------------------------------------------------

STOPWORDS = {
    "find", "search", "show", "list", "get", "give", "me", "all", "any", "anything",
    "account", "accounts", "company", "companies", "name", "names", "named", "called",
    "with", "in", "the", "a", "an", "of", "to", "that", "have", "has", "having",
    "contain", "contains", "containing", "related", "and", "or", "is", "are", "do",
    "does", "please", "how", "many", "much", "count", "number", "them", "it", "its",
}

CONCEPT_MAP = {
    "law": ["law", "llp", "legal", "attorney", "partners"],
    "legal": ["law", "llp", "legal", "attorney", "partners"],
    "lawyer": ["law", "llp", "legal", "attorney"],
    "lawyers": ["law", "llp", "legal", "attorney"],
    "partnership": ["llp", "partners", "partnership"],
    "partnerships": ["llp", "partners", "partnership"],
    "firm": ["llp", "llc", "inc", "group", "partners"],
    "firms": ["llp", "llc", "inc", "group", "partners"],
    "wealth": ["wealth", "capital", "advisors", "advisers", "investors", "asset"],
    "investor": ["investors", "capital", "invest", "ventures", "equity", "partners"],
    "investors": ["investors", "capital", "invest", "ventures", "equity", "partners"],
    "investment": ["capital", "investors", "invest", "ventures", "equity", "asset"],
    "university": ["university", "college", "institute", "research", "academy"],
    "universities": ["university", "college", "institute", "research", "academy"],
    "school": ["school", "university", "college", "academy", "education"],
    "research": ["research", "institute", "laboratories", "labs", "sciences"],
    "bank": ["bank", "bancorp", "banking", "financial", "trust"],
    "banks": ["bank", "bancorp", "banking", "financial", "trust"],
    "tech": ["tech", "technologies", "technology", "software", "systems", "digital"],
    "technology": ["tech", "technologies", "technology", "software", "systems"],
    "health": ["health", "medical", "healthcare", "hospital", "pharma", "clinic"],
    "healthcare": ["health", "medical", "healthcare", "hospital", "pharma"],
    "medical": ["medical", "health", "healthcare", "hospital", "pharma", "clinic"],
    "insurance": ["insurance", "assurance", "mutual"],
    "hotel": ["hotel", "hotels", "resort", "hospitality", "inn"],
    "hotels": ["hotel", "hotels", "resort", "hospitality", "inn"],
    "media": ["media", "broadcasting", "entertainment", "communications", "news"],
    "energy": ["energy", "power", "electric", "gas", "oil", "utilities"],
}

REFINEMENT_RE = re.compile(
    r"^\s*(now|only|just|also|and|but|then|them|those|these|that one|from those|of those|filter)\b",
    re.IGNORECASE,
)
NUMERIC_FILTER_RE = re.compile(
    r"\b(?:id|ids)\s*(over|above|greater than|more than|under|below|less than)\s*([\d,]+)",
    re.IGNORECASE,
)


_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _norm_text(s: str, split_camel: bool = False) -> str:
    if split_camel:
        s = _CAMEL_RE.sub(" ", s)  # 'HealthCare' -> 'Health Care' (index side)
    s = s.replace("'", "").replace("’", "")  # O'Brien / OBrien match either way
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _skeleton(token: str) -> str:
    """Consonant skeleton: 'abndence' and 'abundance' both -> 'bndnc'."""
    return re.sub(r"[aeiou]+", "", token)


def _allowed_dist(token: str) -> int:
    """Max edit distance for fuzzy matching, by length: short tokens are
    exact-only so 'bank' can never merge with 'rank'."""
    return 0 if len(token) < 5 else 1 if len(token) < 7 else 2


def _deletes(token: str, max_d: int) -> set:
    """All variants of `token` with up to max_d characters deleted (SymSpell)."""
    out, frontier = {token}, {token}
    for _ in range(max_d):
        frontier = {t[:i] + t[i + 1:] for t in frontier for i in range(len(t))}
        out |= frontier
    return out


def _damerau(a: str, b: str, cap: int) -> int:
    """Damerau-Levenshtein distance (transposition = 1 edit), early-exit > cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


INDEX_VERSION = 2
INDEX_PICKLE_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "search_index.pkl",
    Path.cwd() / "search_index.pkl",
]


def _index_source_rows():
    """(id, account_id, name) rows for the fuzzy index: the local DB when
    present, else the bundled CSV (same names, same row order) — so a remote-DB
    deployment never streams 500k rows over the network at cold start."""
    db_path = _local_db()
    if db_path is not None:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            yield from conn.execute("SELECT id, account_id, account_name FROM accounts ORDER BY id")
        finally:
            conn.close()
        return
    csv_path = next((p for p in CSV_CANDIDATES if p.exists()), None)
    if csv_path is None:
        raise FileNotFoundError("Neither accounts.db nor the accounts CSV found.")
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.reader(f), start=1):
            if r:
                yield (i, int(r[0]), r[1])


def _build_index() -> dict:
    """entries + vocabulary frequency + SymSpell deletes + consonant skeletons.
    The vocabulary comes from the data itself, so misspellings stored in account
    names ('managment') are vocabulary tokens too — expanding a query token to
    ALL nearby vocabulary tokens therefore fixes typos in BOTH directions."""
    entries, freq, skeletons = [], {}, {}
    for i, account_id, name in _index_source_rows():
        norm = _norm_text(name, split_camel=True)
        entries.append((i, account_id, name, norm, norm.replace(" ", "")))
        for tok in set(norm.split()):
            if len(tok) > 1:
                freq[tok] = freq.get(tok, 0) + 1
                skeletons.setdefault(_skeleton(tok), set()).add(tok)
    deletes = {}
    for tok in freq:
        for v in _deletes(tok, _allowed_dist(tok)):
            deletes.setdefault(v, []).append(tok)
    return {"version": INDEX_VERSION, "db_mtime": _db_mtime(),
            "entries": entries, "freq": freq, "deletes": deletes, "skeletons": skeletons}


def _db_mtime() -> float:
    """Freshness stamp for the pickled index: whichever source it builds from."""
    src = _local_db() or next((p for p in CSV_CANDIDATES if p.exists()), None)
    return src.stat().st_mtime if src else 0.0


def _load_index() -> dict:
    for p in INDEX_PICKLE_CANDIDATES:
        if p.exists():
            try:
                with open(p, "rb") as f:
                    idx = pickle.load(f)
                if idx.get("version") == INDEX_VERSION and idx.get("db_mtime") == _db_mtime():
                    return idx
            except Exception:  # noqa: BLE001 - stale/corrupt pickle: rebuild
                pass
    idx = _build_index()
    for p in INDEX_PICKLE_CANDIDATES:
        try:
            with open(p, "wb") as f:
                pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
            break
        except OSError:  # read-only deployment filesystem: in-memory only
            continue
    return idx


_IDX = _load_index()
_ENTRIES, _FREQ, _DELETES, _SKELETONS = (_IDX["entries"], _IDX["freq"],
                                         _IDX["deletes"], _IDX["skeletons"])


def _expand_token(token: str) -> set:
    """The full typo cluster for a query token: every vocabulary token within
    its allowed edit distance (plus consonant-skeleton rescues), guarded so
    unrelated words never merge. Known words expand only to much rarer
    (typo-of-it) or much more frequent (it's-the-typo) siblings."""
    known = token in _FREQ
    out = {token} if known else set()
    cap = _allowed_dist(token)
    cands = set()
    if cap:
        for v in _deletes(token, cap):
            cands.update(_DELETES.get(v, ()))
    cands.update(_SKELETONS.get(_skeleton(token), ()))
    cands.discard(token)
    sk, scored = _skeleton(token), []
    for c in cands:
        if c[0] != token[0]:  # typos almost never hit the first letter
            continue
        if _skeleton(c) == sk and abs(len(c) - len(token)) <= 3:
            d = 0  # vowel-mangling rescue ('abndence' -> 'abundance')
        else:
            if abs(len(c) - len(token)) > 2:
                continue
            d = _damerau(token, c, 2)
            if d > min(cap, _allowed_dist(c)):
                continue
        if known:
            ft, fc = _FREQ[token], _FREQ[c]
            # similar frequency = probably a genuinely different word: skip
            if not (fc <= ft * 0.25 or fc >= ft * 4):
                continue
        scored.append((d, -_FREQ[c], c))
    out.update(c for _, _, c in sorted(scored)[:6])
    return out


def instant_search(query: str) -> dict | None:
    """Millisecond fuzzy search over account names. Returns None when the query
    is out of scope for the index (no usable terms) so the AI path can run."""
    q = _norm_text(query)

    numeric = None
    m = NUMERIC_FILTER_RE.search(query)
    if m:
        op, num = m.group(1).lower(), int(m.group(2).replace(",", ""))
        numeric = (op in ("over", "above", "greater than", "more than"), num)
        q = _norm_text(NUMERIC_FILTER_RE.sub(" ", query))

    tokens = [t for t in q.split() if t not in STOPWORDS and len(t) > 1 and not t.isdigit()]

    # term groups: each query token expands to {itself, its full typo cluster,
    # concept synonyms} — the cluster covers query-side AND data-side typos
    groups = []
    for tok in tokens:
        direct = {tok} | _expand_token(tok)
        concept = set()
        for t in direct:
            concept.update(CONCEPT_MAP.get(t, []))
        concept -= direct
        group = {"direct": direct, "concept": concept}
        if group not in groups:  # dedupe synonymous tokens (e.g. "law" + "legal")
            groups.append(group)

    if not groups and numeric is None:
        return None

    scored = []
    for row_id, account_id, name, norm, squashed in _ENTRIES:
        if numeric is not None:
            greater, num = numeric
            if (account_id > num) != greater and account_id != num:
                continue
        hit_groups, score = 0, 0.0
        name_tokens = norm.split()
        for group in groups:
            group_hit = 0.0
            for term in group["direct"]:
                if term in norm:
                    group_hit = max(group_hit, 2.0 if term in name_tokens else 1.5)
                elif term in squashed:  # 'healthcare' query vs 'Health Care' name
                    group_hit = max(group_hit, 1.5)
            if not group_hit:  # concept synonyms are weaker evidence than the word itself
                for term in group["concept"]:
                    if term in norm:
                        group_hit = max(group_hit, 0.75)
            if group_hit:
                hit_groups += 1
                score += group_hit
        if groups and not hit_groups:
            continue
        scored.append((hit_groups, score, row_id, account_id, name))

    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    # keep only the best tier: rows matching as many term groups as the best row does
    max_hit = scored[0][0]
    pool = [s for s in scored if s[0] == max_hit] if groups else scored
    return {
        "total": len(pool),
        "rows": [[r[2], r[3], r[4]] for r in pool[:MAX_ROWS_TO_MODEL]],
        "columns": ["id", "account_id", "account_name"],
    }


# --------------------------------------------------------------------------
# SQL validation (same rules as the Streamlit app)
# --------------------------------------------------------------------------

class SQLValidationError(Exception):
    pass


def validate_sql(sql: str) -> str:
    if not sql or not sql.strip():
        raise SQLValidationError("Empty SQL query.")
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].strip()
    if ";" in s:
        raise SQLValidationError("Multiple statements are not allowed.")
    low = s.lower()
    # WITH (CTE) is read-only SELECT syntax — needed for quarter/period bucketing;
    # the forbidden-keyword scan below still blocks any write inside the CTE body.
    if not (low.startswith("select") or low.startswith("with")):
        raise SQLValidationError("Only SELECT statements (optionally WITH ... SELECT) are allowed.")
    tokens = set(re.findall(r"[a-z_]+", low))
    banned = tokens & FORBIDDEN_KEYWORDS
    if banned:
        raise SQLValidationError(f"Forbidden keyword(s) used: {', '.join(banned)}")
    if not any(t in low for t in TABLES):
        raise SQLValidationError(f"Query must select from {' or '.join(TABLES)}.")
    is_aggregate = any(k in low for k in ("count(", "group by", "avg(", "sum(", "max(", "min(", "total("))
    limit_match = re.search(r"\blimit\s+(\d+)", low)
    if limit_match:
        if int(limit_match.group(1)) > MAX_ROWS_TO_MODEL:
            s = re.sub(r"\blimit\s+\d+", f"LIMIT {MAX_ROWS_TO_MODEL}", s, flags=re.IGNORECASE)
    elif not is_aggregate:
        s = f"{s} LIMIT {MAX_ROWS_TO_MODEL}"
    return s


# --------------------------------------------------------------------------
# OpenRouter (urllib, non-streaming)
# --------------------------------------------------------------------------

def _openrouter_post(body: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://vercel.app",
            "X-Title": "AccountSearch AI",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} from {body['model']}: {detail}") from e


def call_model(messages: list, model: str, api_key: str, max_tokens: int, temperature: float) -> str:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Reasoning models burn 30s+ on hidden chain-of-thought; ask providers to skip it.
        "reasoning": {"enabled": False},
    }
    try:
        data = _openrouter_post(body, api_key)
    except RuntimeError as e:
        # Some endpoints (e.g. gpt-oss :free) refuse to disable reasoning; retry
        # with it on and a bigger budget so reasoning tokens don't starve the answer.
        if "easoning" not in str(e) or "mandatory" not in str(e):
            raise
        body.pop("reasoning")
        body["max_tokens"] = max(max_tokens, 4000)
        data = _openrouter_post(body, api_key)
    choice = data["choices"][0]
    content = choice["message"].get("content")
    if not content:
        raise RuntimeError(f"{model} returned no content (finish_reason={choice.get('finish_reason')}).")
    if choice.get("finish_reason") == "length":
        raise RuntimeError(f"{model} response truncated by token budget.")
    return content


def with_retries(fn, preferred_model: str):
    """fn(model) -> result. Cycles primary/fallback for RETRY_ROUNDS rounds."""
    candidates = [m for m in MODELS if m == preferred_model] + [m for m in MODELS if m != preferred_model]
    attempts = candidates * RETRY_ROUNDS
    last_err = None
    for i, model in enumerate(attempts):
        if i > 0:
            time.sleep(RETRY_DELAY_SECONDS)
        try:
            return fn(model), model
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"All model attempts failed: {last_err}")


def extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        text = brace.group(0)
    return json.loads(text)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

ANALYTICAL_RE = re.compile(
    r"\b(compar\w*|versus|vs\b|averag\w*|avg\b|mean\b|median\w*|pattern\w*|insight\w*|"
    r"analy\w*|trend\w*|distribut\w*|breakdown\w*|summar\w*|explain\w*|why\b|"
    r"most common|least\b|top\s+\d+|differen\w*|correlat\w*|categor\w*|classif\w*|"
    r"which kind|what kind)",
    re.IGNORECASE,
)

# Questions about booking activity / money / recency live in monthly_activity —
# the instant name index knows nothing about them, so they must take the AI path,
# and they deserve a conversational answer rather than a row-count template.
ACTIVITY_RE = re.compile(
    r"\b(book\w*|trip\w*|revenue\w*|profit\w*|reservation\w*|reserv\w*|"
    r"spend\w*|spent|earn\w*|churn\w*|inactive|silent|lapsed|"
    r"month\w*|year\w*|quarter\w*|recent\w*|since|elite|last\s+(?:booked|connected))\b",
    re.IGNORECASE,
)


def needs_analytical_answer(query: str, sql: str) -> bool:
    return (bool(ANALYTICAL_RE.search(query)) or bool(ACTIVITY_RE.search(query))
            or "group by" in sql.lower())


def template_answer(rows: list, columns: list) -> str:
    """Instant local summary for simple queries — no second model call."""
    if not rows:
        return ("No accounts matched, even after broadening the search. "
                "Try different or more general keywords.")
    # Single aggregate value (e.g. COUNT)
    if len(rows) == 1 and len(columns) == 1:
        label = columns[0].replace("_", " ")
        return f"**{rows[0][0]}** — {label} for your query."
    total = len(rows)
    name_idx = columns.index("account_name") if "account_name" in columns else None
    if name_idx is not None:
        examples = ", ".join(str(r[name_idx]) for r in rows[:3])
        more = f" — e.g. {examples}" if examples else ""
    else:
        more = ""
    cap_note = f" (showing the first {MAX_ROWS_TO_MODEL})" if total >= MAX_ROWS_TO_MODEL else ""
    return (f"Found **{total} matching account{'s' if total != 1 else ''}**{cap_note}{more}. "
            f"Full results in the table below.")


def run_pipeline(query: str, history: list, api_key: str, preferred_model: str,
                 force_ai: bool = False) -> dict:
    history = [
        {"role": m["role"], "content": str(m["content"])}
        for m in history[-MAX_HISTORY_MESSAGES:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    # Instant path: plain name searches never need a model. Skip it when the
    # client explicitly asks for AI (mode="ai"), for analytical or booking-
    # activity questions (the name index has no activity data), and for
    # follow-up refinements that depend on conversation context.
    is_refinement = bool(history) and bool(REFINEMENT_RE.search(query))
    if (not force_ai and not needs_analytical_answer(query, "")
            and not ACTIVITY_RE.search(query) and not is_refinement):
        hit = instant_search(query)
        if hit and hit["rows"]:
            wants_count = bool(re.search(r"\bhow many|count\b", query, re.IGNORECASE))
            if wants_count:
                answer = f"**{hit['total']}** matching accounts."
            else:
                examples = ", ".join(r[2] for r in hit["rows"][:3])
                cap = f" (showing the first {MAX_ROWS_TO_MODEL})" if hit["total"] > MAX_ROWS_TO_MODEL else ""
                answer = (f"Found **{hit['total']} matching account"
                          f"{'s' if hit['total'] != 1 else ''}**{cap} — e.g. {examples}. "
                          f"Full results in the table below.")
            return {
                "answer": answer,
                "sql": None,
                "reason": "Matched instantly against the in-memory fuzzy name index (typo- and concept-aware) — no AI call needed.",
                "rows": hit["rows"] if not wants_count else hit["rows"],
                "total_rows": hit["total"],
                "columns": hit["columns"],
                "model_used": [],
            }

    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured on the server.")

    plan_messages = (
        [{"role": "system", "content": SQL_SYSTEM_PROMPT}]
        + history
        + [{"role": "user", "content": query}]
    )

    def plan_step(model):
        content = call_model(plan_messages, model, api_key, max_tokens=2000, temperature=0.1)
        plan = extract_json(content)
        if "sql" not in plan:
            raise ValueError("Model response missing 'sql' field.")
        return plan

    plan, plan_model = with_retries(plan_step, preferred_model)
    try:
        sql = validate_sql(plan["sql"])
    except SQLValidationError as ve:
        # Give the model one shot at fixing a rejected query instead of erroring.
        fix_messages = plan_messages + [
            {"role": "assistant", "content": json.dumps(plan)},
            {"role": "user", "content": (
                f"Your SQL was rejected by validation: {ve} "
                "Rewrite it as ONE read-only statement (WITH ... SELECT is allowed) "
                "that satisfies every rule, and respond with the same JSON format.")},
        ]
        plan, plan_model = with_retries(
            lambda m: extract_json(call_model(fix_messages, m, api_key, 2000, 0.1)),
            preferred_model,
        )
        sql = validate_sql(plan["sql"])
    reason = plan.get("reason", "")

    conn = load_db()
    sql_error = None
    rows, columns = [], []
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except sqlite3.Error as e:
        sql_error = str(e)

    if sql_error or not rows:
        note = (
            f"Your previous SQL failed with error: {sql_error}. Broaden the query."
            if sql_error
            else "Your previous SQL returned 0 rows. Broaden the query: use fuzzy LIKE "
            "matching, relax the keywords, or add more OR conditions covering synonyms."
        )
        retry_messages = plan_messages + [
            {"role": "assistant", "content": json.dumps(plan)},
            {"role": "user", "content": note},
        ]
        try:
            plan2, plan_model = with_retries(
                lambda m: (lambda c: (extract_json(c)))(call_model(retry_messages, m, api_key, 2000, 0.1)),
                preferred_model,
            )
            sql2 = validate_sql(plan2["sql"])
            cur = conn.execute(sql2)
            columns2 = [d[0] for d in cur.description]
            rows2 = cur.fetchall()
            if rows2:
                sql, reason, rows, columns = sql2, plan2.get("reason", reason), rows2, columns2
        except Exception:  # noqa: BLE001 - keep the original empty result
            pass

    # Fast mode: simple find/list/count queries get an instant built-in summary.
    # Only analytical questions pay for a second model call.
    if needs_analytical_answer(query, sql) and rows:
        row_dicts = [dict(zip(columns, r)) for r in rows[:ANSWER_ROWS_TO_MODEL]]
        answer_messages = (
            [{"role": "system", "content": ANSWER_SYSTEM_PROMPT}]
            + history
            + [{
                "role": "user",
                "content": (
                    f"User question: {query}\n\n"
                    f"Matching rows ({len(rows)} total, showing up to {ANSWER_ROWS_TO_MODEL}):\n"
                    f"{json.dumps(row_dicts, default=str)}"
                ),
            }]
        )
        answer, answer_model = with_retries(
            lambda m: call_model(answer_messages, m, api_key, max_tokens=4000, temperature=0.4),
            preferred_model,
        )
        models_used = sorted({plan_model, answer_model})
    else:
        answer = template_answer(rows, columns)
        models_used = [plan_model]

    return {
        "answer": answer,
        "sql": sql,
        "reason": reason,
        "rows": [list(r) for r in rows[:MAX_ROWS_TO_MODEL]],
        "total_rows": len(rows),
        "columns": columns,
        "model_used": models_used,
    }


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802 - Vercel convention
        # No early key check: the instant path needs no key; run_pipeline
        # raises a clear error if the AI path is reached without one.
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            query = (body.get("query") or "").strip()
            if not query:
                self._send(400, {"error": "Missing 'query'."})
                return
            history = body.get("history") or []
            model = body.get("model") or MODELS[0]
            if model not in MODELS:
                model = MODELS[0]
            force_ai = body.get("mode") == "ai"
            result = run_pipeline(query, history, api_key, model, force_ai=force_ai)
            self._send(200, result)
        except Exception as e:  # noqa: BLE001
            self._send(502, {"error": str(e)[:500]})
