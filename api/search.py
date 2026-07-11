"""AccountSearch AI — Vercel Python serverless function.

POST /api/search  {"query": "...", "history": [{"role","content"}...], "model": "optional-id"}
->  {"answer", "sql", "reason", "rows", "columns", "model_used"}   or   {"error": "..."}

Stdlib only (urllib instead of requests) to keep the function bundle tiny.
The 10k-row CSV ships with the deployment and is loaded into in-memory
SQLite on each invocation (~75 ms).
"""

import csv
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Paid tier: fast, reliable, ~$0.00025/query at defaults. Override without a code
# change by setting the MODEL_IDS env var (comma-separated, primary first).
MODELS = [
    m.strip()
    for m in os.environ.get("MODEL_IDS", "openai/gpt-oss-120b,openai/gpt-4o-mini").split(",")
    if m.strip()
]
TABLE = "accounts"
MAX_ROWS_TO_MODEL = 50  # hard cap on rows fetched/displayed
ANSWER_ROWS_TO_MODEL = 25  # rows sent to the answer model (smaller = faster)
MAX_HISTORY_MESSAGES = 20
RETRY_ROUNDS = 2
RETRY_DELAY_SECONDS = 4
CALL_TIMEOUT_SECONDS = 90

CSV_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "Accounts-VadlamudiRamesh.csv",
    Path.cwd() / "Accounts-VadlamudiRamesh.csv",
]

SCHEMA_DESCRIPTION = f"""
Table: {TABLE}
Columns:
- id (INTEGER, primary key, row number)
- account_id (INTEGER) - the original account number from the source system
- account_name (TEXT) - the company / account name

There are 10,000 rows total. Every column is exact as imported from a real CRM
export; there are no other fields (no dates, costs, or categories) - all
matching must be done on account_id and account_name.
"""

SQL_SYSTEM_PROMPT = f"""You are a database search planner for a company accounts table. Given the
schema below and a user question, respond ONLY with a single JSON object of the form:
{{"sql": "<one SELECT query>", "reason": "<one sentence on how you interpreted the request>"}}

Rules:
- SELECT only, from the {TABLE} table. No INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA/ATTACH.
- Exactly one statement, no semicolons.
- Add LIMIT 50 unless the query is a pure COUNT/aggregation.
- Use LOWER(account_name) LIKE '%...%' with lowercase values for fuzzy text matching.
- Correct obvious typos in the user's words before building the LIKE pattern (e.g.
  "companise" -> "companies", "capitl managment" -> "capital management").
- For conceptual/category queries with no literal keyword given (e.g. "law firms",
  "wealth management", "universities"), expand to the relevant industry keywords you'd expect
  in a company name (e.g. law firms -> '%law%' OR '%llp%' OR '%partners%'; wealth management ->
  '%wealth%' OR '%capital%' OR '%advisors%' OR '%investors%'; universities -> '%university%'
  OR '%college%' OR '%institute%') and combine with OR conditions, explaining your
  interpretation in "reason".
- For counting questions ("how many..."), use SELECT COUNT(*) AS count ... and do not add LIMIT.
- Never invent column names not in the schema.

{SCHEMA_DESCRIPTION}
"""

ANSWER_SYSTEM_PROMPT = """You are AccountSearch AI, a friendly, concise assistant in the style of
Claude. Using ONLY the rows provided below (never invent data), answer the user's question
conversationally. Cite account_id values when referencing specific accounts, mention the total
count of matching records, and call out any notable patterns you notice in the names. If the
rows are empty, say so plainly and suggest a broader query. Keep the answer focused and
skimmable - short paragraphs or a brief bulleted list."""

FORBIDDEN_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "attach", "detach", "pragma",
    "replace", "truncate", "grant", "revoke", "vacuum", "reindex", "sqlite_master",
}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_db() -> sqlite3.Connection:
    csv_path = next((p for p in CSV_CANDIDATES if p.exists()), None)
    if csv_path is None:
        raise FileNotFoundError("Accounts CSV not found in deployment bundle.")
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, account_id INTEGER, account_name TEXT)")
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = [(i, int(r[0]), r[1]) for i, r in enumerate(csv.reader(f), start=1) if r]
    cur.executemany(f"INSERT INTO {TABLE} VALUES (?,?,?)", rows)
    conn.commit()
    return conn


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


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _trigrams(s: str) -> set:
    padded = f"  {s} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def _skeleton(token: str) -> str:
    """Consonant skeleton: 'abndence' and 'abundance' both -> 'bndnc'."""
    return re.sub(r"[aeiou]+", "", token)


def _build_index():
    csv_path = next((p for p in CSV_CANDIDATES if p.exists()), None)
    if csv_path is None:
        raise FileNotFoundError("Accounts CSV not found in deployment bundle.")
    entries, vocab, skeletons = [], {}, {}
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.reader(f), start=1):
            if not r:
                continue
            name = r[1]
            norm = _norm_text(name)
            entries.append((i, int(r[0]), name, norm))
            for tok in norm.split():
                if len(tok) > 1:
                    vocab.setdefault(tok, _trigrams(tok))
                    skeletons.setdefault(_skeleton(tok), set()).add(tok)
    return entries, vocab, skeletons


_ENTRIES, _VOCAB, _SKELETONS = _build_index()


def _similar_tokens(token: str) -> set:
    """Vocabulary tokens near `token` by trigram Dice similarity. Always
    searched (even when the token exists verbatim) because the data itself
    contains typos — e.g. querying 'managment' must also match 'management'."""
    tg = _trigrams(token)
    best, best_score = None, 0.0
    for vocab_tok, vtg in _VOCAB.items():
        if vocab_tok == token:
            continue
        inter = len(tg & vtg)
        if not inter:
            continue
        score = 2 * inter / (len(tg) + len(vtg))
        if score > best_score:
            best, best_score = vocab_tok, score
    out = set()
    if token in _VOCAB:
        out.add(token)
        if best_score >= 0.70:  # verbatim hit: only add a very close sibling
            out.add(best)
    elif best_score >= 0.55:  # unknown word: take the nearest correction
        out.add(best)
    if token not in _VOCAB:
        # consonant-skeleton match rescues typos trigrams miss ('abndence'->'abundance')
        for cand in _SKELETONS.get(_skeleton(token), ()):
            if cand[0] == token[0] and abs(len(cand) - len(token)) <= 3:
                out.add(cand)
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

    # term groups: each query token expands to {itself, typo-correction, concept synonyms}
    groups = []
    for tok in tokens:
        direct = {tok} | _similar_tokens(tok)
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
    for row_id, account_id, name, norm in _ENTRIES:
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
    if not low.startswith("select"):
        raise SQLValidationError("Only SELECT statements are allowed.")
    tokens = set(re.findall(r"[a-z_]+", low))
    banned = tokens & FORBIDDEN_KEYWORDS
    if banned:
        raise SQLValidationError(f"Forbidden keyword(s) used: {', '.join(banned)}")
    if TABLE not in low:
        raise SQLValidationError(f"Query must select from the {TABLE} table.")
    is_aggregate = "count(" in low or "group by" in low or "avg(" in low or "sum(" in low
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

def call_model(messages: list, model: str, api_key: str, max_tokens: int, temperature: float) -> str:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Reasoning models burn 30s+ on hidden chain-of-thought; ask providers to skip it.
        "reasoning": {"enabled": False},
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
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
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} from {model}: {body}") from e
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


def needs_analytical_answer(query: str, sql: str) -> bool:
    return bool(ANALYTICAL_RE.search(query)) or "group by" in sql.lower()


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


def run_pipeline(query: str, history: list, api_key: str, preferred_model: str) -> dict:
    history = [
        {"role": m["role"], "content": str(m["content"])}
        for m in history[-MAX_HISTORY_MESSAGES:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    # Instant path: plain name searches never need a model. Skip it for
    # analytical questions and for follow-up refinements that depend on
    # conversation context (the AI path understands those).
    is_refinement = bool(history) and bool(REFINEMENT_RE.search(query))
    if not needs_analytical_answer(query, "") and not is_refinement:
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
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            self._send(500, {"error": "OPENROUTER_API_KEY is not configured on the server."})
            return
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
            result = run_pipeline(query, history, api_key, model)
            self._send(200, result)
        except Exception as e:  # noqa: BLE001
            self._send(502, {"error": str(e)[:500]})
