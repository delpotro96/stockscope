/* InfraScope data layer for the Chrome extension.
 *
 * This is the JS port of server.py: it talks to Naver / Yahoo directly
 * (the extension's host_permissions bypass CORS, so no local proxy is
 * needed). It exposes window.InfraData.api(path), which returns a
 * fetch-Response-like object, so index.html can call it exactly like it
 * calls the local server's /api endpoints.
 *
 * Guard: only activates inside an extension. When index.html is served by
 * the Python server over http://, chrome.runtime.id is undefined, so
 * window.InfraData stays unset and the page falls back to fetch('/api/...').
 */
(function () {
  "use strict";
  if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.id) return;

  const TTL = 10000; // ms — mirrors server CACHE_TTL
  const cache = new Map();    // url -> { t, v }
  const inflight = new Map(); // url -> Promise

  const DAY_COUNTS = { "5d": 5, "1mo": 22, "3mo": 66, "6mo": 130, "1y": 248, "2y": 500, "5y": 1250 };
  const ALLOWED_RANGES = new Set(["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"]);
  const ALLOWED_INTERVALS = new Set(["1m", "2m", "5m", "15m", "30m", "60m", "1d", "1wk", "1mo"]);
  const DOMESTIC_STOCK_RE = /^([0-9A-Z]{6})\.(KS|KQ)$/;
  const DOMESTIC_INDEX = { "^KS11": "KOSPI", "^KQ11": "KOSDAQ" };
  const RANK_MARKETS = { KOSPI: ".KS", KOSDAQ: ".KQ" };
  const FCHART_ITEM_RE = /data="([^"]+)"/g;

  /* ---------- cached fetch with single-flight ---------- */
  // kind: "json" -> {status, data}; "eucxml" -> {status, text} (EUC-KR decoded)
  function cachedFetch(url, kind) {
    const hit = cache.get(url);
    if (hit && Date.now() - hit.t < TTL) return Promise.resolve(hit.v);
    if (inflight.has(url)) return inflight.get(url);
    const p = (async () => {
      let v;
      try {
        const resp = await fetch(url);
        if (kind === "eucxml") {
          const buf = await resp.arrayBuffer();
          v = { status: resp.status, text: new TextDecoder("euc-kr").decode(buf) };
        } else {
          const text = await resp.text();
          let data = null;
          try { data = JSON.parse(text); } catch (e) { /* leave null */ }
          v = { status: resp.status, data };
        }
        if (resp.status === 200) cache.set(url, { t: Date.now(), v });
      } catch (e) {
        v = { status: 502, data: null, text: "" };
      } finally {
        inflight.delete(url);
      }
      return v;
    })();
    inflight.set(url, p);
    return p;
  }

  /* ---------- domestic (Naver) helpers — ports of server.py ---------- */
  function kstEpoch(stamp) {
    const y = +stamp.slice(0, 4), mo = +stamp.slice(4, 6), da = +stamp.slice(6, 8);
    let hh = 15, mm = 30;
    if (stamp.length >= 12) { hh = +stamp.slice(8, 10); mm = +stamp.slice(10, 12); }
    // wall-clock is KST (UTC+9); subtract 9h to get the UTC epoch
    return Math.floor(Date.UTC(y, mo - 1, da, hh, mm) / 1000) - 9 * 3600;
  }

  async function fchartRows(nsym, timeframe, count) {
    const url = `https://fchart.stock.naver.com/sise.nhn?symbol=${encodeURIComponent(nsym)}` +
                `&timeframe=${timeframe}&count=${count}&requestType=0`;
    const r = await cachedFetch(url, "eucxml");
    if (r.status !== 200) return [];
    const rows = [];
    FCHART_ITEM_RE.lastIndex = 0;
    let m;
    while ((m = FCHART_ITEM_RE.exec(r.text)) !== null) {
      const parts = m[1].split("|"); // time|open|high|low|close|volume
      if (parts.length >= 6 && parts[4] !== "" && parts[4] !== "null") {
        const close = parseFloat(parts[4]);
        if (!isFinite(close)) continue;
        let vol = (parts[5] !== "" && parts[5] !== "null") ? parseFloat(parts[5]) : 0;
        if (!isFinite(vol)) vol = 0;
        rows.push([parts[0], close, vol]);
      }
    }
    return rows;
  }

  async function indexMinuteRows(nsym) {
    const url = `https://api.stock.naver.com/chart/domestic/index/${encodeURIComponent(nsym)}/minute?count=500`;
    const r = await cachedFetch(url, "json");
    if (r.status !== 200 || !Array.isArray(r.data)) return [];
    const rows = [];
    for (const it of r.data) {
      const t = String((it && it.localDateTime) || "");
      const v = it && it.currentPrice;
      if (t.length >= 12 && v != null) {
        const vol = (it.accumulatedTradingVolume) || 0;
        rows.push([t.slice(0, 12), Number(v), Number(vol)]);
      }
    }
    return rows;
  }

  function splitSessions(rows) {
    const byDay = {};
    for (const row of rows) {
      const t = row[0], hm = t.slice(8, 12);
      if (hm >= "0900" && hm <= "1535") (byDay[t.slice(0, 8)] || (byDay[t.slice(0, 8)] = [])).push(row);
    }
    return byDay;
  }

  function downsample(rows, minutes) {
    const buckets = {};
    for (const row of rows) {
      const t = row[0];
      const bucket = Math.floor((+t.slice(8, 10) * 60 + +t.slice(10, 12)) / minutes);
      buckets[t.slice(0, 8) + String(bucket).padStart(4, "0")] = row;
    }
    return Object.keys(buckets).sort().map(k => buckets[k]);
  }

  function cumToDelta(series) {
    const out = [];
    let prevDay = null, prev = 0;
    for (const [t, c, v] of series) {
      const day = t.slice(0, 8);
      const dv = day !== prevDay ? v : Math.max(0, v - prev);
      out.push([t, c, dv]);
      prevDay = day; prev = v;
    }
    return out;
  }

  async function prevSessionClose(nsym, beforeDate) {
    const rows = await fchartRows(nsym, "day", 10);
    const closes = rows.filter(r => r[0].slice(0, 8) < beforeDate).map(r => r[1]);
    return closes.length ? closes[closes.length - 1] : null;
  }

  async function naverChart(symbol, nsym, isIndex, rng) {
    let prevClose = null, series = [];
    if (rng === "1d") {
      const rows = isIndex ? await indexMinuteRows(nsym) : await fchartRows(nsym, "minute", 1500);
      const byDay = splitSessions(rows);
      const days = Object.keys(byDay);
      if (days.length) {
        const latest = days.reduce((a, b) => (a > b ? a : b));
        series = byDay[latest];
        if (!isIndex) series = cumToDelta(series);
        prevClose = await prevSessionClose(nsym, latest);
      } else if (isIndex) {
        series = await fchartRows(nsym, "day", 10);
        if (series.length >= 2) prevClose = series[series.length - 2][1];
      }
    } else if (rng === "5d" && !isIndex) {
      const byDay = splitSessions(await fchartRows(nsym, "minute", 3000));
      series = [];
      for (const date of Object.keys(byDay).sort().slice(-5)) series.push(...byDay[date]);
      series = cumToDelta(downsample(series, 10));
    } else {
      series = await fchartRows(nsym, "day", DAY_COUNTS[rng] || 248);
    }
    if (!series.length) {
      return { status: 502, body: { chart: { result: null, error: { description: "no domestic data" } } } };
    }
    const meta = {
      currency: "KRW", symbol, exchangeTimezoneName: "Asia/Seoul",
      regularMarketPrice: series[series.length - 1][1],
    };
    if (prevClose != null) { meta.chartPreviousClose = prevClose; meta.previousClose = prevClose; }
    return { status: 200, body: { chart: { result: [{
      meta,
      timestamp: series.map(r => kstEpoch(r[0])),
      indicators: { quote: [{ close: series.map(r => r[1]), volume: series.map(r => Math.round(r[2])) }] },
    }], error: null } } };
  }

  /* ---------- search ---------- */
  function hasHangul(s) { return /[ㄱ-ㆎ가-힣]/.test(s); }

  async function searchNaver(q) {
    const url = `https://ac.stock.naver.com/ac?q=${encodeURIComponent(q)}&target=stock`;
    const r = await cachedFetch(url, "json");
    if (r.status !== 200 || !r.data) return [];
    const items = r.data.items || [];
    const out = [];
    for (const it of items) {
      const code = it.code, name = it.name;
      const type = (it.typeCode || "").toUpperCase();
      if (!code || !name) continue;
      let suffix;
      if (type.startsWith("KOSPI")) suffix = ".KS";
      else if (type.startsWith("KOSDAQ")) suffix = ".KQ";
      else continue;
      out.push({ symbol: code + suffix, shortname: name, longname: name, quoteType: "EQUITY", exchDisp: type });
    }
    return out;
  }

  async function searchYahoo(q) {
    const url = `https://query2.finance.yahoo.com/v1/finance/search` +
                `?q=${encodeURIComponent(q)}&quotesCount=12&newsCount=0&listsCount=0`;
    const r = await cachedFetch(url, "json");
    if (r.status !== 200 || !r.data) return [];
    return r.data.quotes || [];
  }

  const DOMESTIC_INDEX_SEARCH = [
    ["^KS11", "코스피 (KOSPI)", ["코스피", "kospi"]],
    ["^KQ11", "코스닥 (KOSDAQ)", ["코스닥", "kosdaq"]],
  ];
  function searchDomesticIndices(q) {
    const ql = q.toLowerCase();
    if (ql.length < 2) return [];
    return DOMESTIC_INDEX_SEARCH
      .filter(([, , keys]) => keys.some(k => k.startsWith(ql) || ql.startsWith(k)))
      .map(([sym, name]) => ({ symbol: sym, shortname: name, longname: name,
        quoteType: "INDEX", exchDisp: sym === "^KS11" ? "KOSPI" : "KOSDAQ" }));
  }

  async function handleSearch(q) {
    q = (q || "").trim();
    if (!q) return { status: 400, body: { error: "missing q" } };
    const kr = hasHangul(q);
    const naver = await searchNaver(q);
    const yahoo = kr ? [] : await searchYahoo(q);
    let ordered = kr ? naver.concat(yahoo) : yahoo.concat(naver);
    ordered = searchDomesticIndices(q).concat(ordered);
    const seen = new Set(), merged = [];
    for (const item of ordered) {
      const sym = item.symbol;
      if (sym && !seen.has(sym)) { seen.add(sym); merged.push(item); }
    }
    return { status: 200, body: { quotes: merged.slice(0, 12) } };
  }

  /* ---------- chart ---------- */
  async function handleChart(qs) {
    const symbol = (qs.get("symbol") || "").trim();
    const rng = qs.get("range") || "1d";
    const interval = qs.get("interval") || "5m";
    if (!symbol || symbol.length > 24) return { status: 400, body: { error: "bad symbol" } };
    if (!ALLOWED_RANGES.has(rng) || !ALLOWED_INTERVALS.has(interval)) {
      return { status: 400, body: { error: "bad range/interval" } };
    }
    const m = DOMESTIC_STOCK_RE.exec(symbol);
    const nsym = m ? m[1] : DOMESTIC_INDEX[symbol];
    if (nsym) return naverChart(symbol, nsym, symbol in DOMESTIC_INDEX, rng);
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
                `?range=${rng}&interval=${interval}&includePrePost=false`;
    const r = await cachedFetch(url, "json");
    return { status: r.status, body: r.data || { chart: { result: null, error: { description: "fetch failed" } } } };
  }

  /* ---------- ranking ---------- */
  function num(x) {
    const n = parseFloat(String(x).replace(/,/g, ""));
    return isFinite(n) ? n : null;
  }
  async function handleRank(qs) {
    const market = qs.get("market") || "KOSPI";
    const dir = qs.get("dir") || "up";
    if (!RANK_MARKETS[market] || (dir !== "up" && dir !== "down")) {
      return { status: 400, body: { error: "bad market/dir" } };
    }
    const url = `https://m.stock.naver.com/api/stocks/${dir}/${market}?page=1&pageSize=9`;
    const r = await cachedFetch(url, "json");
    if (r.status !== 200 || !r.data) return { status: 502, body: { error: "ranking unavailable" } };
    const stocks = r.data.stocks || [];
    const out = [];
    for (const s of stocks) {
      if (!s || typeof s !== "object") continue;
      const code = s.itemCode, name = s.stockName;
      const price = num(s.closePrice), ratio = num(s.fluctuationsRatio);
      if (!code || !name || price == null || ratio == null) continue;
      out.push({ symbol: code + RANK_MARKETS[market], name, price, changePct: ratio });
    }
    return { status: 200, body: { items: out } };
  }

  /* ---------- public api: mimics fetch(path) ---------- */
  async function api(path) {
    const u = new URL(path, "http://local");
    const qs = u.searchParams;
    let res;
    try {
      if (u.pathname === "/api/search") res = await handleSearch(qs.get("q") || "");
      else if (u.pathname === "/api/chart") res = await handleChart(qs);
      else if (u.pathname === "/api/rank") res = await handleRank(qs);
      else res = { status: 404, body: { error: "not found" } };
    } catch (e) {
      res = { status: 500, body: { error: String(e) } };
    }
    return {
      ok: res.status >= 200 && res.status < 300,
      status: res.status,
      json: async () => res.body,
    };
  }

  window.InfraData = { api };
})();
