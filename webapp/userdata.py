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
    _upgrade_once(con)
    return con


_SCHEMA_VERSION = 3  # 2 = [DBT1/DBT6] cascades+indexes · 3 = [ANA3] transactions_json


def _upgrade_once(con: sqlite3.Connection) -> None:
    """One-time per-DB upgrade work, gated by PRAGMA user_version so the
    per-connection _init_schema stays cheap. [DBT1/DBT6]"""
    if con.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
        return
    # Indexes: every hot query filters on user_id / strategy_id.
    con.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_strategies_user ON strategies(user_id);
        CREATE INDEX IF NOT EXISTS idx_rules_strategy ON rules(strategy_id);
        CREATE INDEX IF NOT EXISTS idx_models_user ON model_portfolios(user_id);
        CREATE INDEX IF NOT EXISTS idx_clients_user ON clients(user_id);
        CREATE INDEX IF NOT EXISTS idx_cportfolios_user ON client_portfolios(user_id);
        CREATE INDEX IF NOT EXISTS idx_cportfolios_client ON client_portfolios(client_id);
        CREATE INDEX IF NOT EXISTS idx_runs_user ON analysis_runs(user_id);
        CREATE INDEX IF NOT EXISTS idx_runs_portfolio ON analysis_runs(portfolio_id, portfolio_kind);
        """
    )
    # Orphan purge: heal rows stranded before application-level cascades
    # existed (analysis_runs of deleted portfolios/strategies etc).
    con.execute(
        "DELETE FROM analysis_runs WHERE portfolio_kind='client' AND portfolio_id "
        "NOT IN (SELECT id FROM client_portfolios)")
    con.execute(
        "DELETE FROM analysis_runs WHERE strategy_id IS NOT NULL AND strategy_id "
        "NOT IN (SELECT id FROM strategies)")
    con.commit()
    con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")


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
    # Migrate [ANA3]: per-portfolio CAS transaction history (purchases/redemptions)
    # powering the cash-flow-aware portfolio movement analytics.
    cols = [r[1] for r in con.execute("PRAGMA table_info(client_portfolios)")]
    if "transactions_json" not in cols:
        con.execute("ALTER TABLE client_portfolios "
                    "ADD COLUMN transactions_json TEXT DEFAULT '[]'")
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


def _tx_json(transactions) -> str:
    return json.dumps(transactions or [], ensure_ascii=False)


def _load_tx(raw) -> list:
    try:
        return json.loads(raw or "[]") or []
    except Exception:
        return []


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
        # [DBT1] application-level cascade (SQLite can't ALTER ADD CONSTRAINT
        # onto existing tables; native FKs would need a table rebuild).
        con.execute("DELETE FROM rules WHERE strategy_id=? AND strategy_id IN "
                    "(SELECT id FROM strategies WHERE user_id=?)", (strategy_id, user_id))
        con.execute("DELETE FROM analysis_runs WHERE strategy_id=? AND strategy_id IN "
                    "(SELECT id FROM strategies WHERE user_id=?)", (strategy_id, user_id))
        con.execute("UPDATE model_portfolios SET strategy_id=NULL WHERE strategy_id=? "
                    "AND user_id=?", (strategy_id, user_id))
        con.execute("UPDATE client_portfolios SET strategy_id=NULL WHERE strategy_id=? "
                    "AND user_id=?", (strategy_id, user_id))
        con.execute("DELETE FROM strategies WHERE id=? AND user_id=?", (strategy_id, user_id))
        con.commit()
    finally:
        con.close()


def set_rules(user_id: int, strategy_id: int, rules: list[dict]) -> None:
    """Replace a strategy's rules. Scoped: only when the strategy belongs to
    ``user_id`` (prevents cross-tenant writes)."""
    con = _conn()
    try:
        owned = con.execute(
            "SELECT 1 FROM strategies WHERE id=? AND user_id=?",
            (strategy_id, user_id)).fetchone()
        if not owned:
            return
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


def get_rules(strategy_id: int, user_id: int | None = None) -> list[dict]:
    """Rules for a strategy. When ``user_id`` is given the read is scoped —
    another user's strategy yields [] instead of its rules."""
    con = _conn()
    try:
        if user_id is not None:
            owned = con.execute(
                "SELECT 1 FROM strategies WHERE id=? AND user_id=?",
                (strategy_id, user_id)).fetchone()
            if not owned:
                return []
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
        # [DBT1] detach client portfolios that were cloned from this model
        # (they keep their own items_json copies; only the provenance link goes).
        con.execute("UPDATE client_portfolios SET model_portfolio_id=NULL "
                    "WHERE model_portfolio_id=? AND user_id=?", (model_id, user_id))
        con.execute("DELETE FROM analysis_runs WHERE strategy_id IS NULL AND "
                    "portfolio_kind='model' AND portfolio_id=? AND user_id=?",
                    (model_id, user_id))
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
        # [DBT1] cascade: portfolios of this client + their cached runs.
        con.execute("DELETE FROM analysis_runs WHERE user_id=? AND portfolio_kind='client' "
                    "AND portfolio_id IN (SELECT id FROM client_portfolios "
                    "WHERE client_id=?)", (user_id, client_id))
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
            d["transactions"] = _load_tx(d.pop("transactions_json"))
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
        d["transactions"] = _load_tx(d.pop("transactions_json"))
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
        if "transactions" in fields:
            sets.append("transactions_json=?")
            vals.append(_tx_json(fields["transactions"]))
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
        # [DBT1] cached analysis results of a deleted portfolio are dead weight.
        con.execute("DELETE FROM analysis_runs WHERE portfolio_kind='client' AND "
                    "portfolio_id=? AND user_id=?", (portfolio_id, user_id))
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