"""Persistent user-data store for Model Portfolios / Strategies / Clients.

Separate SQLite DB (``data/userdata.db``) from the read-only build cache
``data/webapp.db`` so user-generated content survives rebuilds. Mirrors
``webapp/auth.py``'s lazy-connection pattern. All rows are scoped by ``user_id``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
USERDATA_DB = BASE_DIR / "data" / "userdata.db"


def _now() -> float:
    return time.time()


def _conn() -> sqlite3.Connection:
    USERDATA_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(USERDATA_DB)
    con.row_factory = sqlite3.Row
    _init_schema(con)
    return con


def _init_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            rules_text TEXT DEFAULT '',
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            rule_type TEXT,
            field TEXT,
            operator TEXT,
            value REAL,
            unit TEXT,
            severity TEXT,
            remark TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS model_portfolios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            strategy_id INTEGER,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            items_json TEXT DEFAULT '[]',
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            org TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            documents_json TEXT DEFAULT '[]',
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS client_portfolios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            model_portfolio_id INTEGER,
            strategy_id INTEGER,
            name TEXT NOT NULL,
            kind TEXT DEFAULT 'model',
            items_json TEXT DEFAULT '[]',
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            portfolio_id INTEGER,
            portfolio_kind TEXT,
            strategy_id INTEGER,
            results_json TEXT,
            created_at REAL
        );
        """
    )
    # Migrate: add allocations column (strategic asset-allocation plan).
    cols = [r[1] for r in con.execute("PRAGMA table_info(model_portfolios)")]
    if "allocations_json" not in cols:
        con.execute("ALTER TABLE model_portfolios ADD COLUMN allocations_json TEXT DEFAULT '{}'")
    cols = [r[1] for r in con.execute("PRAGMA table_info(client_portfolios)")]
    if "allocations_json" not in cols:
        con.execute("ALTER TABLE client_portfolios ADD COLUMN allocations_json TEXT DEFAULT '{}'")
    # Migrate: per-rule remark / direction note.
    cols = [r[1] for r in con.execute("PRAGMA table_info(rules)")]
    if "remark" not in cols:
        con.execute("ALTER TABLE rules ADD COLUMN remark TEXT DEFAULT ''")
    # Migrate: client documents (CAS / statements / other uploads).
    cols = [r[1] for r in con.execute("PRAGMA table_info(clients)")]
    if "documents_json" not in cols:
        con.execute("ALTER TABLE clients ADD COLUMN documents_json TEXT DEFAULT '[]'")
    con.commit()


def _items(items) -> str:
    return json.dumps(items or [], ensure_ascii=False)


def _load_items(raw: str) -> list:
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


def _alloc_json(allocations) -> str:
    return json.dumps(allocations or {}, ensure_ascii=False)


def _load_alloc(raw) -> dict:
    try:
        return json.loads(raw or "{}") or {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# Strategies + rules
# --------------------------------------------------------------------------
def list_strategies(user_id: int) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM strategies WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_strategy(user_id: int, strategy_id: int) -> dict | None:
    con = _conn()
    try:
        r = con.execute(
            "SELECT * FROM strategies WHERE id=? AND user_id=?",
            (strategy_id, user_id)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def create_strategy(user_id: int, name: str, description: str, rules_text: str) -> dict:
    con = _conn()
    try:
        now = _now()
        cur = con.execute(
            "INSERT INTO strategies (user_id, name, description, rules_text, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)", (user_id, name, description, rules_text, now, now))
        con.commit()
        return get_strategy(user_id, cur.lastrowid)
    finally:
        con.close()


def update_strategy(user_id: int, strategy_id: int, **fields) -> dict | None:
    allowed = {"name", "description", "rules_text"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_strategy(user_id, strategy_id)
    sets["updated_at"] = _now()
    con = _conn()
    try:
        cols = ", ".join(f"{k}=?" for k in sets)
        con.execute(f"UPDATE strategies SET {cols} WHERE id=? AND user_id=?",
                    (*sets.values(), strategy_id, user_id))
        con.commit()
    finally:
        con.close()
    return get_strategy(user_id, strategy_id)


def delete_strategy(user_id: int, strategy_id: int) -> None:
    con = _conn()
    try:
        con.execute("DELETE FROM rules WHERE strategy_id=? AND strategy_id IN "
                    "(SELECT id FROM strategies WHERE user_id=?)", (strategy_id, user_id))
        con.execute("DELETE FROM strategies WHERE id=? AND user_id=?", (strategy_id, user_id))
        con.commit()
    finally:
        con.close()


def set_rules(strategy_id: int, rules: list[dict]) -> None:
    con = _conn()
    try:
        con.execute("DELETE FROM rules WHERE strategy_id=?", (strategy_id,))
        for r in rules:
            con.execute(
                "INSERT INTO rules (strategy_id, rule_type, field, operator, value, unit, severity, remark) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (strategy_id, r.get("rule_type"), r.get("field"), r.get("operator"),
                 r.get("value"), r.get("unit"), r.get("severity"), r.get("remark") or ""))
        con.commit()
    finally:
        con.close()


def get_rules(strategy_id: int) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM rules WHERE strategy_id=?", (strategy_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# --------------------------------------------------------------------------
# Model portfolios
# --------------------------------------------------------------------------
def list_models(user_id: int) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM model_portfolios WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = _load_items(d.pop("items_json"))
            d["allocations"] = _load_alloc(d.pop("allocations_json"))
            out.append(d)
        return out
    finally:
        con.close()


def get_model(user_id: int, model_id: int) -> dict | None:
    con = _conn()
    try:
        r = con.execute("SELECT * FROM model_portfolios WHERE id=? AND user_id=?",
                        (model_id, user_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["items"] = _load_items(d.pop("items_json"))
        d["allocations"] = _load_alloc(d.pop("allocations_json"))
        return d
    finally:
        con.close()


def create_model(user_id: int, name: str, description: str, strategy_id, items: list,
                    allocations: dict | None = None) -> dict:
    con = _conn()
    try:
        now = _now()
        cur = con.execute(
            "INSERT INTO model_portfolios (user_id, strategy_id, name, description, items_json, allocations_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, strategy_id, name, description, _items(items),
             _alloc_json(allocations or {}), now, now))
        con.commit()
        return get_model(user_id, cur.lastrowid)
    finally:
        con.close()


def update_model(user_id: int, model_id: int, **fields) -> dict | None:
    con = _conn()
    try:
        sets = []
        vals = []
        for k in ("name", "description", "strategy_id"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        if "items" in fields:
            sets.append("items_json=?")
            vals.append(_items(fields["items"]))
        if "allocations" in fields:
            sets.append("allocations_json=?")
            vals.append(_alloc_json(fields["allocations"]))
        if not sets:
            return get_model(user_id, model_id)
        sets.append("updated_at=?")
        vals.append(_now())
        con.execute(f"UPDATE model_portfolios SET {', '.join(sets)} WHERE id=? AND user_id=?",
                    (*vals, model_id, user_id))
        con.commit()
    finally:
        con.close()
    return get_model(user_id, model_id)


def delete_model(user_id: int, model_id: int) -> None:
    con = _conn()
    try:
        con.execute("DELETE FROM model_portfolios WHERE id=? AND user_id=?", (model_id, user_id))
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
def list_clients(user_id: int) -> list[dict]:
    con = _conn()
    try:
        out = []
        for r in con.execute("SELECT * FROM clients WHERE user_id=? ORDER BY name", (user_id,)).fetchall():
            d = dict(r)
            d["documents"] = _load_docs(d.pop("documents_json", None))
            out.append(d)
        return out
    finally:
        con.close()


def _load_docs(raw) -> list:
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


def create_client(user_id: int, name: str, org: str, notes: str) -> dict:
    con = _conn()
    try:
        cur = con.execute(
            "INSERT INTO clients (user_id, name, org, notes, documents_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, name, org, notes, "[]", _now()))
        con.commit()
        rid = cur.lastrowid
    finally:
        con.close()
    return get_client(user_id, rid)


def get_client(user_id: int, client_id: int) -> dict | None:
    con = _conn()
    try:
        r = con.execute("SELECT * FROM clients WHERE id=? AND user_id=?",
                        (client_id, user_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["documents"] = _load_docs(d.pop("documents_json", None))
        return d
    finally:
        con.close()


def add_client_document(user_id: int, client_id: int, doc: dict) -> dict:
    con = _conn()
    try:
        r = con.execute("SELECT documents_json FROM clients WHERE id=? AND user_id=?",
                        (client_id, user_id)).fetchone()
        if not r:
            return None
        docs = _load_docs(r["documents_json"])
        docs.append(doc)
        con.execute("UPDATE clients SET documents_json=? WHERE id=? AND user_id=?",
                    (json.dumps(docs, ensure_ascii=False), client_id, user_id))
        con.commit()
    finally:
        con.close()
    return get_client(user_id, client_id)


def update_client(user_id: int, client_id: int, **fields) -> dict | None:
    allowed = {"name", "org", "notes"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    con = _conn()
    try:
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            con.execute(f"UPDATE clients SET {cols} WHERE id=? AND user_id=?",
                        (*sets.values(), client_id, user_id))
            con.commit()
    finally:
        con.close()
    return get_client(user_id, client_id)


def delete_client(user_id: int, client_id: int) -> None:
    con = _conn()
    try:
        con.execute("DELETE FROM client_portfolios WHERE user_id=? AND client_id=?",
                    (user_id, client_id))
        con.execute("DELETE FROM clients WHERE id=? AND user_id=?", (client_id, user_id))
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# Client portfolios
# --------------------------------------------------------------------------
def list_client_portfolios(user_id: int) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT * FROM client_portfolios WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = _load_items(d.pop("items_json"))
            d["allocations"] = _load_alloc(d.pop("allocations_json"))
            out.append(d)
        return out
    finally:
        con.close()


def get_client_portfolio(user_id: int, portfolio_id: int) -> dict | None:
    con = _conn()
    try:
        r = con.execute("SELECT * FROM client_portfolios WHERE id=? AND user_id=?",
                        (portfolio_id, user_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["items"] = _load_items(d.pop("items_json"))
        d["allocations"] = _load_alloc(d.pop("allocations_json"))
        return d
    finally:
        con.close()


def create_client_portfolio(user_id: int, client_id: int, name: str, kind: str,
                            items: list, model_portfolio_id=None, strategy_id=None,
                            allocations: dict | None = None) -> dict:
    con = _conn()
    try:
        now = _now()
        cur = con.execute(
            "INSERT INTO client_portfolios (user_id, client_id, model_portfolio_id, strategy_id, "
            "name, kind, items_json, allocations_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, client_id, model_portfolio_id, strategy_id, name, kind,
             _items(items), _alloc_json(allocations or {}), now, now))
        con.commit()
        return get_client_portfolio(user_id, cur.lastrowid)
    finally:
        con.close()


def update_client_portfolio(user_id: int, portfolio_id: int, **fields) -> dict | None:
    allowed = {"name", "kind", "strategy_id"}
    con = _conn()
    try:
        sets, vals = [], []
        for k in allowed:
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        if "items" in fields:
            sets.append("items_json=?")
            vals.append(_items(fields["items"]))
        if "allocations" in fields:
            sets.append("allocations_json=?")
            vals.append(_alloc_json(fields["allocations"]))
        if sets:
            sets.append("updated_at=?")
            vals.append(_now())
            con.execute(f"UPDATE client_portfolios SET {', '.join(sets)} WHERE id=? AND user_id=?",
                        (*vals, portfolio_id, user_id))
            con.commit()
    finally:
        con.close()
    return get_client_portfolio(user_id, portfolio_id)


def delete_client_portfolio(user_id: int, portfolio_id: int) -> None:
    con = _conn()
    try:
        con.execute("DELETE FROM client_portfolios WHERE id=? AND user_id=?",
                    (portfolio_id, user_id))
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# Analysis runs (cached results)
# --------------------------------------------------------------------------
def save_analysis_run(user_id: int, portfolio_id, portfolio_kind, strategy_id, results: dict) -> int:
    con = _conn()
    try:
        cur = con.execute(
            "INSERT INTO analysis_runs (user_id, portfolio_id, portfolio_kind, strategy_id, "
            "results_json, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, portfolio_id, portfolio_kind, strategy_id,
             json.dumps(results, ensure_ascii=False, default=str), _now()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_analysis_runs(user_id: int, limit: int = 20) -> list[dict]:
    con = _conn()
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, portfolio_id, portfolio_kind, strategy_id, created_at "
            "FROM analysis_runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)).fetchall()]
    finally:
        con.close()