"""InfraScope local server.

Serves the dashboard UI and proxies market-data requests to Yahoo Finance
so the browser never talks to finance domains directly (no CORS issues,
and nothing finance-related shows up in the address bar).

Run:  python server.py
Open: http://127.0.0.1:8137
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HOST = "127.0.0.1"   # local only on purpose -- never expose on the LAN
PORT = 8137
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ALLOWED_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"}
ALLOWED_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "1d", "1wk", "1mo"}

CACHE_TTL = 10  # seconds; keeps refreshes snappy and avoids rate limits
_cache = {}
_cache_lock = threading.Lock()
_inflight = {}  # url -> threading.Event, guarded by _cache_lock


def _do_fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or json.dumps({"error": f"upstream {e.code}"}).encode()
    except Exception as e:  # noqa: BLE001 - report any network failure to the client
        return 502, json.dumps({"error": str(e)}).encode()


def _cache_get(url):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], hit[2]
    return None


def fetch_upstream(url):
    """Cached fetch with single-flight: concurrent requests for the same URL
    wait for the first fetch instead of all hitting the upstream at once."""
    while True:
        with _cache_lock:
            hit = _cache_get(url)
            if hit:
                return hit
            ev = _inflight.get(url)
            if ev is None:
                ev = _inflight[url] = threading.Event()
                break  # this thread owns the fetch
        ev.wait(timeout=20)
        with _cache_lock:
            hit = _cache_get(url)
            if hit:
                return hit
            if url not in _inflight:
                continue  # owner finished with a non-200; retry as new owner
        return _do_fetch(url)  # owner stuck past timeout; fetch directly
    try:
        status, body = _do_fetch(url)
        if status == 200:
            with _cache_lock:
                _cache[url] = (time.time(), status, body)
                if len(_cache) > 512:
                    oldest = min(_cache, key=lambda k: _cache[k][0])
                    _cache.pop(oldest, None)
        return status, body
    finally:
        with _cache_lock:
            _inflight.pop(url, None)
        ev.set()


# ---------- domestic (Naver) market data ----------
# Korean stocks/indices come from Naver instead of Yahoo: Yahoo delays KRX
# quotes by 15-20 minutes, Naver's chart feeds are near-realtime.
KST = timezone(timedelta(hours=9))
DOMESTIC_STOCK_RE = re.compile(r"^([0-9A-Z]{6})\.(KS|KQ)$")  # KRX short codes can contain letters
DOMESTIC_INDEX = {"^KS11": "KOSPI", "^KQ11": "KOSDAQ"}
FCHART_ITEM_RE = re.compile(r'data="([^"]+)"')
DAY_COUNTS = {"5d": 5, "1mo": 22, "3mo": 66, "6mo": 130, "1y": 248, "2y": 500, "5y": 1250}


def kst_epoch(stamp):
    """'20260610' or '202606101430' (KST) -> epoch seconds. Daily bars are
    stamped at the 15:30 market close."""
    y, mo, da = int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8])
    if len(stamp) >= 12:
        hh, mm = int(stamp[8:10]), int(stamp[10:12])
    else:
        hh, mm = 15, 30
    return int(datetime(y, mo, da, hh, mm, tzinfo=KST).timestamp())


def fchart_rows(nsym, timeframe, count):
    """fchart.stock.naver.com XML -> [(stamp, close, volume), ...] ascending.
    NOTE: minute-timeframe volume is cumulative within the day (convert via
    cum_to_delta); day-timeframe volume is already per-bar."""
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={quote(nsym)}"
           f"&timeframe={timeframe}&count={count}&requestType=0")
    status, body = fetch_upstream(url)
    if status != 200:
        return []
    rows = []
    for m in FCHART_ITEM_RE.finditer(body.decode("utf-8", "ignore")):
        parts = m.group(1).split("|")  # time|open|high|low|close|volume
        if len(parts) >= 6 and parts[4] not in ("", "null"):
            try:
                close = float(parts[4])
            except ValueError:
                continue
            try:
                vol = float(parts[5]) if parts[5] not in ("", "null") else 0.0
            except ValueError:
                vol = 0.0
            rows.append((parts[0], close, vol))
    return rows


def index_minute_rows(nsym):
    """Today's 1-minute bars for a domestic index (fchart has no index
    minutes). Despite the field name, accumulatedTradingVolume here is
    per-bar, not cumulative (observed: values fluctuate, never monotonic)."""
    url = f"https://api.stock.naver.com/chart/domestic/index/{quote(nsym)}/minute?count=500"
    status, body = fetch_upstream(url)
    if status != 200:
        return []
    try:
        items = json.loads(body)
    except ValueError:
        return []
    rows = []
    for it in items if isinstance(items, list) else []:
        t, v = str(it.get("localDateTime") or ""), it.get("currentPrice")
        if len(t) >= 12 and v is not None:
            vol = it.get("accumulatedTradingVolume") or 0
            rows.append((t[:12], float(v), float(vol)))
    return rows


def split_sessions(rows):
    """Minute rows -> {date: [row, ...]}, regular session only
    (drops pre-open auction prints stamped before 09:00)."""
    by_day = {}
    for row in rows:
        t = row[0]
        if "0900" <= t[8:12] <= "1535":
            by_day.setdefault(t[:8], []).append(row)
    return by_day


def downsample(rows, minutes):
    """Keep the last sample of each N-minute bucket (per day). Volume stays
    cumulative through this step, so bucketing keeps deltas exact."""
    buckets = {}
    for row in rows:
        t = row[0]
        key = t[:8] + format((int(t[8:10]) * 60 + int(t[10:12])) // minutes, "04d")
        buckets[key] = row
    return [buckets[k] for k in sorted(buckets)]


def cum_to_delta(series):
    """Convert cumulative day volumes to per-bar volumes, resetting at each
    day boundary (first bar of a day keeps its cumulative as its volume)."""
    out, prev_day, prev = [], None, 0.0
    for t, c, v in series:
        day = t[:8]
        dv = v if day != prev_day else max(0.0, v - prev)
        out.append((t, c, dv))
        prev_day, prev = day, v
    return out


def prev_session_close(nsym, before_date):
    closes = [row[1] for row in fchart_rows(nsym, "day", 10) if row[0][:8] < before_date]
    return closes[-1] if closes else None


def naver_chart(symbol, nsym, is_index, rng):
    """Build a Yahoo-v8-shaped chart payload from Naver data, so the
    frontend needs no idea which upstream served it."""
    prev_close = None
    if rng == "1d":
        rows = index_minute_rows(nsym) if is_index else fchart_rows(nsym, "minute", 1500)
        by_day = split_sessions(rows)
        if by_day:
            latest = max(by_day)
            series = by_day[latest]
            if not is_index:
                series = cum_to_delta(series)
            prev_close = prev_session_close(nsym, latest)
        elif is_index:
            # the index minute feed only covers today; before the open fall
            # back to recent daily bars instead of erroring
            series = fchart_rows(nsym, "day", 10)
            if len(series) >= 2:
                prev_close = series[-2][1]  # delta vs the prior session, like the stock path
        else:
            series = []
    elif rng == "5d" and not is_index:
        by_day = split_sessions(fchart_rows(nsym, "minute", 3000))
        series = []
        for date in sorted(by_day)[-5:]:
            series.extend(by_day[date])
        series = cum_to_delta(downsample(series, 10))
    else:
        series = fchart_rows(nsym, "day", DAY_COUNTS.get(rng, 248))
    if not series:
        return 502, {"chart": {"result": None, "error": {"description": "no domestic data"}}}
    meta = {
        "currency": "KRW",
        "symbol": symbol,
        "exchangeTimezoneName": "Asia/Seoul",
        "regularMarketPrice": series[-1][1],
    }
    if prev_close is not None:
        meta["chartPreviousClose"] = prev_close
        meta["previousClose"] = prev_close
    return 200, {"chart": {"result": [{
        "meta": meta,
        "timestamp": [kst_epoch(row[0]) for row in series],
        "indicators": {"quote": [{
            "close": [row[1] for row in series],
            "volume": [int(row[2]) for row in series],
        }]},
    }], "error": None}}


RANK_DIRS = {"up", "down"}
RANK_MARKETS = {"KOSPI": ".KS", "KOSDAQ": ".KQ"}


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (ValueError, TypeError):
        return None


def fetch_ranking(market, direction, size=9):
    """Top gainers/losers from Naver's mobile API, normalized."""
    url = f"https://m.stock.naver.com/api/stocks/{direction}/{market}?page=1&pageSize={size}"
    status, body = fetch_upstream(url)
    if status != 200:
        return None
    try:
        stocks = json.loads(body).get("stocks") or []
    except (ValueError, AttributeError):
        return None
    out = []
    for s in stocks:
        if not isinstance(s, dict):
            continue
        code, name = s.get("itemCode"), s.get("stockName")
        price = _num(s.get("closePrice"))
        ratio = _num(s.get("fluctuationsRatio"))
        if not code or not name or price is None or ratio is None:
            continue
        out.append({"symbol": code + RANK_MARKETS[market], "name": name,
                    "price": price, "changePct": ratio})
    return out


DOMESTIC_INDEX_SEARCH = [
    ("^KS11", "코스피 (KOSPI)", ("코스피", "kospi")),
    ("^KQ11", "코스닥 (KOSDAQ)", ("코스닥", "kosdaq")),
]


def search_domestic_indices(q):
    ql = q.lower()
    if len(ql) < 2:
        return []
    return [{"symbol": sym, "shortname": name, "longname": name,
             "quoteType": "INDEX", "exchDisp": "KOSPI" if sym == "^KS11" else "KOSDAQ"}
            for sym, name, keys in DOMESTIC_INDEX_SEARCH
            if any(k.startswith(ql) or ql.startswith(k) for k in keys)]


def _has_hangul(s):
    return any("ㄱ" <= ch <= "ㆎ" or "가" <= ch <= "힣" for ch in s)


def search_naver(q):
    """Korean domestic stocks via Naver autocomplete; mapped to Yahoo symbols."""
    url = f"https://ac.stock.naver.com/ac?q={quote(q)}&target=stock"
    status, body = fetch_upstream(url)
    if status != 200:
        return []
    try:
        items = json.loads(body).get("items") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for it in items:
        code, name = it.get("code"), it.get("name")
        type_code = (it.get("typeCode") or "").upper()
        if not code or not name:
            continue
        if type_code.startswith("KOSPI"):
            suffix = ".KS"
        elif type_code.startswith("KOSDAQ"):
            suffix = ".KQ"
        else:
            continue  # KONEX etc. -- not on Yahoo
        out.append({
            "symbol": code + suffix,
            "shortname": name,
            "longname": name,
            "quoteType": "EQUITY",
            "exchDisp": type_code,
        })
    return out


def search_yahoo(q):
    url = ("https://query2.finance.yahoo.com/v1/finance/search"
           f"?q={quote(q)}&quotesCount=12&newsCount=0&listsCount=0")
    status, body = fetch_upstream(url)
    if status != 200:
        return []
    try:
        return json.loads(body).get("quotes") or []
    except (ValueError, AttributeError):
        return []


class Handler(BaseHTTPRequestHandler):
    server_version = "InfraScope/2.4"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, status, message):
        self._send(status, json.dumps({"error": message}).encode())

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")

            elif path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    self._send_json_error(400, "missing q")
                    return
                naver = search_naver(q)
                yahoo = [] if _has_hangul(q) else search_yahoo(q)
                ordered = naver + yahoo if _has_hangul(q) else yahoo + naver
                ordered = search_domestic_indices(q) + ordered
                seen, merged = set(), []
                for item in ordered:
                    sym = item.get("symbol")
                    if sym and sym not in seen:
                        seen.add(sym)
                        merged.append(item)
                payload = json.dumps({"quotes": merged[:12]}, ensure_ascii=False)
                self._send(200, payload.encode("utf-8"))

            elif path == "/api/chart":
                symbol = (qs.get("symbol") or [""])[0].strip()
                rng = (qs.get("range") or ["1d"])[0]
                interval = (qs.get("interval") or ["5m"])[0]
                if not symbol or len(symbol) > 24:
                    self._send_json_error(400, "bad symbol")
                    return
                if rng not in ALLOWED_RANGES or interval not in ALLOWED_INTERVALS:
                    self._send_json_error(400, "bad range/interval")
                    return
                m = DOMESTIC_STOCK_RE.match(symbol)
                nsym = m.group(1) if m else DOMESTIC_INDEX.get(symbol)
                if nsym:  # KRX symbols -> Naver (near-realtime)
                    status, payload = naver_chart(symbol, nsym, symbol in DOMESTIC_INDEX, rng)
                    self._send(status, json.dumps(payload).encode("utf-8"))
                else:     # everything else -> Yahoo
                    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
                           f"?range={rng}&interval={interval}&includePrePost=false")
                    status, body = fetch_upstream(url)
                    self._send(status, body)

            elif path == "/api/rank":
                market = (qs.get("market") or ["KOSPI"])[0]
                direction = (qs.get("dir") or ["up"])[0]
                if market not in RANK_MARKETS or direction not in RANK_DIRS:
                    self._send_json_error(400, "bad market/dir")
                    return
                items = fetch_ranking(market, direction)
                if items is None:
                    self._send_json_error(502, "ranking unavailable")
                    return
                payload = json.dumps({"items": items}, ensure_ascii=False)
                self._send(200, payload.encode("utf-8"))

            else:
                self._send_json_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send_json_error(500, str(e))
            except OSError:
                pass


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"InfraScope running -> http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
