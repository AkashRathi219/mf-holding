"""AI-assisted holdings extraction for outline-rendered / scanned PDFs.

Some AMC factsheets (e.g. ICICI Prudential's digital factsheets) draw text as
vector outlines -- zero fonts, zero chars -- so OCR is the only classic route
and its table reconstruction stays noisy. This module adds an optional AI tier:

    render page -> image -> vision LLM -> strict-JSON holdings rows

Provider: OpenRouter (OpenAI-compatible ``/chat/completions``), configured in
``config/settings.yaml`` under ``ai:`` with the API key read from an ENV VAR
(never stored in the repo)::

    ai:
      enabled: true            # gate; requires the key env var as well
      api_key_env: OPENROUTER_API_KEY
      base_url: https://openrouter.ai/api/v1
      model: google/gemini-2.0-flash-001
      mode: image              # image | text  (image = render pages for a VLM)
      max_pages: 4             # per-document page cap (cost control)
      dpi: 150                 # render DPI for images
      trigger_rows: 12         # heuristic OCR below this many rows -> try AI

Cost control: results are cached downstream via the sha256 parse cache, so a
document is billed at most once per source revision.

Every failure path degrades gracefully: callers fall back to heuristic OCR.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "api_key_env": "OPENROUTER_API_KEY",
    "base_url": "https://openrouter.ai/api/v1",
    "model": "google/gemini-2.0-flash-001",
    "mode": "image",          # image | text
    "max_pages": 4,
    "dpi": 150,
    "trigger_rows": 12,
    "timeout": 90,
}

SYSTEM_PROMPT = (
    "You are a precise financial-data extraction engine for Indian mutual fund "
    "monthly portfolio disclosures. From the provided factsheet pages, extract "
    "every scheme's portfolio holdings EXACTLY as printed. Rules:\n"
    "- Output ONLY a JSON object matching the requested schema.\n"
    "- percent_nav is the number printed in the '% to NAV'/'% of AUM' column "
    "(float, no % sign). Skip rows without one.\n"
    "- Keep company/instrument names verbatim (including 'Ltd.' etc.). Do NOT "
    "invent ISINs; include them only when actually printed.\n"
    "- Include every scheme found on the pages under 'schemes', each with its "
    "'name' and 'date' (as on date) when visible.\n"
    "- Ignore performance tables, fund managers, disclaimers and footnotes."
)

USER_PROMPT = (
    "Extract all portfolio holdings from these factsheet page(s). Respond with "
    'JSON: {"schemes": [{"name": str, "date": str, "holdings": '
    '[{"company": str, "percent_nav": number, "isin": str|null}]}]}'
)


class ExtractError(RuntimeError):
    """Raised when the AI provider cannot produce usable output."""


def load_cfg() -> dict:
    """Merge DEFAULTS <- settings.yaml ai: <- env toggles."""
    cfg = dict(DEFAULTS)
    try:
        import yaml
        p = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        if p.exists():
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            cfg.update(doc.get("ai") or {})
    except Exception:
        pass
    if re.match(r"^(1|true|yes)$", str(__import__("os").environ.get("AI_EXTRACT", "")), re.I):
        cfg["enabled"] = True
    return cfg


def is_configured(cfg: dict | None = None) -> bool:
    c = cfg or load_cfg()
    if not c.get("enabled"):
        return False
    import os
    return bool(os.environ.get(c.get("api_key_env") or "OPENROUTER_API_KEY"))


def _headers(cfg: dict) -> dict:
    import os
    key = os.environ.get(cfg["api_key_env"] or "OPENROUTER_API_KEY", "")
    return {"Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _page_image_b64(pdf_path: Path, page, dpi: int) -> str:
    pix = page.get_pixmap(dpi=dpi)
    from PIL import Image
    import io
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _chat(cfg: dict, messages: list[dict]) -> str:
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 4000,
    }
    last_err: Exception | None = None
    with httpx.Client(timeout=cfg.get("timeout", 90)) as client:
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                r = client.post(f"{cfg['base_url']}/chat/completions",
                                headers=_headers(cfg), json=payload)
                if r.status_code >= 500 or r.status_code == 429:
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                data = r.json()
                content = (data.get("choices") or [{}])[0].get(
                    "message", {}).get("content", "")
                usage = data.get("usage") or {}
                logger.info("AI extract: model=%s tokens=%s",
                            cfg["model"], usage.get("total_tokens"))
                return content or ""
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500 and e.response.status_code != 429:
                    raise ExtractError(
                        f"provider rejected request: HTTP {e.response.status_code} "
                        f"{e.response.text[:200]}") from e
                last_err = e
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_err = e
    raise ExtractError(f"AI provider unreachable: {last_err}")


def extract_pdf(pdf_path: Path, ocr_text: str = "") -> tuple[list[dict], dict]:
    """Extract holdings rows for ONE document via the AI provider.

    Returns ({company, percent_nav, isin} rows, meta) — meta carries
    model/seconds/token cost and validation stats. Raises ExtractError on
    unusable provider responses."""
    cfg = load_cfg()
    if not is_configured(cfg):
        raise ExtractError("AI extraction not configured (ai.enabled / key env)")
    import fitz

    t0 = time.perf_counter()
    mode = cfg.get("mode", "image")
    content: list[dict] = []
    doc = fitz.open(pdf_path)
    try:
        n_pages = min(doc.page_count, int(cfg.get("max_pages", 4)))
        if mode == "image":
            images = [_page_image_b64(pdf_path, doc[i], int(cfg.get("dpi", 150)))
                      for i in range(n_pages)]
            content = [{"type": "text", "text": USER_PROMPT}]
            content += [{"type": "image_url", "image_url": {"url": b64}}
                        for b64 in images]
        else:
            text = ocr_text.strip()
            if not text:
                text = "\n\n".join(doc[i].get_text() for i in range(n_pages))
            if not text.strip():
                raise ExtractError("no text available for text-mode extraction")
            content = [{"type": "text",
                        "text": USER_PROMPT + "\n\nFACTSHEET TEXT:\n" + text[:24000]}]
    finally:
        doc.close()

    raw = _chat(cfg, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ])
    secs = round(time.perf_counter() - t0, 1)

    rows = _parse_response(raw)
    if not rows:
        raise ExtractError("AI returned no usable holdings rows")
    meta = {"model": cfg["model"], "mode": mode, "pages": len(content) - 1
            if mode == "image" else 0, "seconds": secs,
            "pct_sum": round(sum(r["percent_nav"] for r in rows), 1)}
    return rows, meta


def _parse_response(raw: str) -> list[dict]:
    """Best-effort parse of the model JSON into normalized holding rows."""
    txt = raw.strip()
    m = re.search(r"\{.*\}", txt, re.S)  # tolerate ```json fences
    if not m:
        return []
    try:
        doc = json.loads(m.group(0))
    except Exception:
        return []
    schemes = doc.get("schemes")
    items = schemes if isinstance(schemes, list) else doc.get("holdings") or []
    rows: list[dict] = []
    seen: set[tuple] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        hold = item.get("holdings")
        cand = hold if isinstance(hold, list) else [item]
        for h in cand:
            if not isinstance(h, dict):
                continue
            name = str(h.get("company") or h.get("name") or "").strip()
            pct = h.get("percent_nav", h.get("weight"))
            if not name or pct is None:
                continue
            try:
                v = float(str(pct).replace("%", "").replace(",", ""))
            except ValueError:
                continue
            if not 0 < v <= 100:
                continue
            isin = str(h.get("isin") or "").strip().upper()
            key = (name.lower(), round(v, 3))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"company": name, "percent_nav": f"{v:g}",
                         "isin": isin if len(isin) == 12 else ""})
    return rows


def weights_valid(rows: list[dict]) -> bool:
    """Same validity rule the DB merge applies (max<=100, sum<=120)."""
    pcts = []
    for r in rows:
        try:
            pcts.append(float(r["percent_nav"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not pcts:
        return True
    return max(pcts) <= 100 and sum(pcts) <= 120
