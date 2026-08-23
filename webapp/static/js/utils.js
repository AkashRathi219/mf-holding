"use strict";

const App = {
  token: localStorage.getItem("fea_token") || "",
  user: null,
};

App.api = async function (path, opts = {}) {
  const headers = {};
  if (App.token) headers["Authorization"] = "Bearer " + App.token;
  const raw = !!opts.raw || opts.body instanceof FormData;
  if (opts.raw) delete opts.raw;
  if (!raw) headers["Content-Type"] = "application/json";
  const res = await fetch("/api" + path, { ...opts, headers });
  if (res.status === 401) {
    localStorage.removeItem("fea_token");
    if (!location.pathname.includes("login") && !location.pathname.includes("register")) {
      location.href = "/login";
    }
    throw new Error("Session expired");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Request failed");
  return data;
};

App.formatINR = function (v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: digits });
};

App.formatNum = function (v, digits = 0) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return Number(v).toLocaleString("en-IN", { maximumFractionDigits: digits });
};

App.formatPct = function (v, digits = 1) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return Number(v).toFixed(digits) + "%";
};

App.formatDate = function (d) {
  if (!d) return "—";
  const s = String(d).trim();
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  // '31-Jul-2026' or 'Portfolio as on 31-Jul-2026' -> '2026-07-31'
  const m = /(\d{1,2})-([A-Za-z]{3})-(\d{4})/.exec(s);
  if (m) {
    const months = { jan: "01", feb: "02", mar: "03", apr: "04", may: "05", jun: "06",
      jul: "07", aug: "08", sep: "09", oct: "10", nov: "11", dec: "12" };
    const mo = months[m[2].toLowerCase()];
    if (mo) return m[3] + "-" + mo + "-" + String(+m[1]).padStart(2, "0");
  }
  return s;
};

App.sourceLabel = function (s) {
  const map = {
    amc_website: "AMC website",
    // Internal key kept for the background aggregator fallback (stable link);
    // user-facing scope attributes the data to the AMC's own disclosures [D2].
    advisorkhoj: "AMC disclosure",
    amfi: "AMFI",
    index: "Index",
    universe_only: "Universe only",
  };
  if (!s) return "\u2014";
  return map[s] || String(s).replace(/_/g, " ");
};

// The ONE place raw source keys may be turned into user-facing labels [D2].
// Every render path MUST go through this (filter dropdowns, tooltips, badges).
App.maskedSource = App.sourceLabel;

App.esc = function (s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
};

App.toast = function (msg, isError = false) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = "toast"; }, 3200);
};

App.badge = function (text, tone) {
  return `<span class="badge ${tone || "grey"}">${App.esc(text)}</span>`;
};

App.capBadge = function (cap) {
  const tones = { large: "blue", mid: "green", small: "amber", microcap: "red", ipo: "grey", sme: "grey", sectoral: "grey", na: "grey", mixed: "amber" };
  return App.badge(cap || "na", tones[cap] || "grey");
};

App.flagBadge = function (name) {
  const tones = {
    has_holdings: "green", no_disclosure: "red", discovery_needed: "amber",
    universe_only: "grey",
  };
  return App.badge(name.replace(/_/g, " "), tones[name] || "grey");
};

App.on = function (id, evt, fn) { document.getElementById(id).addEventListener(evt, fn); };

App.show = function (id, show) { document.getElementById(id).style.display = show ? "" : "none"; };

// Debounced input helper
App.debounce = function (fn, ms = 300) {
  let t; return function (...args) { clearTimeout(t); t = setTimeout(() => fn.apply(null, args), ms); };
};

// markdown -> html (safe, minimal)
App.md = function (md) {
  const esc = (s) => App.esc(s).replace(/\*\*/g, "").replace(/`/g, "");
  return md
    .split(/\n{2,}/)
    .map(block => {
      if (block.startsWith("# ")) return `<h2>${esc(block.slice(2))}</h2>`;
      if (block.startsWith("## ")) return `<h3>${esc(block.slice(3))}</h3>`;
      if (block.startsWith("> ")) return `<blockquote>${esc(block.slice(2))}</blockquote>`;
      if (block.startsWith("| ")) {
        const lines = block.split("\n");
        const head = lines[0].split("|").slice(1, -1);
        const rows = lines.slice(2).map(l => l.split("|").slice(1, -1));
        let html = "<table class='data'><thead><tr>" +
          head.map(h => `<th>${esc(h)}</th>`).join("") + "</tr></thead><tbody>";
        rows.forEach(r => {
          html += "<tr>" + r.map(c => `<td>${esc(c)}</td>`).join("") + "</tr>";
        });
        return html + "</tbody></table>";
      }
      if (block.startsWith("- ")) {
        return "<ul>" + block.split("\n").map(l => `<li>${esc(l.slice(2))}</li>`).join("") + "</ul>";
      }
      return `<p>${esc(block)}</p>`;
    })
    .join("");
};

App.debounced = {};