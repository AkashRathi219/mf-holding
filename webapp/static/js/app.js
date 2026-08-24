"use strict";

const screens = {
  schemes: { key: "schemes", title: "Scheme Explorer", sub: "Filter 6,400+ schemes across 50+ AMCs", init: initSchemes },
  securities: { key: "securities", title: "Security Directory", sub: "3,941 unique ISINs · 846 pure listed stocks", init: initSecurities },
  bonds: { key: "bonds", title: "Bonds", sub: "NSE debt market: G-Sec, SDL, T-Bills & corporate bonds with YTM", init: initBonds },
  compare: { key: "compare", title: "Compare Schemes", sub: "Performance & risk, side by side — factual AMFI-NAV math only", init: initCompare },
  models: { key: "models", title: "Model Portfolios", sub: "Strategies, clients, client portfolios & compliance", init: initModels },
  admin: { key: "admin", title: "Superadmin · Data Ops", sub: "Refresh pipelines, telemetry and error logs", init: initAdmin },
};

let filters = null;
let userInfo = null;

// ---------- boot ----------
async function boot() {
  try {
    const res = await App.api("/auth/me");
    userInfo = res.user;
    document.getElementById("userName").textContent = userInfo.name;
    document.getElementById("userEmail").textContent = userInfo.email;
    if (res.superadmin) document.getElementById("navAdminLink").style.display = "";
  } catch (e) {
    return; // redirected to /login
  }
  document.getElementById("logoutBtn").addEventListener("click", (ev) => {
    ev.preventDefault();
    localStorage.removeItem("fea_token");
    location.href = "/login";
  });
  document.getElementById("drawerBackdrop").addEventListener("click", (e) => {
    if (e.target.id === "drawerBackdrop") closeDrawer();
  });
  window.addEventListener("hashchange", route);
  if (!location.hash || location.hash === "#dashboard") location.hash = "#schemes";
  route();
  loadFilters();
}

function route() {
  const hash = (location.hash || "#dashboard").replace("#", "");
  const schemeMatch = hash.match(/^scheme\/(\d+)$/);
  if (schemeMatch) {
    document.querySelectorAll(".screen").forEach(s => s.style.display = "none");
    document.getElementById("screen-schemedetail").style.display = "";
    document.querySelectorAll("#nav a").forEach(a => a.classList.remove("active"));
    document.getElementById("pageTitle").textContent = "Scheme Details";
    document.getElementById("pageSub").textContent = "Holdings, NAV and the two important dates";
    renderSchemeDetail(Number(schemeMatch[1]), document.getElementById("schemeDetailBody"),
                       document.getElementById("schemeDetailTitle"));
    window.scrollTo(0, 0);
    return;
  }
  const secMatch = hash.match(/^security\/([A-Z0-9]+)$/i);
  if (secMatch) {
    document.querySelectorAll(".screen").forEach(s => s.style.display = "none");
    document.getElementById("screen-secdetail").style.display = "";
    document.querySelectorAll("#nav a").forEach(a => a.classList.remove("active"));
    document.getElementById("pageTitle").textContent = "Security Details";
    document.getElementById("pageSub").textContent = "Price, corporate actions, financial reports & scheme exposure";
    renderSecurityDetail(secMatch[1].toUpperCase(), document.getElementById("secDetailBody"),
                         document.getElementById("secDetailTitle"));
    window.scrollTo(0, 0);
    return;
  }
  const screen = screens[hash];
  if (!screen) {
    // [U2] unknown hashes get an explicit 404 panel — the old silent
    // fallback to Scheme Explorer is how four built screens went missing.
    document.querySelectorAll(".screen").forEach(s => s.style.display = "none");
    document.getElementById("screen-notfound").style.display = "";
    document.querySelectorAll("#nav a").forEach(a => a.classList.remove("active"));
    const h = document.getElementById("notfoundHash");
    if (h) h.textContent = "#" + hash;
    document.getElementById("pageTitle").textContent = "Not found";
    document.getElementById("pageSub").textContent = "Unknown screen";
    window.scrollTo(0, 0);
    return;
  }
  const key = screen.key;
  document.querySelectorAll(".screen").forEach(s => s.style.display = "none");
  document.getElementById("screen-" + key).style.display = "";
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("active", a.dataset.screen === key));
  document.getElementById("pageTitle").textContent = screen.title;
  document.getElementById("pageSub").textContent = screen.sub;
  if (typeof screen.init === "function") screen.init();
  window.scrollTo(0, 0);
}

// ---------- filters ----------
async function loadFilters() {
  try {
    filters = await App.api("/filters");
    fillSelect("schemeAmc", filters.amcs);
    fillSelect("schemeCategory", filters.categories);
    fillSelect("schemeSource", filters.sources.map(s => ({ v: s, l: App.sourceLabel(s) })));  // [U4L] never render raw source keys
    fillSelect("schemeCoverage", filters.coverage.map(s => ({ v: s, l: s.replace(/_/g, " ") })));
    fillSelect("secCap", filters.caps);
    fillSelect("secSector", filters.sectors);
  } catch (e) { /* handled by App.api */ }
}

function fillSelect(id, items) {
  const sel = document.getElementById(id);
  const cur = sel.value;
  sel.innerHTML = `<option value="">All</option>` +
    items.map(i => {
      const v = typeof i === "object" ? i.v : i, l = typeof i === "object" ? i.l : i;
      return `<option value="${App.esc(v)}">${App.esc(l)}</option>`;
    }).join("");
  if (cur) sel.value = cur;
}

// ---------- dashboard ----------
let dashLoaded = false;
function initDashboard() {
  if (dashLoaded) return;
  dashLoaded = true;
  loadDashboard();
}

async function loadDashboard() {
  const m = await App.api("/meta");
  const kpis = [
    { l: "AMCs covered", v: App.formatNum(m.amcs), s: "distinct AMCs", cls: "accent" },
    { l: "Schemes", v: App.formatNum(m.schemes), s: App.formatNum(m.schemes_with_holdings) + " with holdings", cls: "" },
    { l: "Holding rows", v: App.formatNum(m.holdings), s: App.formatPct(m.isin_completeness, 1) + " with ISIN", cls: "green" },
    { l: "Pure listed stocks", v: App.formatNum(m.pure_stocks), s: "of " + App.formatNum(m.securities) + " securities", cls: "" },
    { l: "ISIN completeness", v: App.formatPct(m.isin_completeness, 1), s: "of holding rows", cls: "accent" },
    { l: "Schemes covered", v: m.schemes ? App.formatPct(m.schemes_with_holdings / m.schemes * 100, 1) : "\u2014", s: "coverage rate", cls: "green" },
  ];
  document.getElementById("kpis").innerHTML = kpis.map(k =>
    `<div class="kpi card ${k.cls}"><div class="kpi-label">${k.l}</div><div class="kpi-value">${k.v}</div><div class="kpi-sub">${k.s}</div></div>`
  ).join("");

  // sector bar chart
  const sectors = Object.entries(m.sector_dist).slice(0, 12);
  const canvas = document.getElementById("chartSector");
  mountChart(() => Charts._renderBar(canvas, sectors.map(s => s[0]), sectors.map(s => s[1]), { height: 280 }));
  Charts._renderBar(canvas, sectors.map(s => s[0]), sectors.map(s => s[1]), { height: 280 });

  // cap donut
  const caps = Object.entries(m.cap_dist);
  const capCanvas = document.getElementById("chartCap");
  mountChart(() => Charts._renderDonut(capCanvas, caps.map(c => ({ label: c[0], value: c[1] })), { center: App.formatNum(m.pure_stocks), centerLabel: "pure stocks" }));
  Charts._renderDonut(capCanvas, caps.map(c => ({ label: c[0], value: c[1] })), { center: App.formatNum(m.pure_stocks), centerLabel: "pure stocks" });
  document.getElementById("capLegend").innerHTML = caps.map((c, i) =>
    `<span><i style="background:${["var(--chart-1)","var(--chart-5)","var(--chart-4)","var(--chart-6)","var(--chart-8)","var(--chart-7)"][i % 6]}"></i>${c[0]}: ${App.formatNum(c[1])}</span>`).join("");

  // ISIN gauge
  const isinCanvas = document.getElementById("chartIsin");
  mountChart(() => Charts._renderGauge(isinCanvas, m.isin_completeness, 100, { label: "ISIN completeness", color: "@success" }));
  Charts._renderGauge(isinCanvas, m.isin_completeness, 100, { label: "ISIN completeness", color: "@success" });

  // coverage bars
  const cov = Object.entries(m.coverage_dist).sort((a, b) => b[1] - a[1]);
  const covColors = { has_holdings: "var(--success)", no_disclosure: "var(--destructive)", discovery_needed: "var(--warning)", universe_only: "var(--text-3)" };
  document.getElementById("coverageBars").innerHTML = cov.map(([k, v]) => {
    const pct = (v / m.schemes * 100);
    return `<div style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:4px">
      <span>${App.esc(k.replace(/_/g, " "))}</span><span class="mono">${App.formatNum(v)}</span></div>
      <div style="height:8px;background:var(--surface-2);border-radius:4px"><div style="height:8px;border-radius:4px;width:${pct}%;background:${covColors[k] || "var(--primary)"}"></div></div></div>`;
  }).join("");

  // category bars
  const cat = Object.entries(m.category_dist).sort((a, b) => b[1] - a[1]);
  const catCanvas2 = document.getElementById("categoryBars");
  // simple inline bars
  document.getElementById("categoryBars").innerHTML = cat.map(([k, v]) => {
    const pct = (v / m.schemes * 100);
    return `<div style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:4px">
      <span>${App.esc(k)}</span><span class="mono">${App.formatNum(v)}</span></div>
      <div style="height:8px;background:var(--surface-2);border-radius:4px"><div style="height:8px;border-radius:4px;width:${pct}%;background:var(--primary)"></div></div></div>`;
  }).join("");
}

// ---------- scheme explorer ----------
const schemeState = { offset: 0, limit: 50 };
function initSchemes() {
  const search = document.getElementById("schemeSearch");
  search.addEventListener("input", App.debounce(() => { schemeState.offset = 0; loadSchemes(); }, 300));
  ["schemeAmc", "schemeCategory", "schemeSource", "schemeCoverage"].forEach(id =>
    document.getElementById(id).addEventListener("change", () => { schemeState.offset = 0; loadSchemes(); }));
  document.getElementById("schemeReset").addEventListener("click", () => {
    ["schemeAmc", "schemeCategory", "schemeSource", "schemeCoverage"].forEach(id => document.getElementById(id).value = "");
    search.value = ""; schemeState.offset = 0; loadSchemes();
  });
  document.getElementById("schemeNext").addEventListener("click", () => { schemeState.offset += schemeState.limit; loadSchemes(); });
  document.getElementById("schemePrev").addEventListener("click", () => { schemeState.offset = Math.max(0, schemeState.offset - schemeState.limit); loadSchemes(); });
  loadSchemes();
}

function confBadge(cf) {
  const TIER = {green: ["high", "var(--success)"], amber: ["medium", "var(--warning)"],
                red: ["low", "var(--destructive)"], grey: ["no data", "var(--text-3)"]};
  const [lbl, col] = TIER[(cf && cf.tier) || "grey"] || TIER.grey;
  const lines = [
    "Data confidence: " + (cf && cf.score != null ? cf.score + " / 100" : "n/a"),
    "Source: " + (cf && cf.source ? App.sourceLabel(cf.source) : "none"),  // [U4L] masked, was raw key
    "Disclosure age: " + (cf && cf.age_days != null ? cf.age_days + " days" : "unknown"),
  ];
  if (cf && cf.stale) lines.push("Holdings disclosure older than 180 days");  // [U3]
  if (cf && cf.isin_pct != null) lines.push("ISIN coverage: " + cf.isin_pct + "%");
  if (cf && cf.reason) lines.push("Reason: " + cf.reason);
  const staleMark = (cf && cf.stale)
    ? `<span title="${App.esc("No fresh holdings disclosure in 180+ days")}" style="color:var(--red);font-weight:600"> \u00b7 stale</span>` : "";
  return `<span title="${App.esc(lines.join("\n"))}" style="display:inline-flex;align-items:center;gap:4px;font-size:10px;color:${col};white-space:nowrap;margin-left:6px">
    <span style="width:7px;height:7px;border-radius:50%;background:${col};display:inline-block"></span>${lbl}${staleMark}</span>`;
}

async function loadSchemes() {
  const p = new URLSearchParams({ limit: schemeState.limit, offset: schemeState.offset });
  const v = (id) => document.getElementById(id).value;
  if (v("schemeSearch")) p.set("search", v("schemeSearch"));
  if (v("schemeAmc")) p.set("amc", v("schemeAmc"));
  if (v("schemeCategory")) p.set("category", v("schemeCategory"));
  if (v("schemeSource")) p.set("source", v("schemeSource"));
  if (v("schemeCoverage")) p.set("coverage", v("schemeCoverage"));
  const tbody = document.getElementById("schemeTbody");
  tbody.innerHTML = `<tr><td colspan="8" class="empty"><span class="spin"></span> Loading schemes…</td></tr>`;
  try {
    const data = await App.api("/schemes?" + p.toString());
    document.getElementById("schemeCount").textContent = App.formatNum(data.total) + " schemes";
    const pages = Math.ceil(data.total / schemeState.limit);
    document.getElementById("schemePageInfo").textContent = `Page ${Math.floor(schemeState.offset / schemeState.limit) + 1} of ${Math.max(1, pages)}`;
    document.getElementById("schemePrev").disabled = schemeState.offset === 0;
    document.getElementById("schemeNext").disabled = schemeState.offset + schemeState.limit >= data.total;
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty">No schemes match your filters.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.items.map(s => {
      const top = s.top_holding ? `${App.esc(s.top_holding)} <span class="mono" style="color:var(--text-3)">${App.formatPct(s.top_holding_pct)}</span>` : "—";
      const flags = [];
      if (s.is_index) flags.push(App.badge("index", "blue"));
      if (s.is_etf) flags.push(App.badge("ETF", "blue"));
      if (s.is_fof) flags.push(App.badge("FoF", "grey"));
      return `<tr class="clickable" onclick="location.hash='#scheme/${s.id}'">
        <td><strong>${App.esc(s.fund_name)}</strong>${confBadge(s.confidence)}<br><span class="mono" style="font-size:11px;color:var(--text-3)">${App.formatDate(s.as_of)}</span></td>
        <td>${App.esc(s.amc)}</td>
        <td>${App.badge(s.category)} ${flags.join(" ")}</td>
        <td>${App.flagBadge(s.coverage)}</td>
        <td class="num">${App.formatNum(s.n_holdings)}</td>
        <td class="num">${App.formatNum(s.aum, 1)}</td>
        <td>${top}</td>
        <td><button class="btn btn-outline btn-sm">Details</button></td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">${App.esc(e.message)}</td></tr>`;
  }
}

async function renderSchemeDetail(id, container, titleEl) {
  container.innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`;
  try {
    const [s, nav] = await Promise.all([
      App.api(`/schemes/${id}?holdings=1`),
      App.api(`/schemes/${id}/nav`).catch(() => null),
    ]);
    navCache = nav;
    const h = s.holdings || [];

    // Shared KPI card — same language as the dashboard strip.
    const kpi = (label, value, sub) => `<div class="kpi card"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div>${sub ? `<div class="kpi-sub">${sub}</div>` : ""}</div>`;
    const CATS = [
      ["stocks", "Stocks"],
      ["debt", "Debt"],
      ["international", "International"],
      ["future_options", "Futures & Options"],
      ["cash_equivalents", "Cash equivalents"],
      ["other", "Other"],
    ];
    const group = (cat) => h.filter(x => (x.asset_class || "other") === cat);
    const stockRow = (x) => `<tr>
        <td>${App.esc(x.company)}</td>
        <td class="mono">${App.esc(x.isin || "—")}</td>
        <td class="num">${App.formatNum(x.quantity)}</td>
        <td class="num">${App.formatINR(x.market_value)}</td>
        <td class="num">${App.formatPct(x.percent_nav)}</td>
        <td>${App.capBadge(x.cap)}</td>
        <td>${App.esc(x.sector || "—")}</td>
      </tr>`;
    const debtRow = (x) => `<tr>
        <td>${App.esc(x.company)}</td>
        <td class="mono">${App.esc(x.isin || "—")}</td>
        <td class="num">${x.coupon != null ? App.formatPct(x.coupon, 2) : "—"}</td>
        <td class="mono">${App.esc(x.maturity_date || "—")}</td>
        <td class="num">${x.ytm != null ? App.formatPct(x.ytm, 2) : "—"}</td>
        <td>${App.badge(x.rating || "—", x.rating ? "green" : "grey")}</td>
        <td class="num">${App.formatPct(x.percent_nav)}</td>
      </tr>`;
    const debtTotalRow = (items, label, sumPct) => `<tr class="total-row">
        <td colspan="6" style="font-weight:700">${App.esc(label)} subtotal (${App.formatNum(items.length)})</td>
        <td class="num" style="font-weight:700">${App.formatPct(sumPct)}</td>
      </tr>`;

    // One table per present asset class -> rendered behind segmented tabs.
    const classTables = [];
    CATS.forEach(([key, label]) => {
      const items = group(key);
      if (!items.length) return;
      const sumPct = items.reduce((a, x) => a + (x.percent_nav || 0), 0);
      const sumMv = items.reduce((a, x) => a + (x.market_value || 0), 0);
      const isDebt = key === "debt";
      const head = isDebt
        ? `<th>Security</th><th>ISIN</th><th class="r">Coupon</th><th>Maturity</th><th class="r">YTM</th><th>Rating</th><th class="r">% NAV</th>`
        : `<th>Company</th><th>ISIN</th><th class="r">Qty</th><th class="r">Market value</th><th class="r">% NAV</th><th>Cap</th><th>Sector</th>`;
      const rows = isDebt ? items.map(debtRow).join("") : items.map(stockRow).join("");
      const subtotal = isDebt
        ? debtTotalRow(items, label, sumPct)
        : `<tr class="total-row">
            <td colspan="2" style="font-weight:700">${App.esc(label)} subtotal (${App.formatNum(items.length)})</td>
            <td class="num" style="font-weight:700">—</td>
            <td class="num" style="font-weight:700">${App.formatINR(sumMv)}</td>
            <td class="num" style="font-weight:700">${App.formatPct(sumPct)}</td>
            <td></td><td></td>
          </tr>`;
      classTables.push({
        key, label,
        count: items.length, pct: sumPct, mv: sumMv,
        table: `<div class="table-wrap" style="max-height:44vh; overflow:auto">
            <table class="data"><thead><tr>${head}</tr></thead><tbody>${rows}${subtotal}</tbody></table>
          </div>`,
      });
    });

    const allSumPct = h.reduce((a, x) => a + (x.percent_nav || 0), 0);
    const allSumMv = h.reduce((a, x) => a + (x.market_value || 0), 0);

    const terReg = s.ter_regular != null ? App.formatPct(s.ter_regular * 100, 3) : "—";
    const terDir = s.ter_direct != null ? App.formatPct(s.ter_direct * 100, 3) : "—";
    const amfiR = s.amfi_regular || "—";
    const amfiD = s.amfi_direct || "—";
    const isinR = s.isin_regular || "—";
    const isinD = s.isin_direct || "—";
    const isDebtFund = (s.category || "").toLowerCase() === "debt";

    const factRow = (label, valueHtml) => `<tr><td style="color:var(--text-2);font-weight:600;width:38%">${label}</td><td>${valueHtml}</td></tr>`;
    let factsRows = "";
    factsRows += factRow("Category", App.badge(s.category));
    factsRows += factRow("Coverage", App.flagBadge(s.coverage));
    factsRows += factRow("Confidence", confBadge(s.confidence));
    factsRows += factRow("Holdings as-of", `${App.esc(s.holdings_date ? App.formatDate(s.holdings_date) : "\u2014")} <span class="page-sub">(weekly announcement)</span>`);
    factsRows += factRow(`AMFI code <span class="page-sub">(reg / dir)</span>`, `<span class="mono">${App.esc(amfiR)} / ${App.esc(amfiD)}</span>`);
    factsRows += factRow(`ISIN <span class="page-sub">(reg / dir)</span>`, `<span class="mono">${App.esc(isinR)} / ${App.esc(isinD)}</span>`);
    if (isDebtFund) {
      factsRows += factRow("Yield to Maturity", s.ytm != null ? App.formatPct(s.ytm * 100, 2) : "—");
      factsRows += factRow("Duration", s.duration != null ? App.formatNum(s.duration, 2) + " yrs" : "—");
      factsRows += factRow("Avg. Maturity", s.avg_maturity != null ? App.formatNum(s.avg_maturity, 2) + " yrs" : "—");
    }

    const topHolding = s.top_holding ? `
      <div class="top-holding" style="margin-bottom:12px">
        <span class="top-holding-label">Top holding</span>
        <span class="top-holding-name">${App.esc(s.top_holding)}</span>
        ${s.top_holding_pct ? `<span class="top-holding-pct">${App.formatPct(s.top_holding_pct)}</span>` : ""}
      </div>` : "";

    if (titleEl) titleEl.textContent = `${s.fund_name} \u00b7 ${s.amc} \u00b7 ${App.sourceLabel(s.source)}`;

    const holdingsCard = h.length ? `
      <div class="card" style="margin-bottom:18px">
        <h3>Holdings by asset class <span class="badge grey">${App.formatNum(h.length)} lines · ${App.formatPct(allSumPct)} of NAV</span></h3>
        ${classTables.length > 1 ? `<div class="tabs hold-tabs" style="margin-bottom:14px">${classTables.map((t, i) =>
          `<button type="button" class="tab${i === 0 ? " active" : ""}" data-hold="${t.key}">${App.esc(t.label)}&nbsp;<span class="pill-count">${App.formatNum(t.count)}</span></button>`).join("")}</div>` : ""}
        ${classTables.map((t, i) => `<div class="tab-panel${i === 0 ? " active" : ""}" data-hold-panel="${t.key}">${t.table}</div>`).join("")}
      </div>`
      : `<div class="empty">No holdings on record (${App.esc(s.coverage.replace(/_/g, " "))}).</div>`;

    container.innerHTML = `
      <h2 style="margin-bottom:2px">${App.esc(s.fund_name)}${confBadge(s.confidence)}</h2>
      <div class="page-sub" style="margin-bottom:16px">${App.esc(s.amc)} · ${App.sourceLabel(s.source)} · ${App.badge(s.category)}</div>

      <div class="grid auto" style="margin-bottom:16px">
        ${kpi("Latest NAV", s.nav_value != null ? App.esc(s.nav_value) : "\u2014", `as of ${App.esc(s.nav_date ? App.formatDate(s.nav_date) : "\u2014")} · daily`)}
        ${kpi("AUM", s.aum != null ? App.formatINR(s.aum, 1) + " cr" : "\u2014", `holdings ${App.esc(s.holdings_date ? App.formatDate(s.holdings_date) : "\u2014")}`)}
        ${kpi("Holdings", App.formatNum(h.length), `${App.formatNum(s.n_equity)} equity`)}
        ${kpi("Expense ratio", `${terReg} → ${terDir}`, "regular → direct")}
      </div>

      <div class="grid two" style="margin-bottom:16px">
        <div class="card">
          <h3>Scheme facts</h3>
          <table class="data" style="min-width:0"><tbody>${factsRows}</tbody></table>
        </div>
        <div class="card">
          <h3>Asset mix</h3>
          ${topHolding}
          <table class="data" style="min-width:0">
            <thead><tr><th>Asset class</th><th class="r">Lines</th><th class="r">Market value</th><th class="r">% NAV</th></tr></thead>
            <tbody>
              ${classTables.map(t => `<tr class="clickable" data-mix="${t.key}" title="Show ${App.esc(t.label)} holdings">
                  <td>${App.esc(t.label)}</td><td class="num">${App.formatNum(t.count)}</td>
                  <td class="num">${App.formatINR(t.mv)}</td><td class="num">${App.formatPct(t.pct)}</td></tr>`).join("")}
              <tr class="total-row"><td>Total</td><td class="num">${App.formatNum(h.length)}</td>
                  <td class="num">${App.formatINR(allSumMv)}</td><td class="num">${App.formatPct(allSumPct)}</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div id="schemeAnalytics"></div>
      ${holdingsCard}
      ${buildNavSection(id, nav)}`;

    // Segmented tabs for asset-class tables; Asset-mix rows jump to the class.
    const activateHoldTab = (key) => {
      container.querySelectorAll(".hold-tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.hold === key));
      container.querySelectorAll("[data-hold-panel]").forEach(p => p.classList.toggle("active", p.dataset.holdPanel === key));
      const pane = container.querySelector(`[data-hold-panel="${key}"]`);
      if (pane && pane.classList.contains("active")) pane.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
    container.querySelectorAll(".hold-tabs .tab").forEach(b =>
      b.addEventListener("click", () => activateHoldTab(b.dataset.hold)));
    container.querySelectorAll("tr[data-mix]").forEach(tr =>
      tr.addEventListener("click", () => activateHoldTab(tr.dataset.mix)));

    renderSchemeAnalytics(id);
  } catch (e) {
    container.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`;
  }
}

// ---------- performance & risk analytics [ANA1] ----------
// "21 Aug 2025 → 21 Aug 2026" from an engine *_window object — the exact
// dates a figure was computed over, shown beside every metric.
function fmtWinRange(w) {
  if (!w || !w.start || !w.end) return "";
  return App.formatDate(w.start) + " → " + App.formatDate(w.end);
}

function renderSchemeAnalytics(id) {
  const el = document.getElementById("schemeAnalytics");
  if (!el) return;
  el.innerHTML = `<div class="empty"><span class="spin"></span> Loading performance &amp; risk…</div>`;
  App.api(`/schemes/${id}/analytics`).then(a => {
    if (a.error) { el.innerHTML = `<div class="empty">${App.esc(a.error)}</div>`; return; }
    const c = a.cagr_pct || {};
    // Window helpers: returns table shows From / To as separate columns so
    // every figure carries the exact dates it was computed over.
    const wStart = (w) => (w && w.start) ? App.formatDate(w.start) : "\u2014";
    const wEnd = (w) => (w && w.end) ? App.formatDate(w.end) : "\u2014";
    const partial = (w) => (w && w.complete === false) ? ` <span class="badge amber">partial</span>` : "";
    const retRow = (label, v, w) => `<tr>
        <td>${label}</td>
        <td class="num">${v != null ? App.formatNum(v, 2) + "%" : "\u2014"}</td>
        <td class="mono">${wStart(w)}</td>
        <td class="mono">${wEnd(w)}</td>
        <td>${partial(w)}</td>
      </tr>`;

    let html = `
      <div class="card" style="margin-bottom:16px">
        <h3>Returns <span class="badge grey">CAGR</span></h3>
        <table class="data" style="min-width:0">
          <thead><tr><th>Period</th><th class="r">CAGR</th><th>From</th><th>To</th><th></th></tr></thead>
          <tbody>
            ${retRow("Since inception", c.since_inception, c.since_inception_window)}
            ${retRow("1 year", c.y1, c.y1_window)}
            ${retRow("3 years", c.y3, c.y3_window)}
            ${retRow("5 years", c.y5, c.y5_window)}
          </tbody>
        </table>
      </div>`;

    const r = a.risk || {};
    const roll = a.rolling_1y;
    const b = a.benchmark;
    const hc = a.history_completeness;

    if (!a.risk) {
      // Honest dated empty-state: which dates were considered, what was found.
      const ru = a.risk_unavailable || {};
      const detail = ru.window_start
        ? `Risk stats need \u2265 ${ru.required_points ?? 30} NAV points spanning \u2265 ${ru.required_span_days ?? 365} days inside ${App.formatDate(ru.window_start)} \u2192 ${App.formatDate(ru.window_end)} (3y window). This scheme's history there: ${ru.found_points ?? 0} points${ru.found_start ? ` (${App.formatDate(ru.found_start)} \u2192 ${App.formatDate(ru.found_end)})` : ""}.`
        : "";
      html += `
      <div class="card" style="margin-bottom:16px">
        <h3>Risk &amp; benchmark</h3>
        <div class="empty" style="padding:18px">Not enough NAV history for risk analytics yet.${detail ? `<div class="page-sub" style="margin-top:6px">${detail}</div>` : ""}</div>
      </div>`;
    } else {
      const riskRow = (label, valHtml, note) => `<tr>
          <td style="color:var(--text-2);font-weight:600;width:34%">${label}</td>
          <td class="num">${valHtml}</td>
          <td class="page-sub" style="text-align:right">${note || ""}</td>
        </tr>`;
      let riskRows = "";
      riskRows += riskRow("Window (risk)", fmtWinRange(r), `${App.formatNum(r.n_points)} NAV points · 3y`);
      riskRows += riskRow("Volatility (ann.)", r.volatility_pct != null ? App.formatNum(r.volatility_pct, 2) + "%" : "\u2014", "annualised, 3y");
      riskRows += riskRow("Sharpe ratio", r.sharpe != null ? App.formatNum(r.sharpe, 2) : "\u2014", `rf assumed ${App.formatNum(a.rf_pct_assumption, 1)}%`);
      riskRows += riskRow("Sortino ratio", r.sortino != null ? App.formatNum(r.sortino, 2) : "\u2014", "");
      riskRows += riskRow("Max drawdown", r.max_drawdown_pct != null ? `<span style="color:var(--red)">${App.formatNum(r.max_drawdown_pct, 2)}%</span>` : "\u2014", "3y");
      if (b) {
        riskRows += `<tr class="total-row"><td colspan="3">vs ${App.esc(a.benchmark_index || "benchmark")} <span class="page-sub">· common days ${fmtWinRange(b)}</span></td></tr>`;
        riskRows += riskRow("Beta", b.beta != null ? App.formatNum(b.beta, 2) : "\u2014", "");
        riskRows += riskRow("Alpha (ann.)", b.alpha_pct != null ? App.formatNum(b.alpha_pct, 2) + "%" : "\u2014", "");
        riskRows += riskRow("Tracking error", b.tracking_error_pct != null ? App.formatNum(b.tracking_error_pct, 2) + "%" : "\u2014", "");
        riskRows += riskRow("Information ratio", b.information_ratio != null ? App.formatNum(b.information_ratio, 2) : "\u2014", "");
      }
      html += `
      <div class="card" style="margin-bottom:16px">
        <h3>Risk &amp; benchmark</h3>
        <table class="data" style="min-width:0"><tbody>${riskRows}</tbody></table>
      </div>`;
    }

    const rollPts = (a.rolling_points || []).filter(p => Array.isArray(p) && p.length === 2);
    if (roll && rollPts.length > 30) {
      const statCell = (label, v) => `<div class="kpi card" style="padding:12px 14px">
          <div class="kpi-label">${label}</div>
          <div class="kpi-value" style="font-size:19px">${v}</div>
        </div>`;
      html += `
      <div class="card" style="margin-bottom:16px">
        <h3>Rolling 1-year return
          <span class="badge grey">${App.formatNum(roll.pct_positive, 1)}% positive</span>
        </h3>
        <div class="grid" style="grid-template-columns:repeat(auto-fit, minmax(120px, 1fr)); gap:10px; margin-bottom:14px">
          ${statCell("% positive", App.formatNum(roll.pct_positive, 1) + "%")}
          ${statCell("Median", App.formatNum(roll.median_pct, 2) + "%")}
          ${statCell("Best", App.formatNum(roll.best_pct, 2) + "%")}
          ${statCell("Worst", App.formatNum(roll.worst_pct, 2) + "%")}
        </div>
        <div class="page-sub">Daily-history windows stepped weekly · windows ${fmtWinRange({ start: roll.first_window_start, end: roll.last_window_end })} · ${App.formatNum(roll.n_periods)} samples</div>
        <div id="schemeRollChart" style="margin-top:8px"></div>
      </div>`;
    }

    html += `
      <div class="page-sub" style="margin-top:2px">As of ${App.formatDate(a.as_of)} · computed from AMFI NAV history (${App.formatNum(a.points)} points${a.inception ? `, inception ${App.formatDate(a.inception.date)}` : ""}, ${App.esc(a.plan_used || "")} plan, methodology ${App.esc(a.methodology_version || "")}).${hc ? ` History: ${App.formatNum(hc.points)} pts · max gap ${App.formatNum(hc.max_gap_days)}d${hc.complete ? " · complete" : ""}.` : ""} Percentile-neutral factual figures only.</div>
      <div class="page-sub" style="margin-top:4px"><b>${App.esc(a.disclaimer)}</b></div>`;

    el.innerHTML = html;
    if (rollPts.length > 30) {
      Charts.mountMultiLine(document.getElementById("schemeRollChart"),
        [{ name: "Rolling 1Y return", color: "@primary",
           dates: rollPts.map(p => p[0]), values: rollPts.map(p => p[1]) }],
        { formatValue: v => App.formatNum(v, 1) + "%" });
    }
  }).catch(e => {
    el.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`;
  });
}

// ---------- NAV history (Regular / Direct collapsible buttons) ----------
let navCache = null;
let navControllers = {};

function buildNavSection(id, nav) {
  const plans = [
    ["regular", "Regular"],
    ["direct", "Direct"],
  ];
  if (!nav) return "";
  const buttons = plans.map(([p, label]) =>
    `<button class="nav-toggle" data-plan="${p}" onclick="toggleNav('${p}')">${label}<span class="caret">▸</span></button>`).join("");
  const panes = plans.map(([p, label]) => {
    const d = nav[p];
    const body = d
      ? `<div class="kv" style="margin-bottom:10px">
           <dt>Inception date</dt><dd>${App.esc(d.inception)}</dd>
           <dt>First NAV</dt><dd>${App.formatNum(d.first_nav, 4)}</dd>
           <dt>Latest NAV</dt><dd>${App.formatNum(d.last_nav, 4)} (${App.esc(d.last_date)})</dd>
           <dt>Data points</dt><dd>${App.formatNum(d.points)}</dd>
         </div>
         <div class="nav-chart" id="nav-chart-${p}"></div>`
      : `<div class="empty">No NAV history available for the ${label} plan (AMFI code ${App.esc((nav[p] && nav[p].code) || "—")}).</div>`;
    return `<div class="nav-pane" id="nav-${p}" hidden>${body}</div>`;
  }).join("");
  return `
    <div class="nav-section">
      <h3>NAV History (since inception)</h3>
      <div class="nav-actions">${buttons}</div>
      ${panes}
    </div>`;
}

function toggleNav(plan) {
  const isOpen = !document.getElementById("nav-" + plan).hidden;
  const targetOpen = !isOpen;  // clicking the open plan closes it; clicking a closed one opens it
  ["regular", "direct"].forEach(p => {
    const pane = document.getElementById("nav-" + p);
    const btn = document.querySelector('.nav-toggle[data-plan="' + p + '"]');
    const on = (p === plan) ? targetOpen : false;  // only the clicked plan can be open
    if (pane) pane.hidden = !on;
    if (btn) {
      btn.classList.toggle("active", on);
      const caret = btn.querySelector(".caret");
      if (caret) caret.textContent = on ? "▾" : "▸";
    }
    if (on) renderNavChart(p);
  });
}

function renderNavChart(plan) {
  const el = document.getElementById("nav-chart-" + plan);
  if (!el || !navCache || !navCache[plan]) return;
  if (!navControllers[plan]) {
    navControllers[plan] = Charts.mountNavChart(el, navCache[plan], {
      color: plan === "direct" ? "@success" : "@primary",
      height: 240,
      formatNav: v => App.formatNum(v, 2),
    });
  }
  // Re-render now so a chart that was mounted while its pane was hidden picks up
  // the real (non-zero) width of the now-visible pane.
  if (navControllers[plan] && navControllers[plan].render) navControllers[plan].render();
}

function closeDrawer() { document.getElementById("drawerBackdrop").classList.remove("open"); }

function openDrawer(html) {
  const el = document.getElementById("drawer");
  el.innerHTML = html;
  document.getElementById("drawerBackdrop").classList.add("open");
}

// ---------- securities ----------
const secState = { offset: 0, limit: 50 };
function initSecurities() {
  const search = document.getElementById("secSearch");
  search.addEventListener("input", App.debounce(() => { secState.offset = 0; loadSecurities(); }, 300));
  ["secEquity", "secCap", "secSector"].forEach(id =>
    document.getElementById(id).addEventListener("change", () => { secState.offset = 0; loadSecurities(); }));
  document.getElementById("secReset").addEventListener("click", () => {
    ["secEquity", "secCap", "secSector"].forEach(id => document.getElementById(id).value = "");
    search.value = ""; secState.offset = 0; loadSecurities();
  });
  document.getElementById("secNext").addEventListener("click", () => { secState.offset += secState.limit; loadSecurities(); });
  document.getElementById("secPrev").addEventListener("click", () => { secState.offset = Math.max(0, secState.offset - secState.limit); loadSecurities(); });
  loadSecurities();
}

async function loadSecurities() {
  const p = new URLSearchParams({ limit: secState.limit, offset: secState.offset });
  const v = (id) => document.getElementById(id).value;
  if (v("secSearch")) p.set("q", v("secSearch"));
  if (v("secEquity")) p.set("confirmed_equity", v("secEquity"));
  if (v("secCap")) p.set("cap", v("secCap"));
  if (v("secSector")) p.set("sector", v("secSector"));
  const tbody = document.getElementById("secTbody");
  tbody.innerHTML = `<tr><td colspan="7" class="empty"><span class="spin"></span> Loading…</td></tr>`;
  try {
    const data = await App.api("/securities?" + p.toString());
    document.getElementById("secCount").textContent = App.formatNum(data.total) + " securities";
    const pages = Math.ceil(data.total / secState.limit);
    document.getElementById("secPageInfo").textContent = `Page ${Math.floor(secState.offset / secState.limit) + 1} of ${Math.max(1, pages)}`;
    document.getElementById("secPrev").disabled = secState.offset === 0;
    document.getElementById("secNext").disabled = secState.offset + secState.limit >= data.total;
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty">No securities match your filters.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.items.map(x => {
      const type = x.confirmed_equity === 1 ? App.badge("Pure listed", "green")
        : x.confirmed_equity === 0.5 ? App.badge("Mixed", "amber")
        : App.badge("Non-equity", "grey");
      return `<tr class="clickable" onclick="location.hash='#security/${App.esc(x.isin)}'">
        <td class="mono">${App.esc(x.isin)}</td>
        <td>${App.esc(x.name)}</td>
        <td>${type}</td>
        <td>${App.capBadge(x.cap)}</td>
        <td>${App.esc(x.sector)}</td>
        <td class="num">${App.formatNum(x.source_count)}</td>
        <td><button class="btn btn-outline btn-sm">Details</button></td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">${App.esc(e.message)}</td></tr>`;
  }
}

// ---------- bonds (NSE debt market) ----------
const bondState = { offset: 0, limit: 50, wired: false };
function initBonds() {
  if (!bondState.wired) {
    bondState.wired = true;
    const search = document.getElementById("bondSearch");
    search.addEventListener("input", App.debounce(() => { bondState.offset = 0; loadBonds(); }, 300));
    ["bondSegment", "bondRating", "bondMaturity", "bondStatus", "bondSort"].forEach(id =>
      document.getElementById(id).addEventListener("change", () => { bondState.offset = 0; loadBonds(); }));
    document.getElementById("bondTraded").addEventListener("change", () => { bondState.offset = 0; loadBonds(); });
    document.getElementById("bondReset").addEventListener("click", () => {
      ["bondSegment", "bondRating", "bondMaturity", "bondStatus", "bondSort"].forEach(id =>
        document.getElementById(id).value = "");
      document.getElementById("bondTraded").checked = false;
      search.value = ""; bondState.offset = 0; loadBonds();
    });
    document.getElementById("bondNext").addEventListener("click", () => { bondState.offset += bondState.limit; loadBonds(); });
    document.getElementById("bondPrev").addEventListener("click", () => { bondState.offset = Math.max(0, bondState.offset - bondState.limit); loadBonds(); });
  }
  loadBondMeta();
  loadBonds();
}

async function loadBondMeta() {
  try {
    const f = await App.api("/bonds/meta");
    fillSelect("bondSegment", Object.entries(f.segments.counts)
      .sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ v: k, l: `${k} (${v})` })));
    fillSelect("bondRating", (f.ratings.items || []).filter(r => r !== "Unrated/Other"));
    fillSelect("bondStatus", (f.statuses.items || []).map(s => ({ v: s, l: s })));
    renderBondMeta(f);
  } catch (e) { /* handled by App.api */ }
}

function renderBondMeta(f) {
  const el = document.getElementById("bondAsOf");
  if (!el) return;
  const fetched = f.fetched_at ? String(f.fetched_at).slice(0, 10) : "—";
  el.innerHTML = `<div><strong>Report trading date:</strong> ${App.esc(f.as_of || "—")} · ` +
    `<strong>Data fetched on:</strong> <span class="mono">${App.esc(fetched)}</span> ` +
    `(NSE bulk files: ${App.esc((f.sources || []).join(" · "))})</div>
    <div class="page-sub" style="margin-top:4px">${App.formatNum(f.n_bonds)} bonds · ` +
    `${App.formatNum(f.n_traded)} with a last-trade price · ` +
    `${App.formatNum(f.n_with_ytm)} with a yield to maturity</div>
    ${coverageCardHtml(f.coverage)}`;
}

function coverageCardHtml(c) {
  if (!c) return "";
  const h = c.holdings || {};
  const y = h.ytm_present || {}, ym = h.ytm_missing || {};
  const cp = h.coupon_present || {}, cm = h.coupon_missing || {};
  const s = c.schemes || {};
  const smb = s.missing_breakdown || {};
  const n = h.total || 0;
  const pctOf = (m) => m > 0 ? " (" + App.formatPct(m / n * 100, 1) + ")" : "";
  const errs = (c.summary ? c.summary.error_rows : 0);
  return `
  <details class="card" style="margin-top:10px;padding:12px 16px">
    <summary style="cursor:pointer"><strong>Data coverage &amp; gaps</strong> — ${App.formatNum(n)} debt holdings of fund portfolios</summary>
    <div class="kv" style="margin-top:10px">
      <dt>Debt funds with scheme YTM</dt><dd>${App.formatNum(s.with_ytm)} of ${App.formatNum(s.debt_funds)} — missing ${App.formatNum(s.missing_ytm || 0)} (${App.formatNum(smb.unmatched_name || 0)} name-merge gap · ${App.formatNum(smb.not_in_universe || 0)} not in universe feed)</dd>
      <dt>YTM of holdings</dt><dd>${App.formatPct(h.ytm_present_pct, 1)} covered — fund text ${App.formatNum(y.fund_text || 0)}${pctOf(y.fund_text || 0)} · NSE-reported ${App.formatNum(y.nse_reported || 0)} · computed from NSE price ${App.formatNum(y.computed || 0)}</dd>
      <dt>YTM missing (no info)</dt><dd>${App.formatNum(ym.never_traded || 0)} never traded · ${App.formatNum(ym.not_in_catalog || 0)} outside NSE debt set · ${App.formatNum(ym.no_isin || 0)} no ISIN — ${App.formatNum(ym.perpetual || 0)} errors</dd>
      <dt>Coupon of holdings</dt><dd>${App.formatPct(h.coupon_present_pct, 1)} covered — fund text ${App.formatNum(cp.fund_text || 0)} · NSE catalog ${App.formatNum(cp.catalog || 0)} · ${App.formatPct(h.coupon_not_applicable_pct, 1)} N/A (T-Bills / CP / zero-coupon / floating)</dd>
      <dt>Coupon missing (no info)</dt><dd>${App.formatNum(cm.not_in_catalog || 0)} outside NSE debt set · ${App.formatNum(cm.no_isin || 0)} no ISIN · ${App.formatNum(cm.no_fixed_coupon || 0)} no fixed coupon stated — ${App.formatNum(cm.extraction_gap || 0)} extraction errors</dd>
    </div>
    <div class="page-sub" style="margin-top:6px">Errors = rows where NSE records carry the value but the pipeline failed to capture it. Everything else is genuinely not provided by the market or the fund.</div>
  </details>`;
}

async function loadBonds() {
  const p = new URLSearchParams({ limit: bondState.limit, offset: bondState.offset });
  const v = (id) => document.getElementById(id).value;
  if (v("bondSearch")) p.set("q", v("bondSearch"));
  if (v("bondSegment")) p.set("segment", v("bondSegment"));
  if (v("bondRating")) p.set("rating", v("bondRating"));
  if (v("bondMaturity")) p.set("maturity", v("bondMaturity"));
  if (v("bondStatus")) p.set("status", v("bondStatus"));
  if (v("bondSort")) p.set("sort", v("bondSort"));
  if (document.getElementById("bondTraded").checked) p.set("only_traded", "1");
  const tbody = document.getElementById("bondTbody");
  tbody.innerHTML = `<tr><td colspan="10" class="empty"><span class="spin"></span> Loading bonds…</td></tr>`;
  try {
    const data = await App.api("/bonds?" + p.toString());
    document.getElementById("bondCount").textContent = App.formatNum(data.total) + " bonds";
    const pages = Math.ceil(data.total / bondState.limit);
    document.getElementById("bondPageInfo").textContent = `Page ${Math.floor(bondState.offset / bondState.limit) + 1} of ${Math.max(1, pages)}`;
    document.getElementById("bondPrev").disabled = bondState.offset === 0;
    document.getElementById("bondNext").disabled = bondState.offset + bondState.limit >= data.total;
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty">No bonds match your filters.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.items.map(x => {
      const segBadge = segmentBadge(x.segment);
      const ratingBadge = x.rating ? App.badge(x.rating, "green")
        : x.rating_band ? App.badge(x.rating_band, "grey") : `<span class="page-sub">—</span>`;
      const ytmTitle = (x.ytm != null && x.last_trade_date) ? ` title="YTM ${App.esc(x.ytm_source || "")}; price/yield quoted at last trade ${App.esc(x.last_trade_date)} (${tradeAgeAgo(x.last_trade_date)})"` : "";
      const ytm = x.ytm != null
        ? `<strong ${ytmTitle}>${App.formatPct(x.ytm, 2)}</strong>${(x.last_trade_date && Date.now() - new Date(x.last_trade_date + "T00:00:00").getTime() > 90 * 86400000) ? ` <span class="page-sub" title="Price is older than 90 days — YTM is quoted at that trade date">stale</span>` : ""}`
        : `<span class="page-sub">—</span>`;
      const price = x.price != null ? App.formatNum(x.price, 2) : `<span class="page-sub">—</span>`;
      const lastTrade = x.last_trade_date
        ? `${App.esc(x.last_trade_date)} <span class="page-sub" title="Last date this bond actually traded on NSE; illiquid bonds may stay untraded for years">· ${tradeAgeAgo(x.last_trade_date)}</span>`
        : `<span class="page-sub">no trade</span>`;
      return `<tr class="clickable" onclick="openBondDetail('${App.esc(x.isin)}')">
        <td class="mono">${App.esc(x.isin)}</td>
        <td><strong>${App.esc(x.name)}</strong></td>
        <td>${segBadge}</td>
        <td>${App.esc(x.issuer || "—")}</td>
        <td class="num">${x.coupon != null ? App.formatNum(x.coupon, 2) + "%" : "—"}</td>
        <td class="mono">${App.esc(x.maturity_date || "—")}</td>
        <td>${ratingBadge}</td>
        <td class="num">${price}</td>
        <td class="num"><strong>${ytm}</strong></td>
        <td class="mono">${lastTrade}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty">${App.esc(e.message)}</td></tr>`;
  }
}

function segmentBadge(segment) {
  const seg = (segment || "Other") + "";
  const tone = seg.startsWith("T-Bill") ? "blue"
    : seg.startsWith("G-Sec") ? "green"
    : seg.startsWith("State") ? "amber"
    : seg.includes("PSU") ? "blue"
    : seg.includes("Commercial") || seg.includes("Certificate") ? "grey"
    : "green";
  return App.badge(seg, tone);
}

function tradeAgeAgo(isoDate) {
  const d = new Date(isoDate + "T00:00:00");
  if (isNaN(d.getTime())) return "";
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 1) return "today";
  if (days < 7) return days + "d ago";
  if (days < 31) return Math.round(days / 7) + "w ago";
  if (days < 365) return Math.round(days / 30) + "mo ago";
  return (days / 365).toFixed(1) + "y ago";
}

// Shared overlap-matrix heat scale (identical on every screen that renders
// the matrix so the same pair never gets different colours).
function overlapHeat(v) {
  if (v <= 0) return "heat-0";
  if (v < 10) return "heat-1";
  if (v < 20) return "heat-2";
  if (v < 30) return "heat-3";
  if (v < 45) return "heat-4";
  return "heat-5";
}

async function openBondDetail(isin) {
  try {
    const b = await App.api(`/bonds/${encodeURIComponent(isin)}`);
    const kv = (l, v) => `<div class="kv"><dt>${App.esc(l)}</dt><dd>${v}</dd></div>`;
    const y = b.ytm != null ? App.formatPct(b.ytm, 2) + ` <span class="page-sub">(${App.esc(b.ytm_source || "—")})</span>` : `<span class="page-sub">—</span>`;
    openDrawer(`
      <h3 style="margin-top:0">${App.esc(b.name || b.isin)}</h3>
      <div class="page-sub" style="margin-bottom:12px"><span class="mono">${App.esc(b.isin)}</span> · ${segmentBadge(b.segment)} ${b.status ? App.badge(b.status, "grey") : ""} ${b.sectype ? `<span class="badge blue">${App.esc(b.sectype)}</span>` : ""}</div>
      <div class="kv" style="margin-bottom:10px">
        ${kv("Issuer", App.esc(b.issuer || ""))}
        ${kv("Coupon", b.coupon != null ? App.formatNum(b.coupon, 2) + "%" + (b.coupon_freq ? ` per ${App.esc(b.coupon_freq)}` : "") : "Floating / zero-coupon")}
        ${kv("Rating", b.rating ? App.badge(b.rating, "green") : App.esc(b.rating_band || "Unrated"))}
        ${kv("Face value", b.face_value != null ? "₹" + App.formatNum(b.face_value, 2) : "—")}
        ${kv("Issue date", App.esc(b.issue_date || "—"))}
        ${kv("Maturity", App.esc(b.maturity_date || "—") + (b.days_to_maturity != null ? ` <span class="page-sub">(${App.formatNum(b.days_to_maturity, 0)} days)</span>` : ""))}
        ${kv("Last trade date", App.esc(b.last_trade_date || "—"))}
        ${kv("Last trade price", b.price != null ? App.formatNum(b.price, 4) + ` <span class="page-sub">(per ₹${App.formatNum(b.face_value || 100, 0)})</span>` : "—")}
        ${kv("Yield to maturity", y)}
        ${kv("Modified duration", b.modified_duration != null ? App.formatNum(b.modified_duration, 2) + " yrs <span class=\"page-sub\">(price sensitivity per 1% yield move \u2248 " + App.formatNum(b.modified_duration, 2) + "%)</span>" : "—")}
        ${kv("Weighted avg price / yield", (b.wa_price != null ? App.formatNum(b.wa_price, 4) : "—") + " / " + (b.wa_yield != null ? App.formatNum(b.wa_yield, 2) + "%" : "—"))}
        ${kv("Traded value", b.traded_value_cr != null ? "" + App.formatNum(b.traded_value_cr, 2) + " ₹cr · last " + App.esc(b.last_trade_value_lakhs != null ? "₹" + App.formatNum(b.last_trade_value_lakhs, 2) + " lakhs" : "n/a") : "—")}
      </div>
      <div class="page-sub">YTM is NSE-reported when available; otherwise calculated from coupon + last-trade price + maturity via the standard bond price equation. Source: ${App.esc(b.source || "—")}.</div>
      <div class="page-sub" style="margin-top:6px"><b>Last trade dates:</b> the date shown is when this bond actually last traded on NSE (from the exchange's own security-master report). Corporate bonds are largely illiquid — many have not traded for months or years, so their prices are the last-quoted ones, and YTM is solved at that trade date.</div>`);
  } catch (e) {
    App.toast(e.message, true);
  }
}


async function renderSecurityDetail(isin, container, titleEl) {
  container.innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`;
  navControllers = {};
  try {
    const [s, price, actions, reports] = await Promise.all([
      App.api(`/securities/${encodeURIComponent(isin)}`),
      App.api(`/securities/${encodeURIComponent(isin)}/price`).catch(() => null),
      App.api(`/securities/${encodeURIComponent(isin)}/actions`).catch(() => null),
      App.api(`/securities/${encodeURIComponent(isin)}/reports`).catch(() => null),
    ]);
    const type = s.confirmed_equity === 1 ? "Pure listed stock" : s.confirmed_equity === 0.5 ? "Mixed (REIT/InvIT/preference/convertible)" : "Non-equity (bond/CP/ETF/fund)";
    const typeTone = s.confirmed_equity === 1 ? "green" : s.confirmed_equity === 0.5 ? "amber" : "grey";
    const rows = s.used_in && s.used_in.length ? s.used_in.map(u => `<tr>
        <td>${App.esc(u.fund_name)}</td>
        <td>${App.esc(u.amc)}</td>
        <td class="num">${App.formatPct(u.percent_nav)}</td>
        <td>${App.formatDate(u.as_of)}</td>
      </tr>`).join("") : `<tr><td colspan="4" class="empty">No weighted holdings reference this security.</td></tr>`;

    const kpi = (label, value, sub) => `<div class="kpi card"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div>${sub ? `<div class="kpi-sub">${sub}</div>` : ""}</div>`;
    const hasPrice = !!(price && price.available !== false && price.dates && price.dates.length);
    const lastDate = hasPrice ? price.dates[price.dates.length - 1] : "\u2014";

    const factRow = (label, valueHtml) => `<tr><td style="color:var(--text-2);font-weight:600;width:38%">${label}</td><td>${valueHtml}</td></tr>`;
    let factsRows = "";
    factsRows += factRow("Type", App.badge(type, typeTone));
    factsRows += factRow("Market cap", App.capBadge(s.cap));
    factsRows += factRow("Sector", App.esc(s.sector || "\u2014"));
    factsRows += factRow("Source count", App.formatNum(s.source_count));
    factsRows += factRow("Aliases", App.esc(s.aliases || "\u2014"));

    if (titleEl) titleEl.textContent = `${s.name} \u00b7 ${s.isin}`;
    container.innerHTML = `
      <h2 style="margin-bottom:2px">${App.esc(s.name)} ${App.badge(type, typeTone)}</h2>
      <div class="mono page-sub" style="margin-bottom:16px">${App.esc(s.isin)}</div>

      <div class="grid auto" style="margin-bottom:16px">
        ${hasPrice ? kpi("Latest close", App.formatNum(price.last_close, 2), App.esc(lastDate)) : ""}
        ${hasPrice ? kpi("Data points", App.formatNum(price.points), `since ${App.esc(price.inception || "\u2014")}`) : ""}
        ${kpi("Sources", App.formatNum(s.source_count), "mapping references")}
      </div>

      <div class="card" style="margin-bottom:16px">
        <h3>Security facts</h3>
        <table class="data" style="min-width:0"><tbody>${factsRows}</tbody></table>
      </div>

      ${buildStockPriceSection(isin, price)}
      ${buildStockActionsSection(actions)}
      ${buildStockReportsSection(reports)}

      <div class="card">
        <h3>Top weighted in schemes <span class="badge grey">${s.used_in ? s.used_in.length : 0}</span></h3>
        <div class="table-wrap" style="max-height:40vh; overflow:auto">
          <table class="data">
            <thead><tr><th>Scheme</th><th>AMC</th><th class="r">% NAV</th><th>As of</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
    stockPriceCache = price;
    renderStockPriceChart(isin);
  } catch (e) {
    container.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`;
  }
}

// ---------- stock price / actions / reports ----------
function buildStockPriceSection(isin, price) {
  if (!price || price.available === false || !price.dates) return "";
  const last = price.dates && price.dates.length ? price.dates[price.dates.length - 1] : "—";
  const snapRow = (label, val) => `<tr><td style="color:var(--text-2);font-weight:600;width:38%">${label}</td><td>${val}</td></tr>`;
  return `
    <div class="card" style="margin-bottom:16px">
      <h3>Price history (daily close)</h3>
      <table class="data" style="min-width:0"><tbody>
        ${snapRow("Latest close", `${App.formatNum(price.last_close, 2)} <span class="page-sub">(${App.esc(last)})</span>`)}
        ${snapRow("Inception", App.esc(price.inception || "—"))}
        ${snapRow("Data points", App.formatNum(price.points))}
      </tbody></table>
      <button class="nav-toggle" data-plan="price" style="margin-top:12px" onclick="toggleStockPrice('${isin}')">Price chart <span class="caret">▸</span></button>
      <div class="nav-pane" id="nav-price" hidden>
        <div class="nav-chart" id="nav-chart-price"></div>
      </div>
    </div>`;
}

function toggleStockPrice(isin) {
  const pane = document.getElementById("nav-price");
  const btn = document.querySelector('.nav-toggle[data-plan="price"]');
  if (!pane) return;
  const show = pane.hidden;
  pane.hidden = !show;
  if (btn) {
    btn.classList.toggle("active", show);
    const caret = btn.querySelector(".caret");
    if (caret) caret.textContent = show ? "▾" : "▸";
  }
  if (show) renderStockPriceChart(isin);
}

function renderStockPriceChart(isin) {
  const el = document.getElementById("nav-chart-price");
  if (!el || !stockPriceCache || !stockPriceCache.dates) return;
  if (!navControllers["price"]) {
    navControllers["price"] = Charts.mountNavChart(el, {
      dates: stockPriceCache.dates,
      navs: stockPriceCache.closes,
    }, {
      color: "@primary", height: 240,
      formatNav: v => App.formatNum(v, 2),
    });
  }
  if (navControllers["price"] && navControllers["price"].render) navControllers["price"].render();
}

let stockPriceCache = null;

function buildStockActionsSection(actions) {
  if (!actions || actions.available === false) return "";
  const divs = actions.dividends || [];
  const splits = actions.splits || [];
  // Sortable 'YYYY-MM-DD' key for a 'DD-Mon-YYYY' date.
  const dateKey = (s) => {
    const m = /^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/.exec(s || "");
    if (m) {
      const months = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
      return m[3] + "-" + String(months[m[2].toLowerCase()] || 0).padStart(2, "0") + "-" + String(+m[1]).padStart(2, "0");
    }
    return s || "";
  };
  const rows = [
    ...divs.map(d => ({ key: dateKey(d.date), html: `<tr><td>${App.esc(d.date)}</td><td>Dividend</td><td class="num">${App.formatNum(d.amount, 4)}</td><td>—</td></tr>` })),
    ...splits.map(s => ({ key: dateKey(s.date), html: `<tr><td>${App.esc(s.date)}</td><td>Split</td><td>—</td><td class="num">${App.esc(s.ratio)}</td></tr>` })),
  ].sort((a, b) => b.key.localeCompare(a.key)).map(r => r.html).join("");
  if (!rows) return "";
  return `
    <div class="card" style="margin-bottom:16px">
      <h3>Corporate actions (dividends &amp; splits) <span class="badge grey">${divs.length + splits.length}</span></h3>
      <button class="nav-toggle" data-sec="actions" onclick="toggleSecPane('actions')">Show actions <span class="caret">▸</span></button>
      <div class="nav-pane" id="sec-pane-actions" hidden style="margin-top:12px">
        <div class="table-wrap" style="max-height:34vh; overflow:auto">
          <table class="data">
            <thead><tr><th>Date</th><th>Type</th><th class="r">Amount (₹)</th><th class="r">Ratio</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    </div>`;
}

function toggleSecPane(name) {
  const pane = document.getElementById("sec-pane-" + name);
  const btn = document.querySelector('.nav-toggle[data-sec="' + name + '"]');
  if (!pane) return;
  const show = pane.hidden;
  pane.hidden = !show;
  if (btn) {
    btn.classList.toggle("active", show);
    btn.firstChild.textContent = show ? "Hide " : "Show ";
    const caret = btn.querySelector(".caret");
    if (caret) caret.textContent = show ? "▾" : "▸";
  }
}

function buildStockReportsSection(reports) {
  if (!reports || reports.available === false) return "";
  const ann = reports.announcements || [];
  if (!ann.length) return "";
  const rows = ann.map(a => `<tr>
      <td>${App.esc(a.date)}</td>
      <td>${App.esc(a.headline)}</td>
      <td>${a.url ? `<a href="${App.esc(a.url)}" target="_blank" rel="noopener">PDF</a>` : "—"}</td>
    </tr>`).join("");
  return `
    <div class="card" style="margin-bottom:16px">
      <h3>Recent financial reports / announcements <span class="badge grey">NSE · ${ann.length}</span></h3>
      <div class="table-wrap" style="max-height:34vh; overflow:auto">
        <table class="data">
          <thead><tr><th>Date</th><th>Headline</th><th>Attachment</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

// ---------- portfolio tools (weighted schemes + stocks) ----------
const pfState = { items: [], prefix: "pf" };
const PF_MAX = 10;

const PF_PRESETS = {
  cas: { label: "CAS Sample", items: [] },
  balanced: {
    label: "Balanced Growth",
    items: [
      { type: "scheme", id: 182, name: "HDFC FLEXI CAP FUND", weight: 20 },
      { type: "scheme", id: 1260, name: "ICICI Prudential Large Cap Fund", weight: 10 },
      { type: "scheme", id: 204, name: "HDFC MID CAP FUND", weight: 10 },
      { type: "scheme", id: 1911, name: "Parag Parikh Flexi Cap Fund", weight: 10 },
      { type: "scheme", id: 2015, name: "SBI Nifty 50 ETF", weight: 10 },
      { type: "scheme", id: 569, name: "ADITYA BIRLA SUN LIFE CORPORATE BOND FUND", weight: 10 },
      { type: "scheme", id: 1986, name: "SBI Liquid Fund", weight: 5 },
      { type: "stock", isin: "INE040A01034", name: "HDFC Bank Ltd.", weight: 10 },
      { type: "stock", isin: "INE002A01018", name: "Reliance Industries Ltd.", weight: 10 },
      { type: "stock", isin: "INE009A01021", name: "Infosys Limited", weight: 5 },
    ],
  },
  aggressive: {
    label: "Equity Aggressive",
    items: [
      { type: "scheme", id: 182, name: "HDFC FLEXI CAP FUND", weight: 25 },
      { type: "scheme", id: 204, name: "HDFC MID CAP FUND", weight: 15 },
      { type: "scheme", id: 568, name: "ADITYA BIRLA SUN LIFE ELSS TAX SAVER FUND", weight: 15 },
      { type: "scheme", id: 1911, name: "Parag Parikh Flexi Cap Fund", weight: 15 },
      { type: "stock", isin: "INE002A01018", name: "Reliance Industries Ltd.", weight: 10 },
      { type: "stock", isin: "INE154A01025", name: "ITC Limited", weight: 10 },
      { type: "stock", isin: "INE467B01029", name: "Tata Consultancy Services Ltd.", weight: 10 },
    ],
  },
  conservative: {
    label: "Conservative",
    items: [
      { type: "scheme", id: 569, name: "ADITYA BIRLA SUN LIFE CORPORATE BOND FUND", weight: 25 },
      { type: "scheme", id: 1986, name: "SBI Liquid Fund", weight: 20 },
      { type: "scheme", id: 161, name: "HDFC BALANCED ADVANTAGE FUND", weight: 25 },
      { type: "scheme", id: 2015, name: "SBI Nifty 50 ETF", weight: 15 },
      { type: "stock", isin: "INE040A01034", name: "HDFC Bank Ltd.", weight: 10 },
      { type: "stock", isin: "INE002A01018", name: "Reliance Industries Ltd.", weight: 5 },
    ],
  },
};

let pfAutoRan = false;
let prAutoRan = false;

function pfMount(prefix, onRun, autoRun) {
  pfState.prefix = prefix;

  const s = document.getElementById(prefix + "Search");
  s.addEventListener("input", App.debounce(async () => {
    const q = s.value.trim();
    const box = document.getElementById(prefix + "SchemeSuggest");
    if (q.length < 2) { box.style.display = "none"; return; }
    document.getElementById(prefix + "SchemeHint").textContent = "Searching\u2026";
    try {
      const d = await App.api("/schemes?search=" + encodeURIComponent(q) + "&coverage=has_holdings&limit=12");
      document.getElementById(prefix + "SchemeHint").textContent = "";
      if (!d.items.length) { box.style.display = "block"; box.innerHTML = `<div class="empty">No schemes found.</div>`; return; }
      box.style.display = "block";
      box.innerHTML = d.items.map(x => `<div class="chip" style="cursor:pointer;display:flex;border-radius:0;background:var(--surface);border-bottom:1px solid var(--border);justify-content:space-between;width:100%"
        onclick="pfAdd('scheme',{id:${x.id},name:'${App.esc(x.fund_name.replace(/'/g, "\\'"))}'})">
        <span>${App.esc(x.fund_name)}</span><span class="badge blue">${App.esc(x.amc)}</span></div>`).join("");
    } catch (e) { document.getElementById(prefix + "SchemeHint").textContent = e.message; }
  }, 250));

  const st = document.getElementById(prefix + "StockSearch");
  st.addEventListener("input", App.debounce(async () => {
    const q = st.value.trim();
    const box = document.getElementById(prefix + "StockSuggest");
    if (q.length < 2) { box.style.display = "none"; return; }
    document.getElementById(prefix + "StockHint").textContent = "Searching\u2026";
    try {
      const d = await App.api("/securities?q=" + encodeURIComponent(q) + "&limit=12");
      document.getElementById(prefix + "StockHint").textContent = "";
      if (!d.items.length) { box.style.display = "block"; box.innerHTML = `<div class="empty">No stocks found.</div>`; return; }
      box.style.display = "block";
      box.innerHTML = d.items.map(x => `<div class="chip" style="cursor:pointer;display:flex;border-radius:0;background:var(--surface);border-bottom:1px solid var(--border);justify-content:space-between;width:100%"
        onclick="pfAdd('stock',{isin:'${x.isin}',name:'${App.esc((x.name || "").replace(/'/g, "\\'"))}'})">
        <span>${App.esc(x.name)}</span><span class="mono" style="color:var(--text-3)">${App.esc(x.isin)}</span></div>`).join("");
    } catch (e) { document.getElementById(prefix + "StockHint").textContent = e.message; }
  }, 250));

  document.getElementById(prefix + "Upload").addEventListener("change", (e) => {
    if (e.target.files[0]) pfLoadUpload(e.target.files[0]);
    e.target.value = "";
  });
  document.getElementById(prefix + "Sample").addEventListener("click", pfLoadSample);
  document.getElementById(prefix + "Run").addEventListener("click", onRun);
  document.getElementById(prefix + "Clear").addEventListener("click", () => { pfState.items = []; pfRender(); });
  document.getElementById(prefix + "Preset").addEventListener("change", (e) => {
    if (e.target.value) pfLoadPreset(e.target.value);
  });

  if (autoRun) {
    if (prefix === "pr") {
      if (!prAutoRan) { prAutoRan = true; pfLoadPreset("cas"); setTimeout(onRun, 700); }
      else { pfRender(); }
    } else {
      if (!pfAutoRan) { pfAutoRan = true; pfLoadPreset("cas"); setTimeout(onRun, 700); }
      else { pfRender(); }
    }
  } else {
    pfRender();
  }
}

function pfLoadPreset(key) {
  if (key === "cas") return pfLoadCasSample();
  const preset = PF_PRESETS[key];
  if (!preset) { App.toast("Unknown preset.", true); return Promise.resolve(); }
  pfState.items = preset.items.map(x => ({ ...x }));
  pfRender();
  App.toast(`Loaded tracker: ${preset.label}`);
}

async function pfLoadCasSample() {
  try {
    const d = await App.api("/cas-sample");
    pfState.items = (d.items || []).map(x => ({
      type: x.type, isin: x.isin || null, name: x.name || "", weight: x.weight,
    }));
    pfRender();
    App.toast(`Loaded ${d.label} (${pfState.items.length} holdings)`);
  } catch (e) { App.toast("CAS sample unavailable: " + e.message, true); }
}

function pfAdd(type, ref) {
  if (pfState.items.length >= PF_MAX) { App.toast(`Maximum ${PF_MAX} lines per portfolio.`, true); return; }
  pfState.items.push({ type, id: ref.id || null, isin: ref.isin || null, name: ref.name || "", weight: null });
  pfRender();
}

function pfRemove(idx) { pfState.items.splice(idx, 1); pfRender(); }

function pfWeight(idx, val) { pfState.items[idx].weight = val === "" ? null : parseFloat(val); }

function pfRender() {
  const p = pfState.prefix;
  const tb = document.getElementById(p + "Tbody");
  const rows = pfState.items.map((it, i) => `<tr>
      <td>${i + 1}</td>
      <td><span class="badge ${it.type === "scheme" ? "blue" : "green"}">${it.type === "scheme" ? "Scheme" : "Stock"}</span></td>
      <td>${App.esc(it.name)}${it.isin ? ` <span class="mono" style="color:var(--text-3)">${App.esc(it.isin)}</span>` : ""}</td>
      <td class="r"><input type="number" min="0" max="100" step="0.1" class="pf-weight" value="${it.weight != null ? it.weight : ""}" placeholder="%" oninput="pfWeight(${i}, this.value)"></td>
      <td><button class="btn btn-outline btn-sm" onclick="pfRemove(${i})">\u2715</button></td>
    </tr>`).join("") || `<tr><td colspan="5" class="empty">No lines yet \u2014 search schemes/stocks above, or upload JSON.</td></tr>`;
  tb.innerHTML = rows;
  document.getElementById(p + "Count").textContent = `${pfState.items.length}/${PF_MAX}`;
  const total = pfState.items.reduce((a, it) => a + (it.weight || 0), 0);
  document.getElementById(p + "Total").textContent = `Allocated: ${App.formatPct(total, 1)}`;
}

function pfLoadSample() {
  pfLoadCasSample();
}

function pfLoadUpload(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(reader.result);
      const list = Array.isArray(parsed) ? parsed : parsed.portfolio || parsed.items || [];
      if (!Array.isArray(list) || !list.length) throw new Error("expected {portfolio:[...]} or a JSON array");
      if (list.length > 50) { App.toast("JSON has too many lines (>50).", true); return; }
      pfState.items = list.map(x => ({
        type: (x.type === "stock" || x.type === "equity") ? "stock" : "scheme",
        id: x.id || null, isin: x.isin || null, name: x.name || "",
        weight: x.weight != null ? Number(x.weight) : null,
      }));
      pfRender();
    } catch (e) { App.toast("Invalid JSON: " + e.message, true); }
  };
  reader.readAsText(file);
}

function pfItemsPayload() {
  return pfState.items.map(it => ({
    type: it.type,
    ...(it.id ? { id: it.id } : {}),
    ...(it.isin ? { isin: it.isin } : {}),
    ...(it.name ? { name: it.name } : {}),
    weight: it.weight || 0,
  }));
}

function pfNormalize100(entries) {
  const total = (entries || []).reduce((a, e) => a + (e.value || 0), 0);
  if (!total) return entries || [];
  const scaled = entries.map(e => ({ ...e, value: +(e.value / total * 100).toFixed(2) }));
  const sum = scaled.reduce((a, e) => a + e.value, 0);
  if (scaled.length && sum !== 100) {
    scaled[scaled.length - 1].value = +(scaled[scaled.length - 1].value + (100 - sum)).toFixed(2);
  }
  return scaled;
}

function capSegmentLabel(k) {
  const m = { "large cap": "Large", "mid cap": "Mid", "small cap": "Small", microcap: "Micro",
              ipo: "IPO", debt: "Debt", "fund units": "Fund units", unclassified: "Unclassified" };
  return m[k] || k;
}

function capPieData(cap) {
  const capLabels = { "large cap": "Large", "mid cap": "Mid", "small cap": "Small", "microcap": "Micro" };
  const caps = Object.keys(capLabels);
  const known = caps.map(c => ({ label: capLabels[c], value: cap[c] || 0 }));
  // Break the residual buckets out individually instead of lumping into "Other".
  const otherLabels = { ipo: "IPO", debt: "Debt", "fund units": "Fund units",
                        unclassified: "Unclassified", "": "Other" };
  for (const [k, v] of Object.entries(cap || {})) {
    if (!caps.includes(k) && v) known.push({ label: otherLabels[k] || k, value: v });
  }
  return known;
}

// Build pie entries as a full 100% breakup of the RESOLVED portion of the
// portfolio (normalised to 100). No "Unresolved / Unallocated" slice is added —
// the weight with no data on record is surfaced separately via the coverage
// note, so every chart is a clean, complete 100% breakout of what is known.
function pfPieFull100(entries, resolvedTotal) {
  const list = (entries || []).filter(e => (e.value || 0) > 0).map(e => ({ ...e }));
  const total = (resolvedTotal != null && resolvedTotal > 0)
    ? resolvedTotal : list.reduce((a, e) => a + (e.value || 0), 0);
  if (!total) return list;
  const scaled = list.map(e => ({ ...e, value: +(e.value / total * 100).toFixed(2) }));
  const sum = scaled.reduce((a, e) => a + e.value, 0);
  if (scaled.length && sum !== 100) {
    scaled[scaled.length - 1].value = +(scaled[scaled.length - 1].value + (100 - sum)).toFixed(2);
  }
  return scaled;
}

async function pfRun() {
  if (!pfState.items.length) { App.toast("Add at least one line.", true); return; }
  const out = document.getElementById("pfResults");
  out.innerHTML = `<div class="empty"><span class="spin"></span> Analysing portfolio\u2026</div>`;
  try {
    const r = await App.api("/portfolio/analysis", { method: "POST", body: JSON.stringify({ items: pfItemsPayload() }) });
    pfRenderResults(r);
  } catch (e) { out.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

function pfRenderResults(r) {
  const out = document.getElementById("pfResults");
  if (!r.effective_holdings || !r.effective_holdings.length) {
    out.innerHTML = `<div class="empty">No holdings resolved${r.errors && r.errors.length ? ` (${r.errors.length} line(s) could not be resolved)` : ""}.</div>`;
    return;
  }
  const allH = r.effective_holdings || [];
  const topPie = allH.map(h => ({ label: h.company, value: h.weight }));
  const c = r.concentration;
  const conc = c.top_holding ? `<div class="kv" style="margin-bottom:12px">
      <dt>Top holding</dt><dd>${App.esc(c.top_holding.company)} ${App.formatPct(c.top_holding.weight)}</dd>
      <dt>Top 1 / 5 / 10</dt><dd>${App.formatPct(c.top1_pct)} / ${App.formatPct(c.top5_pct)} / ${App.formatPct(c.top10_pct)}</dd>
      <dt>Lines</dt><dd>${r.schemes.length} schemes \u00b7 ${r.stocks.length} stocks</dd>
      <dt>Allocated</dt><dd>${App.formatPct(r.total_weight, 1)}</dd>
    </div>` : "";
  const holdingsRows = (r.effective_holdings || []).map(h => `<tr>
      <td>${App.esc(h.company)}</td><td class="mono">${App.esc(h.isin || "\u2014")}</td>
      <td>${App.esc(h.sector || "\u2014")}</td><td>${App.badge(h.asset_class || "other")}</td>
      <td class="num"><strong>${App.formatPct(h.weight)}</strong></td></tr>`).join("");
  const coverageNote = (r.coverage_pct != null && r.coverage_pct < 99.9)
    ? `<div class="page-sub" style="margin-top:6px">Analysis resolves ${App.formatPct(r.coverage_pct, 1)} of the portfolio (${App.formatPct(r.effective_total, 1)} of ${App.formatPct(r.total_weight, 1)} allocated) \u2014 the remainder has no holdings data on record and is excluded from these charts.</div>`
    : "";
  const errRows = (r.errors || []).length ? `<div class="empty" style="margin-top:12px">${r.errors.map(e => `${App.esc(e.type)} "${App.esc(e.name || e.isin || e.id || "")}" \u2014 ${App.esc(e.error)}`).join("; ")}</div>` : "";
  const sectorRows = (r.sector_table || []).slice(0, 15).map(s => `<tr>
      <td>${App.esc(s.sector)}</td><td class="num"><strong>${App.formatPct(s.weight)}</strong></td></tr>`).join("");
  const debt = r.debt_analysis || {};
  const ovRows = (r.overlap || []).filter(x => x && x.scheme);
  const ovCard = ovRows.length >= 2 ? `
    <div class="card" style="margin-top:16px">
      <h3>Mutual fund overlap matrix <span class="badge blue">${ovRows.length} schemes</span></h3>
      <div class="page-sub" style="margin-top:0">% of portfolio in common underlying holdings between each pair of schemes.</div>
      <div class="table-wrap" style="max-height:40vh;overflow:auto">
        <table class="data overlap-matrix">
          <thead><tr><th>Scheme</th>${ovRows.map(m => `<th>${App.esc((m.scheme || "").split(" ").slice(0, 3).join(" "))}</th>`).join("")}</tr></thead>
          <tbody>${ovRows.map(m => `<tr><td>${App.esc(m.scheme)}</td>${ovRows.map(k => `<td class="num">${m.id === k.id ? "\u2014" : App.formatPct(m["c_" + k.id] || 0)}</td>`).join("")}</tr>`).join("")}</tbody>
        </table>
      </div>
    </div>` : "";
  const debtCard = (debt.n_debt_holdings || 0) ? `
    <div class="card" style="margin-top:16px">
      <h3>Debt portfolio analysis <span class="badge blue">${App.formatPct(debt.debt_pct, 1)} debt</span></h3>
      <div class="kv" style="margin-bottom:12px">
        <dt>Yield to maturity</dt><dd>${debt.ytm_pct != null ? App.formatPct(debt.ytm_pct, 2) : "\u2014"}</dd>
        <dt>Avg maturity</dt><dd>${debt.avg_maturity_yrs != null ? App.formatNum(debt.avg_maturity_yrs, 2) + " yrs" : "\u2014"}</dd>
        <dt>Debt holdings</dt><dd>${App.formatNum(debt.n_debt_holdings)}</dd>
        <dt>YTM coverage</dt><dd>${debt.ytm_cover != null ? App.formatPct(debt.ytm_cover, 0) : "\u2014"} <span class="page-sub">(incl. NSE-computed)</span></dd>
      </div>
      <div class="grid" style="grid-template-columns: 1fr 1fr; gap:14px; align-items:start">
        <div>
          <h4 style="margin:0 0 6px">Credit quality</h4>
          <div class="pf-chart-plot"><canvas id="pfDebtCredit" class="chart-canvas"></canvas><div class="pf-tip" id="pfDebtCreditTip" hidden></div></div>
          <div class="pf-legend" id="pfDebtCreditLegend"></div>
        </div>
        <div>
          <h4 style="margin:0 0 6px">Instrument mix</h4>
          <div class="pf-chart-plot"><canvas id="pfDebtInstr" class="chart-canvas"></canvas><div class="pf-tip" id="pfDebtInstrTip" hidden></div></div>
          <div class="pf-legend" id="pfDebtInstrLegend"></div>
        </div>
      </div>
      ${debt.top_debt_holdings && debt.top_debt_holdings.length ? `<div class="table-wrap" style="margin-top:12px;max-height:30vh;overflow:auto">
        <table class="data"><thead><tr><th>Security</th><th>ISIN</th><th>Rating</th><th class="r">YTM</th><th class="r">Weight %</th></tr></thead>
        <tbody>${debt.top_debt_holdings.map(h => `<tr><td>${App.esc(h.company)}</td><td class="mono">${App.esc(h.isin || "\u2014")}</td><td>${App.badge(h.rating || "\u2014")}</td><td class="num">${h.yield != null ? App.formatPct(h.yield, 2) : "\u2014"}${h.ytm_source && (h.ytm_source.indexOf("computed") === 0) ? ` <span class="badge blue" title="${App.esc(h.ytm_source)}">calc</span>` : ""}</td><td class="num">${App.formatPct(h.weight)}</td></tr>`).join("")}</tbody></table>
      </div>` : ""}
    </div>` : "";
  out.innerHTML = `
    <div class="grid" style="grid-template-columns: 1.5fr 1fr; align-items:start;">
      <div class="card">
        <h3>Effective holdings \u2014 concentration <span class="badge blue">${r.n_holdings} securities</span></h3>
        ${conc}
        ${coverageNote}
        <div class="table-wrap" style="max-height:44vh; overflow:auto">
          <table class="data"><thead><tr><th>Holding</th><th>ISIN</th><th>Sector</th><th>Asset</th><th class="r">Weight %</th></tr></thead>
          <tbody>${holdingsRows}</tbody></table>
        </div>
        ${errRows}
      </div>
      <div>
        <div class="card" style="margin-bottom:16px">
          <h3>Top holdings pie</h3>
          <div class="pf-chart-plot"><canvas id="pfPieTop" class="chart-canvas"></canvas><div class="pf-tip" id="pfPieTopTip" hidden></div></div>
          <div class="pf-legend" id="pfPieTopLegend"></div>
        </div>
        <div class="card" style="margin-bottom:16px">
          <h3>Asset allocation (Equity / Debt / Gold / International)</h3>
          <div class="pf-chart-plot"><canvas id="pfPieAsset" class="chart-canvas"></canvas><div class="pf-tip" id="pfPieAssetTip" hidden></div></div>
          <div class="pf-legend" id="pfPieAssetLegend"></div>
        </div>
        <div class="card" style="margin-bottom:16px">
          <h3>Equity cap split (large / mid / small / micro)</h3>
          <div class="pf-chart-plot"><canvas id="pfPieCap" class="chart-canvas"></canvas><div class="pf-tip" id="pfPieCapTip" hidden></div></div>
          <div class="pf-legend" id="pfPieCapLegend"></div>
        </div>
        <div class="card">
          <h3>Sector concentration (equity)</h3>
          <div class="pf-chart-plot"><canvas id="pfPieSector" class="chart-canvas"></canvas><div class="pf-tip" id="pfPieSectorTip" hidden></div></div>
          <div class="pf-legend" id="pfPieSectorLegend"></div>
          <div class="table-wrap" style="margin-top:10px;max-height:30vh;overflow:auto">
            <table class="data"><thead><tr><th>Sector</th><th class="r">Weight %</th></tr></thead>
            <tbody>${sectorRows}</tbody></table>
          </div>
        </div>
      </div>
    </div>
    ${ovCard}
    ${debtCard}
    <div class="card" style="margin-top:16px">
      <h3>Feedback \u2014 what analysis would you like next?</h3>
      <p class="page-sub" style="margin-top:0">Tell us which analysis results you want in this tool (e.g. scenario/what-if, YTD & drawdown returns, tax-adjusted yield, risk metrics, rebalancing suggestions). This helps us build more tools.</p>
      <div style="display:flex; gap:8px; flex-wrap:wrap">
        <input type="text" id="pfFeedback" class="pf-weight" style="flex:1; min-width:260px; width:auto" placeholder="e.g. Show scenario stress, tax impact, YTD returns, rebalancing advice\u2026">
        <button class="btn btn-primary" id="pfFeedbackBtn" type="button">Send feedback</button>
      </div>
    </div>`;
  const effTotal = r.effective_total || 0;
  Charts._renderDonut(document.getElementById("pfPieTop"), pfPieFull100(topPie, effTotal), {
    center: App.formatPct(effTotal, 1), centerLabel: "resolved",
    legendEl: document.getElementById("pfPieTopLegend"), tipEl: document.getElementById("pfPieTopTip"),
    legendLimit: 60,
    fmtVal: v => App.formatPct(v, 1),
  });
  Charts._renderDonut(document.getElementById("pfPieAsset"), pfPieFull100(r.asset_split, effTotal), {
    center: App.formatPct(effTotal, 1), centerLabel: "resolved",
    legendEl: document.getElementById("pfPieAssetLegend"), tipEl: document.getElementById("pfPieAssetTip"),
    fmtVal: v => App.formatPct(v, 1),
  });
  const capEntriesPf = capPieData(r.cap_split_raw || {});
  const capTotalPf = capEntriesPf.reduce((a, e) => a + (e.value || 0), 0);
  const capPieFull = pfPieFull100(capEntriesPf, capTotalPf);
  if (capPieFull.length && document.getElementById("pfPieCap")) {
    Charts._renderDonut(document.getElementById("pfPieCap"), capPieFull, {
      center: App.formatPct(capTotalPf, 1), centerLabel: "tagged equity",
      legendEl: document.getElementById("pfPieCapLegend"), tipEl: document.getElementById("pfPieCapTip"),
      fmtVal: v => App.formatPct(v, 1),
    });
  }
  Charts._renderDonut(document.getElementById("pfPieSector"), pfNormalize100(r.sector_split || []), {
    center: App.formatPct(effTotal, 1), centerLabel: "resolved",
    legendEl: document.getElementById("pfPieSectorLegend"), tipEl: document.getElementById("pfPieSectorTip"),
    fmtVal: v => App.formatPct(v, 1),
  });
  if (debt.credit_split && document.getElementById("pfDebtCredit")) {
    Charts._renderDonut(document.getElementById("pfDebtCredit"), pfNormalize100(debt.credit_split), {
      center: App.formatPct(debt.debt_pct, 0), centerLabel: "of portfolio",
      legendEl: document.getElementById("pfDebtCreditLegend"), tipEl: document.getElementById("pfDebtCreditTip"),
      fmtVal: v => App.formatPct(v, 1),
    });
  }
  if (debt.instrument_split && document.getElementById("pfDebtInstr")) {
    Charts._renderDonut(document.getElementById("pfDebtInstr"), pfNormalize100(debt.instrument_split), {
      center: App.formatPct(debt.debt_pct, 0), centerLabel: "of portfolio",
      legendEl: document.getElementById("pfDebtInstrLegend"), tipEl: document.getElementById("pfDebtInstrTip"),
      fmtVal: v => App.formatPct(v, 1),
    });
  }
  rerenderCharts();
  makeCardsCollapsible(out);
  document.getElementById("pfFeedbackBtn").addEventListener("click", pfSendFeedback);
}

async function pfSendFeedback() {
  const input = document.getElementById("pfFeedback");
  const message = (input.value || "").trim();
  if (!message) { App.toast("Type a feedback message first.", true); return; }
  const context = pfState.items.map(it => `${it.type}:${it.name || it.isin || it.id} (${it.weight || 0}%)`).join(" | ");
  try {
    await App.api("/feedback", { method: "POST", body: JSON.stringify({ message, context }) });
    input.value = "";
    App.toast("Thanks! Feedback recorded.");
  } catch (e) { App.toast("Feedback failed: " + e.message, true); }
}

// ---------- overlap ----------
const overlapState = { selected: new Map() };
function initOverlap() {
  pfMount("pf", pfRun, true);
  const search = document.getElementById("overlapSearch");
  const hint = document.getElementById("overlapSearchHint");
  search.addEventListener("input", App.debounce(async () => {
    const q = search.value.trim();
    if (q.length < 2) { document.getElementById("overlapSuggest").style.display = "none"; return; }
    hint.textContent = "Searching…";
    try {
      const data = await App.api("/schemes?search=" + encodeURIComponent(q) + "&coverage=has_holdings&limit=15");
      hint.textContent = "";
      const box = document.getElementById("overlapSuggest");
      if (!data.items.length) { box.style.display = "block"; box.innerHTML = `<div class="empty">No schemes found.</div>`; return; }
      box.style.display = "block";
      box.innerHTML = data.items.map(s => `<div class="chip" style="cursor:pointer;display:flex;border-radius:0;background:var(--surface);border-bottom:1px solid var(--border);justify-content:space-between;width:100%"
        onclick="pickOverlap(${s.id}, '${App.esc(s.fund_name.replace(/'/g, "\\'"))}')">
        <span>${App.esc(s.fund_name)}</span><span class="badge blue">${App.esc(s.amc)}</span></div>`).join("");
    } catch (e) { hint.textContent = e.message; }
  }, 250));
  document.getElementById("overlapRun").addEventListener("click", runOverlap);
  document.getElementById("overlapClear").addEventListener("click", () => {
    overlapState.selected.clear(); renderOverlapChips();
  });
}

function pickOverlap(id, name) {
  if (!overlapState.selected.has(id) && overlapState.selected.size >= 12) {
    App.toast("Maximum 12 schemes per analysis.", true); return;
  }
  overlapState.selected.set(id, name);
  renderOverlapChips();
  document.getElementById("overlapSuggest").style.display = "none";
  document.getElementById("overlapSearch").value = "";
}

function renderOverlapChips() {
  const chips = document.getElementById("overlapChips");
  chips.innerHTML = [...overlapState.selected.entries()].map(([id, name]) =>
    `<span class="chip">${App.esc(name)} <button onclick="removeOverlap(${id})">✕</button></span>`).join("") ||
    `<span class="page-sub">No schemes selected yet.</span>`;
}

function removeOverlap(id) { overlapState.selected.delete(id); renderOverlapChips(); }

async function runOverlap() {
  let ids = [...overlapState.selected.keys()];
  const body = { scheme_ids: ids };
  // If no schemes were picked manually, fall back to the current builder portfolio.
  if (ids.length < 2 && pfState.items.length >= 2) {
    body.items = pfState.items.map(it => ({ type: it.type, isin: it.isin || null, name: it.name || "" }));
  }
  const out = document.getElementById("overlapResults");
  out.innerHTML = `<div class="empty"><span class="spin"></span> Computing overlap…</div>`;
  try {
    const r = await App.api("/overlap", { method: "POST", body: JSON.stringify(body) });
    renderOverlapResults(r);
  } catch (e) { out.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

function renderOverlapResults(r) {
  const out = document.getElementById("overlapResults");
  const ids = r.ids;
  const headers = `<th>Scheme</th>` + ids.map(id => `<th>${App.esc(r.schemes.find(s => s.id === id).fund_name.length > 28 ? r.schemes.find(s => s.id === id).fund_name.slice(0, 27) + "…" : r.schemes.find(s => s.id === id).fund_name)}</th>`).join("");
  const matrixRows = r.matrix.map(m => {
    const cells = ids.map(id => `<td class="cell ${overlapHeat(m["c_" + id])}">${m["c_" + id].toFixed(1)}%</td>`).join("");
    return `<tr><td style="text-align:left;max-width:260px">${App.esc(m.scheme)}</td>${cells}</tr>`;
  }).join("");

  const concRows = r.concentration.length ? r.concentration.map(c => `<tr>
      <td>${App.esc(c.company)}</td><td class="mono">${App.esc(c.isin)}</td><td>${App.esc(c.sector || "—")}</td>
      <td class="num"><strong>${App.formatPct(c.total_pct)}</strong></td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">No common holdings across selected schemes.</td></tr>`;

  const debtPanel = r.debt_risk.length ? r.debt_risk.map(d => `
      <div class="card" style="margin-bottom:12px">
        <h3>${App.esc(d.scheme)}</h3>
        <div class="kv" style="margin-bottom:10px">
          <dt>Yield to maturity</dt><dd>${App.formatPct(d.ytm != null ? d.ytm * 100 : null, 2)}</dd>
          <dt>Duration</dt><dd>${App.formatNum(d.duration, 2)} yrs</dd>
          <dt>Avg maturity</dt><dd>${App.formatNum(d.avg_maturity, 2)} yrs</dd>
        </div>
        <div class="table-wrap" style="max-height:200px;overflow:auto">
          <table class="data"><thead><tr><th>Holding</th><th>ISIN</th><th class="r">% NAV</th></tr></thead>
          <tbody>${d.top.map(h => `<tr><td>${App.esc(h.company)}</td><td class="mono">${App.esc(h.isin || "—")}</td><td class="num">${App.formatPct(h.percent_nav)}</td></tr>`).join("")}</tbody></table>
        </div>
      </div>`).join("") : `<div class="empty">No debt-category schemes selected for credit-risk analysis.</div>`;

  out.innerHTML = `
    <div class="grid" style="grid-template-columns: 1.6fr 1fr; align-items:start;">
      <div class="card">
        <h3>Portfolio Overlap Matrix <span class="badge blue">% of portfolio in common stocks</span></h3>
        <div class="table-wrap">
          <table class="data overlap-matrix"><thead><tr>${headers}</tr></thead><tbody>${matrixRows}</tbody></table>
        </div>
        <div class="legend">
          <span><i style="background:var(--heat-0)"></i>0%</span>
          <span><i style="background:var(--heat-3)"></i>~50%</span>
          <span><i style="background:var(--heat-5);border-radius:50%"></i>≥80%</span>
        </div>
      </div>
      <div>
        <div class="card" style="margin-bottom:16px">
          <h3>Concentration Summary</h3>
          <div class="table-wrap"><table class="data">
            <thead><tr><th>Holding</th><th>ISIN</th><th>Sector</th><th class="r">Combined weight</th></tr></thead>
            <tbody>${concRows}</tbody></table></div>
        </div>
        <div class="card">
          <h3>Debt Risk Analysis</h3>
          ${debtPanel}
        </div>
      </div>
    </div>`;
  out.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- compare schemes [ANA2] ----------
const COMPARE_COLORS = ["@chart-1", "@chart-5", "@chart-8", "@chart-4", "@chart-6",
                        "@chart-7", "@chart-9", "@chart-10", "@chart-2", "@chart-3",
                        "@success", "@destructive"];
const compareState = { selected: new Map() };

function initCompare() {
  const search = document.getElementById("compareSearch");
  const hint = document.getElementById("compareSearchHint");
  search.addEventListener("input", App.debounce(async () => {
    const q = search.value.trim();
    if (q.length < 2) { document.getElementById("compareSuggest").style.display = "none"; return; }
    hint.textContent = "Searching…";
    try {
      const data = await App.api("/schemes?search=" + encodeURIComponent(q) + "&coverage=has_holdings&limit=15");
      hint.textContent = "";
      const box = document.getElementById("compareSuggest");
      if (!data.items.length) { box.style.display = "block"; box.innerHTML = `<div class="empty">No schemes found.</div>`; return; }
      box.style.display = "block";
      box.innerHTML = data.items.map(s => `<div class="chip" style="cursor:pointer;display:flex;border-radius:0;background:var(--surface);border-bottom:1px solid var(--border);justify-content:space-between;width:100%"
        onclick="pickCompare(${s.id}, '${App.esc(s.fund_name.replace(/'/g, "\\'"))}')">
        <span>${App.esc(s.fund_name)}</span><span class="badge blue">${App.esc(s.amc)}</span></div>`).join("");
    } catch (e) { hint.textContent = e.message; }
  }, 250));
  document.getElementById("compareRun").addEventListener("click", runCompare);
  document.getElementById("compareClear").addEventListener("click", () => {
    compareState.selected.clear(); renderCompareChips();
  });
}

function pickCompare(id, name) {
  if (!compareState.selected.has(id) && compareState.selected.size >= 12) {
    App.toast("Maximum 12 schemes per comparison.", true); return;
  }
  compareState.selected.set(id, name);
  renderCompareChips();
  document.getElementById("compareSuggest").style.display = "none";
  document.getElementById("compareSearch").value = "";
}

function removeCompare(id) { compareState.selected.delete(id); renderCompareChips(); }

function renderCompareChips() {
  const chips = document.getElementById("compareChips");
  chips.innerHTML = [...compareState.selected.entries()].map(([id, name], i) =>
    `<span class="chip" style="border-left:4px solid ${COMPARE_COLORS[i % COMPARE_COLORS.length]}">${App.esc(name)} <button onclick="removeCompare(${id})">✕</button></span>`).join("") ||
    `<span class="page-sub">No schemes selected yet.</span>`;
}

async function runCompare() {
  const ids = [...compareState.selected.keys()];
  const out = document.getElementById("compareResults");
  if (ids.length < 2) { App.toast("Pick at least two schemes to compare.", true); return; }
  out.innerHTML = `<div class="empty"><span class="spin"></span> Computing comparison…</div>`;
  try {
    const r = await App.api("/schemes/compare", { method: "POST", body: JSON.stringify({ scheme_ids: ids }) });
    renderCompareResults(r);
  } catch (e) { out.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

function renderCompareResults(r) {
  const out = document.getElementById("compareResults");
  if (r.error || !r.schemes || r.schemes.length < 2) {
    out.innerHTML = `<div class="empty">${App.esc(r.error || "Not enough comparable schemes.")}</div>`;
    return;
  }
  const S = r.schemes.map((s, i) => ({ ...s, color: COMPARE_COLORS[i % COMPARE_COLORS.length] }));
  const shortName = (n) => n.length > 34 ? n.slice(0, 33) + "…" : n;

  // metric table rows (window-labelled, neutral wording). Each cell carries
  // the exact dates that row's figure spans for THAT scheme (schemes differ).
  const mrow = (label, get, win) => `<tr><td>${label}</td>${S.map(s => {
    const v = get(s);
    const w = win ? win(s) : null;
    const sub = w && w.start && w.end
      ? `<span class="metric-dates">${App.formatDate(w.start)} → ${App.formatDate(w.end)}${w.complete === false ? " · partial" : ""}</span>` : "";
    return `<td class="num">${v != null ? App.formatNum(v, 2) : "—"}${sub}</td>`;
  }).join("")}</tr>`;
  const riskWin = s => s.metrics.risk ? { start: s.metrics.risk.window_start, end: s.metrics.risk.window_end } : null;
  const rollWin = s => s.metrics.rolling_1y ? { start: s.metrics.rolling_1y.first_window_start, end: s.metrics.rolling_1y.last_window_end } : null;
  const benchWin = s => s.metrics.benchmark ? { start: s.metrics.benchmark.window_start, end: s.metrics.benchmark.window_end } : null;
  const metricTable = `
    <table class="data">
      <thead><tr><th>Metric</th>${S.map(s => `<th title="${App.esc(s.fund_name)}">${App.esc(shortName(s.fund_name))}</th>`).join("")}</tr></thead>
      <tbody>
        ${mrow("CAGR since inception", s => s.metrics.cagr_pct.since_inception, s => s.metrics.cagr_pct.since_inception_window)}
        ${mrow("1Y CAGR %", s => s.metrics.cagr_pct.y1, s => s.metrics.cagr_pct.y1_window)}
        ${mrow("3Y CAGR %", s => s.metrics.cagr_pct.y3, s => s.metrics.cagr_pct.y3_window)}
        ${mrow("5Y CAGR %", s => s.metrics.cagr_pct.y5, s => s.metrics.cagr_pct.y5_window)}
        ${mrow("Volatility ann. % (3y)", s => s.metrics.risk && s.metrics.risk.volatility_pct, riskWin)}
        ${mrow("Sharpe (rf " + App.formatNum(r.rf_pct_assumption, 1) + "%)", s => s.metrics.risk && s.metrics.risk.sharpe, riskWin)}
        ${mrow("Sortino", s => s.metrics.risk && s.metrics.risk.sortino, riskWin)}
        ${mrow("Max drawdown % (3y)", s => s.metrics.risk && s.metrics.risk.max_drawdown_pct, riskWin)}
        ${mrow("Rolling 1Y % positive", s => s.metrics.rolling_1y && s.metrics.rolling_1y.pct_positive, rollWin)}
        ${mrow("Beta vs benchmark", s => s.metrics.benchmark && s.metrics.benchmark.beta, benchWin)}
        ${mrow("Alpha vs benchmark", s => s.metrics.benchmark && s.metrics.benchmark.alpha_pct, benchWin)}
        ${mrow("Tracking error %", s => s.metrics.benchmark && s.metrics.benchmark.tracking_error_pct, benchWin)}
      </tbody>
    </table>`;
  const benchNote = S.filter(s => s.benchmark_index).length
    ? `<div class="page-sub" style="margin-top:6px">Benchmarks: ${S.filter(s => s.benchmark_index).map(s => `${App.esc(shortName(s.fund_name))} → ${App.esc(s.benchmark_index)}`).join(" · ")}</div>`
    : "";

  let chartsHtml = "";
  const growthSeries = S.filter(s => s.growth_100).map(s => ({
    name: shortName(s.fund_name), color: s.color,
    dates: s.growth_100.dates, values: s.growth_100.values }));
  if (growthSeries.length >= 1) {
    chartsHtml += `<div class="card" style="margin-bottom:18px">
      <h3>Growth of ₹100</h3>
      <div class="page-sub">Common window ${App.formatDate(r.window.start)} → ${App.formatDate(r.window.end)}${r.window.common ? "" : " · ⚠ overlapping history too short for a fair common start — series shown from each scheme's own first shared NAV"}</div>
      <div id="compareGrowthChart" style="margin-top:10px"></div>
    </div>`;
  }
  const rollSeries = S.filter(s => s.rolling_1y && s.rolling_1y.dates.length > 30)
    .map(s => ({ name: shortName(s.fund_name), color: s.color,
                 dates: s.rolling_1y.dates, values: s.rolling_1y.values }));
  if (rollSeries.length >= 1) {
    chartsHtml += `<div class="card" style="margin-bottom:18px">
      <h3>Rolling 1-year returns (%)</h3>
      <div class="page-sub">Daily-history windows stepped weekly · per scheme's own full record</div>
      <div id="compareRollingChart" style="margin-top:10px"></div>
    </div>`;
  }

  out.innerHTML = `
    <div class="card" style="margin-bottom:18px">
      <h3>Metric table <span class="page-sub">as of ${App.formatDate(r.as_of)} · ${App.formatNum(S[0].metrics.points)}+ NAV points per scheme</span></h3>
      <div class="table-wrap" style="max-height:none">
        ${metricTable}
      </div>
      ${benchNote}
      <div class="page-sub" style="margin-top:8px"><b>${App.esc(r.disclaimer)}</b> Factual figures computed from AMFI NAV history — not a recommendation; percentile-neutral by design.</div>
    </div>
    ${chartsHtml}`;
  if (growthSeries.length >= 1) {
    Charts.mountMultiLine(document.getElementById("compareGrowthChart"), growthSeries,
                          { formatValue: v => "₹" + App.formatNum(v, 0) });
  }
  if (rollSeries.length >= 1) {
    Charts.mountMultiLine(document.getElementById("compareRollingChart"), rollSeries,
                          { formatValue: v => App.formatNum(v, 1) + "%" });
  }
}

// ---------- proposal (uses the shared portfolio builder) ----------
function initProposal() {
  pfMount("pr", pfRunProposal, true);
  document.getElementById("prLoadCpBtn").addEventListener("click", loadClientPortfolioIntoBuilder);
  // populate the "load client portfolio" dropdown
  apiT("/client-portfolios").then(d => {
    const sel = document.getElementById("prLoadCp");
    const opts = (d.items || []).map(p => `<option value="${p.id}">${App.esc(p.name)} (${App.esc(p.client_name || "")})</option>`).join("");
    if (sel) sel.innerHTML = `<option value="">— none —</option>${opts}`;
  }).catch(() => {});
}

async function pfRunProposal() {
  if (!pfState.items.length) { App.toast("Add at least one line.", true); return; }
  const out = document.getElementById("proposalOut");
  out.innerHTML = `<div class="empty"><span class="spin"></span> Generating proposal\u2026</div>`;
  try {
    const r = await App.api("/proposal", { method: "POST", body: JSON.stringify({ items: pfItemsPayload() }) });
    window._proposalData = { items: pfItemsPayload() };
    window._lastProposal = r.markdown;
    renderProposalEditor(out, r);
  } catch (e) { out.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

function renderProposalEditor(out, r) {
  const rows = (r.lines || []).map(ln => `<tr>
      <td>${ln.index + 1}</td>
      <td style="max-width:220px">${App.esc(ln.name)}${ln.resolved ? "" : ` <span class="badge red">unresolved</span>`}</td>
      <td><span class="badge ${ln.type === "scheme" ? "blue" : "green"}">${ln.type === "scheme" ? "Scheme" : "Stock"}</span></td>
      <td>${App.esc(ln.category)}</td>
      <td class="num">${App.formatPct(ln.weight, 1)}</td>
      <td><input class="prop-action" data-idx="${ln.index}" value="${App.esc(ln.type === "scheme" ? "Retain" : "Hold")}" placeholder="e.g. Switch to Direct, Trim 50%\u2026"></td>
      <td><textarea class="prop-remark" data-idx="${ln.index}" rows="2" placeholder="Long remarks / rationale\u2026"></textarea></td>
    </tr>`).join("");
  out.innerHTML = `
    <div class="card">
      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap">
        <div>
          <h3 style="margin:0">Proposed realignment</h3>
          <div class="page-sub">Edit the action & remarks per line, then regenerate the white-label document.</div>
        </div>
        <div style="display:flex; gap:8px">
          <button class="btn btn-outline btn-sm" onclick="copyProposal()">\U0001F4CB Copy markdown</button>
          <button class="btn btn-primary" id="propRegenerate">Regenerate proposal</button>
        </div>
      </div>
      <div class="table-wrap" style="max-height:45vh; overflow:auto; margin-top:12px">
        <table class="data">
          <thead><tr><th>#</th><th>Instrument</th><th>Type</th><th>Category</th><th class="r">Alloc %</th><th>Proposed action</th><th>Remarks</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div id="proposalPreview" style="margin-top:16px"></div>
    </div>`;
  document.getElementById("propRegenerate").addEventListener("click", regenerateProposal);
  document.getElementById("proposalPreview").innerHTML = renderPreview(r.markdown);
}

async function regenerateProposal() {
  if (!window._proposalData) return;
  const replacements = {}, remarks = {};
  document.querySelectorAll(".prop-action").forEach(inp => { replacements[inp.dataset.idx] = inp.value.trim() || "Retain"; });
  document.querySelectorAll(".prop-remark").forEach(ta => { if (ta.value.trim()) remarks[ta.dataset.idx] = ta.value.trim(); });
  document.getElementById("proposalPreview").innerHTML = `<div class="empty"><span class="spin"></span> Regenerating…</div>`;
  try {
    const r = await App.api("/proposal", { method: "POST", body: JSON.stringify({ items: pfItemsPayload(), replacements, remarks }) });
    document.getElementById("proposalPreview").innerHTML = renderPreview(r.markdown);
    window._lastProposal = r.markdown;
    App.toast("Proposal updated.");
  } catch (e) { document.getElementById("proposalPreview").innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

function renderPreview(md) {
  const wrap = document.createElement("div");
  wrap.innerHTML = App.md(md);
  wrap.querySelectorAll("table").forEach(t => t.classList.add("data"));
  return wrap.innerHTML;
}

async function copyProposal() {
  const md = window._lastProposal || "";
  try { await navigator.clipboard.writeText(md); App.toast("Proposal markdown copied to clipboard."); }
  catch (e) { App.toast("Copy failed \u2014 select and copy manually.", true); }
}

// ---------- model portfolios / strategies / compliance ----------
let mv = null;          // current sub-view key
let modelEditId = null; // model being edited in the builder (null = new)

function initModels() {
  document.querySelectorAll("#modelsTabs .nav-toggle").forEach(btn => {
    btn.addEventListener("click", () => {
      const v = btn.dataset.mview;
      document.querySelectorAll("#modelsTabs .nav-toggle").forEach(b => b.classList.toggle("active", b === btn));
      ["overview", "strategies", "clients", "clientportfolios"].forEach(k => {
        document.getElementById("mview-" + k).style.display = (k === v) ? "" : "none";
      });
      mv = v;
      renderMView(v);
    });
  });
  mv = "overview";
  renderMView("overview");
}

function renderMView(v) {
  const el = document.getElementById("mview-" + v);
  if (!el) return;
  if (v === "overview") mvOverview(el);
  else if (v === "strategies") mvStrategies(el);
  else if (v === "clients") mvClients(el);
  else if (v === "clientportfolios") mvClientPortfolios(el);
}

function mvOverview(el) {
  el.innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`;
  apiT("/overview").then(d => {
    const rows = (d.items || []).map(p => `<tr>
      <td style="max-width:240px">${App.esc(p.name)}</td>
      <td>${App.esc(p.client_name)}</td>
      <td>${p.kind === "actual" ? '<span class="badge amber">Actual</span>' : '<span class="badge blue">Model</span>'}</td>
      <td>${App.esc(p.strategy_name)}</td>
      <td class="num">${p.compliance != null
        ? `<span class="badge ${p.compliance >= 100 ? "green" : p.compliance >= 50 ? "amber" : "red"}">${App.formatPct(p.compliance, 1)}</span>`
        : (p.error ? '<span class="badge red">error</span>' : '<span class="badge grey">no strategy</span>')}</td>
      <td class="num">${p.passed != null ? `${p.passed}/${p.total}` : "\u2014"}</td>
      <td>${p.compliance != null ? `<button class="btn btn-outline btn-sm" onclick="mvAnalyzeCp(${p.id})">Analyse</button>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="7" class="empty">No client portfolios yet. Create a strategy, model and client, then deploy — or load the sample data.</td></tr>`;
    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px">
        <h3 style="margin:0">Client portfolio compliance overview</h3>
        <button class="btn btn-outline btn-sm" id="mvSeed">Load sample data</button>
      </div>
      <div class="table-wrap" style="max-height:72vh; overflow:auto">
        <table class="data">
          <thead><tr><th>Portfolio</th><th>Client</th><th>Kind</th><th>Strategy</th><th class="r">Compliance</th><th class="r">Passed</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
    document.getElementById("mvSeed").addEventListener("click", async () => {
      await apiT("/seed-samples", { method: "POST", body: JSON.stringify({}) });
      App.toast("Sample data loaded.");
      mvOverview(el);
    });
  }).catch(e => el.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`);
}

async function apiT(path, opts = {}) {
  return await App.api(path, opts);
}

// ---------------- Strategies ----------------
async function mvStrategies(el) {
  el.innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`;
  try {
    const d = await apiT("/strategies");
    const list = (d.items || []).map(s => `
      <div class="card" style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <strong>${App.esc(s.name)}</strong>
          <span style="display:flex;gap:6px">
            <button class="btn btn-outline btn-sm" onclick="mvStrategyEdit(${s.id})">Edit</button>
            <button class="btn btn-outline btn-sm" onclick="mvStrategyDelete(${s.id})">Delete</button>
          </span>
        </div>
        ${s.description ? `<div class="page-sub">${App.esc(s.description)}</div>` : ""}
        <div class="mono" style="font-size:12px;color:var(--text-3);white-space:pre-wrap;margin-top:6px">${App.esc(s.rules_text || "")}</div>
      </div>`).join("") || `<div class="empty">No strategies yet.</div>`;
    el.innerHTML = `
      <div class="card" style="margin-bottom:14px">
        <h3>New / edit mandate (IPS)</h3>
        <div class="toolbar" style="margin-bottom:6px">
          <div class="field" style="flex:1"><input id="mvStratName" placeholder="Mandate name — e.g. Conservative Hybrid IPS"></div>
        </div>
        <div class="page-sub" style="margin:2px 0 4px">Investment-policy rules, one per line:</div>
        <textarea id="mvStratText" rows=4 style="width:100%;padding:8px;border:1px solid var(--border);border-radius:var(--radius-sm);font-family:inherit"
          placeholder="e.g. Max 10% single stock. Max 20% sector. Min 30% debt. Max 5% cash. Max top-5 25%. Max overlap 30%."></textarea>
        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn btn-outline btn-sm" id="mvStratParse">Parse rules</button>
          <button class="btn btn-primary" id="mvStratSave">Save strategy</button>
        </div>
        <div id="mvStratRules" class="pf-legend" style="margin-top:10px"></div>
      </div>
      <div class="grid two"><div class="card"><h3>Strategies</h3>${list}</div></div>`;
    document.getElementById("mvStratParse").addEventListener("click", mvStratParse);
    document.getElementById("mvStratSave").addEventListener("click", () => mvStratSave());
    document.getElementById("mvStratText").addEventListener("input", App.debounce(() => {
      if (document.getElementById("mvStratText").value.trim()) mvStratParse();
      else document.getElementById("mvStratRules").innerHTML = "";
    }, 450));
  } catch (e) { el.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

function mvStratRenderRules(rules, remarks) {
  window._mvStratRemarks = remarks || (rules || []).map(() => "");
  const box = document.getElementById("mvStratRules");
  const rows = (rules || []).map((x, i) => `
    <div class="rule-edit-row">
      <span class="chip">${App.esc(x.field)} ${App.esc(x.operator)} ${App.esc(x.value)}${App.esc(x.unit)}</span>
      <input class="pf-weight" style="flex:1;min-width:200px" data-ri="${i}"
        placeholder="Remark / direction for computing this rule\u2026"
        value="${App.esc(window._mvStratRemarks[i] || "")}">
    </div>`).join("");
  const notes = (window._mvStratNotes || []).length ? `<div class="page-sub">Notes (not evaluated): ${window._mvStratNotes.map(App.esc).join("; ")}</div>` : "";
  box.innerHTML = `<div class="page-sub">Parsed ${(rules || []).length} rule(s) \u2014 add a remark / direction for each:</div>${rows || '<div class="empty">No rules recognised.</div>'}${notes}`;
  box.querySelectorAll("input[data-ri]").forEach(inp => inp.addEventListener("input", () => {
    window._mvStratRemarks[Number(inp.dataset.ri)] = inp.value;
  }));
}

function mvStratParse() {
  const text = document.getElementById("mvStratText").value;
  const box = document.getElementById("mvStratRules");
  box.innerHTML = `<div class="page-sub">Parsing…</div>`;
  apiT("/strategies/parse", { method: "POST", body: JSON.stringify({ text }) }).then(r => {
    window._mvStratNotes = r.unparsed || [];
    mvStratRenderRules(r.rules || []);
  }).catch(e => box.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`);
}

async function mvStratSave() {
  const name = document.getElementById("mvStratName").value.trim();
  const rules_text = document.getElementById("mvStratText").value;
  const remarks = window._mvStratRemarks || [];
  if (!name) { App.toast("Enter a strategy name.", true); return; }
  if (modelEditId) {
    await apiT(`/strategies/${modelEditId}`, { method: "PUT", body: JSON.stringify({ name, rules_text, remarks }) });
    modelEditId = null;
  } else {
    await apiT("/strategies", { method: "POST", body: JSON.stringify({ name, rules_text, remarks }) });
  }
  App.toast("Strategy saved.");
  mvStrategies(document.getElementById("mview-strategies"));
}

async function mvStrategyEdit(id) {
  const s = await apiT(`/strategies/${id}`);
  modelEditId = id;
  await mvStrategies(document.getElementById("mview-strategies"));
  document.getElementById("mvStratName").value = s.name;
  document.getElementById("mvStratText").value = s.rules_text;
  mvStratRenderRules(s.rules || [], (s.rules || []).map(x => x.remark || ""));
}

async function mvStrategyDelete(id) {
  if (!confirm("Delete this strategy?")) return;
  await apiT(`/strategies/${id}`, { method: "DELETE" });
  modelEditId = null;
  mvStrategies(document.getElementById("mview-strategies"));
}

// ---------------- Models ----------------
function pfBuilderHtml(prefix, runLabel) {
  return `
    <div class="toolbar" style="margin-bottom:8px; align-items:flex-start">
      <div class="field">
        <label class="page-sub" for="${prefix}Preset" style="display:block;margin-bottom:2px">Preset tracker</label>
        <select id="${prefix}Preset">
          <option value="cas">CAS Sample</option>
          <option value="balanced">Balanced Growth</option>
          <option value="aggressive">Equity Aggressive</option>
          <option value="conservative">Conservative</option>
          <option value="">Custom</option>
        </select>
      </div>
      <div class="field" style="flex:1; min-width:200px">
        <input type="text" id="${prefix}Search" placeholder="Search schemes to add\u2026">
        <div class="hint" id="${prefix}SchemeHint"></div>
        <div id="${prefix}SchemeSuggest" style="display:none;border:1px solid var(--border);border-radius:var(--radius-sm);max-height:180px;overflow:auto"></div>
      </div>
      <div class="field" style="flex:1; min-width:200px">
        <input type="text" id="${prefix}StockSearch" placeholder="Search stocks (name / ISIN)\u2026">
        <div class="hint" id="${prefix}StockHint"></div>
        <div id="${prefix}StockSuggest" style="display:none;border:1px solid var(--border);border-radius:var(--radius-sm);max-height:180px;overflow:auto"></div>
      </div>
      <div class="field">
        <div style="display:flex; gap:8px">
          <label class="btn btn-outline btn-sm" style="cursor:pointer;margin:0">Upload JSON<input type="file" id="${prefix}Upload" accept=".json,application/json" style="display:none"></label>
          <button class="btn btn-outline btn-sm" id="${prefix}Sample" type="button">Sample</button>
        </div>
      </div>
    </div>
    <div class="table-wrap" style="max-height:34vh; overflow:auto">
      <table class="data" id="${prefix}Table">
        <thead><tr><th>#</th><th>Type</th><th>Name / ISIN</th><th class="r">Allocation %</th><th></th></tr></thead>
        <tbody id="${prefix}Tbody"></tbody>
      </table>
    </div>
    <div style="margin-top:10px; display:flex; align-items:center; gap:12px; flex-wrap:wrap">
      <span class="pill-count" id="${prefix}Count"></span>
      <span class="page-sub" id="${prefix}Total"></span>
      <div class="spacer"></div>
      <button class="btn btn-primary" id="${prefix}Run">${runLabel}</button>
      <button class="btn btn-outline" id="${prefix}Clear">Clear</button>
    </div>`;
}

async function mvClientDelete(id) {
  if (!confirm("Delete this client and its portfolios?")) return;
  await apiT(`/clients/${id}`, { method: "DELETE" });
  mvClients(document.getElementById("mview-clients"));
}

async function mvClients(el) {
  el.innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`;
  try {
    const d = await apiT("/clients");
    const list = (d.items || []).map(c => {
      const docs = (c.documents || []).map(x => `<span class="chip-mini">${App.esc(x.name)} · <span class="${x.status === "parsed" ? "" : "page-sub"}">${App.esc(x.status)}</span></span>`).join(" ");
      return `
      <div class="card" style="margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div>
            <strong>${App.esc(c.name)}</strong>
            ${c.org ? ` <span class="page-sub">${App.esc(c.org)}</span>` : ""}
            ${c.notes ? `<div class="page-sub">${App.esc(c.notes)}</div>` : ""}
            ${docs ? `<div style="margin-top:4px">${docs}</div>` : ""}
          </div>
          <span style="display:flex;gap:6px">
            <button class="btn btn-outline btn-sm" onclick="mvClientUpload(${c.id})">Upload document</button>
            <button class="btn btn-outline btn-sm" onclick="mvClientEdit(${c.id})">Edit</button>
            <button class="btn btn-outline btn-sm" onclick="mvClientDelete(${c.id})">Delete</button>
          </span>
        </div>
      </div>`;
    }).join("") || `<div class="empty">No clients yet.</div>`;
    el.innerHTML = `
      <div class="card" style="margin-bottom:14px">
        <h3 id="mvClientFormTitle">Onboard a client</h3>
        <div class="toolbar">
          <div class="field" style="flex:1"><label class="page-sub">Client name</label><input id="mvClientName" placeholder="e.g. Rajesh Sharma"></div>
          <div class="field" style="flex:1"><label class="page-sub">Family / Group</label><input id="mvClientOrg" placeholder="e.g. Sharma HUF"></div>
          <div class="field" style="flex:2"><label class="page-sub">Mandate notes</label><input id="mvClientNotes" placeholder="Objectives, risk profile, horizon…"></div>
          <button class="btn btn-primary" id="mvClientSave" style="align-self:flex-end">Save client</button>
        </div>
        <div class="page-sub" style="margin-top:6px">After saving, use <b>Upload document</b> on the client card to attach a CAS document (JSON or PDF) \u2014 it is parsed into the client's portfolio at current market value. Other document types will be covered later.</div>
      </div>
      <div class="card"><h3>Clients</h3>${list}</div>`;
    window._mvClientEditId = null;
    document.getElementById("mvClientSave").addEventListener("click", mvClientSave);
  } catch (e) { el.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

function mvClientUpload(clientId) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,.pdf";
  input.onchange = async () => {
    const f = input.files && input.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    App.toast("Uploading document\u2026");
    try {
      const r = await apiT(`/clients/${clientId}/documents`, { method: "POST", body: fd, raw: true });
      App.toast(`Document ${r.document.status}${r.parsed ? ` (${r.parsed} holdings)` : ""}.`);
      mvClients(document.getElementById("mview-clients"));
    } catch (e) { App.toast("Upload failed: " + e.message, true); }
  };
  input.click();
}

async function mvClientSave() {
  const name = document.getElementById("mvClientName").value.trim();
  const org = document.getElementById("mvClientOrg").value.trim();
  const notes = document.getElementById("mvClientNotes").value.trim();
  if (!name) { App.toast("Enter a client name.", true); return; }
  const id = window._mvClientEditId;
  if (id) {
    await apiT(`/clients/${id}`, { method: "PUT", body: JSON.stringify({ name, org, notes }) });
    window._mvClientEditId = null;
  } else {
    await apiT("/clients", { method: "POST", body: JSON.stringify({ name, org, notes }) });
  }
  App.toast("Client saved.");
  mvClients(document.getElementById("mview-clients"));
}

async function mvClientEdit(id) {
  const d = await apiT("/clients");
  const c = (d.items || []).find(x => x.id === id);
  if (!c) return;
  await mvClients(document.getElementById("mview-clients"));
  window._mvClientEditId = id;
  document.getElementById("mvClientFormTitle").textContent = "Edit client";
  document.getElementById("mvClientName").value = c.name || "";
  document.getElementById("mvClientOrg").value = c.org || "";
  document.getElementById("mvClientNotes").value = c.notes || "";
}

// ---------------- Client portfolios ----------------
async function mvClientPortfolios(el) {
  el.innerHTML = `<div class="empty"><span class="spin"></span> Loading…</div>`;
  try {
    const [s, c, cp] = await Promise.all([apiT("/strategies"), apiT("/clients"), apiT("/client-portfolios")]);
    const stratOptions = (s.items || []).map(x => `<option value="${x.id}">${App.esc(x.name)}</option>`).join("");
    const rows = (cp.items || []).map(p => `
      <div class="card" style="margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div>
            <strong>${App.esc(p.name)}</strong>
            <span class="badge ${p.kind === "actual" ? "amber" : "blue"}">${p.kind === "actual" ? "Actual" : "Portfolio"}</span>
            <span class="page-sub">client: ${App.esc(p.client_name)} \u00b7 strategy: ${App.esc(p.strategy_name || "—")} \u00b7 ${(p.items || []).length} lines</span>
          </div>
          <span style="display:flex;gap:6px">
            <button class="btn btn-outline btn-sm" onclick="mvAnalyzeCp(${p.id})">Analyse</button>
            <button class="btn btn-outline btn-sm" onclick="mvCpDelete(${p.id})">Delete</button>
          </span>
        </div>
      </div>`).join("") || `<div class="empty">No client portfolios yet.</div>`;
    el.innerHTML = `
      <div class="card" style="margin-bottom:14px">
        <h3>Deploy a mandate to a client</h3>
        <div class="toolbar">
          <div class="field" style="flex:1"><label class="page-sub">Client (new or existing)</label><input id="mvDepClientName" placeholder="e.g. Rajesh Sharma"></div>
          <div class="field" style="flex:1"><label class="page-sub">IPS / compliance strategy</label><select id="mvDepStrategy">${stratOptions || '<option value="">No strategies</option>'}</select></div>
          <button class="btn btn-primary" id="mvDepRun" style="align-self:flex-end">Create client portfolio</button>
        </div>
        <div class="page-sub" style="margin-top:6px">Creates the client (if new) and links their portfolio to the selected strategy. Attach actual holdings via the Clients tab (CAS upload) or Portfolio Tools.</div>
      </div>
      <div class="card"><h3>Client portfolios</h3>${rows}</div>
      <div id="mvCpAna"></div>`;
    document.getElementById("mvDepRun").addEventListener("click", async () => {
      const name = document.getElementById("mvDepClientName").value.trim();
      const strategy_id = document.getElementById("mvDepStrategy").value;
      if (!name) { App.toast("Enter a client name.", true); return; }
      if (!strategy_id) { App.toast("Select a strategy.", true); return; }
      // find or create the client
      let client_id = (c.items || []).find(x => x.name.toLowerCase() === name.toLowerCase())?.id;
      if (!client_id) {
        const cl = await apiT("/clients", { method: "POST", body: JSON.stringify({ name, org: "", notes: "" }) });
        client_id = cl.id;
      }
      await apiT("/client-portfolios", { method: "POST", body: JSON.stringify({ client_id, strategy_id: Number(strategy_id), kind: "actual", name: `${name} portfolio` }) });
      App.toast("Client portfolio created.");
      mvClientPortfolios(el);
    });
  } catch (e) { el.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

async function mvCpDelete(id) {
  if (!confirm("Delete this client portfolio?")) return;
  await apiT(`/client-portfolios/${id}`, { method: "DELETE" });
  mvClientPortfolios(document.getElementById("mview-clientportfolios"));
}

// ---------------- Analysis ----------------
async function mvAnalyzeCp(id) {
  // Render the compliance analysis inline in the CURRENTLY-VISIBLE view.
  // Always (re)create the container inside the visible pane — reusing one
  // left behind in a hidden pane made results appear "missing" on Overview.
  const panes = ["overview", "strategies", "clients", "clientportfolios"];
  const visible = panes.find(k => {
    const p = document.getElementById("mview-" + k);
    return p && p.style.display !== "none";
  }) || "overview";
  const host = document.getElementById("mview-" + visible) || document.body;
  document.querySelectorAll("#mvCpAna").forEach(old => old.remove());
  const container = document.createElement("div");
  container.id = "mvCpAna";
  host.appendChild(container);
  container.innerHTML = `<div class="empty"><span class="spin"></span> Analysing\u2026</div>`;
  container.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const r = await apiT("/analyze", { method: "POST", body: JSON.stringify({ portfolio_id: Number(id), portfolio_kind: "client" }) });
    renderCompliance(container, r);
    renderPortfolioAnalyticsBlock(container, id);
  } catch (e) { container.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
}

// [ANA3] portfolio-level performance & risk, shown under the compliance table
async function renderPortfolioAnalyticsBlock(container, portfolioId) {
  const host = document.createElement("div");
  host.id = "mvCpAnalytics";
  host.className = "card";
  host.style.marginTop = "18px";
  container.appendChild(host);
  host.innerHTML = `<h3>Performance &amp; risk</h3>
    <div class="page-sub">Weight-blended NAV reconstruction of this portfolio over its schemes' common window.</div>
    <div class="pa-body" style="margin-top:8px"><span class="spin"></span> Loading…</div>`;
  try {
    const a = await apiT("/portfolio-analytics", { method: "POST", body: JSON.stringify({ portfolio_id: Number(portfolioId), portfolio_kind: "client" }) });
    const body = host.querySelector(".pa-body");
    if (a.error) { body.innerHTML = `<div class="empty">${App.esc(a.error)}</div>`; return; }
    const c = a.cagr_pct || {}, rk = a.risk || {}, roll = a.rolling_1y;
    const cell = (label, v, sub) => `<div style="flex:1;min-width:96px;text-align:center;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px">
      <div style="font-size:10.5px;color:var(--text-2);text-transform:uppercase;letter-spacing:.04em">${label}</div>
      <div style="font-size:17px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px">${v != null ? App.formatNum(v, 2) + "%" : "—"}</div>
      ${sub ? `<div class="metric-dates">${sub}</div>` : ""}</div>`;
    const partial = w => w && w.complete === false ? " · partial" : "";
    let html = `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
      ${cell("CAGR incep.", c.since_inception, fmtWinRange(c.since_inception_window) + partial(c.since_inception_window))}
      ${cell("1Y", c.y1, fmtWinRange(c.y1_window) + partial(c.y1_window))}
      ${cell("3Y", c.y3, fmtWinRange(c.y3_window) + partial(c.y3_window))}
      ${cell("5Y", c.y5, fmtWinRange(c.y5_window) + partial(c.y5_window))}
      ${cell("Volatility (3y)", rk.volatility_pct, fmtWinRange(rk))}
    </div><div class="kv">
      <dt>Window (risk)</dt><dd>${fmtWinRange(rk)}${rk.n_points != null ? ` <span class="page-sub">· ${App.formatNum(rk.n_points)} NAV points</span>` : ""}</dd>
      <dt>Sharpe / Sortino</dt><dd>${rk.sharpe != null ? App.formatNum(rk.sharpe, 2) : "—"} / ${rk.sortino != null ? App.formatNum(rk.sortino, 2) : "—"}</dd>
      <dt>Max drawdown (3y)</dt><dd>${rk.max_drawdown_pct != null ? App.formatNum(rk.max_drawdown_pct, 2) + "%" : "—"}</dd>
      ${roll ? `<dt>Rolling 1Y positive</dt><dd>${App.formatNum(roll.pct_positive, 1)}% <span class="page-sub">of ${App.formatNum(roll.n_periods)} windows · median ${App.formatNum(roll.median_pct, 2)}% · windows ${fmtWinRange({ start: roll.first_window_start, end: roll.last_window_end })}</span></dd>` : ""}
      <dt>Constituents</dt><dd>${(a.constituents || []).map(x => `${App.esc(x.fund_name)} (${App.formatNum(x.weight, 1)}%, ${App.esc(x.plan_used)})`).join(" · ")}</dd>
    </div><div id="paGrowthChart" style="margin-top:10px"></div>
    <div class="page-sub" style="margin-top:6px"><b>${App.esc(a.disclaimer)}</b> Diagnostic of your own portfolio — factual AMFI-NAV math, not adviser track record.</div>`;
    body.innerHTML = html;
    if (a.growth_100 && a.growth_100.dates.length > 10) {
      Charts.mountMultiLine(document.getElementById("paGrowthChart"),
        [{ name: a.label || "Portfolio", color: "@primary",
           dates: a.growth_100.dates, values: a.growth_100.values }],
        { formatValue: v => "₹" + App.formatNum(v, 0) });
    }
    renderPortfolioMovementBlock(container, a);
  } catch (e) {
    host.querySelector(".pa-body").innerHTML = `<div class="empty">${App.esc(e.message)}</div>`;
  }
}

// [ANA3 movement] cash-flow-aware portfolio value path below Performance & risk.
function renderPortfolioMovementBlock(container, a) {
  const mv = a.movement;
  const host = document.createElement("div");
  host.id = "mvCpMovement";
  host.className = "card";
  host.style.marginTop = "14px";
  container.appendChild(host);
  if (!mv) {
    host.innerHTML = `<h3>Portfolio movement</h3>
      <div class="page-sub">No transaction history — movement analytics needs the actual purchases/redemptions from a CAS/actual-holdings statement. The weight-blended reconstruction above is the weighted index, not cash flows.</div>`;
    return;
  }
  if (mv.error) {
    host.innerHTML = `<h3>Portfolio movement</h3><div class="empty">${App.esc(mv.error)}</div>`;
    return;
  }
  const annNote = mv.annualized_unavailable
    ? ` · span ${App.formatNum(mv.days)}d &lt; ${App.formatNum(mv.annualized_unavailable.required_span_days)}d floor — not annualised`
    : "";
  const cell = (label, v, sub) => `<div style="flex:1;min-width:130px;text-align:center;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px">
      <div style="font-size:10.5px;color:var(--text-2);text-transform:uppercase;letter-spacing:.04em">${label}</div>
      <div style="font-size:17px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px">${v != null ? v : "—"}</div>
      ${sub ? `<div class="metric-dates">${sub}</div>` : ""}</div>`;
  const ddSub = mv.drawdown_unavailable
    ? `unavailable: ${App.esc(mv.drawdown_unavailable.reason || "")} · ${App.formatNum(mv.drawdown_unavailable.artifact_days)} inconsistent statement days`
    : "on flow-adjusted path";
  host.innerHTML = `<h3>Portfolio movement <span class="page-sub">actual cash flows · ${App.esc(a.movement_source || "")}</span></h3>
    <div class="page-sub">Reconstructed ${App.formatDate(mv.start)} → ${App.formatDate(mv.end)} · ${App.formatNum(mv.days)} days · marked daily at scheme NAVs, flow-adjusted daily returns.</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">
      ${cell("Opening value", App.formatINR(mv.opening_value, 2), App.formatDate(mv.start) + " · deduced at statement start")}
      ${cell("Terminal value", App.formatINR(mv.terminal_value, 2), App.formatDate(mv.end))}
      ${cell("Total invested", "₹" + App.formatNum(Math.abs(mv.total_net_flow), 2), "net purchases minus redemptions")}
      ${cell("TWR till date", mv.total_twr_pct != null ? App.formatNum(mv.total_twr_pct, 2) + "%" : "—", "unannualised · flow-adjusted")}
      ${cell("TWR ann. till date", mv.annualized_twr_pct != null ? App.formatNum(mv.annualized_twr_pct, 2) + "%" : "—", (mv.annualized_window ? fmtWinRange(mv.annualized_window) : "—") + annNote)}
      ${cell("XIRR (money-weighted)", mv.xirr_pct != null ? App.formatNum(mv.xirr_pct, 2) + "%" : "—", annNote || "annualised, 365.25 basis")}
      ${cell("Max drawdown", mv.max_drawdown_pct != null ? App.formatNum(mv.max_drawdown_pct, 2) + "%" : "—", ddSub)}
    </div>
    <div id="mvValueChart" style="margin-top:10px"></div>
    <div class="page-sub" style="margin-top:14px">Daily returns (%) — flow-adjusted, 1-day move of the portfolio value</div>
    <div id="mvReturnChart" style="margin-top:8px"></div>
    <div class="table-wrap" style="max-height:26vh;overflow:auto;margin-top:14px">
      <table class="data"><thead><tr><th>Scheme</th><th class="r">Tx</th><th class="r">Opening units</th><th class="r">End units</th><th>First tx</th><th>Last tx</th></tr></thead>
      <tbody>${(mv.constituents || []).map(x => `<tr>
        <td>${App.esc(x.name)}</td><td class="num">${App.formatNum(x.tx_count)}</td>
        <td class="num">${x.opening_units != null ? App.formatNum(x.opening_units, 2) : "—"}</td>
        <td class="num">${x.end_units != null ? App.formatNum(x.end_units, 2) : "—"}</td>
        <td class="mono">${App.formatDate(x.first_tx)}</td><td class="mono">${App.formatDate(x.last_tx)}</td></tr>`).join("") || `<tr><td colspan="6" class="empty">\u2014</td></tr>`}</tbody></table>
    </div>
    <div class="page-sub" style="margin-top:8px"><b>${App.esc(mv.disclaimer)}</b> Money-weighted view of the investor's own income — purchases/redemptions included, unlike the weight-blended index above. Daily-path figures are flow-adjusted; the money-weighted XIRR reflects the statement's entire transaction history (holdings before the earliest transaction are deduced and valued at the earliest available NAV — partial statements will show a distorted opening and a noisy XIRR).</div>`;
  const vs = mv.value_series || {};
  if (vs.dates && vs.dates.length > 1) {
    Charts.mountMultiLine(document.getElementById("mvValueChart"),
      [{ name: "Portfolio value", color: "@primary", dates: vs.dates, values: vs.values }],
      { formatValue: v => "₹" + App.formatNum(v, 0) });
  }
  const dr = mv.daily_returns || {};
  if (dr.dates && dr.dates.length > 1) {
    // downsample for drawability when > ~800 daily points (weekly step)
    let idx = dr.dates.map((_, i) => i).filter(i => i % 7 === 0 || i === dr.dates.length - 1);
    Charts.mountMultiLine(document.getElementById("mvReturnChart"),
      [{ name: "Daily return", color: "#c94f4f",
         dates: idx.map(i => dr.dates[i]), values: idx.map(i => dr.values[i]) }],
      { formatValue: v => App.formatNum(v, 2) + "%" });
  }
}

function fmtDeviation(x) {
  if (!x.deviation) return "\u2014";
  const d = x.deviation;
  const bad = (d.kind === "max" && d.diff > 0) || (d.kind === "min" && d.diff > 0);
  let txt;
  if (d.kind === "max") {
    txt = d.diff > 0 ? `+${d.diff.toFixed(1)}${d.unit} over` : `${d.diff.toFixed(1)}${d.unit} under`;
  } else {
    txt = d.diff > 0 ? `${d.diff.toFixed(1)}${d.unit} below` : `+${(-d.diff).toFixed(1)}${d.unit} above`;
  }
  return bad ? `<span class="badge amber">${txt}</span>` : `<span class="badge green">${txt}</span>`;
}

function renderRuleContext(x) {
  const ctx = x.context || {};
  const fmtFunds = (funds) => (funds || []).map(f => `<div class="fund-contrib"><span>${App.esc(f.fund)}</span><span class="num">${App.formatPct(f.value)}</span></div>`).join("");
  const stockTable = (rows, label) => {
    const body = (rows || []).map(r => `<tr>
        <td>${App.esc(r.security)}</td><td class="num">${App.formatPct(r.weight)}</td>
        <td>${App.esc((r.funds || []).join(", ") || "\u2014")}</td></tr>`).join("");
    return `<div class="table-wrap" style="max-height:22vh;overflow:auto"><table class="data">
      <thead><tr><th>${App.esc(label)}</th><th class="r">Weight %</th><th>Held by funds</th></tr></thead>
      <tbody>${body || `<tr><td colspan="3" class="empty">\u2014</td></tr>`}</tbody></table></div>`;
  };
  if (ctx.kind === "single_stock") {
    return `<div class="rule-ctx">
      <div class="kv" style="margin-bottom:6px"><dt>Top stock (single-stock rule)</dt><dd><strong>${App.esc(ctx.security)}</strong> \u2014 ${App.formatPct(ctx.value)} <span class="mono">${App.esc(ctx.isin || "")}</span></dd></div>
      <div class="page-sub" style="display:block;margin-bottom:4px">Held by:</div>${fmtFunds(ctx.funds)}
    </div>`;
  }
  if (ctx.kind === "sector" || ctx.kind === "sector_topn") {
    const bd = (ctx.breakdown || []).map(s => `<tr><td>${App.esc(s.sector)}</td><td class="num">${App.formatPct(s.weight)}</td></tr>`).join("");
    const label = ctx.kind === "sector_topn"
      ? `Top ${ctx.n} sector concentration`
      : `Sector breakdown (top ${(ctx.breakdown || []).length})`;
    const focus = ctx.kind === "sector_topn" ? "" : `<div class="page-sub" style="display:block;margin:8px 0 4px">Stocks in <strong>${App.esc(ctx.sector)}</strong> (${App.formatPct(ctx.weight)}) & their funds:</div>${stockTable(ctx.stocks, "Stock")}`;
    return `<div class="rule-ctx">
      <div class="page-sub" style="display:block;margin-bottom:4px">${App.esc(label)}:</div>
      <div class="table-wrap" style="max-height:16vh;overflow:auto"><table class="data"><thead><tr><th>Sector</th><th class="r">Weight %</th></tr></thead><tbody>${bd}</tbody></table></div>
      ${ctx.funds && ctx.funds.length ? `<div class="page-sub" style="display:block;margin:8px 0 4px">Funds contributing to these sectors:</div>${fmtFunds(ctx.funds)}` : ""}
      ${focus}
    </div>`;
  }
  if (ctx.kind === "asset") {
    return `<div class="rule-ctx">
      <div class="page-sub" style="display:block;margin-bottom:4px">${App.esc(ctx.asset)} \u2014 ${App.formatPct(ctx.value)} of the portfolio, by fund:</div>
      ${fmtFunds(ctx.funds)}
      <div class="page-sub" style="display:block;margin:8px 0 4px">Top securities in this category & their funds:</div>
      ${stockTable(ctx.top, "Security")}
    </div>`;
  }
  if (ctx.kind === "cap") {
    return `<div class="rule-ctx">
      <div class="page-sub" style="display:block;margin-bottom:4px">${App.esc(ctx.cap)} segment \u2014 ${App.formatPct(ctx.value)} of the portfolio, by fund:</div>
      ${fmtFunds(ctx.funds)}
      <div class="page-sub" style="display:block;margin:8px 0 4px">Stocks in this cap segment & their funds:</div>
      ${stockTable(ctx.stocks, "Stock")}
    </div>`;
  }
  if (ctx.kind === "topn") {
    return `<div class="rule-ctx">
      <div class="page-sub" style="display:block;margin-bottom:4px">Top ${ctx.n} holdings \u2014 ${App.formatPct(ctx.value)} of the portfolio:</div>
      ${stockTable(ctx.stocks, "Holding")}
    </div>`;
  }
  if (ctx.kind === "overlap") {
    const funds = (ctx.funds || []).join(" , ") || "\u2014";
    return `<div class="rule-ctx"><div class="kv"><dt>Largest overlap</dt><dd><strong>${App.formatPct(ctx.value)}</strong> between: ${App.esc(funds)}</dd></div></div>`;
  }
  if (ctx.kind === "schemes") {
    const rows = (ctx.schemes || []).map(s => `<div class="fund-contrib"><span>${App.esc(s.fund)}</span><span class="num">${App.formatPct(s.value)}</span></div>`).join("");
    return `<div class="rule-ctx"><div class="page-sub" style="display:block;margin-bottom:4px">Schemes held (with portfolio weight):</div>${rows}</div>`;
  }
  return "";
}

function toggleRuleFunds(el) {
  const sub = el.closest("tr").nextElementSibling;
  if (sub && sub.classList.contains("subrow")) {
    sub.hidden = !sub.hidden;
    el.textContent = sub.hidden ? "\u25B8" : "\u25BE";
  }
}

async function backfillStaleNavs(btn) {
  btn.disabled = true; btn.textContent = "Backfilling\u2026";
  try {
    const stale = (window._lastAnalysis && window._lastAnalysis.stale_holdings) || [];
    const isins = stale.map(x => x.isin).filter(Boolean);
    const r = await apiT("/nav-freshness", { method: "POST", body: JSON.stringify({ backfill: true, isins }) });
    const b = r.backfilled || {};
    App.toast(`Backfilled ${b.ok || 0}/${b.attempted || 0} funds. Click Analyse again to refresh.`);
  } catch (e) { App.toast("Backfill failed: " + e.message, true); }
  btn.disabled = false; btn.textContent = "Backfill stale NAVs";
}

function toggleCard(card) {
  card.classList.toggle("collapsed");
  const caret = card.querySelector("h3 .caret");
  if (caret) caret.textContent = card.classList.contains("collapsed") ? "\u25B8" : "\u25BE";
}

function makeCardsCollapsible(container) {
  if (!container) return;
  container.querySelectorAll(".card:not(.no-collapse)").forEach(card => {
    if (card.querySelector(".collapsible-head")) return;
    const head = card.querySelector("h3");
    if (!head) return;
    head.classList.add("collapsible-head");
    head.innerHTML += ` <span class="caret">\u25BE</span>`;
    head.addEventListener("click", () => toggleCard(card));
  });
}

function downloadMdReport() {
  const r = window._lastAnalysis;
  if (!r || !r.markdown) { App.toast("No report available.", true); return; }
  const blob = new Blob([r.markdown], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = (r.label || "portfolio").replace(/[^\w\-]+/g, "-") + ".md";
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

function renderCompliance(out, r) {
  window._lastAnalysis = r;
  const c = r.compliance || {};
  const rows = (c.rows || []).map(x => {
    const funds = x.funds || [];
    const hasFunds = funds.length > 0 || !!x.context;
    const unitPct = (x.unit || "%") === "%";
    const fundHtml = funds.map(f => {
      const v = (typeof f === "object") ? f.value : null;
      const label = (typeof f === "object") ? f.fund : f;
      return `<div class="fund-contrib"><span>${App.esc(label)}</span><span class="num">${v != null ? (unitPct ? App.formatPct(v, 2) : App.formatNum(v, 0)) : "\u2014"}</span></div>`;
    }).join("");
    const body = (x.remark ? `<div class="rule-remark">Remark / direction: ${App.esc(x.remark)}</div>` : "")
      + (x.context
        ? renderRuleContext(x)
        : `<span class="page-sub" style="display:block;margin-bottom:4px">Responsible funds &amp; contribution to this rule:</span>${fundHtml}`);
    const badge = x.pass === true ? '<span class="badge green">PASS</span>' : (x.pass === false ? `<span class="badge ${x.severity === "high" ? "red" : "amber"}">BREACH</span>` : '<span class="badge grey">N/A</span>');
    return `
      <tr class="rule-row">
        <td>${hasFunds ? `<span class="rule-toggle" onclick="toggleRuleFunds(this)">\u25B8</span> ` : ""}${App.esc(x.rule)}</td>
        <td class="num">${App.esc(x.limit)}</td>
        <td class="num"><strong>${App.esc(x.actual)}</strong></td>
        <td class="num">${fmtDeviation(x)}</td>
        <td>${badge}</td>
      </tr>
      ${hasFunds ? `<tr class="subrow" hidden><td colspan="5">${body}</td></tr>` : ""}`;
  }).join("") || `<tr><td colspan="5" class="empty">No rules defined for this portfolio's strategy.</td></tr>`;
  const pa = r.analysis || {};
  const isAlloc = !!pa.allocations;
  const effTotal = pa.effective_total || 0;
  const totalW = pa.total_weight || effTotal;
  const coverageLine = !isAlloc ? `<div class="page-sub" style="margin-top:6px">Analysis resolves ${App.formatPct(pa.coverage_pct, 1)} of the portfolio (${App.formatPct(effTotal, 1)} of ${App.formatPct(totalW, 1)} allocated) \u2014 the remainder has no holdings data on record and is excluded from these charts.</div>` : "";
  const allH = pa.effective_holdings || [];
  const topPie = allH.map(h => ({ label: h.company, value: h.weight }));
  const capEntries = capPieData(pa.cap_split_raw || {});
  const capTotal = capEntries.reduce((a, e) => a + (e.value || 0), 0);
  // Items-based: pies show the full 100% breakup of the RESOLVED portfolio
  // (normalised). Allocation models: cap split is a within-equity breakdown.
  let pieData, pieTitle;
  if (isAlloc) {
    pieData = capEntries.length ? pfPieFull100(capEntries, capTotal) : [{ label: "No cap split", value: 100 }];
    pieTitle = "Equity cap split";
  } else if (topPie.length) {
    pieData = pfPieFull100(topPie, effTotal);
    pieTitle = "Top holdings pie";
  } else {
    pieData = pfPieFull100(capEntries, effTotal);
    pieTitle = "Equity cap split";
  }
  const allocNote = isAlloc ? `<div class="page-sub">Allocation target (asset classes + caps + direct-stock split) \u2014 no specific holdings.</div>` : "";
  const concCard = !isAlloc ? `<div class="card"><h3>Concentration summary</h3>
          <div class="kv"><dt>Top 1 / 5 / 10</dt><dd>${App.formatPct((pa.concentration || {}).top1_pct)} / ${App.formatPct((pa.concentration || {}).top5_pct)} / ${App.formatPct((pa.concentration || {}).top10_pct)}</dd></div>
        </div>` : `<div class="card"><h3>Allocation targets</h3>
          <div class="kv"><dt>Direct stocks</dt><dd>${App.formatPct((pa.allocations || {}).direct_stock_pct, 1)}</dd></div>
        </div>`;
  const debt = pa.debt_analysis || {};
  const capPieFull = pfPieFull100(capEntries, capTotal);
  const effHoldingsCard = !isAlloc && allH.length ? `
    <div class="card">
      <h3>Effective holdings \u2014 full 100% breakup <span class="badge blue">${pa.n_holdings} securities</span></h3>
      <div class="table-wrap" style="max-height:40vh;overflow:auto">
        <table class="data"><thead><tr><th>Holding</th><th>ISIN</th><th>Sector</th><th>Asset</th><th class="r">Weight %</th></tr></thead>
        <tbody>${allH.map(h => `<tr><td>${App.esc(h.company)}</td><td class="mono">${App.esc(h.isin || "\u2014")}</td><td>${App.esc(h.sector || "\u2014")}</td><td>${App.badge(h.asset_class || "other")}</td><td class="num"><strong>${App.formatPct(h.weight)}</strong></td></tr>`).join("")}</tbody></table>
      </div>
    </div>` : "";
  const capSchemes = pa.cap_schemes || {};
  const pd = r.portfolio || {};
  const portfolioCard = !isAlloc ? `
    <div class="card">
      <h3>Portfolio details <span class="badge blue">${App.esc(r.label || "\u2014")}</span></h3>
      <div class="kv" style="margin-bottom:8px">
        <dt>Compliance</dt><dd><strong>${pd.compliance == null ? "N/A" : App.formatPct(pd.compliance, 1)}</strong></dd>
        <dt>Rules passed</dt><dd>${pd.passed}/${pd.total || 0}</dd>
        <dt>Allocated</dt><dd>${App.formatPct(pd.total_weight, 1)}</dd>
        <dt>Resolved / coverage</dt><dd>${App.formatPct(pd.effective_total, 1)} / ${App.formatPct(pd.coverage_pct, 1)}</dd>
        <dt>Securities</dt><dd>${pd.n_holdings}</dd>
        <dt>Schemes</dt><dd>${pd.n_schemes}</dd>
      </div>
      ${r.report_path ? `<div class="page-sub" style="margin-top:8px">Report saved: <span class="mono">${App.esc(r.report_path)}</span></div>` : ""}
    </div>` : "";
  const deviationRows = (c.rows || []).filter(x => x.deviation).map(x => `<tr>
      <td>${App.esc(x.rule)}</td>
      <td class="num">${App.esc(x.limit)}</td>
      <td class="num">${App.esc(x.actual)}</td>
      <td class="num">${fmtDeviation(x)}</td>
      <td>${x.pass === true ? '<span class="badge green">PASS</span>' : (x.pass === false ? '<span class="badge red">BREACH</span>' : '<span class="badge grey">N/A</span>')}</td>
    </tr>`).join("");
  const deviationCard = deviationRows ? `
    <div class="card">
      <h3>Deviation from limits</h3>
      <div class="table-wrap" style="max-height:34vh;overflow:auto"><table class="data">
        <thead><tr><th>Rule</th><th class="r">Limit</th><th class="r">Actual</th><th class="r">Deviation</th><th>Status</th></tr></thead>
        <tbody>${deviationRows}</tbody></table></div>
    </div>` : "";
  const assetLabels = { stocks: "Equity", debt: "Debt", gold: "Gold", cash_equivalents: "Cash",
                        international: "International", future_options: "F&O", other: "Other" };
  const holdings = r.holdings || [];
  const holdingRows = holdings.map(h => {
    const parts = Object.entries(h.composition || {}).filter(([, v]) => Math.abs(v) > 0.05).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
      .map(([k, v]) => `${assetLabels[k] || k} ${App.formatPct(v, 1)}`).join(" \u00b7 ");
    return `<tr class="holding-row" data-w="${(h.weight || 0).toFixed(4)}">
        <td>${App.esc(h.name)}</td>
        <td class="num">${h.units != null ? App.formatNum(h.units) : "\u2014"}</td>
        <td class="num">${h.nav != null ? App.esc(h.nav) : "\u2014"}</td>
        <td class="num">${App.esc(h.nav_date || "\u2014")}</td>
        <td class="num holding-val">${h.value != null ? "\u20B9" + Math.round(h.value).toLocaleString("en-IN") : "\u2014"}</td>
        <td class="num"><strong>${App.formatPct(h.weight)}</strong></td>
        <td class="page-sub">${App.esc(parts || "\u2014")}</td>
      </tr>`;
  }).join("");
  const totalMv = r.total_market_value;
  const holdingCard = holdings.length ? `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
        <h3 style="margin:0">Portfolio holding statement <span class="badge blue">${holdings.length} schemes</span></h3>
        ${totalMv ? `<span class="badge green">Total market value \u20B9${Math.round(totalMv).toLocaleString("en-IN")}</span>` : ""}
      </div>
      <div class="page-sub" style="margin-top:6px">Units held, latest NAV (and its date) and current market value per scheme (units \u00d7 NAV).</div>
      <div class="table-wrap" style="max-height:44vh;overflow:auto">
        <table class="data"><thead><tr><th>Scheme</th><th class="r">Units</th><th class="r">Latest NAV</th><th class="r">NAV date</th><th class="r">Market value (\u20B9)</th><th class="r">% of portfolio</th><th>Asset composition</th></tr></thead>
        <tbody>${holdingRows}</tbody></table>
      </div>
    </div>` : "";
  const ovM = r.overlap_matrix || [];
  const shortScheme = (s) => {
    let t = (s || "").replace(/portfolio of/i, "").replace(/as on.*/i, "").replace(/\bFund\b/gi, "").replace(/\bPlan\b/gi, "").replace(/\bDirect\b/gi, "").replace(/\bRegular\b/gi, "");
    const w = t.trim().split(/\s+/).filter(Boolean).slice(0, 3).join(" ");
    return w || s || "";
  };
  const ovPairs = [];
  for (let i = 0; i < ovM.length; i++) {
    for (let j = i + 1; j < ovM.length; j++) {
      const a = ovM[i], b = ovM[j];
      const v = a["c_" + b.id] || 0;
      if (v > 0) ovPairs.push({ a: a.scheme, b: b.scheme, v: Math.round(v * 10) / 10 });
    }
  }
  ovPairs.sort((x, y) => y.v - x.v);
  const ovTopPairs = ovPairs.slice(0, 8).map(p => `<tr><td>${App.esc(shortScheme(p.a))}</td><td>${App.esc(shortScheme(p.b))}</td><td class="num"><strong>${App.formatPct(p.v)}</strong></td></tr>`).join("");
  const overlapCard2 = ovM.length >= 2 ? `
    <div class="card">
      <h3>Mutual fund overlap matrix <span class="badge blue">${ovM.length} schemes</span></h3>
      <div class="page-sub" style="margin-top:0">% of portfolio in common underlying holdings between each pair of schemes. Scroll sideways for all schemes.</div>
      <div class="table-wrap" style="max-height:48vh;overflow:auto">
        <table class="data overlap-matrix">
          <thead><tr><th>Scheme</th>${ovM.map(m => `<th title="${App.esc(m.scheme)}">${App.esc(shortScheme(m.scheme))}</th>`).join("")}</tr></thead>
          <tbody>${ovM.map(m => `<tr><td>${App.esc(m.scheme)}</td>${ovM.map(k => m.id === k.id
            ? `<td class="cell" style="background:transparent">\u2014</td>`
            : `<td class="cell ${overlapHeat(m["c_" + k.id] || 0)}">${((m["c_" + k.id] || 0)).toFixed(1)}%</td>`).join("")}</tr>`).join("")}</tbody>
        </table>
      </div>
      <h4 style="margin:14px 0 6px">Highest overlap pairs</h4>
      <div class="table-wrap" style="max-height:30vh;overflow:auto">
        <table class="data"><thead><tr><th>Scheme A</th><th>Scheme B</th><th class="r">Overlap %</th></tr></thead>
        <tbody>${ovTopPairs || `<tr><td colspan="3" class="empty">No common holdings.</td></tr>`}</tbody></table>
      </div>
    </div>` : "";
  const stale = r.stale_holdings || [];
  const staleCard = stale.length ? `
    <div class="card" style="border-left:3px solid var(--warning)">
      <h3>Data freshness check <span class="badge amber">${stale.length} stale / missing</span></h3>
      <div class="page-sub">These holdings' latest NAV/price is older than 10 days (or missing). Backfill re-pulls their NAV history from the last known date to today.</div>
      <div class="table-wrap" style="max-height:26vh;overflow:auto">
        <table class="data"><thead><tr><th>Holding</th><th>ISIN</th><th class="r">Last NAV</th><th class="r">Last date</th></tr></thead>
        <tbody>${stale.map(x => `<tr><td>${App.esc(x.name || "")}</td><td class="mono">${App.esc(x.isin || "\u2014")}</td><td class="num">${x.nav != null ? App.esc(x.nav) : "\u2014"}</td><td class="num">${App.esc(x.nav_date || "\u2014")}</td></tr>`).join("")}</tbody></table>
      </div>
      <button class="btn btn-outline btn-sm" style="margin-top:10px" onclick="backfillStaleNavs(this)">Backfill stale NAVs</button>
    </div>` : "";
  const capFundRows = Object.entries(capSchemes).map(([seg, funds]) => `<tr>
      <td>${App.esc(capSegmentLabel(seg))}</td>
      <td>${App.esc((funds || []).join(", ") || "\u2014")}</td></tr>`).join("");
  const capCard = (capPieFull.length || capFundRows) ? `
    <div class="card" style="margin-bottom:14px">
      <h3>Equity cap split (large / mid / small / micro)</h3>
      <div class="pf-chart-plot"><canvas id="mvAnaCap" class="chart-canvas"></canvas><div class="pf-tip" id="mvAnaCapTip" hidden></div></div>
      <div class="pf-legend" id="mvAnaCapLegend"></div>
      ${capFundRows ? `<div class="table-wrap" style="margin-top:10px;max-height:26vh;overflow:auto">
        <table class="data"><thead><tr><th>Cap segment</th><th>Funds holding these</th></tr></thead><tbody>${capFundRows}</tbody></table>
      </div>` : ""}
    </div>` : "";
  const debtFunds = (pa.debt_schemes || []).join(", ");
  const debtFundsLine = debtFunds ? `<div class="kv"><dt>Debt via funds</dt><dd>${App.esc(debtFunds)}</dd></div>` : "";
  const debtCard = (debt.n_debt_holdings || 0) ? `
    <div class="card">
      <h3>Debt portfolio analysis <span class="badge blue">${App.formatPct(debt.debt_pct, 1)} debt</span></h3>
      <div class="kv" style="margin-bottom:12px">
        <dt>Yield to maturity</dt><dd>${debt.ytm_pct != null ? App.formatPct(debt.ytm_pct, 2) : "\u2014"}</dd>
        <dt>Avg maturity</dt><dd>${debt.avg_maturity_yrs != null ? App.formatNum(debt.avg_maturity_yrs, 2) + " yrs" : "\u2014"}</dd>
        <dt>Debt holdings</dt><dd>${App.formatNum(debt.n_debt_holdings)}</dd>
        <dt>YTM coverage</dt><dd>${debt.ytm_cover != null ? App.formatPct(debt.ytm_cover, 0) : "\u2014"} <span class="page-sub">(incl. NSE-computed)</span></dd>
      </div>
      ${debtFundsLine}
      <div class="grid" style="grid-template-columns: 1fr 1fr; gap:14px; align-items:start">
        <div>
          <h4 style="margin:0 0 6px">Credit quality</h4>
          <div class="pf-chart-plot"><canvas id="mvDebtCredit" class="chart-canvas"></canvas><div class="pf-tip" id="mvDebtCreditTip" hidden></div></div>
          <div class="pf-legend" id="mvDebtCreditLegend"></div>
        </div>
        <div>
          <h4 style="margin:0 0 6px">Instrument mix</h4>
          <div class="pf-chart-plot"><canvas id="mvDebtInstr" class="chart-canvas"></canvas><div class="pf-tip" id="mvDebtInstrTip" hidden></div></div>
          <div class="pf-legend" id="mvDebtInstrLegend"></div>
        </div>
      </div>
      ${debt.top_debt_holdings && debt.top_debt_holdings.length ? `<div class="table-wrap" style="margin-top:12px;max-height:30vh;overflow:auto">
        <table class="data"><thead><tr><th>Security</th><th>ISIN</th><th>Rating</th><th class="r">YTM</th><th class="r">Weight %</th></tr></thead>
        <tbody>${debt.top_debt_holdings.map(h => `<tr><td>${App.esc(h.company)}</td><td class="mono">${App.esc(h.isin || "\u2014")}</td><td>${App.badge(h.rating || "\u2014")}</td><td class="num">${h.yield != null ? App.formatPct(h.yield, 2) : "\u2014"}${h.ytm_source && (h.ytm_source.indexOf("computed") === 0) ? ` <span class="badge blue" title="${App.esc(h.ytm_source)}">calc</span>` : ""}</td><td class="num">${App.formatPct(h.weight)}</td></tr>`).join("")}</tbody></table>
      </div>` : ""}
    </div>` : "";
  out.innerHTML = `
    <div class="grid" style="grid-template-columns: 1.5fr 1fr; align-items:start;">
      <div class="card">
        <h3>${App.esc(r.label || "Portfolio")} \u2014 compliance
          <span class="badge ${c.total === 0 || c.compliance == null ? "grey" : c.compliance >= 100 ? "green" : c.compliance >= 50 ? "amber" : "red"}">${c.total === 0 || c.compliance == null ? "N/A \u00b7 no rules" : App.formatPct(c.compliance, 1)}</span>
          ${r.markdown ? `<button class="btn btn-outline btn-sm" style="float:right" onclick="downloadMdReport()">\u2B07 Download .md report</button>` : ""}
        </h3>
        <div class="kv" style="margin-bottom:12px">
          <dt>Rules passed</dt><dd>${c.passed}/${c.total || 0}</dd>
          <dt>Allocated</dt><dd>${App.formatPct(pa.total_weight, 1)}</dd>
          <dt>Securities</dt><dd>${pa.n_holdings}</dd>
        </div>
        ${coverageLine}
        <div class="table-wrap" style="max-height:44vh; overflow:auto">
          <table class="data"><thead><tr><th>Rule</th><th class="r">Limit</th><th class="r">Actual</th><th class="r">Deviation</th><th>Status</th></tr></thead>
          <tbody>${rows}</tbody></table>
        </div>
        ${holdingCard}
      </div>
      <div>
        <div class="card" style="margin-bottom:14px">
          <h3>${pieTitle}</h3>
          <div class="pf-chart-plot"><canvas id="mvAnaPie" class="chart-canvas"></canvas><div class="pf-tip" id="mvAnaPieTip" hidden></div></div>
          <div class="pf-legend" id="mvAnaPieLegend"></div>
        </div>
        <div class="card" style="margin-bottom:14px">
          <h3>Asset allocation (Equity / Debt / Gold / International)</h3>
          <div class="pf-chart-plot"><canvas id="mvAnaAsset" class="chart-canvas"></canvas><div class="pf-tip" id="mvAnaAssetTip" hidden></div></div>
          <div class="pf-legend" id="mvAnaAssetLegend"></div>
        </div>
        ${capCard}
        ${concCard}
        ${staleCard}
      </div>
    </div>
    <div class="grid" style="grid-template-columns: 1fr; margin-top:16px; align-items:start">
      ${portfolioCard}
      ${overlapCard2}
      ${deviationCard}
      ${effHoldingsCard}
      ${debtCard}
    </div>
    ${allocNote}`;
  Charts._renderDonut(document.getElementById("mvAnaPie"), pieData, {
    center: isAlloc ? App.formatPct(pa.total_weight, 0) : App.formatPct(effTotal, 1),
    centerLabel: isAlloc ? "allocated" : "resolved",
    legendEl: document.getElementById("mvAnaPieLegend"), tipEl: document.getElementById("mvAnaPieTip"),
    legendLimit: 60,
    fmtVal: v => App.formatPct(v, 1),
  });
  Charts._renderDonut(document.getElementById("mvAnaAsset"), pfPieFull100(pa.asset_split, effTotal), {
    center: App.formatPct(effTotal, 1), centerLabel: "resolved",
    legendEl: document.getElementById("mvAnaAssetLegend"), tipEl: document.getElementById("mvAnaAssetTip"),
    fmtVal: v => App.formatPct(v, 1),
  });
  if (capPieFull.length && document.getElementById("mvAnaCap")) {
    Charts._renderDonut(document.getElementById("mvAnaCap"), capPieFull, {
      center: App.formatPct(capTotal, 1), centerLabel: "tagged equity",
      legendEl: document.getElementById("mvAnaCapLegend"), tipEl: document.getElementById("mvAnaCapTip"),
      fmtVal: v => App.formatPct(v, 1),
    });
  }
  if (debt.credit_split && document.getElementById("mvDebtCredit")) {
    Charts._renderDonut(document.getElementById("mvDebtCredit"), pfNormalize100(debt.credit_split), {
      center: App.formatPct(debt.debt_pct, 0), centerLabel: "of portfolio",
      legendEl: document.getElementById("mvDebtCreditLegend"), tipEl: document.getElementById("mvDebtCreditTip"),
      fmtVal: v => App.formatPct(v, 1),
    });
  }
  if (debt.instrument_split && document.getElementById("mvDebtInstr")) {
    Charts._renderDonut(document.getElementById("mvDebtInstr"), pfNormalize100(debt.instrument_split), {
      center: App.formatPct(debt.debt_pct, 0), centerLabel: "of portfolio",
      legendEl: document.getElementById("mvDebtInstrLegend"), tipEl: document.getElementById("mvDebtInstrTip"),
      fmtVal: v => App.formatPct(v, 1),
    });
  }
  rerenderCharts();
  makeCardsCollapsible(out);
}

// ---------------- integration helpers ----------------
async function loadClientPortfolioIntoBuilder() {
  const sel = document.getElementById("prLoadCp");
  const id = sel ? sel.value : "";
  if (!id) { App.toast("Select a client portfolio to load.", true); return; }
  const d = await apiT("/client-portfolios");
  const p = (d.items || []).find(x => String(x.id) === String(id));
  if (!p) { App.toast("Client portfolio not found.", true); return; }
  pfState.items = (p.items || []).map(i => ({ ...i }));
  pfRender();
  pfRunProposal();
}

// ---------- API & mapping ----------
function initApi() {
  const search = document.getElementById("mapSearch");
  search.addEventListener("input", App.debounce(async () => {
    const q = search.value.trim();
    const box = document.getElementById("mapResults");
    if (q.length < 2) { box.innerHTML = `<div class="empty">Search by company name, ticker or ISIN.</div>`; return; }
    box.innerHTML = `<div class="empty"><span class="spin"></span> Searching…</div>`;
    try {
      const data = await App.api("/mapping?q=" + encodeURIComponent(q));
      if (!data.items.length) { box.innerHTML = `<div class="empty">No mappings found for “${App.esc(q)}”.</div>`; return; }
      box.innerHTML = `<div class="table-wrap"><table class="data">
        <thead><tr><th>Company</th><th>ISIN</th><th>Type</th><th>Cap</th><th>Sector</th><th>In scheme</th></tr></thead>
        <tbody>${data.items.map(m => `<tr>
          <td>${App.esc(m.company)}</td>
          <td class="mono">${App.esc(m.isin)}</td>
          <td>${m.confirmed_equity === 1 ? App.badge("Listed", "green") : m.confirmed_equity === 0.5 ? App.badge("Mixed", "amber") : App.badge("Other", "grey")}</td>
          <td>${App.capBadge(m.cap)}</td>
          <td>${App.esc(m.sector)}</td>
          <td>${App.esc(m.scheme)}</td></tr>`).join("")}</tbody></table></div>`;
    } catch (e) { box.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`; }
  }, 250));
  document.querySelectorAll(".tabs .tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll(".tabs .tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(tab.dataset.tab).classList.add("active");
  }));
  loadApiSamples();
}

async function loadApiSamples() {
  try {
    const meta = await App.api("/meta");
    document.getElementById("payloadMeta").textContent = JSON.stringify(meta, null, 2);
    const schemes = await App.api("/schemes?limit=2");
    document.getElementById("payloadSchemes").textContent = JSON.stringify(schemes, null, 2).slice(0, 1800);
  } catch (e) {}
}

// ---------------- superadmin: data ops ----------------
const ADMIN_PIPELINES = ["nav_daily", "amfi_fetch", "bond_refresh", "stock_refresh"];
let adminTimer = null;

function initAdmin() {
  const body = document.getElementById("adminBody");
  body.innerHTML = `
    <div id="healthCard" class="card" style="margin-bottom:16px">
      <h3 style="margin-bottom:4px">Data health <span id="dhOverall" class="badge grey">…</span></h3>
      <div class="page-sub">Composite of coverage, completeness, freshness and pipeline telemetry · <span id="dhAt"></span></div>
      <div id="dhBars" style="margin-top:10px"><span class="spin"></span> Loading…</div>
    </div>
    <div id="relianceCard" class="card" style="margin-bottom:16px">
      <h3 style="margin-bottom:4px">Data reliance — all schemes <span id="relAvg" class="badge grey">…</span></h3>
      <div class="page-sub">Per-scheme confidence (source trust − staleness + holdings quality) across the whole catalogue</div>
      <div id="relBody" style="margin-top:10px"><span class="spin"></span> Loading…</div>
    </div>
    <div id="freshCard" class="card" style="margin-bottom:16px">
      <h3 style="margin-bottom:4px">NAV freshness <span id="nfBadge" class="badge grey">…</span></h3>
      <div class="page-sub">Every scheme's latest NAV vs the AMFI publication calendar (day-T NAVs publish ~23:00 IST; nothing on weekends/holidays) · expected latest <b id="nfExpected">…</b> <button class="btn btn-outline btn-sm" style="margin-left:10px" onclick="refreshFreshnessData(true)">Live sample</button></div>
      <div id="nfBody" style="margin-top:10px"><span class="spin"></span> Loading…</div>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px">
      <h3 style="margin:0">Refresh pipelines <span id="admSched" class="badge grey"></span></h3>
      <button class="btn btn-outline btn-sm" onclick="initAdmin()">Reload</button>
    </div>
    <div id="adminCards" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px;margin-bottom:16px"></div>
    <div class="card">
      <h3>Recent refresh events <span class="page-sub">(newest first · last 200)</span></h3>
      <div class="table-wrap" style="max-height:52vh;overflow:auto">
        <table class="data"><thead><tr><th>Time</th><th>Pipeline</th><th>Status</th><th class="r">Took</th><th>Detail / error</th></tr></thead>
        <tbody id="admLogRows"><tr><td colspan="5" class="empty"><span class="spin"></span> Loading…</td></tr></tbody></table>
      </div>
    </div>`;
  refreshAdminData();
  if (adminTimer) clearInterval(adminTimer);
  adminTimer = setInterval(() => {
    if (document.getElementById("screen-admin") &&
        document.getElementById("screen-admin").style.display !== "none") refreshAdminData();
    else clearInterval(adminTimer);
  }, 30000);
}

const DH_LABELS = {
  coverage: "Holdings coverage", completeness: "ISIN completeness",
  nav: "NAV freshness", holdings: "Disclosure freshness",
  stocks_bonds: "Stocks & bonds", pipelines: "Refresh pipelines"};

function dhBandColor(band) {
  return {green: "var(--success)", amber: "var(--warning)", red: "var(--destructive)", grey: "var(--text-3)"}[band] || "var(--text-3)";
}

async function refreshHealthData() {
  const wrap = document.getElementById("dhBars");
  if (!wrap) return;
  refreshRelianceData();
  try {
    const h = await App.api("/admin/data-health");
    const el = document.getElementById("dhOverall");
    if (!el) return;
    el.className = "badge " + h.band;
    el.textContent = h.overall + " / 100";
    document.getElementById("dhAt").textContent = "computed " +
      String(h.computed_at || "").replace("T", " ");
    wrap.innerHTML = Object.entries(h.components || {}).map(([k, c]) => `
      <div style="display:flex;align-items:center;gap:10px;margin:6px 0">
        <div style="width:170px;font-size:.8rem">${DH_LABELS[k] || k}</div>
        <div style="flex:1;background:var(--surface-2);border-radius:4px;height:8px;overflow:hidden">
          <div style="width:${Math.max(0, Math.min(100, c.score || 0))}%;height:100%;background:${dhBandColor(h.band)}"></div>
        </div>
        <div class="mono" style="width:52px;text-align:right;font-size:.78rem">${c.score != null ? c.score : "—"}</div>
        <details style="font-size:.72rem;color:var(--text-2)"><summary style="cursor:pointer">info</summary>
          <pre class="mono" style="white-space:pre-wrap;margin:4px 0 0;font-size:.7rem">${App.esc(JSON.stringify(c.detail || {}, null, 1))}</pre>
        </details>
      </div>`).join("") || `<div class="empty">No components.</div>`;
  } catch (e) {
    const o = document.getElementById("dhOverall");
    if (o) { o.className = "badge grey"; o.textContent = "N/A"; }
    wrap.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`;
  }
}

const REL_TIER_LABELS = {green: "High", amber: "Medium", red: "Low", grey: "No data"};

async function refreshRelianceData() {
  const body = document.getElementById("relBody");
  if (!body) return;
  try {
    const r = await App.api("/admin/reliance");
    const avgEl = document.getElementById("relAvg");
    if (!avgEl) return;
    avgEl.className = "badge " + (r.avg_score >= 80 ? "green" : (r.avg_score >= 55 ? "amber" : "red"));
    avgEl.textContent = `avg ${r.avg_score != null ? r.avg_score : "—"} · ${App.formatNum(r.total_schemes)} schemes`;

    const total = Math.max(1, r.total_schemes);
    const tiers = Object.entries(r.tiers || {}).map(([t, n]) => {
      const col = dhBandColor(t);
      return `<div style="flex:1;text-align:center">
        <div style="background:var(--surface-2);border-radius:4px;height:8px;overflow:hidden">
          <div style="width:${Math.round(n / total * 100)}%;height:100%;background:${col}"></div>
        </div>
        <div style="font-size:.72rem;margin-top:3px"><b style="color:${col}">${REL_TIER_LABELS[t]}</b> ${App.formatNum(n)} (${Math.round(n / total * 100)}%)</div>
      </div>`;
    }).join("");

    const srcRows = Object.entries(r.by_source || {}).sort((a, b) => b[1].count - a[1].count)
      .map(([src, a]) => `<tr>
        <td><strong>${App.esc(App.sourceLabel(src))}</strong></td>  <!-- [U4L] raw keys must never render -->
        <td class="num">${App.formatNum(a.count)}</td>
        <td class="num">${a.avg_score != null ? a.avg_score : "—"}</td>
        <td class="num">${a.stale_gt_45d ? `<span style="color:var(--red)">${App.formatNum(a.stale_gt_45d)}</span>` : "0"}</td>
        <td class="num">${a.avg_isin_pct != null ? a.avg_isin_pct + "%" : "—"}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty">No sources.</td></tr>`;

    const worstRows = (r.worst || []).map(w => `<tr class="clickable" onclick="location.hash='#scheme/${w.id}'">
        <td>${App.esc(w.fund_name)}</td>
        <td>${App.esc(w.amc)}</td>
        <td>${App.esc(w.source ? App.sourceLabel(w.source) : "none")}</td>  <!-- [U4L] -->
        <td class="mono">${App.formatDate(w.as_of)}</td>
        <td class="num"><span class="badge red">${w.score}</span></td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty">Nothing below threshold — good.</td></tr>`;

    body.innerHTML = `
      <div style="display:flex;gap:14px;margin-bottom:12px;flex-wrap:wrap">${tiers}</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <div style="flex:1;min-width:300px">
          <table class="data"><thead><tr><th>Source</th><th class="r">Schemes</th><th class="r">Avg score</th><th class="r">Stale &gt;45d</th><th class="r">Avg ISIN</th></tr></thead>
          <tbody>${srcRows}</tbody></table>
        </div>
        <div style="flex:1;min-width:300px">
          <h4 style="margin:2px 0 6px">Least reliable schemes</h4>
          <div class="table-wrap" style="max-height:240px;overflow:auto">
            <table class="data"><thead><tr><th>Scheme</th><th>AMC</th><th>Source</th><th>As of</th><th class="r">Score</th></tr></thead>
            <tbody>${worstRows}</tbody></table>
          </div>
        </div>
      </div>`;
  } catch (e) {
    const avgEl = document.getElementById("relAvg");
    if (avgEl) { avgEl.className = "badge grey"; avgEl.textContent = "N/A"; }
    body.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`;
  }
}

const NF_BUCKET_META = {
  current:      { label: "Current",       tone: "green" },
  lag1:         { label: "1 day behind",  tone: "green" },
  stale_recent: { label: "Stale 2-6d",    tone: "amber" },
  stale_deep:   { label: "Stale 7-44d",   tone: "amber" },
  dead_suspect: { label: "Dead suspect",  tone: "red" },
  no_history:   { label: "No history",    tone: "grey" },
};

async function refreshFreshnessData(liveSample = false) {
  const body = document.getElementById("nfBody");
  if (!body) return;
  try {
    const r = await App.api(`/admin/nav-freshness?live=${liveSample ? 5 : 0}`);
    const badge = document.getElementById("nfBadge");
    if (badge) {
      badge.className = "badge " + (r.healthy_pct >= 99 ? "green" : r.healthy_pct >= 95 ? "amber" : "red");
      badge.textContent = `${r.healthy_pct}% within grace · ${App.formatNum(r.files_scanned)} files`;
    }
    const exp = document.getElementById("nfExpected");
    if (exp) exp.textContent = r.expected_latest || "—";

    const total = Math.max(1, r.files_scanned);
    const bars = Object.entries(NF_BUCKET_META).map(([b, meta]) => {
      const n = (r.buckets || {})[b] || 0;
      if (!n) return "";
      return `<div style="flex:1;min-width:86px;text-align:center">
        <div style="background:var(--surface-2);border-radius:4px;height:8px;overflow:hidden">
          <div style="width:${Math.round(n / total * 100)}%;height:100%;background:${dhBucketColor(b)}"></div>
        </div>
        <div style="font-size:.72rem;margin-top:3px"><b style="color:${dhBucketColor(b)}">${meta.label}</b> ${App.formatNum(n)}</div>
      </div>`;
    }).join("");

    let sampleHtml = "";
    if (r.sample && r.sample.sample_size > 0) {
      const s = r.sample;
      sampleHtml = `<div class="page-sub" style="margin:8px 0 0">Correctness sample: ${s.live_checked}/${s.sample_size} verified against live AMFI` +
        (s.live_checked ? ` · date current ${s.date_current}/${s.live_checked} · NAV match ${s.nav_match}/${s.live_checked} · DB match ${s.db_nav_match}/${s.sample_size}` : "") +
        (s.live_error ? ` · <span style="color:var(--red)">live fetch failed: ${App.esc(s.live_error)}</span>` : "") +
        `</div>`;
    }

    const offRows = (r.offenders || []).map(o => `<tr>
        <td class="mono">${App.esc(o.code)}</td>
        <td>${App.esc(o.fund || "—")}</td>
        <td class="mono">${App.esc(o.last_date || "—")}</td>
        <td class="num">${o.publish_gap != null ? o.publish_gap + " pub days" : "—"}</td>
        <td><span class="badge ${NF_BUCKET_META[o.bucket] ? NF_BUCKET_META[o.bucket].tone : "grey"}">${App.esc(o.bucket)}</span></td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty">No offenders — every scheme is within grace of the publication calendar.</td></tr>`;

    body.innerHTML = `
      <div style="display:flex;gap:14px;margin-bottom:10px;flex-wrap:wrap">${bars}</div>
      ${sampleHtml}
      <div class="table-wrap" style="max-height:240px;overflow:auto;margin-top:10px">
        <table class="data"><thead><tr><th>Code</th><th>Scheme</th><th>Last NAV</th><th class="r">Gap</th><th>Bucket</th></tr></thead>
        <tbody>${offRows}</tbody></table>
      </div>`;
  } catch (e) {
    const badge = document.getElementById("nfBadge");
    if (badge) { badge.className = "badge grey"; badge.textContent = "N/A"; }
    body.innerHTML = `<div class="empty">${App.esc(e.message)}</div>`;
  }
}

function dhBucketColor(bucket) {
  const tone = (NF_BUCKET_META[bucket] || {}).tone;
  return { green: "var(--success)", amber: "var(--warning)",
           red: "var(--destructive)", grey: "var(--text-3)" }[tone] || "var(--text-3)";
}

function admBadge(status) {
  if (status === "success") return '<span class="badge green">success</span>';
  if (status === "error") return '<span class="badge red">error</span>';
  if (status === "started") return '<span class="badge amber">running</span>';
  return `<span class="badge grey">${App.esc(status || "no runs")}</span>`;
}

function admDetail(d, err) {
  if (err) return `<span class="mono" style="color:var(--red)">${App.esc(String(err).slice(0, 160))}</span>`;
  const skip = new Set(["ts", "pipeline", "status", "duration_s", "error", "trace"]);
  const parts = Object.entries(d || {}).filter(([, v]) => v !== null && v !== "")
    .map(([k, v]) => `${App.esc(k)}=<b>${App.esc(String(v).slice(0, 40))}</b>`);
  return parts.length ? parts.join(" · ") : "—";
}

async function refreshAdminData() {
  try {
    refreshHealthData();
    refreshFreshnessData();
    const [sum, logs] = await Promise.all([
      App.api("/admin/refresh-summary"),
      App.api("/admin/refresh-logs?limit=200"),
    ]);
    const sched = document.getElementById("admSched");
    sched.className = "badge " + (sum.scheduler_enabled ? "green" : "grey");
    sched.textContent = sum.scheduler_enabled ? "scheduler ON" : "scheduler OFF";

    document.getElementById("adminCards").innerHTML = ADMIN_PIPELINES.map(name => {
      const p = (sum.pipelines || {})[name] || {};
      const running = (sum.running || []).includes(name);
      return `<div class="card" style="margin:0">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <strong>${name}</strong>${admBadge(running ? "started" : p.last_status)}
        </div>
        <div class="kv" style="margin:8px 0 6px">
          <dt>Last run</dt><dd>${p.last_ts ? App.esc(p.last_ts.replace("T", " ")) : "—"}</dd>
          <dt>Took</dt><dd>${p.last_duration_s != null ? p.last_duration_s + "s" : "—"}</dd>
          <dt>24h</dt><dd><span class="badge green">${p.ok_24h || 0} ok</span> ${(p.err_24h || 0) ? `<span class="badge red">${p.err_24h} err</span>` : ""}</dd>
          ${p.last_error ? `<dt>Error</dt><dd class="mono" style="color:var(--red);font-size:.75rem">${App.esc(String(p.last_error).slice(0, 140))}</dd>` : ""}
          ${!p.last_error && Object.keys(p.last_detail || {}).length ? `<dt>Detail</dt><dd class="page-sub">${admDetail(p.last_detail)}</dd>` : ""}
        </div>
        <button class="btn btn-outline btn-sm" onclick="adminRun('${name}')"
          ${running ? "disabled" : ""}>${running ? "Running…" : "Run now"}</button>
      </div>`;
    }).join("");

    document.getElementById("admLogRows").innerHTML = (logs.items || []).map(e => `<tr>
        <td class="mono">${App.esc((e.ts || "").replace("T", " "))}</td>
        <td>${e.pipeline}</td>
        <td>${admBadge(e.status)}</td>
        <td class="num">${e.duration_s != null ? e.duration_s + "s" : "—"}</td>
        <td>${admDetail(e.status === "error" ? null : e, e.error)}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty">No events yet.</td></tr>`;
  } catch (e) {
    document.getElementById("adminBody").innerHTML =
      `<div class="empty">${App.esc(e.message)}</div>`;
  }
}

async function adminRun(pipeline) {
  try {
    await App.api(`/admin/run/${pipeline}`, { method: "POST", body: "{}" });
    App.toast(`'${pipeline}' started.`);
    setTimeout(refreshAdminData, 1200);
  } catch (e) { App.toast(e.message, true); }
}

// boot
document.addEventListener("DOMContentLoaded", boot);