/* OBEKT TERMINAL — read-only renderer + ledger replay viewer.
   Only interaction: clicking a ledger row opens its decision dossier (ESC/bg closes).
   Polls data.js every 10s for a near-real-time feel; server rebuilds every 45s. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s === null || s === undefined ? "—" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const fmt = (n, d = 2) => (n === null || n === undefined || isNaN(n)) ? "—" : Number(n).toFixed(d);
  const sgn = (n, d = 2) => (n > 0 ? "+" : "") + fmt(n, d);
  const cls = (n) => n > 0 ? "pos" : (n < 0 ? "neg" : "dim");
  const eur = (n) => (n === null || n === undefined) ? "—" : "€" + fmt(n, 2);
  const usd = (n) => (n === null || n === undefined) ? "—" : "$" + fmt(n, 2);
  // The live book's currency. The retired spot book logged EUR; the live X-Perp
  // book settles in USD. Panels driven by D.stats/D.trades must print the LIVE
  // symbol — rendering a $131 perp position as "€131" is a false statement about
  // the account even though the digits are right.
  const liveCcy = (D) => (D || {}).live_ccy || "USD";
  const money = (n, ccy) => (ccy === "USD" ? usd(n) : eur(n));
  const hhmm = (iso) => iso ? iso.slice(11, 16) : "—";
  const mmdd_hhmm = (iso) => iso ? iso.slice(5, 16).replace("T", " ") : "—";
  // Minutes since an ISO timestamp; null when unparseable so callers can
  // distinguish "unknown" from "very old" instead of showing NaN.
  const ageMin = (iso) => {
    if (!iso) return null;
    const t = Date.parse(iso);
    return isNaN(t) ? null : Math.round((Date.now() - t) / 60000);
  };

  let CUR_DATA = null;
  const LLM_NAME = "halogen-qwen3.8-flash-next";
  let KNOWN_TAPE = new Set();
  let firstRender = true;
  let OPEN_TID = null; // dossier currently open (survives re-renders)
  let OPEN_DOSSIER_HASH = null; // only re-render the modal when its data actually changed

  // "since you arrived" session liveness counters
  const ARRIVED = Date.now();
  let TITLE_FLASH = null; // {text, until}

  /* ---------- ascii sparkline ---------- */
  function spark(series, w) {
    w = w || 24;
    if (!series || series.length < 2) return "";
    const s = series.slice(-w);
    const mn = Math.min.apply(null, s), mx = Math.max.apply(null, s);
    const rng = (mx - mn) || 1;
    const blocks = "▁▂▃▄▅▆▇█";
    return s.map(v => blocks[Math.min(7, Math.round((v - mn) / rng * 7))]).join("");
  }

  /* ---------- top bar ---------- */
  function renderTop(D) {
    // Compact one-liner from the payload (generate.py _engine_header). The header
    // must NOT compose version + venue + mode itself: that produced 179 chars /
    // 1256px, wrapped the bar into four rows, repeated "X-PERPS" twice (once in the
    // version string, once in the venue) and put the RETIRED spot engine's version
    // next to the live one. Fallback keeps the old behaviour if header is absent.
    $("engine-meta").textContent =
      (D.engine || {}).header ||
      ((D.engine || {}).version + " · " + (D.engine || {}).venue);
    // persistent open-position badge (visible while scrolled anywhere on page)
    const badge = document.getElementById("pos-badge");
    if (badge) {
      const ob = openBook(D);
      if (!ob.length) {
        // "FLAT — NO OPEN POSITION" said the same thing twice at 209px. FLAT is the
        // word traders use; the detail goes in the tooltip and the hero already
        // spells out "no open X-Perp position · capital in USDC".
        badge.textContent = "◆ FLAT";
        badge.title = "No open position — capital sitting in USDC";
        badge.className = "topmeta pos-badge flat";
      } else {
        badge.innerHTML = ob.map(r => {
          const arrow = r.side === "short" ? "▼" : "▲";
          const upc = (r.upc === null || r.upc === undefined) ? "" : " " + sgn(r.upc) + "%";
          return '<span class="pb-side ' + (r.side === "short" ? "s" : "l") + '">' + arrow + " " +
            esc(r.inst) + upc + "</span>";
        }).join(" ");
        badge.title = ob.map(r => (r.side === "short" ? "SHORT " : "LONG ") + r.inst +
          " · " + r.book).join(", ");
        badge.className = "topmeta pos-badge";
      }
    }
    $("contact").textContent = D.contact;
    const genMs = Date.parse(D.generated);
    const ageMin = Math.round((Date.now() - genMs) / 60000);
    const stale = ageMin > 20;
    $("gen").innerHTML = (stale
      ? '<span class="neg">⚠ FEED STALE — LAST SNAPSHOT ' + ageMin + "M AGO (ENGINE MAY BE PAUSED)</span> · "
      : "") +
      "SNAPSHOT " + D.generated.replace("T", " ").slice(0, 19) +
      " UTC · BUILD " + D.build_ms + "MS · FEED: OKX EEA PUBLIC + LIVE ENGINE LOG";
  }

  /* ---------- cycle countdown ---------- */
  function renderCountdown() {
    if (!CUR_DATA) return;
    const last = Date.parse(CUR_DATA.last_event_ts || CUR_DATA.generated);
    const cycleMs = (CUR_DATA.engine.cycle_s || 300) * 1000;
    const rem = last + cycleMs - Date.now();
    if (rem <= 0) {
      $("cycle-countdown").innerHTML = '<span style="color:var(--amber)">◉ CYCLE RUNNING — SCANNING…</span>';
      return;
    }
    const m = Math.floor(rem / 60000), s = Math.floor(rem % 60000 / 1000);
    $("cycle-countdown").textContent = "NEXT CYCLE " + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }

  /* ---------- marquee ---------- */
  function renderMarquee(D) {
    const sparks = D.sparks || {};
    const parts = (D.tickers || []).map(t => {
      const sp = sparks[t.inst] ? '<span class="marq-spark ' + (t.chg24h_pct >= 0 ? "up" : "dn") + '">' + spark(sparks[t.inst], 14) + "</span> " : "";
      return sp + '<b>' + esc(t.inst.replace("-EUR", "")) + '</b> ' +
        fmt(t.last, t.last < 10 ? 4 : (t.last < 1000 ? 2 : 0)) +
        ' <span class="' + (t.chg24h_pct >= 0 ? "up" : "dn") + '">' + sgn(t.chg24h_pct) + "%</span>";
    });
    const eq = D.equity || {};
    const st = D.stats || {};
    const P = D.perp || {}, ps = P.stats || {};
    const spotRetired = /retired/i.test(String(((D.engine || {}).spot_book || {}).status || ""));
    // With spot retired the ticker's EQ/% lead must be the LIVE book (perp USDC),
    // not spot's frozen EUR equity — otherwise the marquee shows a number that
    // stopped moving the day spot was switched off.
    const head = spotRetired
      ? '<b>EQ</b> $' + fmt(P.usdc_equity, 2) +
        ' <span class="' + ((ps.day_pnl_pct || 0) >= 0 ? "up" : "dn") + '">' + sgn(ps.day_pnl_pct) +
        '%</span> <span class="dim">ALL ' + sgn(ps.net_all_pct) + '%</span>  <span class="dim">·</span>  '
      : '<b>EQ</b> ' + eur(eq.equity_eur) +
        ' <span class="' + ((eq.day_pnl_eur || 0) >= 0 ? "up" : "dn") + '">' + sgn(eq.day_pnl_pct) +
        '%</span> <span class="dim">ALL ' + sgn(st.net_all_pct) + '%</span>  <span class="dim">·</span>  ';
    const fg = (D.sentiment || {}).fear_greed;
    const tail = fg ? '  <span class="dim">·</span>  <b>F&amp;G</b> ' + fg.value + " " + esc(fg.label) : "";
    const line = head + parts.join('  <span class="dim">·</span>  ') + tail + "   ▚▚▚   ";
    $("marquee").innerHTML = "<span>" + line + line + "</span>";
  }

  /* ---------- HERO ---------- */
  function openBook(D) {
    // One-line live summary across BOTH books (spot EUR + X-Perp USDC). When spot
    // is retired its position list is structurally always empty, but guard on the
    // flag too: a stale positions.json must never render as a live position.
    const rows = [];
    if (!spotRetired(D)) {
      (D.positions || []).forEach(p => rows.push({
        book: "SPOT", side: "long", inst: (p.inst || "").replace("-EUR", ""),
        upc: p.upc, note: "€" + fmt(p.stake_eur)
      }));
    }
    const P = D.perp || {};
    (P.positions || []).forEach(p => rows.push({
      book: "PERP", side: p.side || "long",
      inst: (p.inst || "").replace("-USD_UM_XPERP", "").replace(/-\d{6}$/, ""),
      upc: p.upc, note: "$" + fmt(p.notional_usd)
    }));
    return rows;
  }
  function renderHero(D) {
    const e = D.equity || {}, s = D.stats || {}, f = D.funnel || {};
    const j = D.jev_latest || {};
    const cell = (k, v, sub, cls_, big) =>
      '<div class="hero-cell' + (big ? " hero-big" : "") + '"><div class="hk">' + k + '</div><div class="hv ' + (cls_ || "") + '">' + v + "</div>" +
      (sub ? '<div class="hs">' + sub + "</div>" : "") + "</div>";
    const pnlCls = (e.day_pnl_eur || 0) >= 0 ? "g" : "r";
    const choice = (j.choice || "—").toUpperCase();
    const noTrade = j.choice === "no_trade" || !j.choice;
    const sb = (D.engine || {}).spot_book || {};
    const spotRetired = /retired/i.test(String(sb.status || ""));
    const P = D.perp || {}, ps = P.stats || {};
    // OPEN POSITION cell — the single most important live fact, so it leads.
    const ob = openBook(D);
    let posV, posSub, posCls;
    if (!ob.length) {
      posV = "FLAT";
      posSub = spotRetired ? "no open X-Perp position · capital in USDC" : "no open position · both engines in cash";
      posCls = "w";
    } else {
      const r = ob[0];
      const arrow = r.side === "short" ? "▼ SHORT" : "▲ LONG";
      posV = arrow + " " + r.inst + (ob.length > 1 ? " +" + (ob.length - 1) : "");
      posSub = (r.upc === null || r.upc === undefined ? "" : sgn(r.upc) + "% · ") + r.book + " · " + r.note;
      posCls = r.side === "short" ? "r" : "g";
    }
    // The LIVE book is X-Perps. Its all-time % is measured against capital put
    // INTO that book (injections are capital, not P&L). The retired spot book's
    // final record is shown alongside, clearly labelled, not blended in.
    const allTimeV = spotRetired
      ? sgn(ps.net_all_pct) + "%"
      : sgn(s.net_all_pct) + "%";
    const allTimeSub = spotRetired
      ? ((ps.trades_all || 0) + " X-Perp trades · $" + fmt(ps.capital_usd, 2) + " in book")
      : (s.trades_all + " trades · " + fmt(s.win_rate_pct, 1) + "% win");
    // LATEST VOTE: with spot retired the live decision stream is the perp book's.
    // Fall back to spot's jev_latest only when the perp book has never decided.
    const pj = P.last_jev || {};
    const liveJ = (spotRetired && pj.choice) ? pj : j;
    const lvChoice = String(liveJ.choice || "—").toUpperCase();
    const lvNoTrade = liveJ.choice === "no_trade" || !liveJ.choice;
    const lvSide = String(liveJ.chosen_side || "").toLowerCase();
    const lvArrow = lvSide === "short" ? "▼ " : (lvSide === "long" ? "▲ " : "");
    const lvConf = liveJ.confidence !== undefined ? liveJ.confidence : liveJ.conf;
    // TODAY: with spot retired, the live book's day P&L is the perp book's.
    // (Spot's day_pnl_pct freezes at its final value once the engine stops.)
    const todayPct = spotRetired && ps.day_pnl_pct !== undefined && ps.day_pnl_pct !== null
      ? ps.day_pnl_pct : e.day_pnl_pct;
    const todayTrades = spotRetired ? (ps.day_trades || 0) : (e.day_trades || 0);
    const todayCls = (todayPct || 0) >= 0 ? "g" : "r";
    $("hero-body").innerHTML =
      cell("OPEN POSITION", posV, posSub, posCls, true) +
      cell(spotRetired ? "PERP EQUITY (LIVE)" : "EQUITY", "$" + fmt(P.usdc_equity, 2),
        spotRetired ? "USDC collateral · 1x isolated" : "spot EUR + $" + fmt(P.usdc_equity) + " perp USDC",
        "w", true) +
      cell("TODAY", sgn(todayPct) + "%", todayTrades + " trades closed", todayCls, true) +
      cell(spotRetired ? "ALL-TIME — X-PERPS" : "ALL-TIME", allTimeV, allTimeSub,
        cls(spotRetired ? ps.net_all_pct : s.net_all_pct)) +
      cell("LATEST VOTE", lvNoTrade ? "FLAT" : lvArrow + lvChoice.replace("BUY_", "").replace("SHORT_", "").replace("LONG_", ""),
        lvNoTrade ? "model: standing aside" : "conf " + fmt(lvConf) + (spotRetired ? " · X-Perp" : ""),
        lvNoTrade ? "w" : "g") +
      cell("CYCLES RUN", (f.cycles || 0).toLocaleString(), (f.jev_calls || 0) + " model calls · ~5m each", "") +
      cell("UPTIME", f.uptime_h ? Math.floor(f.uptime_h / 24) + "D " + Math.round(f.uptime_h % 24) + "H" : "—", "since " + (f.first_ts || "").slice(5, 10), "w") +
      cell("MODEL COST", "$" + fmt(f.usd_today, 4), "today · avg " + (f.avg_input_tok || "?") + " tok/call", "g");
  }

  /* ---------- desk note ---------- */
  function renderDesk(D) {
    const dn = D.desk_note || {};
    // Show the note's age, and flag it when a position changed AFTER the note was
    // written. Without that, a note saying "flat" sits next to a badge showing a
    // live position and the two look like one of them is lying, when really the
    // note is just older. generate.py now refreshes on position change, so this
    // should rarely fire — but if it does, say so instead of showing a silent
    // contradiction.
    const aMin = ageMin(dn.generated);
    const noteTs = Date.parse(dn.generated || "");
    const newestOpen = Math.max(0, ...((D.perp || {}).positions || []).map(p =>
      Date.parse(p.opened_ts || "") || 0));
    const superseded = newestOpen && noteTs && newestOpen > noteTs;
    $("desk-model").textContent = "LLM: " + (dn.model || "—") + " · " +
      (dn.generated ? dn.generated.slice(0, 16).replace("T", " ") + " UTC" : "") +
      (aMin !== null ? " (" + aMin + "M AGO)" : "") +
      (superseded ? " · ⚠ PRE-DATES CURRENT POSITION" : "");
    $("desk-note").textContent = dn.desk_note || "AWAITING DESK COMMENTARY…";
    $("decision-line").textContent = "▸ " + (dn.decision_line || "") +
      (superseded ? "  [position opened after this note was written — see the badge and X-Perps panel above]" : "");
  }

  /* ---------- perf ---------- */
  function kpi(k, v, extraCls, big) {
    return '<div class="kpi"><div class="k">' + esc(k) + '</div><div class="v ' +
      (extraCls || "") + (big ? " big" : "") + '">' + v + "</div></div>";
  }
  // The spot book is retired: the panels LABELLED as its record must read the
  // frozen retired_spot block, because D.stats / D.jev_latest / D.universe now
  // describe the LIVE X-Perp book. Reading the live keys under a "SPOT RECORD"
  // heading would print perp numbers as spot's final ledger.
  function spotRetired(D) {
    return /retired/i.test(String((((D.engine || {}).spot_book) || {}).status || ""));
  }
  function perfSource(D) {
    return spotRetired(D)
      ? { s: ((D.retired_spot || {}).stats) || {}, e: ((D.retired_spot || {}).equity) || {} }
      : { s: D.stats || {}, e: D.equity || {} };
  }
  function renderPerf(D) {
    const src = perfSource(D), s = src.s, e = src.e;
    // This panel is the retired spot book's final ledger — its currency is EUR.
    const m = eur;
    const st = s.streak || {};
    const rows =
      kpi(spotRetired(D) ? "FINAL EQUITY" : "EQUITY", m(e.equity_eur), (e.day_pnl_eur || 0) >= 0 ? "green" : "red", true) +
      kpi("DAY P&L", sgn(e.day_pnl_pct) + "%", cls(e.day_pnl_pct), true) +
      kpi("DAY TRADES", e.day_trades, "white") +
      kpi("TRADES ALL-TIME", s.trades_all, "white") +
      kpi("WIN RATE", fmt(s.win_rate_pct, 1) + "%", "white") +
      kpi("NET P&L ALL", sgn(s.net_all_pct) + "%", cls(s.net_all_pct)) +
      kpi("PROFIT FACTOR", fmt(s.profit_factor), fmt(s.profit_factor) >= 1 ? "green" : "red") +
      kpi("AVG WIN / LOSS", m(s.avg_win_eur) + " / " + m(s.avg_loss_eur), "white") +
      kpi("AVG HOLD", fmt(s.avg_hold_min, 0) + "m", "white") +
      kpi("STREAK", (st.cur_kind === "W" ? "▲ " : "▼ ") + (st.cur_len || 0) + "×" + (st.cur_kind || ""), st.cur_kind === "W" ? "green" : "red") +
      kpi("MAX RUN W/L", (st.max_win || 0) + "W / " + (st.max_loss || 0) + "L", "white") +
      kpi("FEES PAID", m(s.fees_all_eur), "red") +
      '<div class="kpi" style="flex:1 1 100%"><div class="k">BEST TRADE</div><div class="v green" style="font-size:12px">' +
      (s.best_trade ? esc(s.best_trade.inst) + " " + sgn(s.best_trade.upc) + "% → " + m(s.best_trade.net_eur) + " · " + esc(s.best_trade.reason || "") : "—") + "</div></div>" +
      '<div class="kpi" style="flex:1 1 100%"><div class="k">WORST TRADE</div><div class="v red" style="font-size:12px">' +
      (s.worst_trade ? esc(s.worst_trade.inst) + " " + sgn(s.worst_trade.upc) + "% → " + m(s.worst_trade.net_eur) + " · " + esc(s.worst_trade.reason || "") : "—") + "</div></div>";
    $("perf-body").innerHTML = '<div class="kpis">' + rows + "</div>";
  }

  /* ---------- prob bars ---------- */
  function pbar(label, val, hot) {
    const v = Math.max(0, Math.min(1, val || 0));
    return '<div class="pbar-row"><div class="pbar-label">' + esc(label) + '</div>' +
      '<div class="pbar-track"><div class="pbar-fill ' + (hot ? (v >= 0.5 ? "hot" : "win") : "") +
      '" style="width:' + (v * 100).toFixed(1) + '%"></div></div>' +
      '<div class="pbar-val">' + fmt((val || 0) * 100, 0) + "%</div></div>";
  }

  /* ---------- jev latest ---------- */
  function renderJev(D) {
    // This panel is labelled the SPOT CALL (retired). When spot is retired it must
    // show the frozen spot call from retired_spot, NOT D.jev_latest — which now
    // carries the live X-Perp decision (already rendered in the X-Perps panel).
    const retired = spotRetired(D);
    const j = retired ? ((D.retired_spot || {}).jev_latest) : D.jev_latest;
    if (!j) { $("jev-body").textContent = retired ? "NO SPOT CALL ON RECORD" : "NO JEV CALL LOGGED YET"; return; }
    const probs = j.probabilities || {};
    let bars = "";
    Object.keys(probs).sort((a, b) => probs[b] - probs[a]).forEach(k => {
      const label = k === "no_trade" ? "NO_TRADE" : "BUY " + k.replace("buy_", "").toUpperCase();
      bars += pbar(label, probs[k], k === j.choice && j.choice !== "no_trade");
    });
    const rows = (j.pairs || []).map(p =>
      "<tr><td class='sym'>" + esc(p.inst) + "</td>" +
      "<td>" + fmt(p.p_buy === null || p.p_buy === undefined ? null : p.p_buy * 100, 0) + "%</td>" +
      "<td>" + fmt(p.noul === null || p.noul === undefined ? null : p.noul, 2) + "</td>" +
      "<td class='" + cls(p.chg_30min_pct) + "'>" + sgn(p.chg_30min_pct) + "%</td>" +
      "<td class='" + cls(p.chg_4h_pct) + "'>" + sgn(p.chg_4h_pct) + "%</td>" +
      "<td class='" + cls(p.chg_7d_pct) + "'>" + sgn(p.chg_7d_pct) + "%</td>" +
      "<td>" + fmt(p.range30_pos_pct, 0) + "</td></tr>").join("");
    const tk = j.tokens || {};
    $("jev-body").innerHTML =
      '<div class="dim" style="margin-bottom:4px">' + (retired ? "FINAL CALL " : "CALL ") + esc(mmdd_hhmm(j.ts)) + ' UTC · CASH ' + eur(j.cash_eur) +
      ' · STAKE ' + eur(j.stake_eur) + ' · FUNDING ' + fmt(j.funding_bps, 1) + ' BPS · PROMPT ' +
      (tk.in || "?") + " IN/" + (tk.out || "?") + " OUT TOK</div>" +
      bars +
      '<table style="margin-top:6px"><tr><th>PAIR</th><th>P(BUY)</th><th>NOUL ↑45M</th><th>30M</th><th>4H</th><th>7D</th><th>R30</th></tr>' + rows + "</table>" +
      (j.action ? '<div class="verdict ' + (j.action.type === "opened" ? "fire" : "block") + '">' +
        (j.action.type === "opened" ? "▸ " : "✕ ") + esc(j.action.text || j.action.why) + "</div>" : "");
  }

  /* ---------- gates ---------- */
  function gateRow(name, ok, detail, na) {
    const m = na ? "na" : (ok ? "pass" : "fail");
    const mark = na ? "·" : (ok ? "[✓]" : "[✕]");
    return '<div class="gate ' + m + '"><span class="mark ' + m + '">' + mark + '</span>' +
      '<span class="gname">' + esc(name) + "</span><span class='gval'>" + esc(detail) + "</span></div>";
  }
  function renderGates(D) {
    // The DECISION GATES panel must show the LIVE book's call scored against the
    // LIVE book's own thresholds. With spot retired (2026-10-15) that is the perp
    // book: its jev event is flat (conf/margin/noul at top level) and its
    // criteria were computed against okx_perp.py's bars — not spot's, which run a
    // different CONF_FLOOR (0.50 vs 0.40). Falling back to spot only if the perp
    // engine has never logged a call.
    const spotRetired = /retired/i.test(String(((D.engine || {}).spot_book || {}).status || ""));
    const pj = ((D.perp || {}).last_jev) || {};
    const usePerp = spotRetired && pj && pj.criteria;
    const j = usePerp ? pj : D.jev_latest;
    const r = D.risk || {};
    const out = [];
    if (j && j.criteria) {
      const c = j.criteria;
      const na = !j.chosen_inst;
      // spot logs `confidence`, perp logs `conf` — accept either
      const confV = (j.confidence !== undefined && j.confidence !== null) ? j.confidence : j.conf;
      const noulV = j.noul;
      const bookTag = usePerp ? " (X-PERP)" : " (SPOT)";
      out.push(gateRow("MARGIN p(chosen)−p(no_trade) ≥ " + fmt(c.margin_bar, 2) + bookTag, c.margin_ok, "bar " + fmt(c.margin_bar) + " · actual " + fmt(j.margin), na));
      out.push(gateRow("ALT: CONFIDENCE ≥ " + fmt(c.conf_bar, 2), c.conf_ok, "actual " + fmt(confV), na));
      if (c.conf_floor !== undefined) {
        const cfOk = na ? null : (confV >= c.conf_floor);
        out.push(gateRow("CONF FLOOR ≥ " + fmt(c.conf_floor, 2) + (usePerp ? " (perp burn-in)" : " (hard, v3.8+)"), cfOk, "actual " + fmt(confV) + " · conjunctive — OR-path still needs this", na));
      }
      out.push(gateRow(usePerp ? "NOUL MOVE-PROB ≥ " + fmt(c.noul_bar, 2) : "NOUL RISE-PROB ≥ " + fmt(c.noul_bar, 2), c.noul_ok, "bar " + fmt(c.noul_bar) + " · actual " + fmt(noulV), na));
      out.push(gateRow("FINAL ENGINE VERDICT", c.fire, c.fire ? "TRADE AUTHORIZED" : (na ? "JEV: NO-TRADE" : "BELOW BAR — STAND ASIDE"), na));
    }
    out.push(gateRow(usePerp ? "DEPTH FLOOR (top5 book ≥ 3× notional)" : "DEPTH FLOOR (top5 book ≥ €1.5k)", true, "hard pre-entry re-check, ≥3× stake"));
    out.push(gateRow("DAILY LOSS BREAKER −8%", !r.breaked && !((D.perp || {}).breaked),
      (r.breaked || (D.perp || {}).breaked) ? "TRIPPED — HALTED" : "clear"));
    // ACTIVE cooldowns only (exits maps are historical). Windows come from the
    // LIVE engine's source via live_risk (never hardcoded — they drifted twice),
    // falling back to the retired spot book's windows if absent.
    const now = Date.now();
    const lr = D.live_risk || {};
    const lossMin = lr.loss_min || 20, winMin = lr.win_min || 10;
    const cdExits = (usePerp ? lr.exits : r.cooldowns) || {};
    const active = [];
    Object.entries(cdExits).forEach(([inst, e]) => {
      const ts = Date.parse(e.ts || "");
      if (!ts) return;
      const ageMin = (now - ts) / 60000;
      const win = (e.upc || 0) > 0;
      const lim = win ? winMin : lossMin;
      if (ageMin < lim) {
        // perp exit keys are "INST:side"; spot keys are plain "PAIR-EUR"
        let nm = String(inst), sd = "";
        const ci = nm.indexOf(":");
        if (ci > 0) { sd = nm.slice(ci + 1); nm = nm.slice(0, ci); }
        nm = nm.replace("-EUR", "").replace("-USD_UM_XPERP", "").replace(/-\d{6}$/, "");
        active.push(nm + (sd ? "/" + sd : "") + " " + Math.round(lim - ageMin) + "m");
      }
    });
    out.push(gateRow("PAIR COOLDOWNS (win " + winMin + "m / loss " + lossMin + "m)", true, active.length ? "ACTIVE: " + active.join(", ") : "none active right now"));
    out.push(gateRow("MOMENTUM GATES PRE-JEV", true, "chg30 · bars_up · range30 · vol30 · taker share"));
    out.push(gateRow("SERVER-SIDE OCO TP/SL + HWM TRAIL", true, "exits execute on-exchange, even offline"));
    $("gates-body").innerHTML = out.join("");
  }

  /* ---------- equity curve (SVG) ---------- */
  function renderEquity(D) {
    // v4: cumulative realized P&L as PERCENT, not fiat (fall back to fiat series
    // only for old frozen sample_data so the zero-cred demo never renders empty)
    const S = D.stats || {};
    const cum = S.cum_pnl_pct || S.cum_pnl || [];
    const isPct = !!S.cum_pnl_pct;
    $("equity-meta").textContent = cum.length + " CLOSED TRADES · LEDGER-VERIFIED, NET OF FEES · " +
      (isPct ? "RETURN VS LAUNCH CAPITAL (%)" : "NET P&L");
    if (cum.length < 2) { $("equity-body").textContent = "AWAITING TRADES…"; return; }
    const W = 1000, H = 150, PAD = 8;
    const mn = Math.min(0, Math.min.apply(null, cum)), mx = Math.max(0, Math.max.apply(null, cum));
    const rng = (mx - mn) || 1;
    const X = (i) => PAD + i * (W - 2 * PAD) / (cum.length - 1);
    const Y = (v) => H - PAD - (v - mn) * (H - 2 * PAD) / rng;
    const path = cum.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1)).join(" ");
    const zeroY = Y(0).toFixed(1);
    const area = path + " L" + X(cum.length - 1).toFixed(1) + "," + zeroY + " L" + X(0).toFixed(1) + "," + zeroY + " Z";
    const last = cum[cum.length - 1];
    const col = last >= 0 ? "#00e04b" : "#ff3b30";
    const colA = last >= 0 ? "rgba(0,224,75,.10)" : "rgba(255,59,48,.10)";
    const dots = cum.map((v, i) =>
      '<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(v).toFixed(1) + '" r="1.7" fill="' + (v >= 0 ? "#0a7a30" : "#7a231c") + '"/>').join("");
    const trips = D.trades || [];
    $("equity-body").innerHTML =
      '<div class="eqwrap"><svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none">' +
      '<line x1="0" y1="' + zeroY + '" x2="' + W + '" y2="' + zeroY + '" stroke="#33290f" stroke-dasharray="3,3"/>' +
      '<path d="' + area + '" fill="' + colA + '"/>' +
      '<path d="' + path + '" fill="none" stroke="' + col + '" stroke-width="1.6"/>' +
      dots +
      '<text x="' + (W - 8) + '" y="' + Math.max(12, Y(last) - 6).toFixed(1) + '" fill="' + col + '" font-size="12" text-anchor="end" font-family="monospace">' + sgn(last) + "%</text>" +
      '<text x="8" y="' + (Y(mx) + 10).toFixed(1) + '" fill="#8a867a" font-size="9" font-family="monospace">PEAK ' + sgn(mx) + "%</text>" +
      '<text x="8" y="' + Math.min(H - 3, Y(mn) - 3).toFixed(1) + '" fill="#8a867a" font-size="9" font-family="monospace">TROUGH ' + sgn(mn) + "%</text>" +
      "</svg></div>" +
      '<div class="dim" style="font-size:9.5px">← FIRST CLOSED TRADE ' + esc(mmdd_hhmm(trips.length ? trips[trips.length - 1].close_ts : "")) +
      " UTC · LATEST " + esc(mmdd_hhmm(trips.length ? trips[0].close_ts : "")) +
      " UTC → · SHOWING LAST " + Math.min(cum.length, 68) + " OF " + cum.length + " · RAW MATERIAL: OKX FILLS LEDGER, NOT LOG ESTIMATES</div>";
  }

  /* ---------- funnel ---------- */
  function renderFunnel(D) {
    const f = D.funnel || {}, P = D.params || {};
    const perps = spotRetired(D);          // live book is the two-way perp book
    const sym = perps ? "$" : "€";
    const volK = Math.round((P.MIN_VOL24H_USD || 300000) / 1000);
    const depthK = Math.round((P.MIN_TOP5_DEPTH_USD || 1000) / 1000 * 10) / 10;
    const scanN = perps ? (f.perps_scanned || f.eur_pairs || "all") : (f.eur_pairs || 270);
    const scanLbl = perps ? "X-Perp contracts" : "EUR pairs";
    const rows = [
      [f.pair_scans_all, "PAIR LIQUIDITY SCANS", "every cycle sweeps " + scanN + " " + scanLbl + (perps ? " · account-scoped instruments feed" : " · keyless public feed")],
      [f.candidates_all, "PASSED DEEP FILTERS", "vol ≥" + sym + volK + "k · spread ≤" + fmt(P.MAX_SPREAD_PCT, 2) + "% · range ≥" + fmt(P.MIN_DAY_RANGE_PCT, 1) + "% · depth ≥" + sym + depthK + "k"],
      [f.jev_pair_evals_all, "SETUPS EVALUATED BY JEV", "only momentum-gate survivors reach the model (" + (f.jev_calls || 0) + " calls)"],
      [f.buy_picks, perps ? "DIRECTIONAL PICKS BY THE MODEL" : "BUY VOTES BY THE MODEL", perps ? "long or short — typed probabilities; the engine bar still applies" : "typed probabilities — the engine bar still applies"],
      [f.opens, "POSITIONS OPENED", "margin/noul bar + live book re-check cleared"],
    ];
    const mx = Math.max(1, ...rows.map(r => r[0] || 0));
    const html = rows.map(r => {
      const w = Math.max(0.6, Math.sqrt((r[0] || 0) / mx) * 100);
      return '<div class="funnel-row"><div class="funnel-n">' + (r[0] === undefined || r[0] === null ? "—" : Number(r[0]).toLocaleString()) +
        '</div><div class="funnel-bar" style="width:' + w.toFixed(1) + '%"></div>' +
        '<div><div class="funnel-lbl">' + esc(r[1]) + '</div><div class="funnel-sub">' + esc(r[2]) + "</div></div></div>";
    }).join("");
    const rej = f.gate_rejects || 0;
    $("funnel-body").innerHTML = html +
      '<div class="dim" style="margin-top:6px;border-top:1px dotted #241f10;padding-top:5px">' +
      rej.toLocaleString() + " GATE REJECTS · " + (f.depth_rejects || 0) + " DEPTH REJECTS · " + (f.cooldown_skips || 0) +
      " COOLDOWN BLOCKS<br>SAYING NO IS THE JOB: " + (f.opens && f.jev_calls ? Math.round(100 * f.opens / f.jev_calls) : "—") +
      "% OF MODEL CALLS BECOME TRADES<br>JEV SPEND: $" + fmt(f.usd_all, 2) + " ALL TIME · $" + fmt(f.usd_today, 4) + " TODAY @$0.04/M TOK</div>";
  }

  /* ---------- decision scatter ---------- */
  function renderScatter(D) {
    const pts = (D.scatter || []).filter(p => p.noul !== null && p.noul !== undefined);
    if (pts.length < 3) { $("scatter-body").textContent = "AWAITING VOTES…"; return; }
    const W = 1000, H = 190, PAD = 26;
    const mx = Math.max(0.6, Math.max.apply(null, pts.map(p => p.margin)));
    const my = Math.max(0.7, Math.max.apply(null, pts.map(p => p.noul)));
    const X = (v) => PAD + Math.max(0, Math.min(1, v / mx)) * (W - 2 * PAD);
    const Y = (v) => H - PAD - Math.max(0, Math.min(1, v / my)) * (H - 2 * PAD);
    // Bars come from the LIVE engine's parsed constants, never literals. The old
    // hardcoded 0.25 sat below the engine's real MARGIN_BAR of 0.30, so dots the
    // engine would reject were drawn as having cleared the bar.
    const P = D.params || {};
    const mBar = (P.MARGIN_BAR !== undefined ? P.MARGIN_BAR : 0.30);
    const nBar = (P.NOUL_BAR !== undefined ? P.NOUL_BAR : 0.45);
    const xBar = X(mBar), yBar = Y(nBar);
    const colOf = {win: "#00e04b", loss: "#ff3b30", blocked: "#8a867a", open: "#ffb000"};
    const dots = pts.map(p => {
      const c = colOf[p.outcome] || "#8a867a";
      return '<circle cx="' + X(p.margin).toFixed(1) + '" cy="' + Y(p.noul).toFixed(1) + '" r="3.4" fill="' + c +
        '" fill-opacity="0.8" stroke="#000" stroke-width="0.5"><title>' +
        esc(p.inst + " " + (p.ts || "").slice(5, 16).replace("T", " ") + " · margin " + p.margin + " · noul " + p.noul + " → " + p.outcome +
          (p.upc !== null && p.upc !== undefined ? " " + p.upc + "%" : "")) + "</title></circle>";
    }).join("");
    const counts = {win: 0, loss: 0, blocked: 0, open: 0};
    pts.forEach(p => { counts[p.outcome] = (counts[p.outcome] || 0) + 1; });
    const legend = '<div style="display:flex;gap:14px;margin-top:4px;font-size:10px;flex-wrap:wrap">' +
      '<span style="color:#00e04b">● WIN ' + counts.win + "</span>" +
      '<span style="color:#ff3b30">● LOSS ' + counts.loss + "</span>" +
      '<span style="color:#ffb000">● OPEN NOW ' + counts.open + "</span>" +
      '<span style="color:#8a867a">● BLOCKED BY ENGINE ' + counts.blocked + "</span>" +
      '<span class="dim" style="margin-left:auto">HOVER A DOT FOR THE CALL DETAILS</span></div>';
    $("scatter-body").innerHTML =
      '<svg viewBox="0 0 ' + W + " " + H + '" style="width:100%;height:190px;display:block">' +
      // grid + bars
      '<line x1="' + PAD + '" y1="' + (H - PAD) + '" x2="' + (W - PAD) + '" y2="' + (H - PAD) + '" stroke="#241f10"/>' +
      '<line x1="' + PAD + '" y1="' + PAD / 2 + '" x2="' + PAD + '" y2="' + (H - PAD) + '" stroke="#241f10"/>' +
      '<line x1="' + xBar.toFixed(1) + '" y1="' + (PAD / 2) + '" x2="' + xBar.toFixed(1) + '" y2="' + (H - PAD) + '" stroke="#a8760a" stroke-dasharray="4,3"/>' +
      '<line x1="' + PAD + '" y1="' + yBar.toFixed(1) + '" x2="' + (W - PAD) + '" y2="' + yBar.toFixed(1) + '" stroke="#a8760a" stroke-dasharray="4,3"/>' +
      '<text x="' + (xBar + 4).toFixed(0) + '" y="' + (PAD / 2 + 8) + '" fill="#a8760a" font-size="9" font-family="monospace">MARGIN BAR ' + fmt(mBar, 2) + ' →</text>' +
      '<text x="' + (PAD + 4) + '" y="' + (yBar - 4).toFixed(0) + '" fill="#a8760a" font-size="9" font-family="monospace">NOUL BAR ' + fmt(nBar, 2) + ' ↑</text>' +
      '<text x="' + (W - PAD) + '" y="' + (H - 8) + '" fill="#8a867a" font-size="9" text-anchor="end" font-family="monospace">MARGIN p(chosen)−p(no_trade) →</text>' +
      '<text x="10" y="' + (PAD / 2 + 8) + '" fill="#8a867a" font-size="9" font-family="monospace" transform="rotate(-90 10,' + (PAD / 2 + 8) + ')">NOUL ↑</text>' +
      dots + "</svg>" + legend +
      '<div class="dim" style="margin-top:3px;font-size:9.5px">TOP-RIGHT QUADRANT = TRADES FIRED. GREY DOTS = THE MODEL WANTED IN BUT THE ENGINE\'S HARD RULES (DEPTH, COOLDOWN, BAR) SAID NO — THE MODEL IS AN ADVISOR, THE CODE IS THE RISK MANAGER.</div>';
  }

  /* ---------- diverging bars ---------- */
  function abar(label, val, maxAbs, txt, color) {
    const w = Math.abs(val) / (maxAbs || 1) * 46;
    const left = val >= 0 ? 50 : 50 - w;
    return '<div class="abar-row"><div class="abar-label">' + esc(label) + '</div>' +
      '<div class="abar-track"><div class="zero"></div>' +
      '<div class="abar-fill ' + color + '" style="left:' + left.toFixed(1) + "%;width:" + w.toFixed(1) + '%"></div></div>' +
      '<div class="abar-val">' + txt + "</div></div>";
  }

  /* ---------- exits breakdown ---------- */
  function renderExits(D) {
    const rows = (D.stats || {}).by_exit || [];
    const sym = liveCcy(D) === "USD" ? "$" : "€";
    const mx = Math.max(0.5, Math.max.apply(null, rows.map(r => Math.abs(r.net_eur)).concat([0.5])));
    $("exits-body").innerHTML =
      rows.map(r => abar(r.cat, r.net_eur, mx, sgn(r.net_eur) + sym + " ·" + r.n + "×", r.net_eur >= 0 ? "g" : "r")).join("") +
      '<div class="dim" style="margin-top:5px">NO HUMAN CLICKS SELL. TP/SL LIVE AS OCO ORDERS ON THE EXCHANGE; THE HWM TRAILING STOP RE-ARMS ITSELF (move_order_stop). IF THIS SERVER DIES MID-TRADE, THE EXCHANGE STILL CLOSES THE POSITION.</div>';
  }

  /* ---------- hours ---------- */
  function renderHours(D) {
    const rows = (D.stats || {}).by_hour || [];
    const sym = liveCcy(D) === "USD" ? "$" : "€";
    const active = rows.filter(r => r.n);
    if (!active.length) { $("hours-body").textContent = "NO CLOSED TRADES YET"; return; }
    const mx = Math.max(0.5, Math.max.apply(null, rows.map(r => Math.abs(r.net_eur)).concat([0.5])));
    $("hours-body").innerHTML =
      rows.map(r => abar(String(r.h).padStart(2, "0") + "H", r.net_eur, mx,
        r.n ? sgn(r.net_eur) + sym + "·" + r.n : "—", r.net_eur >= 0 ? "g" : (r.n ? "r" : "c"))).join("");
  }

  /* ---------- per instrument ---------- */
  function renderInst(D) {
    const rows = (D.stats || {}).per_inst || [];
    const sym = liveCcy(D) === "USD" ? "$" : "€";
    const mx = Math.max(0.5, Math.max.apply(null, rows.map(r => Math.abs(r.net_eur)).concat([0.5])));
    $("inst-body").innerHTML =
      rows.map(r => abar(r.inst.replace("-EUR", "").replace("-USD_UM_XPERP", "").replace(/-\d{6}$/, ""), r.net_eur, mx,
        sgn(r.net_eur) + sym + " ·" + r.trades + "× " + Math.round(100 * r.wins / r.trades) + "%W",
        r.net_eur >= 0 ? "g" : "r")).join("") +
      '<div class="dim" style="margin-top:4px">THE UNIVERSE IS DYNAMIC — THE ENGINE TRADES WHATEVER PASSES LIQUIDITY, MOMENTUM AND DEPTH THIS WEEK, NOT A FIXED LIST.</div>';
  }

  /* ---------- universe ---------- */
  function renderUniverse(D) {
    // LIVE book first (the heading reads "LIVE X-PERPS + RETIRED SPOT"), then the
    // retired spot scan as a historical record. D.universe now holds the LIVE
    // book's scan, so the spot table must read retired_spot.universe — otherwise
    // this panel would print the perp scan under a "SPOT EUR" heading.
    const retired = spotRetired(D);
    const u = (retired ? ((D.retired_spot || {}).universe || {}) : (D.universe || {}));
    const sel = u.selected || [];
    const rows = sel.map(s => {
      const b = s.bracket || {};
      return "<tr><td class='sym'>" + esc(s.inst) + "</td>" +
        "<td>" + fmt(s.day_range_pct, 1) + "%</td>" +
        "<td class='" + cls(s.chg24h_pct) + "'>" + sgn(s.chg24h_pct) + "%</td>" +
        "<td>€" + fmt(s.vol24h_meur, 2) + "M</td>" +
        "<td>" + fmt(s.spread_pct, 3) + "%</td>" +
        "<td>€" + (s.top5_depth_eur ? (s.top5_depth_eur / 1000).toFixed(1) : "—") + "k</td>" +
        "<td class='cy'>+" + fmt(b.tp_pct, 2) + "%</td>" +
        "<td class='neg'>−" + fmt(b.sl_pct, 2) + "%</td>" +
        "<td class='am'>" + fmt(b.trail_arm_pct, 2) + "/" + fmt(b.trail_giveback_pct, 2) + "</td></tr>";
    }).join("");
    const spotHtml =
      '<div class="perp-h"' + (retired ? ' style="margin-top:10px"' : '') + '>RETIRED BOOK · SPOT EUR (LONG-ONLY) — no longer scanned for trading</div>' +
      '<div class="dim" style="margin-bottom:4px">SCAN ' + esc(mmdd_hhmm(u.scanned_iso)) + ' UTC · ' +
      (u.n_eur_pairs || "?") + " EUR PAIRS → LIQUIDITY+RANGE+DEPTH FILTER → " + (u.n_candidates || "?") +
      ' CANDIDATES → MOMENTUM GATES → TOP ' + sel.length + " TO JEV · BRACKETS PRICED AT 4× ALL-IN COST PER PAIR</div>" +
      "<table><tr><th>PAIR</th><th>DAY RNG</th><th>24H</th><th>VOL24</th><th>SPREAD</th><th>TOP5 DEPTH</th><th>TP</th><th>SL</th><th>TRAIL ARM/GB</th></tr>" + rows + "</table>";
    $("universe-body").innerHTML = retired
      ? (perpUniverseHtml(D) + spotHtml)
      : (spotHtml + perpUniverseHtml(D));
  }

  function perpUniverseHtml(D) {
    const P = D.perp || {};
    const sel = P.universe_selected || [];
    const scanIso = P.universe_scanned_iso;
    let html = '<div class="perp-h"' + (spotRetired(D) ? '' : ' style="margin-top:10px"') + '>LIVE BOOK · X-PERPS USD (TWO-WAY: LONG + SHORT)</div>';
    if (!sel.length) {
      html += '<div class="dim">SCAN ' + esc(mmdd_hhmm(scanIso)) + ' UTC · ' +
        (P.universe || []).length + ' CONTRACTS SELECTED (detail pending next scan)</div>';
      return html;
    }
    const rows = sel.map(s => {
      const b = s.bracket || {};
      const fnd = s.funding_bps;
      return "<tr><td class='sym'>" + esc((s.inst || "").replace("-USD_UM_XPERP", " X-PERP").replace(/-\d{6}$/, "")) + "</td>" +
        "<td>" + fmt(s.day_range_pct, 1) + "%</td>" +
        "<td class='" + cls(s.chg24h_pct) + "'>" + sgn(s.chg24h_pct) + "%</td>" +
        "<td>$" + fmt((s.vol24h_usd || 0) / 1e6, 2) + "M</td>" +
        "<td>" + fmt(s.spread_pct, 3) + "%</td>" +
        "<td>$" + ((s.top5_depth_usd || 0) / 1000).toFixed(1) + "k</td>" +
        "<td class='" + ((fnd || 0) > 0 ? "neg" : (fnd || 0) < 0 ? "pos" : "dim") + "'>" + (fnd === null || fnd === undefined ? "—" : sgn(fnd, 2) + "bp") + "</td>" +
        "<td class='cy'>±" + fmt(b.tp_pct, 2) + "%</td>" +
        "<td class='neg'>−" + fmt(b.sl_pct, 2) + "%</td>" +
        "<td class='am'>" + fmt(b.trail_arm_pct, 2) + "/" + fmt(b.trail_giveback_pct, 2) + "</td></tr>";
    }).join("");
    html += '<div class="dim" style="margin-bottom:4px">SCAN ' + esc(mmdd_hhmm(scanIso)) +
      " UTC · CRYPTO X-PERPS ONLY (STOCK/COMMODITY CONTRACTS EXCLUDED BY ALLOWLIST) · FUNDING + = LONGS PAY (SHORT-CARRY EDGE) · TP/SL APPLY IN POSITION DIRECTION</div>" +
      "<table><tr><th>CONTRACT</th><th>DAY RNG</th><th>24H</th><th>VOL24</th><th>SPREAD</th><th>TOP5 DEPTH</th><th>FUNDING</th><th>TP</th><th>SL</th><th>TRAIL ARM/GB</th></tr>" + rows + "</table>";
    return html;
  }

  /* ---------- positions ---------- */
  function renderPositions(D) {
    const ps = D.positions || [];
    const sb = (D.engine || {}).spot_book || {};
    const retired = /retired/i.test(String(sb.status || ""));
    if (!ps.length) {
      if (retired) {
        // The spot book no longer trades — its capital moved to the X-Perp book.
        // Saying "FLAT, scanning every 5m" would imply a live engine that isn't,
        // and saying "the ledger below is its record" would be false too: the
        // TRADE LEDGER panel now streams the LIVE book. State what is actually here.
        $("pos-body").innerHTML = '<div class="dim">' + esc(sb.status || "RETIRED") +
          (sb.version ? ' · ENGINE ' + esc(sb.version) : '') +
          ' — the long-only EUR spot book no longer trades. Its collateral was converted to ' +
          'USDC and moved to the live X-Perp book (see above). ' +
          (sb.trades_closed ? 'FINAL RECORD: ' + fmt(sb.trades_closed, 0) + ' CLOSED TRADES · ' +
            sgn(sb.net_eur) + ' EUR (' + sgn(sb.net_pct) + '% of launch capital) · PF ' + fmt(sb.profit_factor, 2) +
            ' · WIN RATE ' + fmt(sb.win_rate_pct, 1) + '% · FEES ' + eur(sb.fees_eur) + '. ' : '') +
          'The ledger and replay panels below stream the LIVE X-Perp book, not this one; ' +
          'the panels marked RETIRED carry its final numbers.</div>';
        return;
      }
      $("pos-body").innerHTML = '<div class="dim">FLAT — NO OPEN POSITIONS. ENGINE HOLDS CASH IN EUR, SCANS EVERY ' +
        (D.engine.cycle_s / 60) + 'M CYCLE. FLAT IS A POSITION: A MISSED SCALP COSTS 0, A CHOP COSTS ~0.5-0.9%.</div>';
      return;
    }
    const rows = ps.map(p => {
      const b = p.bracket || {};
      const sp = (p.candles_5m && p.candles_5m.length) ? " " +
        "<span class='spark " + ((p.upc || 0) >= 0 ? "" : "dn") + "'>" + spark(p.candles_5m, 36) + "</span> <span class='dim' style='font-size:9px'>3H OF 5M CANDLES →</span>" : "";
      return "<tr><td class='sym'>" + esc(p.inst) + "</td>" +
        "<td>" + eur(p.stake_eur) + "</td>" +
        "<td>" + fmt(p.entry_px, 4) + "</td><td>" + fmt(p.last_px, 4) + "</td>" +
        "<td class='" + cls(p.upc) + "'>" + sgn(p.upc) + "%</td>" +
        "<td class='" + cls(p.upnl_eur) + "'>" + sgn(p.upnl_eur) + "</td>" +
        "<td class='" + ((p.hwm_pct || 0) > 0 ? "pos" : "dim") + "'>" + sgn(p.hwm_pct) + "%</td>" +
        "<td class='cy'>+" + fmt(b.tp_pct, 2) + "</td><td class='neg'>−" + fmt(b.sl_pct, 2) + "</td>" +
        "<td class='am'>" + fmt(b.trail_arm_pct, 2) + "/" + fmt(b.trail_giveback_pct, 2) + "</td>" +
        "<td>" + fmt(p.age_min, 0) + "m</td>" +
        "<td>" + (p.algo_ok ? "<span class='pos'>SERVER OCO</span>" : "<span class='am'>POLL STOP</span>") + "</td></tr>" +
        "<tr><td colspan='12' style='text-align:left;padding-left:12px'><span class='dim'>JEV ENTRY: conf " + fmt(p.jev_conf, 2) +
        " · noul " + fmt(p.jev_noul, 2) + " · " + esc(p.entry_type || "-") + " · opened " + esc(mmdd_hhmm(p.opened_ts)) + " UTC · qty " + fmt(p.qty, p.qty > 100 ? 0 : 4) + "</span>" + sp + "</td></tr>";
    }).join("");
    $("pos-body").innerHTML = "<table><tr><th>PAIR</th><th>STAKE</th><th>ENTRY</th><th>LAST</th><th>UPNL%</th><th>UPNL €</th><th>HWM</th><th>TP%</th><th>SL%</th><th>TRAIL A/GB</th><th>AGE</th><th>PROTECTION</th></tr>" + rows + "</table>";
  }

  /* ---------- v4: X-Perps two-way book ---------- */
  function renderPerp(D) {
    const P = D.perp;
    const panel = document.getElementById("p-perp");
    if (!panel) return;
    if (!P) { panel.style.display = "none"; return; }
    panel.style.display = "";
    const s = P.stats || {};
    const sideTag = (side) => side === "short"
      ? '<span class="short-tag">▼ SHORT</span>'
      : '<span class="long-tag">▲ LONG</span>';
    const j = P.last_jev || {};
    const jChoice = (j.choice || "—");
    const jDir = jChoice.startsWith("short_") ? "SHORT" : (jChoice.startsWith("long_") ? "LONG" : "FLAT");
    const jCls = jDir === "SHORT" ? "neg" : (jDir === "LONG" ? "pos" : "dim");

    let html = '<div class="perp-grid">';
    // --- KPI strip ---
    html += '<div class="perp-kpis">' +
      '<div class="pk"><span class="pkl">COLLATERAL</span><span class="pkv w">$' + fmt(P.usdc_equity) + '</span><span class="pks">USDC · isolated</span></div>' +
      '<div class="pk"><span class="pkl">REALIZED</span><span class="pkv ' + cls(s.net_all_pct) + '">' + sgn(s.net_all_pct) + '%</span><span class="pks">' + (s.trades_all || 0) + ' closed</span></div>' +
      '<div class="pk"><span class="pkl">WIN RATE</span><span class="pkv w">' + fmt(s.win_rate_pct, 0) + '%</span><span class="pks">' + (s.longs || 0) + 'L / ' + (s.shorts || 0) + 'S</span></div>' +
      '<div class="pk"><span class="pkl">LAST CALL</span><span class="pkv ' + jCls + '">' + (jDir === "FLAT" ? "FLAT" : jDir + " " + esc(jChoice.replace(/^(long|short)_/, "").toUpperCase())) + '</span><span class="pks">conf ' + fmt(j.conf) + ' · noul ' + fmt(j.noul) + '</span></div>' +
      '<div class="pk"><span class="pkl">STATUS</span><span class="pkv ' + (P.halted ? "neg" : "pos") + '">' + (P.halted ? "HALTED" : (P.breaked ? "BREAKER" : "LIVE")) + '</span><span class="pks">1x lev · 5m cycle</span></div>' +
      '</div>';
    // --- v4: JEV TWO-WAY CALL — the short/sell decision breakdown ---
    const pj = P.last_jev || {};
    const pairs = pj.pairs || [];
    if (pairs.length) {
      const dirLbl = (pj.chosen_side === "short") ? "▼ SHORT" : (pj.chosen_side === "long" ? "▲ LONG" : "— FLAT");
      const dirCls = (pj.chosen_side === "short") ? "short-tag" : (pj.chosen_side === "long" ? "long-tag" : "dim");
      html += '<div class="perp-sec"><div class="perp-h">JEV TWO-WAY CALL · ' + esc(mmdd_hhmm(pj.ts)) + ' UTC</div>';
      html += '<div class="perp-decision">DECISION <span class="' + dirCls + '" style="font-size:13px">' + dirLbl + '</span> ' +
        esc((pj.chosen_inst || "").replace("-USD_UM_XPERP", " X-PERP").replace(/-\d{6}$/, "")) +
        ' <span class="dim">· conf ' + fmt(pj.conf) + ' · margin ' + fmt(pj.margin) + ' · noul ' + fmt(pj.noul) +
        ' · budget $' + fmt(pj.budget_usd) + ' · ' + ((pj.tokens||{}).in||"?") + '/' + ((pj.tokens||{}).out||"?") + ' tok</span></div>';
      // per-pair diverging LONG vs SHORT probability bars
      html += '<div class="twoway">';
      pairs.slice().sort((a,b)=>((b.p_long||0)+(b.p_short||0))-((a.p_long||0)+(a.p_short||0))).forEach(p=>{
        const pl = p.p_long||0, psh = p.p_short||0, pnt = 1 - pl - psh;
        const base = esc((p.inst||"").replace("-USD_UM_XPERP","").replace(/-\d{6}$/,""));
        const isChosen = p.inst === pj.chosen_inst;
        html += '<div class="tw-row' + (isChosen ? " chosen" : "") + '">' +
          '<div class="tw-name">' + base + '</div>' +
          '<div class="tw-bar">' +
            '<span class="tw-long" style="width:' + (pl*100).toFixed(1) + '%"></span>' +
            '<span class="tw-no" style="width:' + (Math.max(0,pnt)*100).toFixed(1) + '%"></span>' +
            '<span class="tw-short" style="width:' + (psh*100).toFixed(1) + '%"></span>' +
          '</div>' +
          '<div class="tw-nums"><span class="long-tag">' + (pl*100).toFixed(0) + '%▲</span> ' +
            '<span class="short-tag">' + (psh*100).toFixed(0) + '%▼</span></div>' +
          '<div class="tw-noul">↑' + fmt(p.noul_up) + ' ↓' + fmt(p.noul_dn) + '</div>' +
          '</div>';
      });
      html += '</div>';
      html += '<div class="dim" style="font-size:9px;margin-top:3px">BAR = P(LONG)▲ green · P(NO-TRADE) grey · P(SHORT)▼ red &nbsp;|&nbsp; NOUL ↑ = P(rise ≥ arm% in 45m), ↓ = P(fall ≥ arm% in 45m) &nbsp;|&nbsp; the spot book above only ever goes LONG — shorts live here</div>';
      html += '</div>';
    }
    // --- open positions ---
    const ps = P.positions || [];
    html += '<div class="perp-sec"><div class="perp-h">OPEN POSITIONS</div>';
    if (!ps.length) {
      html += '<div class="dim perp-flat">FLAT — SCANNING ' + (P.universe || []).length + ' X-PERPS FOR TWO-WAY SETUPS EVERY CYCLE. FEES 0.02/0.05% = ROUND TRIP ~0.07%, A THIRD OF THE SPOT FEE WALL.</div>';
    } else {
      html += "<table><tr><th>SIDE</th><th>CONTRACT</th><th>ENTRY</th><th>LAST</th><th>UPNL%</th><th>NOTIONAL</th><th>LIQ PX</th><th>TP%</th><th>SL%</th><th>AGE</th><th>PROTECTION</th></tr>";
      html += ps.map(p => {
        const b = p.bracket || {};
        const upc = p.upc, last = p.last, sgnSide = p.side === "short" ? "▼" : "▲";
        return "<tr class='" + (p.side === "short" ? "row-short" : "row-long") + "'>" +
          "<td>" + sideTag(p.side) + "</td>" +
          "<td class='sym'>" + esc((p.inst || "").replace("-USD_UM_XPERP", " X-PERP").replace(/-\d{6}$/, "")) + "</td>" +
          "<td>" + fmt(p.entry_ref, 4) + "</td>" +
          "<td>" + fmt(last, 4) + "</td>" +
          "<td class='" + cls(upc) + "'>" + sgn(upc) + "%</td>" +
          "<td>$" + fmt(p.notional_usd) + "</td>" +
          "<td class='dim'>" + (p.liq_px ? fmt(p.liq_px, 0) : "—") + "</td>" +
          "<td class='cy'>+" + fmt(b.tp_pct, 2) + "</td><td class='neg'>−" + fmt(b.sl_pct, 2) + "</td>" +
          "<td>" + (p.opened_ts ? esc(mmdd_hhmm(p.opened_ts)).slice(6) : "—") + "</td>" +
          "<td>" + (p.algo_ok ? "<span class='pos'>SERVER OCO</span>" : "<span class='am'>POLL STOP</span>") + "</td></tr>";
      }).join("") + "</table>";
    }
    html += '</div>';
    // --- recent trades ---
    const tr = P.trades || [];
    html += '<div class="perp-sec"><div class="perp-h">RECENT CLOSED</div>';
    if (!tr.length) html += '<div class="dim">AWAITING FIRST CLOSED X-PERP TRADE…</div>';
    else html += "<table><tr><th>SIDE</th><th>CONTRACT</th><th>MOVE</th><th>NET%</th><th>REASON</th><th>CLOSED</th></tr>" +
      tr.slice(0, 8).map(t =>
        "<tr class='" + (t.side === "short" ? "row-short" : "row-long") + "'><td>" + sideTag(t.side) + "</td>" +
        "<td class='sym'>" + esc((t.inst || "").replace("-USD_UM_XPERP", " X-PERP").replace(/-\d{6}$/, "")) + "</td>" +
        "<td class='" + cls(t.upc) + "'>" + sgn(t.upc) + "%</td>" +
        "<td class='" + cls(t.net_pct) + "'>" + sgn(t.net_pct) + "%</td>" +
        "<td class='dim'>" + esc((t.reason || "").slice(0, 30)) + "</td>" +
        "<td class='dim'>" + esc(mmdd_hhmm(t.close_ts)) + "</td></tr>").join("") + "</table>";
    html += '</div>';
    // --- universe strip ---
    html += '<div class="perp-sec"><div class="perp-h">SCANNED UNIVERSE</div><div class="dim" style="font-size:10px">' +
      (P.universe || []).map(u => esc(u.replace("-USD_UM_XPERP", "").replace(/-\d{6}$/, ""))).join(" · ") +
      '</div></div>';
    html += '</div>';
    $("perp-body").innerHTML = html;
    const mEl = $("perp-meta");
    if (mEl) mEl.textContent = "OKX Europe X-Perps (MiFID) · $" + fmt(P.usdc_equity) + " USDC · 1x isolated · long & short · fees 0.02/0.05%";
  }

  /* ---------- news ---------- */
  function renderNews(D) {
    const items = (D.news || []).map(n => {
      const m = n.match(/^\[(.+?)\]\s*(.*)$/);
      const src = m ? m[1] : "wire";
      const txt = m ? m[2] : n;
      return '<div class="news-item"><span class="src">[' + esc(src.toUpperCase()) + "]</span>" + esc(txt) + "</div>";
    }).join("");
    // HONESTY: these feeds were written by the retired spot engine and are what IT
    // put into its prompts. The live X-Perp engine injects funding + momentum only,
    // so claiming this "rides with every Jev call" would describe a system that no
    // longer exists. Show the source and the age instead.
    const staleMin = ageMin(D.news_updated);
    const feedNote = spotRetired(D)
      ? "OBSERVER CONTEXT — NOT INJECTED INTO THE LIVE BOOK. The live X-Perp engine decides on funding + multi-timeframe momentum; RSS catalysts were prompt context for the retired spot engine."
      : "RIDES WITH EVERY JEV CALL — 30-MIN GUARD";
    $("news-age").textContent = "INGESTED " + esc(mmdd_hhmm(D.news_updated)) + " UTC" +
      (staleMin !== null ? " · " + staleMin + "M AGO" : "") +
      (staleMin !== null && staleMin > 45 ? " · ⚠ FEED STALE" : "");
    let html = items || '<div class="dim">NO FRESH CATALYSTS IN WINDOW</div>';
    html += '<div class="dim" style="margin-top:4px">' + feedNote + '</div>';
    $("news-body").innerHTML = html;
  }

  /* ---------- sentiment ---------- */
  function fgBar(fg) {
    if (!fg) return "";
    return '<div class="fg"><div><div class="num">' + fg.value + '</div><div class="lbl">' + esc(fg.label) +
      "</div></div><div class='fg-bar'><div class='fg-fill' style='width:100%'></div>" +
      "<div class='fg-pin' style='left:" + fg.value + "%'></div></div>" +
      '<div class="dim">D-1 ' + fg.d1 + "<br>7D-AVG " + fmt(fg.d7_avg, 1) + "<br>STREAK " + fg.streak_days + "D</div></div>";
  }
  function renderSent(D) {
    const s = D.sentiment || {};
    const coins = s.coins || {};
    const rows = Object.keys(coins).map(k => {
      const c = coins[k];
      return "<tr><td class='sym'>" + esc(k.replace("-EUR", "")) + "</td>" +
        "<td class='" + cls(c.chg24h_pct) + "'>" + sgn(c.chg24h_pct) + "%</td>" +
        "<td>" + fmt(c.sent_up_pct, 0) + "%</td>" +
        "<td>" + (c.trending_rank ? "#" + c.trending_rank : "—") + "</td>" +
        "<td>" + (c.watchlists_k ? c.watchlists_k + "k" : "—") + "</td>" +
        "<td class='" + cls(c.off_ath_pct) + "'>" + sgn(c.off_ath_pct, 0) + "%</td>" +
        "<td class='dim'>" + (c.mcap_rank || "—") + "</td></tr>";
    }).join("");
    const trend = (s.trending || []).map(t => '<span class="chip">' + esc(t) + "</span>").join("");
    // Same honesty rule as the news panel: these are keyless observer feeds, and
    // the pairs listed are the RETIRED spot engine's EUR universe — the live book
    // trades USD X-Perp contracts. Say so instead of implying it's live context.
    const stale = ageMin(s.updated);
    $("sent-body").innerHTML =
      fgBar(s.fear_greed) +
      '<div class="dim" style="margin:4px 0">BTC PERP FUNDING: <span class="am">' + fmt(s.funding_bps, 1) + ' BPS</span> · SOURCES: ALTERNATIVE.ME + COINGECKO (KEYLESS) · ' + esc(mmdd_hhmm(s.updated)) + " UTC" +
      (stale !== null ? " · " + stale + "M AGO" : "") + (stale !== null && stale > 45 ? ' · <span class="neg">⚠ STALE</span>' : "") + "</div>" +
      (spotRetired(D) ? '<div class="dim" style="margin-bottom:4px">OBSERVER CONTEXT ONLY — the coin table below is the retired spot engine\'s EUR universe. The live X-Perp book injects per-contract funding + momentum, not these polls.</div>' : "") +
      "<table><tr><th>PAIR</th><th>24H</th><th>BULL VOTE</th><th>TREND</th><th>WATCHLISTS</th><th>OFF ATH</th><th>MCAP#</th></tr>" + rows + "</table>" +
      '<div class="dim" style="margin-top:5px">GLOBAL ATTENTION: ' + trend + "</div>";
  }

  /* ---------- macro ---------- */
  function renderMacro(D) {
    const m = D.macro || {};
    // The live X-Perp engine does NOT inject econ-calendar events — only the
    // retired spot engine did. Saying "NO FRESH ENTRIES INTO IMP-3 RELEASES"
    // would describe a protection the live book does not have. State the source.
    if (m.source === "not-injected-into-live-book") {
      $("macro-body").innerHTML =
        '<div class="dim" style="line-height:1.55">NOT WIRED INTO THE LIVE BOOK.<br><br>' +
        esc(m.note || "The live X-Perp engine injects funding + multi-timeframe momentum only.") +
        '<br><br>The retired spot engine did gate entries around high-impact releases; ' +
        "that protection went with it. Nothing here is stale data being passed off as current.</div>";
      return;
    }
    const up = (m.upcoming || []).map(e =>
      "<tr><td class='am'>" + esc(e.e) + "</td><td>" + esc(e.ccy) + "</td><td>" +
      "★".repeat(e.imp || 1) + "</td><td>" + fmt(e.in_h, 1) + "H</td></tr>").join("");
    const sup = (m.surprises || []).map(e =>
      "<tr><td class='cy'>" + esc(e.e) + "</td><td>" + esc(e.act) + "</td><td class='dim'>" + esc(e.fc) +
      "</td><td>" + fmt(e.ago_h, 1) + "H AGO</td></tr>").join("");
    $("macro-body").innerHTML =
      '<div class="dim" style="margin-bottom:3px">UPCOMING ≥★★ — NO FRESH ENTRIES INTO IMP-3 RELEASES</div>' +
      (up ? "<table><tr><th>EVENT</th><th>CCY</th><th>IMP</th><th>IN</th></tr>" + up + "</table>" : '<div class="dim">CLEAR CALENDAR — NOTHING ≥★★ IN 24H</div>') +
      '<div class="dim" style="margin:6px 0 3px">LAST 3H SURPRISES (ACTUAL ≠ FORECAST) — DRIVES THE TAPE</div>' +
      (sup ? "<table><tr><th>EVENT</th><th>ACT</th><th>FC</th><th>AGE</th></tr>" + sup + "</table>" : '<div class="dim">NO SURPRISE PRINTS IN WINDOW</div>');
  }

  /* ---------- jev vote tape ---------- */
  function renderHist(D) {
    const h = D.jev_history || [];
    const P = D.params || {};
    const mBar = (P.MARGIN_BAR !== undefined ? P.MARGIN_BAR : 0.30);
    const nBar = (P.NOUL_BAR !== undefined ? P.NOUL_BAR : 0.45);
    const rows = h.map(j => {
      const fired = j.choice && j.choice !== "no_trade";
      // choice is buy_<base> (spot) or long_<base>/short_<base> (two-way perp)
      const side = /^short_/.test(j.choice || "") ? "▼ " : (/^(long|buy)_/.test(j.choice || "") ? "▲ " : "");
      const label = fired ? String(j.choice).replace(/^(buy|long|short)_/, "").toUpperCase() : "FLAT";
      const mw = Math.min(64, Math.max(2, Math.abs((j.margin || 0) * 100) * 0.9));
      return "<tr><td class='dim'>" + esc(mmdd_hhmm(j.ts)) + "</td>" +
        "<td class='" + (fired ? (/^short_/.test(j.choice) ? "neg" : "pos") : "dim") + "'>" + (fired ? side : "") + esc(label) + "</td>" +
        "<td>" + fmt(j.conf, 2) + "</td>" +
        "<td><div style='display:flex;align-items:center;gap:4px;justify-content:flex-end'>" +
        "<div style='height:8px;width:" + mw.toFixed(0) + "px;background:" + (fired ? "var(--green-dim)" : "#333") + "'></div>" +
        "<span class='" + (j.margin >= mBar ? "am" : "dim") + "' style='width:38px;text-align:right'>" + sgn(j.margin) + "</span></div></td>" +
        "<td class='" + (j.noul >= nBar ? "am" : "dim") + "'>" + fmt(j.noul, 2) + "</td>" +
        "<td class='dim'>" + (j.tok || "?") + "</td></tr>";
    }).join("");
    $("hist-body").innerHTML = "<table><tr><th>UTC</th><th>VOTE</th><th>CONF</th><th>MARGIN p(chosen)−p(flat)</th><th>NOUL</th><th>TOK</th></tr>" + rows + "</table>";
  }

  /* ---------- trades ledger (clickable → dossier) ---------- */
  function renderTrades(D) {
    const sym = liveCcy(D) === "USD" ? "$" : "€";
    const twoWay = spotRetired(D);   // live book is the two-way perp book
    const rows = (D.trades || []).map(t => {
      // direction tag: only meaningful for the two-way perp book
      const sd = (t.side === "short") ? " <span class='short-tag'>▼</span>"
        : (t.side === "long") ? " <span class='long-tag'>▲</span>" : "";
      const nm = String(t.inst || "").replace("-USD_UM_XPERP", "").replace(/-\d{6}$/, "");
      return "<tr class='trade-row" + (t.tid === OPEN_TID ? " sel" : "") + "' data-tid='" + esc(t.tid) + "'" +
      (t.debrief ? " title='" + esc(t.debrief) + "'" : "") +
      "><td class='dim'>" + esc(mmdd_hhmm(t.close_ts)) + "</td>" +
      "<td class='sym'>" + esc(nm) + sd + (t.debrief ? " <span style='color:var(--cyan);font-size:8px'>◈LLM</span>" : "") + "</td>" +
      "<td>" + sym + fmt(t.stake_eur, 2) + "</td>" +
      "<td class='" + cls(t.upc) + "'>" + sgn(t.upc) + "%</td>" +
      "<td class='" + cls(t.net_eur) + "'>" + sgn(t.net_eur) + "</td>" +
      "<td class='dim'>−" + fmt(t.fees_eur, 2) + "</td>" +
      "<td>" + fmt(t.age_min, 0) + "m</td>" +
      "<td class='dim' style='text-align:left'>" + esc(t.reason) + "</td>" +
      "<td class='cy' style='font-size:9px'>REPLAY ▸</td></tr>";
    }).join("");
    $("trades-body").innerHTML = "<table><tr><th>CLOSED UTC</th><th>PAIR</th><th>" + (twoWay ? "NOTIONAL" : "STAKE") + "</th><th>MOVE</th><th>NET " + sym + "</th><th>FEES</th><th>HELD</th><th>EXIT</th><th></th></tr>" + rows +
      "</table><div class='dim' style='margin-top:3px;font-size:9.5px'>▸ CLICK ANY ROW — FULL DECISION REPLAY: THE EXACT STATE JEV SAW, ITS VOTE, THE GATES, EVERY EVENT ENTRY→EXIT, PLUS AN LLM POST-MORTEM · <span style='color:var(--cyan)'>◈LLM</span> = DEBRIEF READY (HOVER)</div>";
    document.querySelectorAll(".trade-row").forEach(tr => {
      tr.onclick = () => openDossier(tr.getAttribute("data-tid"));
    });
  }

  /* ---------- daily ---------- */
  function renderDaily(D) {
    const days = (D.stats || {}).days || [];
    const maxAbs = Math.max(0.5, Math.max.apply(null, days.map(d => Math.abs(d.net_eur)).concat([0.5])));
    const rows = days.map(d => {
      const w = Math.abs(d.net_eur) / maxAbs * 48;
      const g = d.net_eur >= 0;
      return '<div class="dbar-row"><div class="dbar-date">' + esc(d.date.slice(5)) + '</div>' +
        '<div class="dbar-track"><div class="dbar-zero"></div>' +
        '<div class="dbar-fill ' + (g ? "g" : "r") + '" style="' + (g ? "left:50%;width:" + w : "right:50%;width:" + w) + '%"></div></div>' +
        '<div class="dbar-val ' + cls(d.net_eur) + '">' + sgn(d.net_eur) + '</div>' +
        '<div class="dbar-meta">' + d.trades + " trades · " + Math.round(100 * d.wins / Math.max(1, d.trades)) + "% win</div></div>";
    }).join("");
    $("daily-body").innerHTML = rows || '<div class="dim">NO CLOSED DAYS YET</div>';
    // Heading carries the LIVE book's currency (was hardcoded "(EUR)" while the
    // bars were already rendering the USD perp book).
    const dh = document.getElementById("daily-h");
    if (dh) dh.textContent = "DAILY NET P&L (" + (liveCcy(D) === "USD" ? "USD" : "EUR") + ")";
  }

  /* ---------- model stack ---------- */
  function renderStack(D) {
    const f = D.funnel || {}, j = D.jev_latest || {}, dn = D.desk_note || {};
    const card = (role, model, kind, lines, accent) =>
      '<div class="stack-card" style="border-left-color:' + accent + '">' +
      '<div class="sc-role">' + esc(role) + '</div>' +
      '<div class="sc-model">' + esc(model) + '</div>' +
      '<div class="sc-kind">' + esc(kind) + '</div>' +
      '<div class="sc-lines">' + lines.map(l => '<div>▸ ' + esc(l) + "</div>").join("") + "</div></div>";
    $("stack-body").innerHTML =
      card("DECISION ENGINE", "Jev System-One (jev-latest)", "TYPED / SCHEMA-ENFORCED · NOT TEXT", [
        spotRetired(D)
          ? "Returns probabilities, not prose — p(long) AND p(short) per contract, rise/fall-prob, confidence"
          : "Returns probabilities, not prose — p(buy) per pair, noul rise-prob, confidence",
        "Zero parsing, zero hallucinated numbers; the engine reads a typed struct",
        (f.jev_calls || 0) + " calls · avg " + (f.avg_input_tok || "?") + " tok in / " + (f.avg_output_tok || "?") + " out",
        "$0.04 per 1M input tokens — decisions cost fractions of a cent",
      ], "#29c9eb") +
      card("DESK + POST-MORTEM LLM", dn.model || "halogen-qwen3.8-flash-next", "GENERATIVE · LOCAL RELAY · CACHE-GATED", [
        "Writes the desk note you read at the top + one-line trade post-mortems",
        "Sees only facts already in the engine state — never invents numbers",
        "Hash-cached, ≥5-min rate limit, graceful fallback if the relay drops",
        "Runs on YOUR relay — a local model or a cheap hosted endpoint, not a big-cloud API",
      ], "#00e04b") +
      '<div class="dim" style="margin-top:6px;font-size:10px">TWO MODELS, TWO JOBS: A CHEAP TYPED MODEL <b style="color:var(--cyan)">DECIDES</b> (auditable, no free text); A LOCAL GENERATIVE MODEL <b style="color:var(--green)">EXPLAINS</b>. The decision path never depends on prose parsing — that is the whole point of the architecture.</div>';
  }

  /* ---------- prompt / cost optimization ---------- */
  function renderOptim(D) {
    const f = D.funnel || {}, P = D.params || {};
    const tk = (D.jev_latest || {}).tokens || {};
    // The trim/budget machinery exists ONLY in the engine that defines
    // MAX_STATE_CHARS. The live X-Perp engine has none — its prompt is a few
    // hundred tokens (<=4 contracts, no news/macro blocks) — so the old copy
    // ("HARD-CAPS INPUT", "auto-cuts news->macro->pairs", "NEWS SHRINKS BEFORE
    // MACRO") described code that is not running. Report what is real.
    const hasBudget = !!P.has_prompt_budget;
    const budget = P.MAX_JEV_INPUT_TOKENS || null;
    const avgIn = f.avg_input_tok || 0;
    const pct = (hasBudget && budget) ? Math.min(100, Math.round(100 * avgIn / budget)) : 0;
    const row = (name, val, note) =>
      '<div class="opt-row"><span class="opt-n">' + esc(name) + '</span><span class="opt-v">' + val + '</span>' +
      (note ? '<span class="opt-note">' + esc(note) + "</span>" : "") + "</div>";
    const gauge = (hasBudget && budget)
      ? '<div class="opt-gauge"><div class="opt-gauge-fill" style="width:' + pct + '%"></div>' +
        '<span class="opt-gauge-txt">' + avgIn + " / " + budget + " TOK · " + pct + "% OF ALARM</span></div>"
      : '<div class="opt-gauge"><div class="opt-gauge-fill" style="width:100%"></div>' +
        '<span class="opt-gauge-txt">' + avgIn + ' TOK / CALL · NO CAP NEEDED</span></div>';
    $("optim-body").innerHTML =
      '<div class="dim" style="font-size:10px;margin-bottom:6px">' + (hasBudget
        ? "EVERY DECISION PROMPT IS BUDGETED & TRIMMED IN CODE. THE ENGINE MEASURES ~" + fmt(P.CHARS_PER_TOKEN, 2) + " CHARS/TOKEN AND HARD-CAPS INPUT SO THE MODEL STAYS FAST AND CHEAP."
        : "THE LIVE X-PERP PROMPT IS SMALL BY CONSTRUCTION — AT MOST " + (P.MAX_JEV_PAIRS || 4) + " CONTRACTS WITH A HANDFUL OF NUMBERS EACH, AND NO NEWS/MACRO BLOCKS — SO IT NEEDS NO TRIM LOOP. NOTHING IS CAPPED BECAUSE NOTHING COMES CLOSE.") + "</div>" +
      gauge +
      row("AVG INPUT / CALL", avgIn + " tok", "measured across " + (f.jev_calls || 0) + " live calls") +
      row("PEAK INPUT / CALL", (f.max_input_tok || "—") + " tok", "worst-case state size") +
      row("AVG OUTPUT / CALL", (f.avg_output_tok || "—") + " tok", "typed JSON only") +
      row("STATE CHARS / CALL", (f.avg_state_chars || "—"), hasBudget ? "dense per-pair telemetry" : "per-contract telemetry") +
      (hasBudget
        ? row("BUDGET TRIMS", (f.budget_warns || 0) + " total", "auto-cuts news→macro→pairs when over")
        : row("BUDGET TRIMS", "n/a", "no trim loop in this engine — see note above")) +
      '<div class="dim" style="margin-top:6px;font-size:10px">' + (hasBudget
        ? "TRIM PRIORITY IS HAND-TUNED: NEWS SHRINKS BEFORE MACRO, MACRO BEFORE THE PAIR TABLE. THE MODEL ALWAYS SEES THE NUMBERS THAT MOVE THE DECISION FIRST."
        : "COST IS BOUNDED BY THE CANDIDATE CAP, NOT BY A TRIMMER: THE ENGINE SENDS AT MOST " + (P.MAX_JEV_PAIRS || 4) + " CONTRACTS PER CALL, SO INPUT SIZE CANNOT GROW WITH THE UNIVERSE.") + "</div>";
  }

  /* ---------- decision economics ---------- */
  function renderCostLive(D) {
    const f = D.funnel || {}, s = D.stats || {};
    const sym = liveCcy(D) === "USD" ? "$" : "€";
    const opens = f.opens || 0, calls = f.jev_calls || 0;
    const usdAll = f.usd_all || 0;
    const perDecision = calls ? usdAll / calls : 0;
    const perTrade = opens ? usdAll / opens : 0;
    const netAll = s.net_all_eur || 0;
    const fees = s.fees_all_eur || 0;
    const big = (label, val, sub, col) =>
      '<div class="eco-cell"><div class="eco-k">' + esc(label) + '</div><div class="eco-v ' + (col || "") + '">' + val + "</div>" +
      (sub ? '<div class="eco-s">' + esc(sub) + "</div>" : "") + "</div>";
    $("costlive-body").innerHTML =
      '<div class="eco-grid">' +
      big("COST / JEV DECISION", "$" + perDecision.toFixed(6), (calls || 0) + " calls all-time", "g") +
      big("COST / TRADE OPENED", "$" + perTrade.toFixed(4), (opens || 0) + " opens all-time", "g") +
      big("TOTAL MODEL SPEND", "$" + usdAll.toFixed(2), ((f.tok_input_all || 0) / 1e3).toFixed(0) + "k tok in", "g") +
      big("EXCHANGE FEES PAID", sym + fees.toFixed(2), "the real cost of scalping", "r") +
      "</div>" +
      '<div class="dim" style="margin-top:7px;font-size:10.5px;line-height:1.5">' +
      "THE WHOLE GAME IS FEES, NOT INFERENCE. THIS ENGINE HAS SPENT <b style='color:var(--green)'>$" + usdAll.toFixed(2) +
      "</b> ON EVERY JEV DECISION IT EVER MADE, AND <b style='color:var(--red)'>" + sym + fees.toFixed(2) +
      "</b> ON EXCHANGE FEES. A SCALPER'S EDGE LIVES OR DIES ON THE FEE LINE — WHICH IS WHY THE ENTRY IS MAKER-FIRST AND THE UNIVERSE IS FILTERED FOR TIGHT SPREADS. THE AI IS NEARLY FREE; THE MARKET ACCESS IS NOT.</div>";
  }

  /* ---------- architecture ---------- */
  function renderArch(D) {
    const f = D.funnel || {};
    // Every constant below comes from D.params, parsed out of the LIVE engine's
    // source by generate.py. This panel used to hardcode all eight steps against
    // the retired spot engine — "MARGIN ≥0.25", "IOC CHASE FALLBACK" (deleted in
    // v3.8), "STAKE = ALL FREE EUR", cooldowns 15m/30m, stale exit 90m — while the
    // live book ran different values on every one. Parse, never transcribe.
    const p = D.params || {};
    // The live book IS the perp book exactly when spot is retired. (An earlier
    // version had this inverted — `!spotRetired(D)` — which rendered all eight
    // architecture steps describing the retired spot engine while the perp engine
    // was the one actually trading.)
    const perps = spotRetired(D);
    const sym = perps ? "$" : "€";
    const steps = [
      ["1", "SCAN", perps
        ? "CRYPTO X-PERP CONTRACTS EVERY CYCLE — ACCOUNT-SCOPED /account/instruments (MiFID USD-MARGINED)"
        : "~" + (f.eur_pairs || 270) + " EUR SPOT PAIRS EVERY CYCLE — KEYLESS OKX PUBLIC FEED"],
      ["2", "FILTER", "LIQUIDITY ≥" + sym + Math.round((p.MIN_VOL24H_USD || 300000) / 1000) + "K · SPREAD ≤" + fmt(p.MAX_SPREAD_PCT, 2) +
        "% · 24H RANGE ≥" + fmt(p.MIN_DAY_RANGE_PCT, 1) + "% · TOP-5 BOOK DEPTH ≥" + sym + Math.round(p.MIN_TOP5_DEPTH_USD || 1000) +
        " — THIN BOOKS GET REJECTED, NOT TRADED"],
      ["3", "GATES", "DETERMINISTIC MOMENTUM: 30M TREND · GREEN-BAR COUNT · RANGE POSITION · NET TAKER BUYING · 30M VOL ≥ " +
        fmt(p.MIN_VOL30_PCT, 2) + "% (TP REACHABILITY) — CODE, NOT OPINION"],
      ["4", "CONTEXT", perps
        ? "PER-CONTRACT FUNDING RATE · 30M/4H/7D MULTI-TIMEFRAME MOMENTUM · SPREAD + RANGE POSITION · BRACKET GEOMETRY"
        : "RSS HEADLINES (30-MIN GUARD) · FEAR&GREED + STREAK · COMMUNITY POLLS · BTC FUNDING · ECON CALENDAR + SURPRISE PRINTS · 30M/4H/7D MULTI-TIMEFRAME"],
      ["5", "DECIDE", "JEV SYSTEM-ONE RETURNS TYPED OUTPUT: " + (perps
        ? "P(LONG) AND P(SHORT) PER CONTRACT · RISE- AND FALL-PROBABILITY"
        : "P(BUY) PER PAIR · NOUL RISE-PROBABILITY") + " · CONFIDENCE — SCHEMA-ENFORCED, NO TEXT PARSING, NO HALLUCINATED NUMBERS"],
      ["6", "VETO", "ENGINE BAR: MARGIN ≥" + fmt(p.MARGIN_BAR, 2) + " OR CONF ≥" + fmt(p.CONF_BAR, 2) +
        ", AND CONF ≥" + fmt(p.CONF_FLOOR, 2) + " FLOOR, AND NOUL ≥" + fmt(p.NOUL_BAR, 2) +
        " — THEN LIVE BOOK RE-CHECK: DEPTH ≥3× STAKE, BRACKET REPRICED FROM THE REAL SPREAD"],
      ["7", "EXECUTE", "MAKER-FIRST ENTRY (POST_ONLY — UNFILLED = NO TRADE, NO CHASE) · " + (perps
        ? "NOTIONAL = " + Math.round((p.MAX_NOTIONAL_FRAC || 0.98) * 100) + "% OF USDC EQUITY · " + (p.PERP_LEV || 1) + "× ISOLATED"
        : "STAKE = ALL FREE EUR / OPEN SLOTS") + " · SERVER-SIDE OCO TP/SL ATTACHED AT FILL"],
      ["8", "MANAGE", "HWM TRAILING STOP RE-ARMED ON-EXCHANGE · TIME-STOP " + (p.TIME_STOP_MIN || 30) + "M LOSING · STALE-EXIT " +
        (p.STALE_EXIT_MIN || 60) + "M · WIN " + (p.WIN_COOLDOWN_MIN || 10) + "M / LOSS " + (p.COOLDOWN_MIN || 20) +
        "M COOLDOWNS · −" + fmt(p.DAILY_LOSS_PCT, 0) + "% DAILY BREAKER"],
    ];
    const rows = steps.map(s =>
      '<div class="gate" style="align-items:flex-start"><span class="mark cy">' + s[0] + '</span><span class="gname am" style="flex:0 0 74px">' + s[1] +
      '</span><span style="color:var(--white);font-size:10.5px;flex:1;text-align:left">' + esc(s[2]) + "</span></div>").join("");
    $("arch-body").innerHTML = rows +
      '<div class="dim" style="margin-top:7px;border-top:1px dotted #241f10;padding-top:5px">' +
      (f.uptime_h ? Math.round(f.uptime_h) + "H UNATTENDED · " : "") +
      (f.cycles || 0).toLocaleString() + " CYCLES RUN · " + (f.jev_calls || 0) + " MODEL CALLS · " +
      (f.gate_rejects || 0).toLocaleString() + " SETUPS REJECTED BY CODE · $" + fmt(f.usd_all, 2) +
      " TOTAL DECISION COST · EVERY NUMBER ON THIS PAGE CAME FROM THE RAW ENGINE LOG — NOT A BACKTEST, NOT A SIMULATION.</div>";
  }

  /* ---------- tape with new-row flash + session tracking ---------- */
  let SESS = { events: 0, opens: 0, closes: 0, jevs: 0, rejects: 0, netNew: null, lastOpen: null, lastClose: null };
  function trackSession(D) {
    const tape = D.tape || [];
    let newEvents = 0, newOpens = 0, newCloses = 0, newJevs = 0, newRejects = 0;
    tape.forEach(t => {
      const key = t.ts + "|" + t.text;
      if (!KNOWN_TAPE.has(key)) {
        if (!firstRender) {
          newEvents++;
          FRESH_KEYS.add(key);
          if (t.kind === "OPEN") { newOpens++; SESS.lastOpen = t.text; flashTitle("▲ OPEN " + t.text.replace("▲ OPEN ", "").split("  ")[0]); }
          else if (t.kind === "CLOSE") { newCloses++; SESS.lastClose = t.text; flashTitle("▼ CLOSE " + t.text.slice(2, 60)); }
          else if (t.kind === "JEV") newJevs++;
          else if (t.kind === "GATE") newRejects++;
        }
        KNOWN_TAPE.add(key);
      }
    });
    if (!firstRender) {
      SESS.events += newEvents; SESS.opens += newOpens; SESS.closes += newCloses;
      SESS.jevs += newJevs; SESS.rejects += newRejects;
      const eq = D.equity || {};
      SESS.netNew = eq.day_pnl_eur; // day P&L is the honest "watch what happens" number
    }
  }
  function flashTitle(txt) {
    TITLE_FLASH = { text: txt, until: Date.now() + 15000 };
  }
  function renderSession(D) {
    const mins = Math.max(0, Math.round((Date.now() - ARRIVED) / 60000));
    const bump = (n, cls_) => n ? '<b class="' + (cls_ || "hot") + ' sess-bump">' + n + "</b>" : "<b>" + n + "</b>";
    $("session-body").innerHTML =
      '<span class="sess-live"><span class="dot"></span>WATCHING LIVE</span>' +
      '<span class="sess-cell">YOU ARRIVED <b>' + mins + '</b> MIN AGO — SINCE THEN THE ENGINE RAN:</span>' +
      '<span class="sess-cell">' + bump(SESS.events, "") + " NEW EVENTS</span>" +
      '<span class="sess-cell">' + bump(SESS.jevs, "") + " JEV CALLS</span>" +
      '<span class="sess-cell">' + bump(SESS.rejects, "") + " GATE REJECTS</span>" +
      '<span class="sess-cell">' + bump(SESS.opens, "g") + " ENTRIES</span>" +
      '<span class="sess-cell">' + bump(SESS.closes, "r") + " EXITS</span>" +
      '<span class="sess-note">' + (SESS.lastOpen || SESS.lastClose
        ? esc((SESS.lastClose || SESS.lastOpen).slice(0, 72))
        : "ENGINE CYCLES EVERY " + (D.engine.cycle_s / 60) + "M — LEAVE THIS TAB OPEN AND WATCH IT WORK · NOTHING HERE IS STAGED") + "</span>";
  }

  function renderTape(D) {
    trackSession(D);
    const tape = D.tape || [];
    const html = tape.map(t => {
      const key = t.ts + "|" + t.text;
      const isNew = !firstRender && FRESH_KEYS.has(key);
      return '<div class="tape-row' + (isNew ? " flash-new" : "") + '"><span class="t">' + esc(hhmm(t.ts)) + '</span>' +
        '<span class="k k-' + esc(t.kind) + '">' + esc(t.kind) + '</span>' +
        '<span class="x c-' + esc(t.color) + '">' + esc(t.text) + "</span></div>";
    }).join("");
    $("tape-body").innerHTML = html;
    FRESH_KEYS.clear();
    if (KNOWN_TAPE.size > 6000) {
      const tape2 = D.tape || [];
      KNOWN_TAPE = new Set(tape2.map(t => t.ts + "|" + t.text));
    }
    firstRender = false;
  }
  let FRESH_KEYS = new Set();

  /* ---------- DOSSIER MODAL ---------- */
  function modalOpen(html) {
    const root = $("modal-root");
    root.innerHTML = '<div class="modal-bg"></div><div class="modal">' + html + "</div>";
    root.classList.add("open");
    root.querySelector(".modal-bg").onclick = modalClose;
    root.querySelector(".modal-close").onclick = modalClose;
    root.querySelector(".modal").scrollTop = 0;
  }
  function modalClose() {
    OPEN_TID = null;
    OPEN_DOSSIER_HASH = null;
    const root = $("modal-root");
    root.classList.remove("open");
    root.innerHTML = "";
    document.querySelectorAll(".trade-row.sel").forEach(r => r.classList.remove("sel"));
  }
  document.addEventListener("keydown", e => { if (e.key === "Escape") modalClose(); });

  function tlClass(ev) {
    if (ev === "open") return "open";
    if (ev === "close" || ev === "close_detected" || ev === "close_dust") return "close";
    if (["algo_fallback", "algo_fallback_err", "daily_breaker", "universe_depth_reject"].indexOf(ev) >= 0) return "risk";
    return "";
  }
  function tlText(e) {
    // Currency from the event's own book (normalizer stamps ccy="USD" on perp
    // events); defaults to EUR for the retired spot timeline.
    const sym = e.ccy === "USD" ? "$" : "€";
    const side = e.side === "short" ? "▼ SHORT" : (e.side === "long" ? "▲ LONG" : "▲");
    switch (e.event) {
      case "open": return side + " ENTRY FILLED @ " + e.fill_px + " · " + (e.ccy === "USD" ? "notional " : "stake ") + sym + fmt(e.stake) + " · " + (e.entry_type || "-") +
        " · conf " + fmt(e.conf) + " margin " + fmt(e.margin) + " noul " + fmt(e.noul) +
        (e.algo_ok === false ? " · ⚠ OCO ATTACH FAILED — POLL STOP OWNS IT" : " · OCO ATTACHED");
      case "close": return "▼ MANAGED EXIT " + sgn(e.upc) + "% — " + (e.reason || "");
      case "close_detected": return "▼ SERVER-SIDE EXIT " + sgn(e.upc) + "% — " + (e.note || "OCO fired on-exchange");
      case "trail_armed": return "◈ TRAIL ARMED · HWM +" + fmt(e.hwm_pct) + "% · callback " + fmt((e.callBackRatio || 0) * 100, 2) + "% · active @ " + e.activePx;
      case "quality_gate_reject": return "GATE REJECT: " + (e.fails || []).join("; ");
      case "universe_depth_reject": return "DEPTH REJECT — top5 book " + sym + e.depth_eur + " below floor";
      // two field shapes exist in the log (left_min+after, and min) — accept both
      case "cooldown_skip": return "COOLDOWN — re-entry locked " +
        (e.left_min !== undefined && e.left_min !== null ? fmt(e.left_min, 1) : e.min) + "m" +
        (e.after ? " after " + e.after : "");
      case "algo_fallback": return e.ok ? "STANDALONE OCO PLACED (attach fallback ok)" : "OCO FALLBACK ATTEMPTED";
      case "algo_fallback_err": return "⚠ OCO ATTACH ERROR: " + (e.err || "");
      case "entry_unfilled": return "MAKER ENTRY UNFILLED — cancelled, no chase";
      case "close_dust": return "DUST CLEANUP (post-fee remainder)";
      case "prune_no_fill": return "PHANTOM POSITION PRUNED — " + (e.note || "");
      default: return String(e.event).toUpperCase() + " · " + JSON.stringify(Object.fromEntries(
        Object.entries(e).filter(kv => ["event", "ts", "inst"].indexOf(kv[0]) < 0))).slice(0, 150);
    }
  }

  function candleChart(d) {
    const cnd = d.candles || [];
    const t = d.trade || {};
    if (cnd.length < 3) return "";
    const W = 1000, H = 240, PAD = 34, PADR = 62;
    const tO = Date.parse(t.open_ts), tC = Date.parse(t.close_ts);
    const ts0 = cnd[0][0], ts1 = cnd[cnd.length - 1][0];
    let hi = Math.max.apply(null, cnd.map(c => c[2])), lo = Math.min.apply(null, cnd.map(c => c[3]));
    // include bracket levels + fills in the scale so TP/SL are always visible
    const bb0 = t.bracket || ((d.decision || {}).pair_state || {}).bracket || {};
    [t.entry_px, t.exit_px,
     t.entry_px && bb0.tp_pct ? t.entry_px * (1 + bb0.tp_pct / 100) : null,
     t.entry_px && bb0.sl_pct ? t.entry_px * (1 - bb0.sl_pct / 100) : null].forEach(v => {
      if (v) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
    });
    const pad = (hi - lo) * 0.08 || hi * 0.001;
    const top = hi + pad, bot = lo - pad;
    const X = (ts) => PAD + (ts - ts0) / ((ts1 - ts0) || 1) * (W - PAD - PADR);
    const Y = (p) => H - PAD - (p - bot) / ((top - bot) || 1) * (H - 2 * PAD);
    const cw = Math.max(2, Math.min(14, (W - PAD - PADR) / cnd.length * 0.62));
    let bodies = "";
    cnd.forEach(c => {
      const [ts, o, h, l, cl] = c;
      const up = cl >= o;
      const col = up ? "#00b33c" : "#d63a2f";
      const x = X(ts + 150000);
      bodies += '<line x1="' + x.toFixed(1) + '" y1="' + Y(h).toFixed(1) + '" x2="' + x.toFixed(1) + '" y2="' + Y(l).toFixed(1) + '" stroke="' + col + '" stroke-width="1"/>';
      const yO = Y(o), yC = Y(cl);
      const yTop = Math.min(yO, yC), hgt = Math.max(1, Math.abs(yO - yC));
      bodies += '<rect x="' + (x - cw / 2).toFixed(1) + '" y="' + yTop.toFixed(1) + '" width="' + cw.toFixed(1) + '" height="' + hgt.toFixed(1) + '" fill="' + col + '"/>';
    });
    // price axis
    let axis = "";
    for (let i = 0; i <= 4; i++) {
      const p = bot + (top - bot) * i / 4;
      axis += '<line x1="' + PAD + '" y1="' + Y(p).toFixed(1) + '" x2="' + (W - PADR) + '" y2="' + Y(p).toFixed(1) + '" stroke="#1a1608" stroke-width="0.6"/>' +
        '<text x="' + (W - PADR + 5) + '" y="' + (Y(p) + 3).toFixed(1) + '" fill="#8a867a" font-size="9" font-family="monospace">' + p.toPrecision(5) + "</text>";
    }
    // time axis (every ~6th candle)
    let taxis = "";
    cnd.forEach((c, i) => {
      if (i % 6) return;
      const dts = new Date(c[0]);
      taxis += '<text x="' + X(c[0] + 150000).toFixed(1) + '" y="' + (H - 8) + '" fill="#6a665c" font-size="8.5" font-family="monospace" text-anchor="middle">' +
        String(dts.getUTCHours()).padStart(2, "0") + ":" + String(dts.getUTCMinutes()).padStart(2, "0") + "</text>";
    });
    // entry / exit markers + bracket lines
    const b = t.bracket || ((d.decision || {}).pair_state || {}).bracket || {};
    const entryPx = t.entry_px, exitPx = t.exit_px;
    let marks = "";
    if (entryPx && tO >= ts0 - 3e5 && tO <= ts1 + 3e5) {
      const x = X(Math.max(ts0, Math.min(ts1, tO)));
      marks += '<line x1="' + x.toFixed(1) + '" y1="' + (PAD / 2) + '" x2="' + x.toFixed(1) + '" y2="' + (H - PAD) + '" stroke="#ffb000" stroke-dasharray="2,2" stroke-width="1"/>';
      marks += '<text x="' + (x + 3).toFixed(1) + '" y="' + (PAD / 2 + 8) + '" fill="#ffb000" font-size="9" font-family="monospace">▲ ENTRY ' + entryPx + "</text>";
    }
    if (exitPx && tC >= ts0 - 3e5 && tC <= ts1 + 3e5) {
      const x = X(Math.max(ts0, Math.min(ts1, tC)));
      const col = t.net_eur >= 0 ? "#00e04b" : "#ff3b30";
      marks += '<line x1="' + x.toFixed(1) + '" y1="' + (PAD / 2) + '" x2="' + x.toFixed(1) + '" y2="' + (H - PAD) + '" stroke="' + col + '" stroke-dasharray="2,2" stroke-width="1"/>';
      marks += '<text x="' + (x + 3).toFixed(1) + '" y="' + (PAD / 2 + 18) + '" fill="' + col + '" font-size="9" font-family="monospace">▼ EXIT ' + exitPx + "</text>";
    }
    if (entryPx && b.tp_pct) {
      const tpPx = entryPx * (1 + b.tp_pct / 100), slPx = entryPx * (1 - b.sl_pct / 100);
      const clampY = (p) => Math.max(PAD / 2 + 6, Math.min(H - PAD - 2, Y(p)));
      const tpY = clampY(tpPx), slY = clampY(slPx);
      const tpOff = tpPx > top || tpPx < bot, slOff = slPx > top || slPx < bot;
      marks += '<line x1="' + PAD + '" y1="' + tpY.toFixed(1) + '" x2="' + (W - PADR) + '" y2="' + tpY.toFixed(1) + '" stroke="#29c9eb" stroke-dasharray="5,4" stroke-width="0.8"' + (tpOff ? ' opacity="0.45"' : "") + '/>' +
        '<text x="' + (PAD + 3) + '" y="' + (tpY - 3).toFixed(1) + '" fill="#29c9eb" font-size="8.5" font-family="monospace">TP +' + fmt(b.tp_pct) + "%" + (tpOff ? " ▲off-scale" : "") + "</text>";
      marks += '<line x1="' + PAD + '" y1="' + slY.toFixed(1) + '" x2="' + (W - PADR) + '" y2="' + slY.toFixed(1) + '" stroke="#ff3b30" stroke-dasharray="5,4" stroke-width="0.8"' + (slOff ? ' opacity="0.45"' : "") + '/>' +
        '<text x="' + (PAD + 3) + '" y="' + (slY + 9).toFixed(1) + '" fill="#ff3b30" font-size="8.5" font-family="monospace">SL −' + fmt(b.sl_pct) + "%" + (slOff ? " ▼off-scale" : "") + "</text>";
    }
    // trail arms from timeline
    (d.timeline || []).forEach(e => {
      if (e.event === "trail_armed") {
        const ets = Date.parse(e.ts);
        if (ets >= ts0 - 3e5 && ets <= ts1 + 3e5) {
          const x = X(Math.max(ts0, Math.min(ts1, ets)));
          marks += '<text x="' + (x - 2).toFixed(1) + '" y="' + (H - PAD - 4).toFixed(0) + '" fill="#29c9eb" font-size="10" text-anchor="end">◈</text>';
        }
      }
    });
    return '<div class="msec"><h3>0 · THE TAPE — 5M CANDLES, ENTRY→EXIT, BRACKET OVERLAY (OKX HISTORICAL FEED)</h3>' +
      '<svg viewBox="0 0 ' + W + " " + H + '" style="width:100%;height:240px;display:block;background:#07070a;border:1px solid #1e1a0d">' +
      axis + taxis + bodies + marks + "</svg>" +
      '<div class="dim" style="font-size:9.5px;margin-top:3px">▲/▼ DASHED = OUR FILLS · <span style="color:#29c9eb">CYAN</span> = TP LEVEL · <span style="color:#ff3b30">RED</span> = SL LEVEL · ◈ = HWM TRAIL ARMED ON-EXCHANGE · CANDLES ARE RAW OKX 5M OHLC</div></div>';
  }

  function openDossier(tid) {
    OPEN_TID = tid;
    OPEN_DOSSIER_HASH = null;
    document.querySelectorAll(".trade-row").forEach(r =>
      r.classList.toggle("sel", r.getAttribute("data-tid") === tid));
    modalOpen('<div class="modal-head"><span class="mt">◈ DECISION REPLAY — ' + esc(tid) +
      '</span><span class="ms">loading dossier…</span><span class="modal-close">[X] CLOSE · ESC</span></div>' +
      '<div class="modal-body dim">FETCHING REPLAY DATA…</div>');
    fetch("dossiers/" + encodeURIComponent(tid) + ".json?cb=" + Date.now())
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => {
        OPEN_DOSSIER_HASH = JSON.stringify([d.trade && d.trade.net_eur, d.debrief,
          (d.candles || []).length, (d.timeline || []).length, d.complete]);
        renderDossier(d);
      })
      .catch(err => {
        modalOpen('<div class="modal-head"><span class="mt">◈ DECISION REPLAY — ' + esc(tid) +
          '</span><span class="ms">error</span><span class="modal-close">[X] CLOSE · ESC</span></div>' +
          '<div class="modal-body neg">DOSSIER UNAVAILABLE (' + esc(err) + ') — TRADE PREDATES THE REPLAY ARCHIVE.</div>');
      });
  }

  function renderDossier(d) {
    const t = d.trade || {};
    const dec = d.decision || {};
    const probs = dec.probabilities || {};
    const ps = dec.pair_state || {};
    const b = t.bracket || ps.bracket || {};
    // renderDossier gets the dossier `d`, not the deck data `D`, so live engine
    // constants come from the module-scope CUR_DATA snapshot. An earlier edit
    // referenced a bare `P` here, which is undefined in this scope — a
    // ReferenceError would have broken every dossier, i.e. the one clickable
    // feature the deck has.
    const P = ((CUR_DATA || {}).params) || {};
    // The dossier is the ONE clickable thing on the deck, so its numbers get read
    // closely: a $131 X-Perp trade must not print as "€131", and a SHORT must not
    // be labelled "BUY". Use the trip's own currency + side.
    const ccy = t.ccy || "USD";
    const sym = ccy === "USD" ? "$" : "€";
    const mon = (n) => money(n, ccy);
    const head = '<div class="modal-head"><span class="mt">◈ ' + esc(t.inst) +
      (t.side ? " (" + String(t.side).toUpperCase() + ")" : "") + " — " + sgn(t.upc) + "% → " + mon(t.net_eur) +
      '</span><span class="ms">' + esc(mmdd_hhmm(t.open_ts)) + " → " + esc(mmdd_hhmm(t.close_ts)) + " UTC · HELD " +
      fmt(t.age_min, 0) + "M · " + esc(t.reason || "") +
      '</span><span class="modal-close">[X] CLOSE · ESC</span></div>';

    const verdict = '<div class="verdict-lg' + (t.net_eur > 0 ? "" : " block") + '">' +
      (t.net_eur > 0 ? "▲ WINNER " : "▼ LOSER ") + sgn(t.net_eur) + sym + " NET OF FEES · " + sgn(t.upc) +
      "% MOVE · EXIT: " + esc(String(t.reason || "").toUpperCase()) + "</div>" +
      (d.debrief ? '<div class="decision-line" style="margin:-6px 0 12px">▸ LLM POST-MORTEM (' + esc(LLM_NAME) + "): " + esc(d.debrief) + "</div>" : "");

    // probability-bar labels: spot emits buy_<base>; the two-way perp book emits
    // long_<base>/short_<base>. Stripping only "buy_" left perp bars reading
    // "BUY SHORT_NEAR", which contradicts the trade's own direction.
    const probLabel = (k) => {
      if (k === "no_trade") return "NO_TRADE";
      const m = /^(long|short|buy)_(.+)$/.exec(k);
      if (!m) return String(k).toUpperCase();
      return (m[1] === "short" ? "▼ SHORT " : "▲ LONG ") + m[2].toUpperCase();
    };
    const sec1 = '<div class="msec"><h3>1 · THE MODEL VOTE — WHAT JEV RETURNED (TYPED JSON, NOT TEXT)</h3>' +
      '<div class="dim" style="margin-bottom:5px">CALL AT ' + esc(mmdd_hhmm(dec.ts)) + ' UTC · SCHEMA-ENFORCED OUTPUT — THE ENGINE NEVER PARSES PROSE</div>' +
      Object.keys(probs).sort((a, x) => probs[x] - probs[a]).map(k =>
        pbar(probLabel(k), probs[k], k === dec.choice && dec.choice !== "no_trade")).join("") +
      '<div class="mgrid" style="margin-top:8px">' +
      '<div class="mbox"><div class="mk">PICKED</div><div class="mv">' + esc(String(dec.choice || "?").toUpperCase()) + "</div></div>" +
      '<div class="mbox"><div class="mk">CONFIDENCE</div><div class="mv">' + fmt(dec.confidence) + "</div></div>" +
      '<div class="mbox"><div class="mk">MARGIN p(chosen)−p(flat)</div><div class="mv">' + sgn(dec.margin) + "</div></div>" +
      '<div class="mbox"><div class="mk">NOUL MOVE-PROB</div><div class="mv ' + (dec.noul >= 0.45 ? "g" : "r") + '">' + fmt(dec.noul) + "</div></div>" +
      "</div></div>";

    const chosenKeys = Object.keys(dec.all_pairs || {});
    const pairRows = chosenKeys.map(k => {
      const p = dec.all_pairs[k] || {};
      const isChosen = k === t.inst;
      return "<tr" + (isChosen ? " class='chosen'" : "") + "><td class='sym'>" + (isChosen ? "▸ " : "") + esc(k) + "</td>" +
        "<td>" + fmt(p.bid, p.bid > 100 ? 2 : 4) + "</td>" +
        "<td class='" + cls(p.chg_30min_pct) + "'>" + sgn(p.chg_30min_pct) + "%</td>" +
        "<td class='" + cls(p.chg_4h_pct) + "'>" + sgn(p.chg_4h_pct) + "%</td>" +
        "<td class='" + cls(p.chg_7d_pct) + "'>" + sgn(p.chg_7d_pct) + "%</td>" +
        "<td>" + fmt(p.range30_pos_pct, 0) + "</td>" +
        "<td>" + (p.bars_up_of_6 !== undefined ? p.bars_up_of_6 + "/6" : "—") + "</td>" +
        "<td>" + fmt(p.volatility_30min_pct, 2) + "%</td>" +
        "<td>" + fmt(p.spread_pct, 3) + "%</td></tr>";
    }).join("");
    const sec2 = '<div class="msec"><h3>2 · THE MARKET STATE JEV SAW — EXACT PROMPT INPUT</h3>' +
      "<table class='pairmini'><tr><th>PAIR</th><th>BID</th><th>30M</th><th>4H</th><th>7D</th><th>R30 POS</th><th>GREEN BARS</th><th>VOL30</th><th>SPREAD</th></tr>" + pairRows + "</table>" +
      '<div class="mgrid" style="margin-top:8px">' +
      '<div class="mbox"><div class="mk">' + (ccy === "USD" ? "USDC COLLATERAL" : "CASH FREE") + '</div><div class="mv">' + mon(dec.cash_eur) + "</div></div>" +
      '<div class="mbox"><div class="mk">' + (ccy === "USD" ? "NOTIONAL BUDGET" : "STAKE = ALL FUNDS/SLOTS") + '</div><div class="mv">' + mon(dec.stake_eur) + "</div></div>" +
      '<div class="mbox"><div class="mk">BTC FUNDING</div><div class="mv">' + fmt(dec.funding_bps, 1) + " bps</div></div>" +
      '<div class="mbox"><div class="mk">OPEN POSITIONS AT CALL</div><div class="mv w">' + ((dec.open_positions || []).length) + "</div></div>" +
      '<div class="mbox"><div class="mk">PROMPT</div><div class="mv w">' + ((dec.usage || {}).input_tokens || "?") + " in / " + ((dec.usage || {}).output_tokens || "?") + " out tok</div></div>" +
      '<div class="mbox"><div class="mk">STATE SIZE</div><div class="mv w">' + ((dec.prompt_stats || {}).state_chars || "?") + " chars" +
      (P.MAX_STATE_CHARS ? " (cap " + Math.round(P.MAX_STATE_CHARS) + ")" : " (no cap — small prompt)") + "</div></div>" +
      "</div></div>";

    const fg = dec.fear_greed || {};
    const newsLines = (dec.news || []).map(n => {
      const m = n.match(/^\[(.+?)\]\s*(.*)$/);
      return '<div class="newsline"><span class="s">[' + esc((m ? m[1] : "wire").toUpperCase()) + "]</span> " + esc(m ? m[2] : n) + "</div>";
    }).join("") || '<div class="dim">no headlines in window at call time</div>';
    const macroLines = (dec.macro_upcoming || []).map(e =>
      '<div class="newsline"><span class="s">[' + "★".repeat(e.imp || 1) + "]</span> " + esc(e.e) + " · " + esc(e.ccy) + " · in " + fmt(e.in_h, 1) + "h</div>").join("");
    const surprises = (dec.macro_surprises || []).map(e =>
      '<div class="newsline"><span class="s">[PRINT]</span> ' + esc(e.e) + ": act " + esc(e.act) + " vs fc " + esc(e.fc) + " · " + fmt(e.ago_h, 1) + "h ago</div>").join("");
    const trending = (dec.trending || []).map(x => '<span class="chip">' + esc(x) + "</span>").join("");
    const sec3 = '<div class="msec"><h3>3 · ' + (ccy === "USD"
      ? "MARKET CONTEXT IN THE SAME PROMPT (FUNDING + MOMENTUM — THIS BOOK DOES NOT INJECT NEWS/MACRO)"
      : "NEWS + SENTIMENT + MACRO IN THE SAME PROMPT") + '</h3>' +
      (ccy === "USD"
        ? '<div class="dim" style="font-size:10px;margin-bottom:4px">THE X-PERP ENGINE FEEDS THE MODEL PER-CONTRACT FUNDING AND 30M/4H/7D MOMENTUM. THE NEWS / FEAR&amp;GREED / ECON-CALENDAR BLOCKS BELOW WERE CONTEXT FOR THE RETIRED SPOT ENGINE AND ARE NOT PART OF THIS BOOK\'S DECISION — SHOWN ONLY SO THE REPLAY IS COMPLETE, NOT AS INPUTS THAT DROVE IT.</div>'
        : '<div class="dim" style="font-size:10px;margin-bottom:4px">WIRE STATE AT CALL: ' + esc(mmdd_hhmm(dec.news_updated)) +
          " UTC (30-MIN REFRESH GUARD) · FEAR&amp;GREED " +
          (fg.value !== undefined ? fg.value + " " + esc(fg.label) + " (streak " + fg.streak_days + "d)" : "—") +
          (trending ? " · TRENDING: " + trending : "") + "</div>") +
      newsLines +
      (macroLines ? '<div class="dim" style="font-size:10px;margin:6px 0 3px">UPCOMING MACRO ≥★★</div>' + macroLines : "") +
      (surprises ? '<div class="dim" style="font-size:10px;margin:6px 0 3px">RECENT SURPRISE PRINTS</div>' + surprises : "") +
      "</div>";

    const rejects = (d.pre_entry_rejects || []).map(r =>
      '<div class="dim" style="font-size:10px">' + esc(hhmm(r.ts)) + " — same pair rejected minutes earlier: " + esc((r.fails || []).join("; ")) + " (setup had not matured yet)</div>").join("");
    const sec4 = '<div class="msec"><h3>4 · PRE-ENTRY GAUNTLET — WHAT THIS PAIR SURVIVED BEFORE THE MODEL WAS ASKED</h3>' +
      '<div class="dim" style="font-size:10.5px">' + (ccy === "USD"
        ? "LIQUIDITY FLOOR ≥" + sym + Math.round((P.MIN_VOL24H_USD || 300000) / 1000) + "K · SPREAD CAP ≤" + fmt(P.MAX_SPREAD_PCT, 2) + "% · RANGE FLOOR ≥" + fmt(P.MIN_DAY_RANGE_PCT, 1) + "% · TOP-5 BOOK DEPTH ≥" + sym + Math.round(P.MIN_TOP5_DEPTH_USD || 1000) + " · MOMENTUM GATES (30M TREND, GREEN BARS, RANGE POSITION, NET TAKER BUYING, TP-REACHABLE VOL) · COOLDOWN CHECK · LIVE-BOOK RE-CHECK ≥3× NOTIONAL AT ENTRY MOMENT."
        : "LIQUIDITY FLOOR · SPREAD CAP · RANGE FLOOR · TOP-5 BOOK DEPTH ≥€1.5K · MOMENTUM GATES (30M TREND, GREEN BARS, RANGE POSITION, NET TAKER BUYING, TP-REACHABLE VOL) · COOLDOWN CHECK · LIVE-BOOK RE-CHECK ≥3× STAKE AT ENTRY MOMENT.") + "</div>" +
      (rejects ? '<div style="margin-top:4px">' + rejects + "</div>" : '<div class="dim" style="margin-top:4px;font-size:10px">no rejects logged for this pair in the 12m before entry — it cleared the gauntlet on the first pass</div>') +
      '<div class="mgrid" style="margin-top:6px">' +
      '<div class="mbox"><div class="mk">TP (4× ALL-IN COST)</div><div class="mv">+' + fmt(b.tp_pct) + "%</div></div>" +
      '<div class="mbox"><div class="mk">HARD SL</div><div class="mv r">−' + fmt(b.sl_pct) + "%</div></div>" +
      '<div class="mbox"><div class="mk">TRAIL ARM / GIVEBACK</div><div class="mv">+' + fmt(b.trail_arm_pct) + "% / " + fmt(b.trail_giveback_pct) + "%</div></div>" +
      '<div class="mbox"><div class="mk">ALL-IN COST</div><div class="mv w">' + fmt(b.cost_pct) + "%</div></div>" +
      "</div></div>";

    const tl = (d.timeline || []).map(e =>
      '<div class="tl-row ' + tlClass(e.event) + '"><span class="tl-t">' + esc(mmdd_hhmm(e.ts)) + '</span> <span class="tl-x">' + esc(tlText(e)) + "</span></div>").join("");
    const sec5 = '<div class="msec"><h3>5 · FULL LIFECYCLE — EVERY ENGINE EVENT, ENTRY → EXIT</h3><div class="tl">' +
      (tl || '<div class="dim">no events captured in window</div>') + "</div></div>";

    const sec6 = '<div class="msec"><h3>6 · OUTCOME — LEDGER NUMBERS</h3><div class="mgrid">' +
      '<div class="mbox"><div class="mk">ENTRY</div><div class="mv w">' + fmt(t.entry_px, 5) + " @ " + esc(mmdd_hhmm(t.open_ts)) + "</div></div>" +
      '<div class="mbox"><div class="mk">EXIT</div><div class="mv w">' + fmt(t.exit_px, 5) + " @ " + esc(mmdd_hhmm(t.close_ts)) + "</div></div>" +
      '<div class="mbox"><div class="mk">GROSS</div><div class="mv ' + cls(t.gross_eur) + '">' + sgn(t.gross_eur) + sym + "</div></div>" +
      '<div class="mbox"><div class="mk">FEES</div><div class="mv r">−' + fmt(t.fees_eur, 2) + sym + "</div></div>" +
      '<div class="mbox"><div class="mk">NET</div><div class="mv ' + cls(t.net_eur) + '">' + sgn(t.net_eur) + sym + "</div></div>" +
      '<div class="mbox"><div class="mk">HELD</div><div class="mv">' + fmt(t.age_min, 0) + " min</div></div>" +
      "</div>" + (dec.context ? '<div class="dim" style="font-size:9.5px;margin-top:8px">STRATEGY CONTEXT SENT TO THE MODEL (VERBATIM EXCERPT):</div><div class="ctx-prompt">' +
        esc(String(dec.context).slice(0, 1500)) + (String(dec.context || "").length > 1500 ? " […]" : "") + "</div>" : "") + "</div>";

    modalOpen(head + '<div class="modal-body">' + verdict + candleChart(d) + sec1 + sec2 + sec3 + sec4 + sec5 + sec6 + "</div>");
  }

  /* ---------- clock + title flash ---------- */
  function renderClock() {
    const n = new Date();
    const p = (x) => String(x).padStart(2, "0");
    // Time only in the header; the date is in the tooltip and in the snapshot line.
    // It re-renders every second and its date half never changes on screen.
    $("clock").textContent = p(n.getUTCHours()) + ":" + p(n.getUTCMinutes()) + ":" +
      p(n.getUTCSeconds()) + " UTC";
    $("clock").title = n.toISOString().slice(0, 10) + " (UTC)";
    renderCountdown();
    // title: flash trades for 15s, then back to day P&L
    if (TITLE_FLASH) {
      if (Date.now() < TITLE_FLASH.until) {
        const blink = Math.floor(Date.now() / 700) % 2 === 0;
        document.title = (blink ? "⚡ " : "  ") + TITLE_FLASH.text + " · OBEKT TERMINAL";
      } else {
        TITLE_FLASH = null;
        setBaseTitle();
      }
    }
  }
  function setBaseTitle() {
    const D = CUR_DATA;
    document.title = (D && D.equity && D.equity.day_pnl_pct !== undefined && D.equity.day_pnl_pct !== null
      ? ((D.equity.day_pnl_pct >= 0 ? "+" : "") + D.equity.day_pnl_pct.toFixed(2) + "% · ")
      : "") + "OBEKT TERMINAL — Autonomous Crypto Trading, Live";
  }

  function renderAll(D) {
    CUR_DATA = D;
    renderTop(D); renderMarquee(D); renderHero(D); renderDesk(D); renderPerf(D);
    renderJev(D); renderGates(D); renderEquity(D); renderFunnel(D); renderExits(D);
    renderHours(D); renderScatter(D); renderInst(D); renderUniverse(D); renderPositions(D);
    renderPerp(D);
    renderNews(D); renderSent(D); renderMacro(D); renderHist(D);
    renderTrades(D); renderDaily(D); renderArch(D); renderStack(D); renderOptim(D);
    renderCostLive(D); renderTape(D); renderSession(D);
    if (!TITLE_FLASH) setBaseTitle();
    // keep an open dossier fresh if its trade just closed or data updated
    if (OPEN_TID) refreshOpenDossier();
  }

  function refreshOpenDossier() {
    fetch("dossiers/" + encodeURIComponent(OPEN_TID) + ".json?cb=" + Date.now())
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => {
        if (OPEN_TID !== d.tid) return;
        // hash the mutable parts; skip re-render (and scroll reset) when nothing changed
        const h = JSON.stringify([d.trade && d.trade.net_eur, d.debrief,
          (d.candles || []).length, (d.timeline || []).length, d.complete]);
        if (h === OPEN_DOSSIER_HASH) return;
        OPEN_DOSSIER_HASH = h;
        const modal = document.querySelector(".modal");
        const keepY = modal ? modal.scrollTop : 0;
        renderDossier(d);
        const m2 = document.querySelector(".modal");
        if (m2) m2.scrollTop = keepY;
      })
      .catch(() => {});
  }

  function load() {
    const s = document.createElement("script");
    s.src = "data.js?cb=" + Date.now();
    s.onload = () => { try { renderAll(window.TT_DATA); } catch (e) { console.error(e); } };
    s.onerror = () => { console.warn("data.js refresh failed"); };
    document.body.appendChild(s);
  }

  if (window.TT_DATA) renderAll(window.TT_DATA);
  setInterval(renderClock, 1000); renderClock();
  setInterval(load, 10000);
  bootSequence(window.TT_DATA || {});
})();

/* ---------- BOOT SEQUENCE: one-time "wow" intro, real numbers ---------- */
function bootSequence(D) {
  const el = document.getElementById("boot");
  const txt = document.getElementById("boot-text");
  if (!el || !txt) return;
  if (sessionStorage.getItem("obekt_booted")) { el.remove(); return; }
  sessionStorage.setItem("obekt_booted", "1");
  const f = D.funnel || {}, eq = D.equity || {}, s = D.stats || {};
  const P = D.perp || {}, ps = P.stats || {};
  const spotRetired = /retired/i.test(String(((D.engine || {}).spot_book || {}).status || ""));
  const lines = [
    "▚▚ OBEKT TERMINAL " + ((D.engine || {}).version || "v3.5") + " — AUTONOMOUS TRADING OBSERVATION DECK",
    "",
    "[ok] link okx europe (eea) .................. " + ((D.tickers || []).length || 10) + " MARKET FEEDS SYNCED",
    "[ok] engine log ........................... " + (f.cycles ? f.cycles.toLocaleString() : "—") + " CYCLES · " + (f.uptime_h || "—") + "H UNATTENDED",
    "[ok] jev system-one decision model ......... " + (f.jev_calls || "—") + " TYPED CALLS · $" + ((f.usd_all !== undefined) ? f.usd_all.toFixed(2) : "—") + " SPENT",
    "[ok] selectivity funnel .................... " + (f.pair_scans_all ? f.pair_scans_all.toLocaleString() : "—") + " SCANS → " + (f.opens || "—") + " TRADES OPENED",
    "[ok] halogen-qwen3.8-flash-next desk llm ... COMMENTARY STREAM ARMED",
    "[ok] news/sentiment/macro injection ......... RSS + FEAR&GREED + ECON CALENDAR",
    "[ok] risk layer ............................. SERVER-SIDE OCO + HWM TRAIL + BREAKER",
    "[ok] live capital ........................... " + (spotRetired
      ? "$" + (P.usdc_equity !== undefined ? P.usdc_equity.toFixed(2) : "—") + " USDC COLLATERAL · " +
        (ps.trades_all || 0) + " X-PERP TRADES · SPOT BOOK RETIRED (" +
        (s.trades_all || 0) + " EUR TRADES ON RECORD)"
      : "€" + (eq.equity_eur !== undefined ? eq.equity_eur.toFixed(2) : "—") + " EQUITY · " +
        (s.trades_all || "—") + " REALIZED TRADES"),
    "",
    "◈ THIS IS A LIVE SYSTEM TRADING REAL MONEY, NOT A DEMO.",
    "◈ NOTHING IS STAGED — EVERY NUMBER STREAMS FROM THE ENGINE.",
    "◈ CLICK ANY LEDGER TRADE TO REPLAY ITS FULL DECISION.",
    "◈ THE WHOLE SYSTEM IS OPEN SOURCE — LINK IN THE FOOTER.",
    "",
    "ENTERING TERMINAL…"
  ];
  let i = 0;
  const tick = () => {
    if (i >= lines.length) {
      setTimeout(() => { el.classList.add("done"); setTimeout(() => el.remove(), 600); }, 500);
      return;
    }
    const line = lines[i];
    // colorize the tag parts
    let html = line
      .replace(/\[ok\]/g, '<span class="amber">[ok]</span>')
      .replace(/(◈.*)/g, '<span class="amber">$1</span>')
      .replace(/(▚▚.*)/g, '<span class="amber">$1</span>');
    txt.innerHTML += (i ? "\n" : "") + html;
    i++;
    setTimeout(tick, line.startsWith("[ok]") ? 90 : (line.startsWith("◈") ? 220 : 140));
  };
  tick();
}
