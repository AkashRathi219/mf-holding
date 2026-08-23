"use strict";

const Charts = {
  _renderBar(canvas, labels, values, opts = {}) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const w = Math.max(rect.width, 300);
    const h = opts.height || 260;
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    const pad = { l: 46, r: 12, t: 16, b: opts.labelsBottom !== false ? 74 : 30 };
    const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
    const max = Math.max(1, ...values.map(v => {
      const n = Number(v);
      return isFinite(n) ? n : 0;
    }));
    const n = labels.length;
    const slot = plotW / n, barW = Math.min(34, slot * 0.62);
    ctx.textBaseline = "middle";
    // grid lines
    ctx.strokeStyle = "#e8edf4"; ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif";
    for (let g = 0; g <= 4; g++) {
      const y = pad.t + plotH - (plotH * g / 4);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      ctx.textAlign = "right";
      const val = max * g / 4;
      ctx.fillText(App.formatNum(val, val >= 1000 ? 0 : 1), pad.l - 6, y);
    }
    // bars
    const colors = opts.colors || Array(n).fill("#2456d6");
    values.forEach((v, i) => {
      const vn = Number(v);
      const val = isFinite(vn) ? vn : 0;          // null/NaN -> zero-height bar
      const bh = Math.max(2, (val / max) * plotH);
      const x = pad.l + slot * i + (slot - barW) / 2;
      const y = pad.t + plotH - bh;
      ctx.fillStyle = colors[i] || "#2456d6";
      ctx.fillRect(x, y, barW, bh);
      ctx.fillStyle = "#f8fafc";
      ctx.fillRect(x + 1, y + 1, barW - 2, bh - 2);
      ctx.fillStyle = colors[i] || "#2456d6";
      ctx.fillRect(x + 1, y + 1, barW - 2, Math.max(2, bh - 2) * 0.85);
      ctx.fillStyle = "#17202f";
      ctx.font = "600 11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(App.formatNum(v), x + barW / 2, y - 8);
      // label
      ctx.fillStyle = "#5b6b7f"; ctx.font = "10.5px sans-serif";
      ctx.textAlign = "center";
      const lab = labels[i].length > 16 ? labels[i].slice(0, 15) + "…" : labels[i];
      ctx.save();
      ctx.translate(x + barW / 2, h - 8);
      ctx.rotate(-0.35);
      ctx.fillText(lab, 0, 0);
      ctx.restore();
    });
  },

  _renderDonut(canvas, entries, opts = {}) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const size = Math.min(rect.width || 300, 300);
    canvas.width = size * dpr; canvas.height = size * dpr;
    canvas.style.height = size + "px"; canvas.style.width = size + "px";
    const ctx = canvas.getContext("2d");
    const cx = size / 2, cy = size / 2, r = size / 2 - 22;
    const total = entries.reduce((a, e) => a + (e.value || 0), 0);
    const palette = ["#2456d6", "#16a085", "#e2a03f", "#7d5cd6", "#d6496b", "#5aa7d6",
      "#8a97a8", "#4b6a9c", "#3fa574", "#c94f4f", "#9b8c3f", "#6a4fa3", "#2b8cbe",
      "#c070c8", "#6a9c3f", "#be6a2b", "#3f7dbe", "#be2b6a"];

    // Slice geometry (normalized angles in [0, 2PI)).
    const slices = [];
    if (total > 0) {
      let a = 0;
      entries.forEach((e, i) => {
        const frac = (e.value || 0) / total;
        slices.push({
          label: e.label, value: e.value, pct: frac * 100,
          color: palette[i % palette.length],
          start: a, end: a + frac * Math.PI * 2,
        });
        a += frac * Math.PI * 2;
      });
    }

    const draw = (hoverIdx) => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size, size);
      if (!slices.length) {
        ctx.fillStyle = "#8a97a8"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
        ctx.fillText("No data", cx, cy);
        return;
      }
      slices.forEach((s, i) => {
        const rr = (i === hoverIdx) ? r + 6 : r;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, rr, -Math.PI / 2 + s.start, -Math.PI / 2 + s.end);
        ctx.closePath();
        ctx.fillStyle = s.color;
        ctx.fill();
        ctx.strokeStyle = (i === hoverIdx) ? "#fff" : "#fff";
        ctx.lineWidth = 2;
        ctx.stroke();
      });
      ctx.beginPath(); ctx.arc(cx, cy, r * 0.62, 0, Math.PI * 2); ctx.fillStyle = "#fff"; ctx.fill();
      ctx.fillStyle = "#17202f"; ctx.font = "700 18px sans-serif"; ctx.textAlign = "center";
      ctx.fillText(opts.center || App.formatNum(total), cx, cy + 2);
      ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif";
      ctx.fillText(opts.centerLabel || "total", cx, cy + 18);
    };
    draw(-1);

    // Legend (color swatch + label + percentage).
    if (opts.legendEl && slices.length) {
      const limit = opts.legendLimit || slices.length;
      const shown = slices.slice(0, limit);
      const more = slices.length - shown.length;
      opts.legendEl.innerHTML = shown.map(s => `
        <div class="pf-legend-row">
          <span class="swatch" style="background:${s.color}"></span>
          <span class="lbl" title="${App.esc(s.label)}">${App.esc(s.label || "\u2014")}</span>
          <span class="val">${App.esc(opts.fmtVal ? opts.fmtVal(s.value) : App.formatPct(s.value, 1))}</span>
        </div>`).join("") + (more > 0
        ? `<div class="pf-legend-row"><span class="lbl" style="color:var(--text-3)">\u2026 and ${more} more \u2014 see full table below</span></div>` : "");
    }

    // Hover tooltip.
    if (opts.tipEl && slices.length) {
      const tip = opts.tipEl;
      canvas.onmousemove = (ev) => {
        const cr = canvas.getBoundingClientRect();
        const x = ev.clientX - cr.left - size / 2;
        const y = ev.clientY - cr.top - size / 2;
        // Slices are drawn from -PI/2 (12 o'clock); map the pointer angle into
        // that same space so every quadrant of the ring is hoverable.
        let ang = Math.atan2(y, x) + Math.PI / 2;
        if (ang < 0) ang += Math.PI * 2;
        let idx = -1;
        slices.forEach((s, i) => { if (idx < 0 && ang >= s.start && ang < s.end) idx = i; });
        draw(idx);
        if (idx >= 0) {
          const s = slices[idx];
          tip.hidden = false;
          tip.style.left = (ev.clientX - cr.left + 14) + "px";
          tip.innerHTML =
            `<div class="tip-label">${App.esc(s.label)}</div>` +
            `<div class="tip-val">${App.esc(opts.fmtVal ? opts.fmtVal(s.value) : App.formatPct(s.value, 1))} ` +
            `(${App.esc(App.formatPct(s.pct, 1))})</div>`;
        } else {
          tip.hidden = true;
        }
      };
      canvas.onmouseleave = () => { tip.hidden = true; draw(-1); };
    }
  },

  _renderGauge(canvas, value, max, opts = {}) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const size = Math.min(rect.width || 200, 200);
    canvas.width = size * dpr; canvas.height = size * dpr;
    canvas.style.height = size + "px"; canvas.style.width = size + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, size, size);
    const cx = size / 2, cy = size / 2 + 6, r = size / 2 - 18;
    const frac = Math.max(0, Math.min(1, value / max));
    ctx.lineWidth = 14; ctx.lineCap = "round";
    ctx.beginPath(); ctx.arc(cx, cy, r, Math.PI, 0); ctx.strokeStyle = "#e8edf4"; ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, r, Math.PI, Math.PI + frac * Math.PI); ctx.strokeStyle = opts.color || "#2456d6"; ctx.stroke();
    ctx.fillStyle = "#17202f"; ctx.font = "700 20px sans-serif"; ctx.textAlign = "center";
    ctx.fillText(App.formatPct(value, 1), cx, cy - 4);
    ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif";
    ctx.fillText(opts.label || "", cx, cy + 18);
  },

  _renderLine(canvas, dates, values, opts = {}) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const w = Math.max(rect.width, 320);
    const h = opts.height || 260;
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    if (!dates || dates.length < 2) {
      ctx.fillStyle = "#8a97a8"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("Insufficient data", w / 2, h / 2);
      return;
    }
    const pad = { l: 52, r: 12, t: 16, b: 34 };
    const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
    const nums = values.map(Number);
    const lo = Math.min(...nums), hi = Math.max(...nums);
    const span = (hi - lo) || 1;
    const loPad = lo - span * 0.08, hiPad = hi + span * 0.08;
    const X = (i) => pad.l + (i / (dates.length - 1)) * plotW;
    const Y = (v) => pad.t + plotH - ((v - loPad) / (hiPad - loPad)) * plotH;
    // grid + y labels
    ctx.strokeStyle = "#e8edf4"; ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif";
    ctx.textAlign = "right";
    for (let g = 0; g <= 4; g++) {
      const y = pad.t + plotH - (plotH * g / 4);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      const val = loPad + (hiPad - loPad) * g / 4;
      ctx.fillText(App.formatNum(val, val >= 1000 ? 0 : 1), pad.l - 6, y);
    }
    // x labels (first/middle/last dates)
    ctx.fillStyle = "#5b6b7f"; ctx.font = "10.5px sans-serif"; ctx.textAlign = "center";
    [0, Math.floor((dates.length - 1) / 2), dates.length - 1].forEach(i => {
      ctx.fillText(dates[i], X(i), h - 10);
    });
    // line
    const stroke = opts.color || "#2456d6";
    ctx.beginPath();
    ctx.moveTo(X(0), Y(nums[0]));
    for (let i = 1; i < nums.length; i++) ctx.lineTo(X(i), Y(nums[i]));
    ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.strokeStyle = stroke; ctx.stroke();
    // fill under line
    ctx.lineTo(X(nums.length - 1), pad.t + plotH);
    ctx.lineTo(X(0), pad.t + plotH);
    ctx.closePath();
    ctx.globalAlpha = 0.12; ctx.fillStyle = stroke; ctx.fill(); ctx.globalAlpha = 1;
  },

  // -----------------------------------------------------------------------
  // Interactive NAV line chart (hover tooltip + preset & custom date ranges)
  // -----------------------------------------------------------------------
  _navKey(s) {
    s = (s || "").trim();
    if (!s) return "";
    let m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (m) return s.slice(0, 10);
    m = /^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/.exec(s);
    if (m) {
      const months = { jan: "01", feb: "02", mar: "03", apr: "04", may: "05", jun: "06",
        jul: "07", aug: "08", sep: "09", oct: "10", nov: "11", dec: "12" };
      const mo = months[m[2].toLowerCase()];
      if (mo) return m[3] + "-" + mo + "-" + String(+m[1]).padStart(2, "0");
    }
    return "";
  },

  _downsample(dates, navs, max) {
    if (dates.length <= max) return { dates, navs };
    const n = dates.length, step = n / max, outD = [], outN = [];
    const idx = new Set([0, n - 1]);
    for (let i = 0; i < max; i++) idx.add(Math.floor(i * step));
    [...idx].sort((a, b) => a - b).forEach(i => { outD.push(dates[i]); outN.push(navs[i]); });
    return { dates: outD, navs: outN };
  },

  _renderLineChart(ctx, w, h, dates, navs, loPad, hiPad, color, hover) {
    const pad = { l: 54, r: 14, t: 16, b: 30 };
    const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
    ctx.clearRect(0, 0, w, h);
    if (!dates || dates.length < 2) {
      ctx.fillStyle = "#8a97a8"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("Insufficient data", w / 2, h / 2);
      return;
    }
    const nums = navs.map(Number);
    const X = (i) => pad.l + (i / (dates.length - 1)) * plotW;
    const Y = (v) => pad.t + plotH - ((v - loPad) / (hiPad - loPad)) * plotH;
    // grid + y labels
    ctx.strokeStyle = "#e8edf4"; ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif";
    ctx.textAlign = "right";
    for (let g = 0; g <= 4; g++) {
      const y = pad.t + plotH - (plotH * g / 4);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      const val = loPad + (hiPad - loPad) * g / 4;
      ctx.fillText(App.formatNum(val, val >= 1000 ? 0 : 1), pad.l - 6, y);
    }
    // x labels (first / middle / last)
    ctx.fillStyle = "#5b6b7f"; ctx.font = "10.5px sans-serif"; ctx.textAlign = "center";
    [0, Math.floor((dates.length - 1) / 2), dates.length - 1].forEach(i => {
      ctx.fillText(dates[i], X(i), h - 10);
    });
    // fill under line
    ctx.beginPath();
    ctx.moveTo(X(0), pad.t + plotH);
    for (let i = 0; i < nums.length; i++) ctx.lineTo(X(i), Y(nums[i]));
    ctx.lineTo(X(nums.length - 1), pad.t + plotH);
    ctx.closePath();
    ctx.globalAlpha = 0.12; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
    // line
    ctx.beginPath();
    ctx.moveTo(X(0), Y(nums[0]));
    for (let i = 1; i < nums.length; i++) ctx.lineTo(X(i), Y(nums[i]));
    ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.strokeStyle = color; ctx.stroke();
    // hover marker (hover = { x, y } pixels in canvas space)
    if (hover) {
      ctx.strokeStyle = "rgba(138,151,168,0.55)"; ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(hover.x, pad.t); ctx.lineTo(hover.x, pad.t + plotH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(hover.x, hover.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = "#fff"; ctx.fill(); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
    }
  },

  // Multi-series line chart for the Compare view [ANA2].
  // series = [{ name, color, dates:[ISO], values:[num] }] on a shared grid.
  mountMultiLine(container, series, opts = {}) {
    const height = opts.height || 260;
    const fmt = opts.formatValue || ((v) => App.formatNum(v, 1));
    const n = series[0] && series[0].dates ? series[0].dates.length : 0;
    container.innerHTML = `
      <div class="nav-plot"><canvas class="ml-canvas"></canvas><div class="nav-tip" hidden></div></div>
      <div class="ml-legend" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:8px">
        ${series.map((s, i) => `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:#5b6b7f">
          <span style="width:10px;height:10px;border-radius:2px;background:${s.color};display:inline-block"></span>
          ${App.esc(s.name)}</span>`).join("")}
      </div>`;
    const canvas = container.querySelector(".ml-canvas");
    const tip = container.querySelector(".nav-tip");
    const plot = container.querySelector(".nav-plot");
    const dpr = window.devicePixelRatio || 1;
    let hoverI = null;

    function render() {
      const rect = plot.getBoundingClientRect();
      const w = Math.max(rect.width || 320, 320), h = height;
      canvas.width = w * dpr; canvas.height = h * dpr;
      canvas.style.height = h + "px"; canvas.style.width = w + "px";
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      if (n < 2) {
        ctx.fillStyle = "#8a97a8"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
        ctx.fillText("Insufficient data", w / 2, h / 2);
        return;
      }
      const pad = { l: 54, r: 14, t: 16, b: 30 };
      const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
      let lo = Infinity, hi = -Infinity;
      series.forEach(s => s.values.forEach(v => { if (v < lo) lo = v; if (v > hi) hi = v; }));
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      const span = (hi - lo) || 1;
      lo -= span * 0.08; hi += span * 0.08;
      const X = (i) => pad.l + (i / (n - 1)) * plotW;
      const Y = (v) => pad.t + plotH - ((v - lo) / (hi - lo)) * plotH;
      ctx.strokeStyle = "#e8edf4"; ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif";
      ctx.textAlign = "right";
      for (let g = 0; g <= 4; g++) {
        const y = pad.t + plotH - (plotH * g / 4);
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
        const val = lo + (hi - lo) * g / 4;
        ctx.fillText(App.formatNum(val, Math.abs(val) >= 1000 ? 0 : 1), pad.l - 6, y);
      }
      const labels = series[0].dates;
      ctx.fillStyle = "#5b6b7f"; ctx.font = "10.5px sans-serif"; ctx.textAlign = "center";
      [0, Math.floor((n - 1) / 2), n - 1].forEach(i => ctx.fillText(labels[i], X(i), h - 10));
      series.forEach(s => {
        ctx.beginPath();
        s.values.forEach((v, i) => { if (i === 0) ctx.moveTo(X(0), Y(v)); else ctx.lineTo(X(i), Y(v)); });
        ctx.strokeStyle = s.color; ctx.lineWidth = 1.8; ctx.stroke();
      });
      if (hoverI != null) {
        const x = X(hoverI);
        ctx.strokeStyle = "rgba(138,151,168,0.55)"; ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + plotH); ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    canvas.addEventListener("mousemove", (e) => {
      const rect = canvas.getBoundingClientRect();
      const padL = 54, plotW = rect.width - padL - 14;
      let idx = Math.round((e.clientX - rect.left - padL) / plotW * (n - 1));
      hoverI = Math.max(0, Math.min(n - 1, idx));
      render();
      const x = padL + (hoverI / (n - 1)) * plotW;
      tip.hidden = false;
      tip.style.left = (x + 14) + "px";
      tip.innerHTML = `<div class="tip-date">${App.esc(series[0].dates[hoverI])}</div>` +
        series.map(s => `<div class="tip-nav" style="color:${s.color}">${App.esc(s.name)}: ${fmt(s.values[hoverI])}</div>`).join("");
    });
    canvas.addEventListener("mouseleave", () => { hoverI = null; tip.hidden = true; render(); });
    mountChart(render);
    render();
    return { render };
  },

  // Builds an interactive NAV chart inside `container` with preset + custom
  // date-range controls. `data` = { dates:[], navs:[], label } (full series,
  // ascending). Returns a controller with .render() / .setRange(fromKey, toKey).
  mountNavChart(container, data, opts = {}) {
    const color = opts.color || "#2456d6";
    const height = opts.height || 240;
    const formatNav = opts.formatNav || ((v) => App.formatNum(v, 2));
    const PRESETS = [
      { key: "1m", label: "1M", days: 30 }, { key: "3m", label: "3M", days: 91 },
      { key: "6m", label: "6M", days: 182 }, { key: "1y", label: "1Y", days: 365 },
      { key: "3y", label: "3Y", days: 1095 }, { key: "5y", label: "5Y", days: 1825 },
      { key: "all", label: "All", days: 0 },
    ];
    const dates = data.dates || [], navs = data.navs || [];
    const keys = dates.map(d => Charts._navKey(d));

    container.innerHTML = `
      <div class="nav-range">
        <div class="nav-presets">
          ${PRESETS.map(p => `<button type="button" class="nav-preset" data-r="${p.key}">${p.label}</button>`).join("")}
        </div>
        <div class="nav-dates">
          <input type="date" class="nav-from" aria-label="From">
          <span class="nav-arrow">→</span>
          <input type="date" class="nav-to" aria-label="To">
          <button type="button" class="btn btn-outline btn-sm nav-apply">Apply</button>
        </div>
      </div>
      <div class="nav-plot">
        <canvas class="nav-canvas"></canvas>
        <div class="nav-tip" hidden></div>
      </div>`;

    const canvas = container.querySelector(".nav-canvas");
    const tip = container.querySelector(".nav-tip");
    const plot = container.querySelector(".nav-plot");
    const dpr = window.devicePixelRatio || 1;

    const state = { i0: 0, i1: dates.length - 1, active: "all", hover: null, from: null, to: null };
    const _activePreset = container.querySelector('[data-r="all"]');
    if (_activePreset) _activePreset.classList.add("active");

    function setPreset(p) {
      state.active = p.key;
      container.querySelectorAll(".nav-preset").forEach(b =>
        b.classList.toggle("active", b.dataset.r === p.key));
      if (p.days === 0) {
        state.i0 = 0; state.i1 = dates.length - 1;
      } else {
        const lastKey = keys[keys.length - 1];
        const from = new Date(lastKey.slice(0, 10));
        from.setDate(from.getDate() - p.days);
        const fromKey = from.toISOString().slice(0, 10);
        state.i0 = 0;
        for (let i = 0; i < keys.length; i++) { if (keys[i] >= fromKey) { state.i0 = i; break; } }
        state.i1 = dates.length - 1;
      }
      state.from = null; state.to = null;
      const fromEl = container.querySelector(".nav-from"), toEl = container.querySelector(".nav-to");
      if (fromEl) fromEl.value = ""; if (toEl) toEl.value = "";
      render();
    }

    function applyCustom() {
      const fromEl = container.querySelector(".nav-from"), toEl = container.querySelector(".nav-to");
      const from = Charts._navKey(fromEl.value), to = Charts._navKey(toEl.value);
      if (!from && !to) { setPreset(PRESETS[PRESETS.length - 1]); return; }
      state.active = null;
      container.querySelectorAll(".nav-preset").forEach(b => b.classList.remove("active"));
      state.i0 = 0; state.i1 = dates.length - 1;
      if (from) { for (let i = 0; i < keys.length; i++) { if (keys[i] >= from) { state.i0 = i; break; } } }
      if (to) { for (let i = keys.length - 1; i >= 0; i--) { if (keys[i] <= to) { state.i1 = i; break; } } }
      state.from = from || null; state.to = to || null;
      render();
    }

    function render() {
      const rect = plot.getBoundingClientRect();
      const w = Math.max(rect.width || 320, 320), h = height;
      canvas.width = w * dpr; canvas.height = h * dpr;
      canvas.style.height = h + "px";
      canvas.style.width = w + "px";
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const count = state.i1 - state.i0 + 1;
      const subD = dates.slice(state.i0, state.i1 + 1);
      const subN = navs.slice(state.i0, state.i1 + 1);
      const pad = { l: 54, r: 14, t: 16, b: 30 };
      const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
      // Full-resolution scale (kept consistent for line + hover marker).
      const lo = Math.min(...subN), hi = Math.max(...subN);
      const span = (hi - lo) || 1;
      const loPad = lo - span * 0.1, hiPad = hi + span * 0.1;
      const X = (i) => pad.l + (i / (count - 1)) * plotW;
      const Y = (v) => pad.t + plotH - ((v - loPad) / (hiPad - loPad)) * plotH;

      const ds = Charts._downsample(subD, subN, 1600);
      let hover = null;
      if (state.hover != null) {
        const idx = Math.max(0, Math.min(count - 1, state.hover - state.i0));
        hover = { x: X(idx), y: Y(subN[idx]) };
      }
      Charts._renderLineChart(ctx, w, h, ds.dates, ds.navs, loPad, hiPad, color, hover);
    }

    function onMove(e) {
      const rect = canvas.getBoundingClientRect();
      const padL = 54, plotW = rect.width - padL - 14;
      const frac = (e.clientX - rect.left - padL) / plotW;
      const count = state.i1 - state.i0 + 1;
      let idx = Math.round(frac * (count - 1));
      idx = Math.max(0, Math.min(count - 1, idx));
      const abs = state.i0 + idx;
      state.hover = abs;
      render();
      const nav = navs[abs], date = dates[abs];
      const x = padL + (idx / (count - 1)) * plotW;
      tip.hidden = false;
      tip.style.left = (x + 14) + "px";
      tip.innerHTML =
        `<div class="tip-date">${App.esc(date)}</div>` +
        `<div class="tip-nav">${App.esc(formatNav(nav))}</div>`;
    }

    function onLeave() { state.hover = null; tip.hidden = true; render(); }

    container.querySelectorAll(".nav-preset").forEach(b =>
      b.addEventListener("click", () => setPreset(PRESETS.find(p => p.key === b.dataset.r))));
    container.querySelector(".nav-apply").addEventListener("click", applyCustom);
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("mousedown", e => { void e; });

    // responsive re-render
    const rerender = () => render();
    mountChart(rerender);

    setPreset(PRESETS[PRESETS.length - 1]); // default: All

    return {
      render,
      setRange(fromKey, toKey) {
        const f = Charts._navKey(fromKey), t = Charts._navKey(toKey);
        const fromEl = container.querySelector(".nav-from"), toEl = container.querySelector(".nav-to");
        if (fromEl) fromEl.value = f; if (toEl) toEl.value = t;
        state.active = null;
        container.querySelectorAll(".nav-preset").forEach(b => b.classList.remove("active"));
        state.i0 = 0; state.i1 = dates.length - 1;
        if (f) { for (let i = 0; i < keys.length; i++) { if (keys[i] >= f) { state.i0 = i; break; } } }
        if (t) { for (let i = keys.length - 1; i >= 0; i--) { if (keys[i] <= t) { state.i1 = i; break; } } }
        render();
      },
    };
  },
};

const chartCanvases = [];

function mountChart(fn) { chartCanvases.push(fn); }
window.addEventListener("resize", () => chartCanvases.forEach(fn => fn()));
function rerenderCharts() { chartCanvases.forEach(fn => fn()); }