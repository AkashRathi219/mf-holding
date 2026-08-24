"""REST API for the Model Portfolios / Strategies / Compliance tools.

A separate APIRouter so user-facing tools stay out of ``webapp/main.py``.
All endpoints require a valid session and scope rows to the logged-in user.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from . import auth, db as dbm, userdata
from .db import _cap_bucket
from .strategy_rules import evaluate_rules, parse_rules

router = APIRouter(prefix="/api")

REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "reports"

# [BUG-M5] hard cap for client-document uploads (CAS PDF/JSON are << this).
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _apply_remarks(rules: list[dict], remarks) -> None:
    """Attach per-rule remark / direction notes (aligned by index)."""
    remarks = remarks or []
    for i, r in enumerate(rules):
        r["remark"] = remarks[i] if i < len(remarks) else (r.get("remark") or "")


def _user(request: Request) -> dict:
    token = request.headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    else:
        token = request.headers.get("X-Auth-Token", "")
    u = auth.user_from_token(token)
    if not u:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    return u


def _uid(user: dict) -> int:
    return int(user.get("id") or 0)


def _to_int(value, field: str = "id") -> int:
    """[BUG-M3] Strict integer coercion for body-supplied ids/params: bad input
    is a client error (400), never an unhandled ValueError (500); JSON floats
    like 4.5 are rejected instead of silently truncating to the wrong row."""
    try:
        if isinstance(value, bool):
            raise ValueError
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"'{field}' must be a whole number.")
    return n


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------
@router.post("/strategies/parse")
def api_strategies_parse(request: Request, body: dict):
    _user(request)
    text = body.get("text") or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="Rules text is required.")
    return parse_rules(text)


@router.get("/strategies")
def api_strategies_list(request: Request):
    u = _user(request)
    return {"items": userdata.list_strategies(_uid(u))}


@router.post("/strategies")
def api_strategies_create(request: Request, body: dict):
    u = _user(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Strategy name is required.")
    strategy = userdata.create_strategy(_uid(u), name, body.get("description") or "",
                                        body.get("rules_text") or "")
    parsed = parse_rules(strategy["rules_text"])
    _apply_remarks(parsed["rules"], body.get("remarks"))
    userdata.set_rules(_uid(u), strategy["id"], parsed["rules"])
    strategy["rules"] = parsed["rules"]
    strategy["unparsed"] = parsed["unparsed"]
    return strategy


@router.get("/strategies/{strategy_id}")
def api_strategies_get(strategy_id: int, request: Request):
    u = _user(request)
    s = userdata.get_strategy(_uid(u), strategy_id)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    s["rules"] = userdata.get_rules(strategy_id, _uid(u))
    return s


@router.put("/strategies/{strategy_id}")
def api_strategies_update(strategy_id: int, request: Request, body: dict):
    u = _user(request)
    s = userdata.update_strategy(_uid(u), strategy_id,
                                 name=body.get("name"), description=body.get("description"),
                                 rules_text=body.get("rules_text"))
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    if body.get("rules_text") is not None:
        parsed = parse_rules(s["rules_text"])
        _apply_remarks(parsed["rules"], body.get("remarks"))
        userdata.set_rules(_uid(u), strategy_id, parsed["rules"])
        s["rules"] = parsed["rules"]
        s["unparsed"] = parsed["unparsed"]
    return s


@router.delete("/strategies/{strategy_id}")
def api_strategies_delete(strategy_id: int, request: Request):
    u = _user(request)
    userdata.delete_strategy(_uid(u), strategy_id)
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Model portfolios
# --------------------------------------------------------------------------
@router.get("/models")
def api_models_list(request: Request):
    u = _user(request)
    return {"items": userdata.list_models(_uid(u))}


@router.post("/models")
def api_models_create(request: Request, body: dict):
    u = _user(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Model name is required.")
    model = userdata.create_model(_uid(u), name, body.get("description") or "",
                                  body.get("strategy_id"), body.get("items") or [],
                                  allocations=body.get("allocations"))
    return model


@router.get("/models/{model_id}")
def api_models_get(model_id: int, request: Request):
    u = _user(request)
    m = userdata.get_model(_uid(u), model_id)
    if not m:
        raise HTTPException(status_code=404, detail="Model not found.")
    return m


@router.put("/models/{model_id}")
def api_models_update(model_id: int, request: Request, body: dict):
    u = _user(request)
    if body.get("name") is not None and not str(body["name"]).strip():
        raise HTTPException(status_code=400, detail="Model name cannot be empty.")
    m = userdata.update_model(_uid(u), model_id, name=body.get("name"),
                              description=body.get("description"),
                              strategy_id=body.get("strategy_id"),
                              items=body.get("items"),
                              allocations=body.get("allocations"))
    if not m:
        raise HTTPException(status_code=404, detail="Model not found.")
    return m


@router.delete("/models/{model_id}")
def api_models_delete(model_id: int, request: Request):
    u = _user(request)
    userdata.delete_model(_uid(u), model_id)
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
@router.get("/clients")
def api_clients_list(request: Request):
    u = _user(request)
    return {"items": userdata.list_clients(_uid(u))}


@router.post("/clients")
def api_clients_create(request: Request, body: dict):
    u = _user(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Client name is required.")
    return userdata.create_client(_uid(u), name, body.get("org") or "", body.get("notes") or "")


@router.put("/clients/{client_id}")
def api_clients_update(client_id: int, request: Request, body: dict):
    u = _user(request)
    if body.get("name") is not None and not str(body["name"]).strip():
        raise HTTPException(status_code=400, detail="Client name cannot be empty.")
    c = userdata.update_client(_uid(u), client_id, name=body.get("name"),
                               org=body.get("org"), notes=body.get("notes"))
    if not c:
        raise HTTPException(status_code=404, detail="Client not found.")
    return c


@router.delete("/clients/{client_id}")
def api_clients_delete(client_id: int, request: Request):
    u = _user(request)
    userdata.delete_client(_uid(u), client_id)
    return {"status": "ok"}


def _cas_json_to_items(doc: dict) -> list[dict]:
    """Turn a CAS holdings JSON into portfolio items (scheme + isin + units)."""
    items = []
    for a in (doc.get("portfolio_summary") or {}).get("allocations") or []:
        name = (a.get("scheme_name") or "").strip()
        isin = (a.get("isin") or "").strip().upper()
        units = a.get("net_units")
        if not name or not isin:
            continue
        try:
            units = float(units)
        except (TypeError, ValueError):
            continue
        items.append({"type": "scheme", "isin": isin, "name": name, "units": units})
    return items


# CAS transaction types -> flow direction [ANA3 movement analytics]:
# unsigned amounts in the source; the TYPE decides the sign.
_TX_IN = {"PURCHASE", "BUY", "SWITCH_IN", "ADDITIONAL_PURCHASE",
          "SYSTEMATIC_INVESTMENT", "REINVESTMENT"}
_TX_OUT = {"REDEEM", "SELL", "SWITCH_OUT", "REDEMPTION",
           "SYSTEMATIC_WITHDRAWAL"}


def parse_cas_transactions(doc) -> list[dict]:
    """Normalise a CAS transaction set to canonical records.

    Accepts either a CAS JSON doc carrying a ``transactions`` array, or the
    standalone extraction envelope (same schema — the sample
    CAS_sample_extracted_transactions.txt IS a JSON envelope). Each record is
    signed by type (PURCHASE/SWITCH_IN +, REDEEM/SWITCH_OUT -), normalised to
    ISO dates and floats; records without a date, or with zero units AND zero
    amount, are dropped. Kept records are sorted by date."""
    raw = doc.get("transactions") if isinstance(doc, dict) else None
    if raw is None:
        try:  # standalone TXT/JSON envelope
            raw = json.loads(doc)["transactions"]
        except Exception:
            raw = None
    out: list[dict] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        date = str(r.get("date") or "").strip()[:10]
        if not date:
            continue
        ttype = str(r.get("transaction_type") or "").strip().upper()
        units = r.get("units")
        amount = r.get("amount")
        try:
            units = float(units) if units not in (None, "") else 0.0
        except (TypeError, ValueError):
            units = 0.0
        try:
            amount = float(amount) if amount not in (None, "") else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        if units == 0.0 and amount == 0.0:
            continue
        if ttype in _TX_IN:
            sign = 1.0
        elif ttype in _TX_OUT:
            sign = -1.0
        else:
            continue
        try:
            nav = float(r.get("nav"))
        except (TypeError, ValueError):
            # [BUG-M4] "N/A"/"—" NAV strings are metadata, not a crash.
            nav = None
        if r.get("nav") in (None, ""):
            nav = None
        out.append({
            "date": date,
            "type": ttype,
            "sign": sign,
            "units": units,
            "amount": abs(amount) * sign,
            "cum_units": units * sign,
            "isin": str(r.get("isin") or "").strip().upper(),
            "amfi_code": str(r.get("amfi_code") or "").strip(),
            "name": str(r.get("scheme_name") or "").strip(),
            "nav": nav,
        })
    out.sort(key=lambda r: (r["date"], r["type"], r["isin"]))
    return out


_ISIN_RE = re.compile(r"^IN[EW][A-Z0-9]{10}$")


def _pdf_cas_items(path: str) -> list[dict]:
    """Best-effort extraction of a CAMS-style CAS PDF portfolio summary:
    rows carrying an ISIN with a scheme name and units. If the layout is not
    recognised it returns [] and the document is flagged needs-parse."""
    import pdfplumber
    items: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [(c or "").replace("\n", " ").strip() for c in row]
                    isin = next((c for c in cells if _ISIN_RE.match(c.upper())), None)
                    if not isin:
                        continue
                    name = next((c for c in cells
                                 if len(c) > 8 and not _ISIN_RE.match(c.upper())
                                 and not re.match(r"^[\d,.\s₹%-]+$", c)), "")
                    nums = [re.sub(r"[^\d.]", "", c) for c in cells if re.search(r"\d", c)]
                    units = None
                    for n in nums:
                        try:
                            v = float(n)
                        except ValueError:
                            continue
                        if 0 < v < 1e12 and ("." in n or int(v) != v or v > 1000):
                            units = v
                            break
                    if name and isin and units:
                        items.append({"type": "scheme", "isin": isin.upper(),
                                      "name": name, "units": units})
    # dedupe by isin keeping the largest units
    seen: dict[str, dict] = {}
    for it in items:
        cur = seen.get(it["isin"])
        if cur is None or it["units"] > cur["units"]:
            seen[it["isin"]] = it
    return list(seen.values())


@router.post("/clients/{client_id}/documents")
async def api_client_documents(client_id: int, request: Request, file: UploadFile = File(...)):
    """Upload a client document (CAS JSON or PDF; other types stored for later)
    and, when it parses, build/refresh the client's portfolio at market value."""
    u = _user(request)
    uid = _uid(u)
    client = userdata.get_client(uid, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found.")
    filename = (file.filename or "document").replace("\\", "/").rsplit("/", 1)[-1]
    ext = Path(filename).suffix.lower()
    data = await file.read()
    # [BUG-M5] cap uploads before anything touches disk/memory.
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Document too large (10 MB limit).")
    ingest = Path(dbm.DATA_DIR) / "raw" / "manual_ingest" / f"client_{client_id}"
    ingest.mkdir(parents=True, exist_ok=True)
    saved = ingest / f"{int(time.time())}_{filename}"
    saved.write_bytes(data)

    doc = {"name": filename, "type": ext or "file", "status": "stored",
           # [BUG-L8] filename only — never leak absolute server paths to clients.
           "file": filename,
           "uploaded_at": datetime.datetime.now().isoformat(timespec="seconds")}
    items: list[dict] = []
    transactions: list[dict] = []
    if ext == ".json":
        try:
            parsed = json.loads(data.decode("utf-8"))
            items = _cas_json_to_items(parsed)
            transactions = parse_cas_transactions(parsed)
            doc["status"] = "parsed" if items else "no_holdings"
            doc["transactions"] = len(transactions)
        except Exception as e:  # noqa: BLE001
            doc["status"] = "error"
            doc["error"] = str(e)
    elif ext == ".pdf":
        try:
            items = _pdf_cas_items(str(saved))
            doc["status"] = "parsed" if items else "needs_parse"
        except Exception as e:  # noqa: BLE001
            doc["status"] = "error"
            doc["error"] = str(e)
    # other extensions: stored only (parsing covered later)

    client = userdata.add_client_document(uid, client_id, doc)

    if items:
        from .market_value import reweight_by_market_value
        items = reweight_by_market_value(items)
        cps = userdata.list_client_portfolios(uid)
        cp = next((p for p in cps if p["client_id"] == client_id), None)
        if cp:
            fields: dict = {"items": items}
            if transactions:
                # [BUG-C4] only overwrite stored cash flows when THIS document
                # actually parsed some — a PDF re-upload must never erase the
                # CAS history captured earlier.
                fields["transactions"] = transactions
            userdata.update_client_portfolio(uid, cp["id"], **fields)
            name = cp["name"]
        else:
            strategies = userdata.list_strategies(uid)
            strat_id = strategies[0]["id"] if strategies else None
            name = f"{client['name']} portfolio"
            userdata.create_client_portfolio(uid, client_id, name, "actual",
                                             items, strategy_id=strat_id)
            if transactions:
                cps = userdata.list_client_portfolios(uid)
                cp = next((p for p in cps if p["client_id"] == client_id), None)
                if cp:
                    userdata.update_client_portfolio(uid, cp["id"],
                                                     transactions=transactions)
        doc["portfolio"] = name
        doc["holdings"] = len(items)

    return {"client": client, "parsed": len(items), "document": doc}


# --------------------------------------------------------------------------
# Client portfolios (deploy model -> client instances)
# --------------------------------------------------------------------------
@router.get("/client-portfolios")
def api_cp_list(request: Request):
    u = _user(request)
    items = userdata.list_client_portfolios(_uid(u))
    clients = {c["id"]: c for c in userdata.list_clients(_uid(u))}
    strategies = {s["id"]: s["name"] for s in userdata.list_strategies(_uid(u))}
    for p in items:
        p["client_name"] = (clients.get(p["client_id"]) or {}).get("name") or "—"
        p["strategy_name"] = strategies.get(p.get("strategy_id")) or "—"
    return {"items": items}


@router.post("/client-portfolios")
def api_cp_create(request: Request, body: dict):
    u = _user(request)
    client_id = _to_int(body.get("client_id") or 0, "client_id")
    if not client_id or not userdata.get_client(_uid(u), client_id):
        raise HTTPException(status_code=400, detail="A valid client is required.")
    model_id = body.get("model_portfolio_id")
    items = body.get("items") or []
    strategy_id = body.get("strategy_id")
    name = (body.get("name") or "").strip() or "Client portfolio"
    kind = body.get("kind") or "model"
    allocations = body.get("allocations")
    if model_id:
        model = userdata.get_model(_uid(u), _to_int(model_id, "model_portfolio_id"))
        if model:
            items = items or model["items"]
            allocations = allocations or model.get("allocations") or {}
            strategy_id = strategy_id or model.get("strategy_id")
            if not name or name == "Client portfolio":
                name = f"{model['name']} — {userdata.get_client(_uid(u), client_id)['name']}"
    return userdata.create_client_portfolio(_uid(u), client_id, name, kind,
                                            items, model_portfolio_id=model_id,
                                            strategy_id=strategy_id,
                                            allocations=allocations)


@router.put("/client-portfolios/{portfolio_id}")
def api_cp_update(portfolio_id: int, request: Request, body: dict):
    u = _user(request)
    if body.get("name") is not None and not str(body["name"]).strip():
        raise HTTPException(status_code=400, detail="Portfolio name cannot be empty.")
    p = userdata.update_client_portfolio(_uid(u), portfolio_id, name=body.get("name"),
                                         kind=body.get("kind"),
                                         strategy_id=body.get("strategy_id"),
                                         items=body.get("items"),
                                         allocations=body.get("allocations"))
    if not p:
        raise HTTPException(status_code=404, detail="Client portfolio not found.")
    return p


@router.delete("/client-portfolios/{portfolio_id}")
def api_cp_delete(portfolio_id: int, request: Request):
    u = _user(request)
    userdata.delete_client_portfolio(_uid(u), portfolio_id)
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Analysis: portfolio vs strategy rules
# --------------------------------------------------------------------------
def _overlap_max(items: list[dict], wdb: dbm.WebDB) -> float:
    ids = wdb.resolve_scheme_ids(items)
    if len(ids) < 2:
        return 0.0
    ov = wdb.overlap(ids)
    matrix = ov.get("matrix") or []
    best = 0.0
    for m in matrix:
        for k in matrix:
            if m["id"] == k["id"]:
                continue
            v = m.get(f"c_{k['id']}", 0) or 0
            if v > best:
                best = v
    return round(best, 2)


def _deviation(row: dict) -> dict | None:
    """Signed deviation of actual from the rule limit, in limit units.

    For a max rule a positive diff means the portfolio is *over* the limit; for
    a min rule a positive diff means it is *below* the minimum.
    """
    if row.get("actual") in (None, "N/A", "NA"):
        return None
    try:
        a = float(str(row["actual"]).rstrip("%").strip())
        limit = float(str(row["limit"]).rstrip("%").strip())
    except (TypeError, ValueError):
        return None
    unit = row.get("unit") or "%"
    if row.get("operator") == "<=":
        return {"diff": round(a - limit, 1), "unit": unit, "kind": "max"}
    return {"diff": round(limit - a, 1), "unit": unit, "kind": "min"}


def _fmt_deviation(dev: dict) -> str:
    d, unit = dev["diff"], dev["unit"]
    if dev["kind"] == "max":
        return f"{d:+.1f}{unit} over limit" if d > 0 else f"{d:.1f}{unit} under limit (headroom)"
    return f"{d:.1f}{unit} below minimum" if d > 0 else f"{d:+.1f}{unit} above minimum"


def _contrib(rows) -> dict[str, float]:
    """Per-fund weight contributed by a set of holdings. Uses the exact
    per-fund weights tracked during aggregation (``by_scheme``); falls back to
    splitting the weight equally among holders for holdings without that data."""
    out: dict[str, float] = {}
    for h in rows:
        bs = h.get("by_scheme")
        if bs:
            for fund, w in bs.items():
                out[fund] = out.get(fund, 0.0) + w
            continue
        holders = [s for s in (h.get("schemes") or []) if s]
        if not holders:
            continue
        share = (h.get("weight") or 0) / len(holders)
        for s in holders:
            out[s] = out.get(s, 0.0) + share
    return out


def _top_contrib(d: dict[str, float]) -> list[dict]:
    return sorted([{"fund": k, "value": round(v, 3)} for k, v in d.items() if v > 0.0001],
                  key=lambda x: -x["value"])


def _attribution(pa: dict) -> dict:
    """Per-rule list of the funds responsible for each metric, each with the
    weight it contributes to the rule's actual value."""
    eh = pa.get("effective_holdings") or []
    st = pa.get("sector_table") or []
    top_sec = st[0]["sector"] if st else ""
    stocks_eh = [h for h in eh if h.get("asset_class") == "stocks"]

    attr: dict = {
        "stocks": _top_contrib(_contrib([h for h in eh if h.get("asset_class") == "stocks"])),
        "debt": _top_contrib(_contrib([h for h in eh if h.get("asset_class") == "debt"])),
        "gold": _top_contrib(_contrib([h for h in eh if h.get("asset_class") == "gold"])),
        "cash_equivalents": _top_contrib(_contrib([h for h in eh if h.get("asset_class") == "cash_equivalents"])),
        "international": _top_contrib(_contrib([h for h in eh if h.get("asset_class") == "international"])),
        "future_options": _top_contrib(_contrib([h for h in eh if h.get("asset_class") == "future_options"])),
        "single stock": _top_contrib(_contrib(stocks_eh[:1])),
        "sector": _top_contrib(_contrib([h for h in eh if (h.get("sector") or "") == top_sec])) if top_sec else [],
        "top 5 concentration": _top_contrib(_contrib(eh[:5])),
        "top 10 concentration": _top_contrib(_contrib(eh[:10])),
    }
    cap = pa.get("cap_split_raw") or {}
    cap_bucket = {"large cap": "large", "mid cap": "mid", "small cap": "small",
                  "microcap": "microcap", "ipo": "ipo"}
    for label, bucket in cap_bucket.items():
        rows = [h for h in eh if h.get("asset_class") == "stocks" and _cap_bucket(h) == bucket]
        raw = _contrib(rows)
        raw_sum = sum(raw.values())
        target = cap.get(label) or 0
        if raw_sum > 0 and target > 0 and abs(raw_sum - target) > 0.001:
            scale = target / raw_sum
            raw = {f: v * scale for f, v in raw.items()}
        attr[label] = _top_contrib(raw)
    attr["schemes"] = [{"fund": s["fund_name"], "value": 1} for s in pa.get("schemes") or []]
    return attr


def _build_contexts(pa: dict, top_stock: dict | None, sector_table: list,
                    overlap: dict) -> dict:
    """Rich per-rule breakdown contexts used for the collapsible rows in the
    compliance report. Every rule gets a readable detail: the contributing
    funds (with weights) and, where relevant, the securities behind the metric."""
    eh = pa.get("effective_holdings") or []
    contexts: dict = {}

    def sec_rows(rows, limit=8):
        return [{"security": h["company"], "weight": round(h["weight"], 2),
                 "funds": [f["fund"] for f in _top_contrib(_contrib([h]))]}
                for h in sorted(rows, key=lambda x: -x["weight"])[:limit]]

    if top_stock:
        contexts["single stock"] = {
            "kind": "single_stock",
            "security": top_stock["company"], "isin": top_stock.get("isin") or "",
            "sector": top_stock.get("sector") or "",
            "value": round(top_stock["weight"], 2),
            "funds": _top_contrib(_contrib([top_stock])),
        }
    if sector_table:
        top_sec = sector_table[0]["sector"]
        sec_rows_all = [h for h in eh if (h.get("sector") or "") == top_sec]
        contexts["sector"] = {
            "kind": "sector", "sector": top_sec, "weight": sector_table[0]["weight"],
            "breakdown": [{"sector": s["sector"], "weight": s["weight"]}
                          for s in sector_table[:8]],
            "funds": _top_contrib(_contrib(sec_rows_all)),
            "stocks": sec_rows(sec_rows_all, 10),
        }

    for key, label in (("stocks", "Equity"), ("debt", "Debt"), ("gold", "Gold"),
                       ("cash_equivalents", "Cash"), ("international", "International"),
                       ("future_options", "Futures & Options")):
        rows = [h for h in eh if h.get("asset_class") == key]
        total = round(sum(h["weight"] for h in rows), 2)
        if total <= 0:
            continue
        contexts[key] = {
            "kind": "asset", "asset": label, "value": total,
            "funds": _top_contrib(_contrib(rows)),
            "top": sec_rows(rows, 8),
        }

    cap_bucket = {"large cap": "large", "mid cap": "mid", "small cap": "small",
                  "microcap": "microcap", "ipo": "ipo"}
    cap = pa.get("cap_split_raw") or {}
    for label, bucket in cap_bucket.items():
        rows = [h for h in eh if h.get("asset_class") == "stocks" and _cap_bucket(h) == bucket]
        if not rows:
            continue
        contexts[label] = {
            "kind": "cap", "cap": label, "value": cap.get(label, 0),
            "funds": _top_contrib(_contrib(rows)),
            "stocks": sec_rows(rows, 10),
        }

    for n, key in ((5, "top 5 concentration"), (10, "top 10 concentration")):
        topn = eh[:n]
        contexts[key] = {
            "kind": "topn", "n": n, "value": round(sum(h["weight"] for h in topn), 2),
            "stocks": sec_rows(topn, n),
        }

    contexts["scheme overlap"] = {
        "kind": "overlap", "value": overlap.get("overlap") or 0,
        "funds": list(overlap.get("funds") or []),
    }
    scheme_w: dict[str, float] = {}
    for s in pa.get("schemes") or []:
        scheme_w[s["fund_name"]] = scheme_w.get(s["fund_name"], 0.0) + (s.get("weight") or 0)
    contexts["schemes"] = {
        "kind": "schemes",
        "schemes": [{"fund": k, "value": round(v, 2)} for k, v in sorted(
            scheme_w.items(), key=lambda x: -x[1])],
    }
    return contexts


def _overlap_pair(items: list[dict], wdb: dbm.WebDB) -> dict:
    """Largest pairwise scheme-overlap: value + the two schemes involved."""
    ids = wdb.resolve_scheme_ids(items)
    if len(ids) < 2:
        return {"overlap": 0.0, "funds": []}
    ov = wdb.overlap(ids)
    matrix = ov.get("matrix") or []
    names = {m["id"]: m.get("scheme") or str(m["id"]) for m in matrix}
    best, bpair = 0.0, []
    for m in matrix:
        for k in matrix:
            if m["id"] == k["id"]:
                continue
            v = m.get(f"c_{k['id']}", 0) or 0
            if v > best:
                best, bpair = v, [names.get(m["id"], ""), names.get(k["id"], "")]
    return {"overlap": round(best, 2), "funds": bpair}


def _per_fund_asset(pa: dict) -> dict:
    """fund -> {asset_class: weight} using the exact per-fund weights tracked
    during aggregation; falls back to equal sharing for holdings without them."""
    out: dict[str, dict] = {}
    for h in pa.get("effective_holdings") or []:
        ac = h.get("asset_class") or "other"
        bs = h.get("by_scheme")
        if bs:
            for s, w in bs.items():
                out.setdefault(s, {})
                out[s][ac] = out[s].get(ac, 0.0) + w
            continue
        holders = [s for s in (h.get("schemes") or []) if s]
        if not holders:
            continue
        share = (h.get("weight") or 0) / len(holders)
        for s in holders:
            out.setdefault(s, {})
            out[s][ac] = out[s].get(ac, 0.0) + share
    return out


_CLASS_LABELS = {"stocks": "Equity", "debt": "Debt", "gold": "Gold",
                 "cash_equivalents": "Cash", "international": "International",
                 "future_options": "Futures & Options", "other": "Other"}


def _class_decision(k: str) -> str:
    return {
        "stocks": "Classified as Equity: listed equities with a recognised market-cap bucket "
                  "(large / mid / small / micro). Money-market fund units and bank short-term "
                  "instruments are excluded from equity.",
        "debt": "Classified as Debt: government securities, state-government SDLs, G-Secs, bank "
                "CDs / CPs, corporate bonds, NCDs and rated short-term instruments.",
        "cash_equivalents": "Classified as Cash: net current assets, TREPS, money-market / liquid / "
                            "savings mutual-fund units (arbitrage carry) and ultra-short cash instruments.",
        "gold": "Classified as Gold: gold exposure detected from the security name / sector.",
        "international": "Classified as International: securities with foreign ISIN prefixes "
                         "(US / LU / DE / GB / JP / HK / SG / …) and overseas ETFs.",
        "future_options": "Classified as Futures & Options: derivatives and short positions.",
        "other": "Unclassified residual.",
    }.get(k, "Unclassified residual.")


def _rule_basis(row: dict, metrics: dict) -> str:
    rt = row.get("rule_type") or ""
    f = row.get("field") or ""

    def pct(key):
        v = metrics.get(key)
        return "—" if v is None else f"{v:.2f}%"

    if rt == "max_single_stock":
        return (f"Largest single effective holding = {pct('single_stock_max')} of the "
                "portfolio (its %NAV within each scheme, scaled by the scheme's portfolio weight).")
    if rt == "max_sector":
        return (f"Largest sector concentration = {pct('sector_max')} — the summed weight of "
                "every holding classified under the dominant sector.")
    if rt in ("min_asset", "max_asset"):
        v = metrics.get("asset_split", {}).get(f)
        vs = "—" if v is None else f"{v:.2f}%"
        return (f"Sum of the weights of all effective holdings classified as '{f}' "
                f"= {vs} of the portfolio.")
    if rt in ("min_large_cap", "max_large_cap", "min_mid_cap", "max_mid_cap",
              "min_small_cap", "max_small_cap", "min_microcap", "max_microcap"):
        v = metrics.get("cap_split", {}).get(f)
        vs = "—" if v is None else f"{v:.2f}%"
        return (f"Weight of holdings in the '{f}' segment (equity cap split only; untagged equity "
                f"distributed proportionally) = {vs} of the portfolio.")
    if rt == "max_top5":
        if "sector" in (row.get("remark") or "").lower():
            return f"Top-5 SECTOR concentration: the 5 largest equity sectors summed = {pct('top5')}."
        return f"Sum of the 5 largest effective holdings = {pct('top5')}."
    if rt == "max_top10":
        if "sector" in (row.get("remark") or "").lower():
            return f"Top-10 SECTOR concentration: the 10 largest equity sectors summed = {pct('top10')}."
        return f"Sum of the 10 largest effective holdings = {pct('top10')}."
    if rt == "max_overlap":
        return (f"Largest pairwise scheme overlap = {pct('overlap_max')} (common securities "
                "weighted by the smaller of the two schemes' %NAV).")
    if rt in ("max_schemes", "min_schemes"):
        if any(k in (row.get("remark") or "").lower()
               for k in ("direct stock", "direct stocks", "stocks held")):
            return (f"Number of mutual funds + individual direct stocks held "
                    f"= {metrics.get('n_schemes')}.")
        return f"Number of distinct schemes held = {metrics.get('n_schemes')}."
    if rt in ("max_holdings", "min_holdings"):
        return f"Number of effective holdings (distinct securities) = {metrics.get('n_holdings')}."
    return ""


def _markdown_report(label: str, kind: str, items: list[dict], pa: dict, metrics: dict,
                     compliance: dict, attribution: dict, overlap: dict,
                     fund_asset: dict) -> str:
    L: list[str] = []
    L.append(f"# Compliance Analysis Report — {label}")
    L.append("")
    L.append(f"- **Portfolio kind:** `{kind}`")
    L.append(f"- **Portfolio lines:** {len(items or [])}")
    L.append(f"- **Allocated:** {pa.get('total_weight', 0)}%  ·  **Resolved:** "
             f"{pa.get('effective_total', 0)}%  ·  **Coverage:** {pa.get('coverage_pct', 0)}%")
    L.append(f"- **Securities (effective holdings):** {pa.get('n_holdings', 0)}")
    _comp = compliance.get("compliance")
    _comp_s = "N/A (no evaluable rules)" if _comp is None else f"{_comp}%"
    L.append(f"- **Compliance:** {_comp_s} "
             f"({compliance.get('passed')} of {compliance.get('total')} rules passed)")
    L.append("")

    L.append("## 1. Asset allocation — how each fund was categorised")
    L.append("")
    L.append("Every effective holding is assigned to one asset class. The allocation table below "
             "lists each category, its weight in the portfolio, the funds that produced it and the "
             "classification decision applied.")
    L.append("")
    for k, v in sorted((pa.get("asset_split_raw") or {}).items(), key=lambda x: -x[1]):
        if v <= 0:
            continue
        funds = attribution.get(k) or []
        funds_str = ", ".join(f"{f['fund']} ({f['value']}%)" for f in funds) if funds else "—"
        L.append(f"### {_CLASS_LABELS.get(k, k)} — {v}% of the portfolio")
        L.append(f"- Funds contributing: {funds_str}")
        L.append(f"- Decision: {_class_decision(k)}")
        L.append("")

    L.append("## 2. Per-fund contribution to asset categories")
    L.append("")
    L.append("Each scheme's portfolio weight is spread across the asset classes of its underlying "
             "securities. A security held by more than one fund has its weight shared equally "
             "between those funds, so each fund's contributions sum to its portfolio weight.")
    L.append("")
    for fund in sorted(fund_asset, key=lambda f: -sum(fund_asset[f].values())):
        parts = ", ".join(f"{_CLASS_LABELS.get(k, k)} {v:.2f}%"
                          for k, v in sorted(fund_asset[fund].items(), key=lambda x: -x[1]))
        L.append(f"- **{fund}** → {parts}")
    L.append("")

    L.append("## 3. Rule-by-rule calculation detail")
    L.append("")
    for row in compliance.get("rows") or []:
        status = "PASS" if row["pass"] is True else ("BREACH" if row["pass"] is False else "N/A")
        L.append(f"### {row['rule']} — **{status}**")
        L.append(f"- Limit: **{row['limit']}**  ·  Actual: **{row['actual']}**")
        if row.get("deviation"):
            L.append(f"- Deviation: **{_fmt_deviation(row['deviation'])}**")
        ctx = row.get("context")
        if ctx and ctx.get("kind") == "single_stock":
            held = "; ".join(f"{f['fund']} {f['value']}%" for f in ctx.get("funds") or []) or "—"
            L.append(f"- Top stock: **{ctx['security']}** ({ctx['value']}% of the portfolio) — "
                     f"held by: {held}")
        elif ctx and ctx.get("kind") == "sector":
            bd = "; ".join(f"{s['sector']} {s['weight']}%" for s in ctx.get("breakdown") or [])
            L.append(f"- Sector breakdown: {bd}")
            stock_line = "; ".join(
                f"{s['security']} {s['weight']}% ({', '.join(s['funds'])})"
                for s in ctx.get("stocks") or []) or "—"
            L.append(f"- Stocks in **{ctx['sector']}** ({ctx['weight']}%) & their funds: {stock_line}")
        elif ctx and ctx.get("kind") == "sector_topn":
            bd = "; ".join(f"{s['sector']} {s['weight']}%" for s in ctx.get("breakdown") or [])
            by_fund = "; ".join(f"{f['fund']} {f['value']}%" for f in ctx.get("funds") or []) or "—"
            L.append(f"- Top {ctx['n']} sector concentration ({ctx['value']}%): {bd}")
            L.append(f"- Funds contributing to these sectors: {by_fund}")
        elif ctx and ctx.get("kind") == "asset":
            by_fund = "; ".join(f"{f['fund']} {f['value']}%" for f in ctx.get("funds") or []) or "—"
            L.append(f"- {ctx['asset']} ({ctx['value']}%) by fund: {by_fund}")
            top_str = "; ".join(
                f"{s['security']} {s['weight']}% ({', '.join(s['funds'])})"
                for s in ctx.get("top") or []) or "—"
            L.append(f"- Top securities in this category & their funds: {top_str}")
        elif ctx and ctx.get("kind") == "cap":
            by_fund = "; ".join(f"{f['fund']} {f['value']}%" for f in ctx.get("funds") or []) or "—"
            L.append(f"- {ctx['cap']} segment ({ctx['value']}%) by fund: {by_fund}")
            stock_line = "; ".join(
                f"{s['security']} {s['weight']}% ({', '.join(s['funds'])})"
                for s in ctx.get("stocks") or []) or "—"
            L.append(f"- Stocks in this segment & their funds: {stock_line}")
        elif ctx and ctx.get("kind") == "topn":
            stock_line = "; ".join(
                f"{s['security']} {s['weight']}%" for s in ctx.get("stocks") or []) or "—"
            L.append(f"- Top {ctx['n']} holdings ({ctx['value']}% of the portfolio): {stock_line}")
        elif ctx and ctx.get("kind") == "overlap":
            L.append(f"- Largest scheme overlap: **{ctx['value']}%** between "
                     f"{', '.join(ctx.get('funds') or []) or 'n/a'}.")
        elif ctx and ctx.get("kind") == "schemes":
            schemes_str = "; ".join(f"{s['fund']} ({s['value']}%)" for s in ctx.get("schemes") or [])
            L.append(f"- Schemes held (portfolio weight): {schemes_str}")
        else:
            funds = row.get("funds") or []
            if funds:
                contrib_str = "; ".join(
                    f"{f['fund']} {f['value']}{'%' if row.get('unit') != 'count' else ''}"
                    for f in funds)
                L.append(f"- Responsible funds & contribution ({len(funds)}): {contrib_str}")
            else:
                L.append("- Responsible funds: — (not fund-attributable for this portfolio)")
        if row.get("remark"):
            L.append(f"- Remark / direction: {row['remark']}")
        L.append(f"- Calculation: {_rule_basis(row, metrics)}")
        L.append("")

    L.append("## 4. Overlap & concentration")
    L.append("")
    L.append(f"- Largest scheme overlap: **{overlap.get('overlap')}%** "
             f"({', '.join(overlap.get('funds') or []) or 'n/a'}).")
    conc = pa.get("concentration") or {}
    L.append(f"- Top 1 / 5 / 10 holdings: **{conc.get('top1_pct')}% / {conc.get('top5_pct')}% / "
             f"{conc.get('top10_pct')}%** of the portfolio.")
    L.append("")

    L.append("## 5. Coverage & data sources")
    L.append("")
    L.append(f"- Resolved **{pa.get('coverage_pct')}%** of the allocated portfolio "
             f"({pa.get('effective_total')} of {pa.get('total_weight')} allocated).")
    errors = pa.get("errors") or []
    if errors:
        L.append(f"- Lines that could not be resolved: "
                 f"{', '.join((e.get('name') or e.get('isin') or '') for e in errors)}.")
    L.append("- Each scheme's holdings are normalised to 100% of that scheme's NAV; the scheme's "
             "portfolio weight is then applied to every holding.")
    L.append("- Sources are matched in priority order: AMFI / mfdata.in → AMC-website monthly "
             "portfolio → AMC-disclosure archive → benchmark/index.")
    L.append("")
    L.append("---")
    L.append("_Diagnostic tool for factual analysis only; not investment advice. Past performance "
             "is not indicative of future returns._")
    return "\n".join(L)


def _save_markdown(label: str, text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{slug or 'portfolio'}-{stamp}.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _reweight_items(items: list[dict]) -> list[dict]:
    """Recompute weights from CURRENT market value (units x latest NAV/price)
    when the items carry ``units`` — otherwise weights are used as-is."""
    try:
        from .market_value import reweight_by_market_value
        return reweight_by_market_value(items or [])
    except Exception:  # noqa: BLE001 — market pricing is best-effort
        return items


def _apply_remark_directions(metrics: dict, rules: list[dict], pa: dict,
                             items: list[dict]) -> dict:
    """Honour per-rule remark / direction notes. A remark can change how that
    rule's metric is computed — e.g. top-N measured sector-wise, or the scheme
    count also including individual direct stocks."""
    metrics = dict(metrics)
    for r in rules or []:
        remark = (r.get("remark") or "").lower()
        rt = r.get("rule_type") or ""
        if rt == "max_top5" and "sector" in remark:
            st = pa.get("sector_table") or []
            metrics["top5"] = round(sum(s["weight"] for s in st[:5]), 2)
        elif rt == "max_top10" and "sector" in remark:
            st = pa.get("sector_table") or []
            metrics["top10"] = round(sum(s["weight"] for s in st[:10]), 2)
        elif rt in ("max_schemes", "min_schemes") and any(
                k in remark for k in ("direct stock", "direct stocks", "stocks held")):
            n_mf = len({s["id"] for s in pa.get("schemes") or []})
            n_direct = len(pa.get("stocks") or [])
            metrics["n_schemes"] = n_mf + n_direct
    return metrics


def _analyze(items: list[dict], strategy_id: int | None, uid: int, wdb: dbm.WebDB,
             allocations: dict | None = None) -> dict:
    """Analyse a portfolio against a strategy. ``items`` covers real holdings;
    ``allocations`` (asset/cap plan) covers allocation-only model portfolios."""
    if allocations:
        return _analyze_allocations(allocations, strategy_id, uid)
    # Use CURRENT market value (units x latest NAV/price) when items carry units,
    # instead of cost-based weights.
    items = _reweight_items(items)
    pa = wdb.portfolio_analysis(items)
    # "Max single holding" applies to ANY security (equity OR debt), not just
    # stocks — effective_holdings is sorted by weight, so [0] is the largest.
    eh = pa["effective_holdings"] or []
    top_holding = eh[0] if eh else None
    metrics = {
        "single_stock_max": top_holding["weight"] if top_holding else 0.0,
        "sector_max": max((s["weight"] for s in pa["sector_table"]), default=0.0),
        "asset_split": pa.get("asset_split_raw") or {},
        "cap_split": pa.get("cap_split_raw") or {},
        "top5": pa["concentration"]["top5_pct"],
        "top10": pa["concentration"]["top10_pct"],
        "overlap_max": _overlap_max(items, wdb),
        "n_schemes": len({s["id"] for s in pa["schemes"]}),
        "n_holdings": pa["n_holdings"],
        # absent asset buckets are a genuine 0 only when everything resolved
        "_portfolio_resolved": (pa.get("n_holdings", 0) > 0
                                and (pa.get("coverage_pct") or 0) >= 99.5),
    }
    result = {
        "analysis": pa,
        "metrics": metrics,
        "strategy_id": strategy_id,
    }
    if strategy_id:
        rules = userdata.get_rules(strategy_id, uid)
        metrics = _apply_remark_directions(metrics, rules, pa, items)
        result["metrics"] = metrics
        result["rules"] = rules
        result["compliance"] = evaluate_rules(rules, metrics)
    else:
        result["rules"] = []
        result["compliance"] = {"rows": [], "passed": 0, "failed": 0,
                                "total": 0, "compliance": None}

    attribution = _attribution(pa)
    overlap = _overlap_pair(items, wdb)
    attribution["scheme overlap"] = [{"fund": f, "value": overlap["overlap"]}
                                     for f in overlap["funds"]]
    # "Max single holding" attribution: the contributing funds behind the
    # largest effective holding (of ANY asset class, not just stocks).
    attribution["single stock"] = _top_contrib(_contrib([top_holding])) if top_holding else []
    # Rich per-rule context for the collapsible breakdown rows.
    contexts = _build_contexts(pa, top_holding, pa.get("sector_table") or [], overlap)
    # Sector-wise top-N rule: show the top sectors instead of the top holdings.
    if any(r.get("rule_type") == "max_top5" and "sector" in (r.get("remark") or "").lower()
           for r in result["rules"] or []):
        st = pa.get("sector_table") or []
        contexts["top 5 concentration"] = {
            "kind": "sector_topn", "n": 5,
            "value": round(sum(s["weight"] for s in st[:5]), 2),
            "breakdown": [{"sector": s["sector"], "weight": s["weight"]} for s in st[:8]],
            "funds": _top_contrib(_contrib([h for h in pa.get("effective_holdings") or []
                                           if (h.get("sector") or "") in {s["sector"] for s in st[:5]}]))
            if st else [],
        }
    for row in result["compliance"]["rows"]:
        row["funds"] = attribution.get(row.get("field")) or []
        row["context"] = contexts.get(row.get("field"))
        row["deviation"] = _deviation(row)
    result["attribution"] = attribution
    result["contexts"] = contexts
    result["overlap"] = overlap
    try:
        _ids = wdb.resolve_scheme_ids(items)
        result["overlap_matrix"] = wdb.overlap(_ids).get("matrix") or [] if len(_ids) >= 2 else []
    except Exception:  # noqa: BLE001
        result["overlap_matrix"] = []
    # Holding statement: per-scheme asset composition, normalised so each
    # scheme's total equals its exact portfolio weight.
    per_fund = _per_fund_asset(pa)
    scheme_w: dict[str, float] = {}
    for s in pa.get("schemes") or []:
        scheme_w[s["fund_name"]] = scheme_w.get(s["fund_name"], 0.0) + (s.get("weight") or 0)
    for fund, ac in per_fund.items():
        cur = sum(ac.values())
        target = scheme_w.get(fund, cur)
        if cur > 0 and abs(cur - target) > 0.01:
            scale = target / cur
            per_fund[fund] = {k: round(v * scale, 4) for k, v in ac.items()}
    result["per_fund"] = per_fund
    # Holding statement: per-scheme current market value + proportion.
    holdings = []
    for it in items:
        key = None
        try:
            if (it.get("type") or "").lower() in ("scheme", "fund", "mf"):
                s = wdb._resolve_scheme_item(it)
                key = s["fund_name"] if s else None
            elif (it.get("type") or "").lower() in ("stock", "equity"):
                sec = wdb._resolve_stock_item(it)
                key = sec["name"] if sec else None
        except Exception:  # noqa: BLE001
            pass
        key = key or (it.get("name") or "")
        holdings.append({
            "name": (it.get("name") or key),
            "type": it.get("type"),
            "units": it.get("units"),
            "nav": it.get("nav"),
            "nav_date": it.get("nav_date"),
            "value": it.get("current_value"),
            "weight": round(it.get("weight") or 0, 2),
            "composition": per_fund.get(key) or {},
        })
    result["holdings"] = holdings
    result["total_market_value"] = round(
        sum((it.get("current_value") or 0) for it in items), 2)
    try:
        from src.nav_freshness import portfolio_stale
        result["stale_holdings"] = portfolio_stale(items)
    except Exception:  # noqa: BLE001
        result["stale_holdings"] = []
    result["portfolio"] = {
        "compliance": result["compliance"]["compliance"],
        "passed": result["compliance"]["passed"],
        "failed": result["compliance"]["failed"],
        "total": result["compliance"]["total"],
        "total_weight": pa.get("total_weight"),
        "effective_total": pa.get("effective_total"),
        "coverage_pct": pa.get("coverage_pct"),
        "n_holdings": pa.get("n_holdings"),
        "n_schemes": len({s["id"] for s in pa["schemes"]}),
    }
    return result


def _analyze_allocations(allocations: dict, strategy_id: int | None, uid: int) -> dict:
    """Evaluate a strategy against an asset-allocation plan (no specific holdings)."""
    asset = allocations.get("asset") or {}
    cap = allocations.get("cap") or {}
    metrics = {
        "asset_split": {
            "stocks": float(asset.get("equity") or 0),
            "debt": float(asset.get("debt") or 0),
            "gold": float(asset.get("gold") or 0),
            "international": float(asset.get("international") or 0),
            "cash_equivalents": float(asset.get("cash") or 0),
        },
        "cap_split": {
            "large cap": float(cap.get("large") or 0),
            "mid cap": float(cap.get("mid") or 0),
            "small cap": float(cap.get("small") or 0),
            "microcap": float(cap.get("micro") or 0),
        },
        "sector_max": None,
        "single_stock_max": None,
        "top5": None, "top10": None, "overlap_max": None,
        "n_schemes": None, "n_holdings": None,
    }
    result = {
        "analysis": {
            "allocations": allocations,
            "asset_split": [{"label": k, "value": round(v, 2)}
                            for k, v in sorted(metrics["asset_split"].items(), key=lambda x: -x[1])],
            "cap_split": [{"label": k, "value": round(v, 2)}
                          for k, v in sorted(metrics["cap_split"].items(), key=lambda x: -x[1])],
            "total_weight": round(sum(metrics["asset_split"].values()), 2),
            "effective_total": round(sum(metrics["asset_split"].values()), 2),
            "coverage_pct": 100.0,
            "n_holdings": 0,
            "top_holdings": [], "others_pct": 0,
            "concentration": {"top1_pct": 0, "top5_pct": 0, "top10_pct": 0},
            "debt_analysis": {"debt_pct": metrics["asset_split"]["debt"],
                              "n_debt_holdings": 0, "ytm_pct": None,
                              "avg_maturity_yrs": None,
                              "credit_split": [], "instrument_split": [],
                              "top_debt_holdings": []},
        },
        "metrics": metrics,
        "strategy_id": strategy_id,
    }
    if strategy_id:
        rules = userdata.get_rules(strategy_id, uid)
        result["rules"] = rules
        result["compliance"] = evaluate_rules(rules, metrics)
    else:
        result["rules"] = []
        result["compliance"] = {"rows": [], "passed": 0, "failed": 0, "total": 0,
                                "na": 0, "compliance": None}
    for row in result["compliance"]["rows"]:
        row["funds"] = []
        row["context"] = None
        row["deviation"] = _deviation(row)
    result["attribution"] = {}
    result["overlap"] = {"overlap": 0.0, "funds": []}
    result["per_fund"] = {}
    result["portfolio"] = {
        "compliance": result["compliance"]["compliance"],
        "passed": result["compliance"]["passed"],
        "failed": result["compliance"]["failed"],
        "total": result["compliance"]["total"],
        "total_weight": round(sum(metrics["asset_split"].values()), 2),
        "effective_total": round(sum(metrics["asset_split"].values()), 2),
        "coverage_pct": 100.0,
        "n_holdings": 0,
        "n_schemes": 0,
    }
    return result


@router.post("/analyze")
def api_analyze(request: Request, body: dict):
    u = _user(request)
    wdb = dbm.get_db()
    uid = _uid(u)
    strategy_id = body.get("strategy_id")
    portfolio_id = body.get("portfolio_id")
    portfolio_kind = body.get("portfolio_kind") or "client"
    items = body.get("items")

    label = ""
    allocations = body.get("allocations")
    if not items:
        if portfolio_kind == "model":
            model = userdata.get_model(uid, int(portfolio_id or 0))
            if not model:
                raise HTTPException(status_code=404, detail="Model not found.")
            items = model["items"]
            allocations = allocations or model.get("allocations")
            label = model["name"]
        else:
            cp = userdata.get_client_portfolio(uid, int(portfolio_id or 0))
            if not cp:
                raise HTTPException(status_code=404, detail="Client portfolio not found.")
            items = cp["items"]
            allocations = allocations or cp.get("allocations")
            label = cp["name"]
            strategy_id = strategy_id or cp.get("strategy_id")

    if not items and not allocations:
        raise HTTPException(status_code=400, detail="No portfolio items or allocations to analyse.")

    result = _analyze(items, strategy_id, uid, wdb, allocations=allocations)
    result["label"] = label
    result["kind"] = portfolio_kind
    result["portfolio"]["label"] = label
    result["portfolio"]["kind"] = portfolio_kind
    # Descriptive markdown report of the allocation & rule calculations.
    try:
        pa = result.get("analysis") or {}
        fund_asset = _per_fund_asset(pa) if not pa.get("allocations") else {}
        result["markdown"] = _markdown_report(
            label, portfolio_kind, items, pa, result.get("metrics") or {},
            result.get("compliance") or {}, result.get("attribution") or {},
            result.get("overlap") or {}, fund_asset)
        result["report_path"] = _save_markdown(label, result["markdown"])
    except Exception:  # noqa: BLE001 — a report is best-effort; never break analysis
        result["markdown"] = ""
        result["report_path"] = ""
    # cache the run for history
    run_id = userdata.save_analysis_run(uid, portfolio_id, portfolio_kind,
                                        strategy_id, {"label": label, "kind": portfolio_kind,
                                                     "compliance": result["compliance"],
                                                     "metrics": metrics_serialize(result["metrics"])})
    result["run_id"] = run_id
    return result


@router.post("/portfolio-analytics")
def api_portfolio_analytics(request: Request, body: dict):
    """Performance & risk over a client portfolio / model / raw items [ANA3].

    Accepts the same resolution inputs as /analyze (items, or
    portfolio_id + portfolio_kind). Reconstructs the weighted scheme basket's
    NAV series on the schemes' common window and runs the standard metric
    engine. Diagnostic of the user's own portfolio — factual NAV math only."""
    u = _user(request)
    uid = _uid(u)
    wdb = dbm.get_db()
    items = body.get("items")
    transactions = parse_cas_transactions(
        body) if body.get("transactions") else None
    if not items:
        kind = body.get("portfolio_kind") or "client"
        pid = _to_int(body.get("portfolio_id") or 0, "portfolio_id")
        if kind == "model":
            model = userdata.get_model(uid, pid)
            if not model:
                raise HTTPException(status_code=404, detail="Model not found.")
            items = model["items"]
        else:
            cp = userdata.get_client_portfolio(uid, pid)
            if not cp:
                raise HTTPException(status_code=404, detail="Client portfolio not found.")
            items = cp["items"]
            if transactions is None:
                # [ANA3 movement] use THIS portfolio's stored cash-flow history
                # (loaded by ingest / seed); a model-kind portfolio simply has none.
                stored = cp.get("transactions") or []
                if stored:
                    transactions = stored
                else:
                    # Seeded demo portfolios created BEFORE transactions_json
                    # existed have none until re-seed — backfill the sample
                    # transactions once, lazily, on first analytics call, and
                    # persist so it happens exactly once per portfolio.
                    transactions = _ensure_demo_transactions(uid, cp)
    out = wdb.portfolio_analytics(items, transactions=transactions)
    out["label"] = body.get("label") or ""
    return out


def metrics_serialize(metrics: dict) -> dict:
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in metrics.items()}


# [BUG-M14] demo portfolios are identified by the durable is_demo flag
# (userdata migration + seed_samples.mark_demo_portfolios), not by name —
# name matching injected sample data into users' own same-named portfolios.
_DEMO_PORTFOLIO_NAMES = ("CAS Sample Portfolio", "Default Portfolio")  # legacy reference only


def _ensure_demo_transactions(uid: int, cp: dict) -> list:
    """Persist the CAS sample transactions onto a seeded demo portfolio that
    predates transaction storage. Returns the list (empty if not applicable).

    [BUG-M14] keyed off the durable ``is_demo`` flag written by seed_samples /
    migration — NEVER by name matching, which used to inject 907 sample
    transactions into any user portfolio that happened to share a demo name."""
    if not cp.get("is_demo"):
        return []
    try:
        from .seed_samples import cas_sample_transactions
        txs = cas_sample_transactions()
    except Exception:
        return []
    if txs:
        try:
            userdata.update_client_portfolio(uid, int(cp["id"]),
                                             transactions=txs)
        except Exception:
            pass
        return txs
    return []


@router.get("/analysis-runs")
def api_analysis_runs(request: Request):
    u = _user(request)
    return {"items": userdata.list_analysis_runs(_uid(u))}


# --------------------------------------------------------------------------
# Overview + seed samples
# --------------------------------------------------------------------------
@router.get("/overview")
def api_overview(request: Request):
    """Per client-portfolio compliance summary (runs each vs its strategy)."""
    u = _user(request)
    uid = _uid(u)
    wdb = dbm.get_db()
    cps = userdata.list_client_portfolios(uid)
    clients = {c["id"]: c["name"] for c in userdata.list_clients(uid)}
    strategies = {s["id"]: s["name"] for s in userdata.list_strategies(uid)}
    rows = []
    for cp in cps:
        row = {k: cp.get(k) for k in
               ("id", "client_id", "model_portfolio_id", "strategy_id", "name", "kind")}
        row["client_name"] = clients.get(cp.get("client_id"), "\u2014")
        row["strategy_name"] = strategies.get(cp.get("strategy_id"), "\u2014")
        row["n_lines"] = len(cp.get("items") or [])
        if not (cp.get("items") or []):
            rows.append(row)
            continue
        try:
            res = _analyze(cp["items"], cp.get("strategy_id"), uid, wdb)
            comp = res["compliance"]
            row.update(compliance=comp["compliance"], passed=comp["passed"],
                       failed=comp["failed"], total=comp["total"])
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)
        rows.append(row)
    return {"items": rows}


@router.post("/seed-samples")
def api_seed_samples(request: Request):
    u = _user(request)
    from .seed_samples import seed_for_user
    created = seed_for_user(_uid(u))
    return created


@router.post("/nav-freshness")
def api_nav_freshness(request: Request, body: dict):
    """Check NAV/price freshness; with ``backfill`` re-pull stale fund histories.

    ``codes`` / ``isins`` restrict the backfill to specific funds (fast);
    otherwise every stale fund is refreshed."""
    _user(request)
    from src.nav_freshness import backfill_navs, run_freshness
    max_age = _to_int(body.get("max_age") or 10, "max_age")
    backfill = bool(body.get("backfill"))
    codes = body.get("codes") or []
    isins = [i for i in (body.get("isins") or []) if i]

    if backfill and not codes and isins:
        from .market_value import latest_nav_index
        idx = latest_nav_index()
        seen = set()
        for i in isins:
            rec = idx.get((i or "").upper())
            c = rec.get("code") if rec else None
            if c and c not in seen:
                seen.add(c)
                codes.append(c)

    if backfill and codes:
        report = run_freshness(max_age_days=max_age, backfill=False)
        report["backfilled"] = backfill_navs(codes)
        from .market_value import invalidate_index
        invalidate_index()  # market values re-read the refreshed history
        return report

    return run_freshness(max_age_days=max_age, backfill=backfill)