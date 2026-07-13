# TODO — Search improvements

## 1. Bidirectional typo-tolerant search (priority — take up later)

**Problem:** Searches often contain spelling mistakes and must still return proper
data. The data itself also contains misspelled entries (e.g. "managment" stored in
account names) and those rows must be found when the user types the correct word.

**Gap in current code:** `_similar_tokens()` in `api/search.py` only adds the single
best similar token, so if data contains several variants ("management", "managment",
"managemnt") a query only picks up one and silently drops rows with the others. It
also scans the entire vocabulary per query token.

**Design:** vocabulary is built from the data, so data-side typos are already vocab
tokens. Expand each query token to ALL vocabulary tokens within a small edit
distance — covers both directions automatically.

DONE 2026-07-12 (all 12 verification checks pass; real data typos covered:
management/managment/mangement/managemet, capital/captial):

- [x] SymSpell deletes dictionary (9,226 vocab tokens → 177,506 delete variants;
      Damerau-Levenshtein verification with early exit)
- [x] Guards: length-scaled distance (<5 exact, 5-6 d1, 7+ d2), same first
      letter, frequency sanity (rare-sibling ≤0.25× or correction ≥4×) —
      bank≠rank verified
- [x] `_similar_tokens` → `_expand_token`: full typo clusters (capped at 6 best),
      skeleton rescue kept for vowel-mangling beyond d2
- [x] Index pickled to search_index.pkl (gitignored; auto-rebuilt when stale via
      INDEX_VERSION + DB mtime; build_search_index.py forces it) — cold start
      2.5s → 0.43s
- [x] Both directions verified end-to-end: "capitl managment" finds correct rows;
      "management" returns 8,211 rows incl. all misspelled-data variants
- [x] Normalization: camelCase split at index time, apostrophe stripping both
      sides, squashed-name matching ("healthcare" ↔ "Health Care", 49 names)
- [ ] (Optional, second pass) Phonetic layer (Double-Metaphone-style keys) for
      misspellings beyond edit distance 2 ("filadelphia" → "philadelphia")
- [x] Port the same cluster expansion to the client-side Quick Search tab
      (done 2026-07-12: JS mirror of SymSpell + Damerau + guards in index.html;
      parity-tested against the Python side — identical clusters and match
      counts; index builds in ~90ms in the browser, queries ~85ms; bonus:
      ?q=<query> URL prefill for shareable searches)

## 2. Other search improvements (from earlier discussion, roughly by impact)

- [ ] Ship a prebuilt SQLite `.db` (FTS5, trigram tokenizer) instead of the CSV —
      kills the per-request CSV parse and cold-start index build; BM25 ranking and
      phrase/prefix/boolean queries for free. Note: FTS5 does NOT do typo
      correction — item 1 stays in front of it.
- [ ] IDF-weight token scoring (rare words dominate; "capital"/"inc"/"group"
      shouldn't rank like discriminating words) — or just use FTS5 BM25
- [ ] Semantic expansion via embeddings at the *vocabulary* level (embed ~20–50k
      unique tokens, not 500k rows) to replace the hand-written CONCEPT_MAP
- [ ] Phrase + exact-match operators: quoted phrases, prefix boost, exact
      account_id lookup short-circuit
- [ ] Warm-instance LRU query cache (in-module dict keyed on normalized query)
- [ ] If data outgrows the bundle: hosted DB (Turso/libSQL, or Neon/Supabase
      Postgres with pg_trgm + FTS + pgvector)

## 3. Analyst dataset — monthly booking activity (started 2026-07-12)

Extend each account with monthly history so an analyst can ask booking/revenue/
recency questions. Agreed spec:

- 24 months: 2024-07 .. 2026-06; rows only for months WITH bookings (no row = 0)
- Tier mix: 15% never, 40% occasional (1–10/mo, gaps), 25% good (~20/mo),
  10% best (~30/mo), 5% elite (~40/mo, sometimes skips), 5% churned (silent 6+ mo)
- Per trip: ~$1,000 revenue / ~$200 profit; ~10% cheap trips $100/$10;
  no bookings → no revenue → no profit
- last_reservation_date snapshot on each monthly row (carry-forward via MAX())
- Local SQLite first (`accounts.db`, gitignored); Vercel hosting later (Turso?)

- [x] Copy Accounts-VadlamudiRamesh.csv → rawdata.csv (future real-export format)
- [x] `generate_synthetic_db.py` — deterministic generator (seed 20260712)
- [x] Verify analyst scenarios against accounts.db (tier averages, months since
      last booking, yearly differentiation, zero-booking accounts) — all pass,
      7.49M rows / 648 MB / 36s generation
- [x] Wire the chat app to the new schema (2026-07-12): api/search.py now opens
      accounts.db read-only, planner prompt teaches both tables + sparse-row
      rules (MAX(month) recency, NOT EXISTS never-booked, SUM/24.0 true monthly
      avg), activity questions bypass the instant name index and get model
      answers; index.html chips/badge updated; smoke-tested end-to-end (needs
      python3.10+, e.g. python3.13 — system 3.9 chokes on `dict | None`)
- [x] Host the DB for the Vercel deployment (done 2026-07-12): Turso database
      `accounts-search` (org srikanth9, aws-ap-northeast-1), 500k accounts +
      7.49M activity rows verified remotely. api/_turso.py = stdlib
      Hrana-over-HTTP client; endpoints prefer local accounts.db, fall back to
      Turso. Read-only DB token + URL in .env (set same in Vercel env vars).
      Gotchas hit: --from-file requires WAL mode (generator now sets it);
      HTTP/2 upload works once WAL is fixed (106s vs 2.4h over HTTP/1.1);
      destroy invalidates DB tokens; read-only token forbids TEMP TABLE →
      deployed analytics serves precomputed analytics_cache.json (85 KB,
      4 preset windows, rebuild with build_analytics_cache.py)
- [ ] Deploy: push to GitHub (triggers Vercel), add TURSO_DATABASE_URL +
      TURSO_AUTH_TOKEN (+ MODEL_IDS from bake-off) to Vercel env vars, verify
      one AI query + Analytics tab on the live URL

## 4. Analytics dashboard (done 2026-07-12)

- [x] `api/analytics.py` — GET endpoint aggregating accounts.db (KPIs, monthly
      trend, segments, recency buckets, top-10 revenue); warm-instance cache
      keyed on DB mtime (~4s cold, instant after)
- [x] "📊 Analytics" tab in index.html — KPI stat tiles + 5 hand-rolled SVG
      charts (line w/ crosshair tooltip, 2-series line, ordinal donut, column,
      horizontal bar); every chart click-toggles its underlying data table with
      CSV download; palette validated per the dataviz method (dark surface)
- [x] Round 2 (2026-07-12): KPI month-over-month deltas (▲/▼/flat, green/red/
      muted), "New vs lost accounts per month" wide line chart (new = first-ever
      booking, lost = 3+ months silent; opening month and last 3 months
      excluded as cohort artifacts), "Churn risk" action table (top 20 lifetime-
      revenue accounts silent 3+ months, CSV export)
- [x] Round 3 (2026-07-12): date-range filter row (Last 3/6/12 mo · All history;
      API takes ?from&to, per-window server cache + client cache, refetch holds
      the previous render at reduced opacity); "Where the revenue comes from"
      wide card (accounts-share vs revenue-share 100% stacked bars + Pareto
      top-1%/top-10% stat); drill-down — clicking a donut slice, recency bar, or
      mix band reveals that bucket's top-20 accounts w/ CSV (lists precomputed
      server-side per bucket, so drill-down is instant)

## 5. New items

- [ ] (to be added)
- 
