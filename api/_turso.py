"""Minimal stdlib client for Turso / libSQL over the Hrana v2 HTTP pipeline.

Mimics just enough of the sqlite3 API for this app: conn.execute(sql, params)
returns a cursor-like object with .description / .fetchone() / .fetchall() /
iteration; conn.cursor() returns the connection itself. A `baton` keeps
sequential requests on the same server connection so TEMP tables survive
between .execute() calls.

Enabled when TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set (Vercel env vars).
The underscore prefix keeps Vercel from exposing this file as an endpoint.
"""

import json
import os
import urllib.request


class TursoError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("TURSO_DATABASE_URL") and os.environ.get("TURSO_AUTH_TOKEN"))


def _encode(p):
    if p is None:
        return {"type": "null"}
    if isinstance(p, bool):
        return {"type": "integer", "value": str(int(p))}
    if isinstance(p, int):
        return {"type": "integer", "value": str(p)}
    if isinstance(p, float):
        return {"type": "float", "value": p}
    return {"type": "text", "value": str(p)}


def _decode(cell):
    if cell is None:
        return None
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    return cell.get("value")


class _Cursor:
    def __init__(self, cols, rows):
        self.description = [(c, None, None, None, None, None, None) for c in cols]
        self._rows = rows
        self._i = 0

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self):
        url = os.environ["TURSO_DATABASE_URL"].strip()
        self._url = url.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
        self._token = os.environ["TURSO_AUTH_TOKEN"].strip()
        self._baton = None

    def cursor(self):
        return self

    def execute(self, sql: str, params=()) -> _Cursor:
        body = {"requests": [{"type": "execute", "stmt": {
            "sql": sql, "args": [_encode(p) for p in params]}}]}
        if self._baton:
            body["baton"] = self._baton
        data = self._post(body)
        self._baton = data.get("baton")
        res = data["results"][0]
        if res.get("type") == "error":
            raise TursoError(res["error"].get("message", "unknown Turso error"))
        r = res["response"]["result"]
        cols = [c["name"] for c in r["cols"]]
        rows = [tuple(_decode(c) for c in row) for row in r["rows"]]
        return _Cursor(cols, rows)

    def commit(self):  # reads only; no-op for API compatibility
        pass

    def close(self):
        if not self._baton:
            return
        try:
            self._post({"baton": self._baton, "requests": [{"type": "close"}]})
        except Exception:  # noqa: BLE001 - best effort
            pass
        self._baton = None

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            # Cold Turso instances page in data lazily — big scans (analytics
            # temp table over 7.5M rows) can take minutes on first touch.
            with urllib.request.urlopen(req, timeout=240) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TursoError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}") from e


def connect() -> Connection:
    return Connection()
