"""Read-only public quotes and explicitly synthetic demo data; no broker imports."""
import ast
import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))
CATALOG = {
    "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER",
    "035720": "카카오", "005380": "현대차", "051910": "LG화학",
    "068270": "셀트리온", "207940": "삼성바이오로직스", "006400": "삼성SDI",
    "000270": "기아", "105560": "KB금융", "055550": "신한지주",
    "012330": "현대모비스", "028260": "삼성물산", "003550": "LG",
    "066570": "LG전자", "086790": "하나금융지주", "009540": "HD한국조선해양",
    "034020": "두산에너빌리티", "042700": "한미반도체", "247540": "에코프로비엠",
    "086520": "에코프로", "196170": "알테오젠", "328130": "루닛",
}
DEMO_PRICES = {"005930": 72000, "000660": 184000, "035420": 198000,
               "035720": 48500, "005380": 242000, "051910": 315000}


def valid_symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("종목코드는 숫자 6자리로 입력해주세요.")
    return value


def number(value, *, positive=False):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value).replace(",", ""))
        if not result.is_finite() or abs(result) > Decimal("1e15") or (positive and result <= 0):
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise ValueError("시세 응답에 유효한 숫자가 없습니다.") from None


def read_url(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 PRISM-Personal/1.0",
                                   "Referer": "https://finance.naver.com/"})
    with urlopen(request, timeout=8) as response:
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError("시세 응답이 너무 큽니다.")
    return payload.decode("utf-8-sig")


def parse_naver_quote(payload, symbol):
    rows = payload.get("datas")
    if not isinstance(rows, list):
        raise ValueError("시세 응답 형식을 확인할 수 없습니다.")
    matches = [item for item in rows if item.get("itemCode") == symbol]
    if len(matches) != 1:
        raise ValueError("해당 종목의 시세가 없거나 중복 응답되었습니다.")
    item = matches[0]
    price = number(item.get("closePriceRaw", item.get("closePrice")), positive=True)
    if price != price.to_integral_value():
        raise ValueError("원 단위 시세가 아닙니다.")
    previous = price - number(item.get("compareToPreviousClosePriceRaw",
                                       item.get("compareToPreviousClosePrice")))
    if previous <= 0:
        raise ValueError("전일 종가를 확인할 수 없습니다.")
    stamp = item.get("localTradedAt")
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("시세의 기준 시각을 확인할 수 없습니다.") from None
    if datetime.now(KST) - parsed > timedelta(days=7) or parsed > datetime.now(KST) + timedelta(minutes=5):
        raise ValueError("시세 기준 시각이 오래되었거나 올바르지 않습니다.")
    if item.get("marketStatus") == "OPEN" and datetime.now(KST) - parsed > timedelta(minutes=10):
        raise ValueError("장중 시세가 오래되어 주문 기준으로 사용할 수 없습니다.")
    if item.get("tradeStopType", {}).get("name") != "TRADING":
        raise ValueError("거래 정지 종목은 모의 주문할 수 없습니다.")
    return {
        "symbol": symbol, "name": str(item.get("stockName") or symbol)[:80],
        "price": int(price), "previous_close": int(previous),
        "change_pct": round(float((price / previous - 1) * 100), 2),
        "source": "naver", "as_of": stamp, "currency": "KRW", "status": "ok",
        "volume": int(number(item.get("accumulatedTradingVolumeRaw",
                                      item.get("accumulatedTradingVolume", 0)))),
        "market_status": item.get("marketStatus", "UNKNOWN"), "history": [],
        "source_url": f"https://finance.naver.com/item/main.naver?code={symbol}",
    }


def parse_naver_history(raw):
    rows = ast.literal_eval(raw.strip())
    if not isinstance(rows, list):
        raise ValueError("차트 형식을 확인할 수 없습니다.")
    seen = set()
    history = []
    for row in rows[1:]:
        if not isinstance(row, list) or len(row) < 5:
            raise ValueError("차트 데이터가 불완전합니다.")
        day = datetime.strptime(str(row[0]), "%Y%m%d").date().isoformat()
        if day in seen:
            raise ValueError("차트 기준일이 중복되었습니다.")
        seen.add(day)
        close = number(row[4], positive=True)
        value = float(close)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("차트 종가를 확인할 수 없습니다.")
        history.append({"date": day, "close": value})
    return sorted(history, key=lambda row: row["date"])


def demo_quote(symbol):
    valid_symbol(symbol)
    if symbol not in CATALOG:
        raise ValueError("데모 시세는 검색 목록에 있는 종목만 지원합니다.")
    seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    price = DEMO_PRICES.get(symbol, (30 + seed % 470) * 1000)
    previous = round(price / (1 + ((seed % 601) - 300) / 10000))
    history = []
    day = date(2026, 6, 1)
    i = 0
    while day <= date(2026, 9, 30):
        if day.weekday() < 5:
            value = price * (0.82 + 0.18 * i / 87 + 0.04 * math.sin(i / 5 + seed % 9))
            history.append({"date": day.isoformat(), "close": round(value)})
            i += 1
        day += timedelta(days=1)
    history[-1]["close"] = price
    return {"symbol": symbol, "name": CATALOG[symbol], "price": price,
            "previous_close": previous, "change_pct": round((price / previous - 1) * 100, 2),
            "source": "demo", "as_of": "2026-09-30T15:30:00+09:00",
            "currency": "KRW", "volume": seed % 10_000_000,
            "status": "ok", "market_status": "DEMO", "history": history,
            "source_url": None, "notice": "합성 예시 데이터입니다. 실제 시세가 아닙니다."}


class Market:
    def __init__(self, provider):
        if provider not in ("demo", "naver"):
            raise ValueError("지원하지 않는 시세 공급자입니다.")
        self.provider = provider
        self.cache = {}
        self.history_cache = {}
        self.lock = threading.Lock()

    def quote(self, symbol, history=False, fresh=False):
        valid_symbol(symbol)
        if self.provider == "demo":
            result = demo_quote(symbol)
            if not history:
                result["history"] = []
            return result
        with self.lock:
            cached = self.cache.get(symbol)
        if not fresh and cached and time.monotonic() - cached[0] < 60:
            result = dict(cached[1])
        else:
            result = parse_naver_quote(json.loads(read_url(
                f"https://polling.finance.naver.com/api/realtime/domestic/stock/{symbol}")), symbol)
            with self.lock:
                self.cache[symbol] = (time.monotonic(), dict(result))
        if history:
            with self.lock:
                cached_history = self.history_cache.get(symbol)
            if cached_history and time.monotonic() - cached_history[0] < 3600:
                result["history"] = cached_history[1]
            else:
                now = datetime.now(KST).date()
                url = (f"https://api.finance.naver.com/siseJson.naver?symbol={symbol}&requestType=1"
                       f"&startTime={(now - timedelta(days=180)):%Y%m%d}&endTime={now:%Y%m%d}&timeframe=day")
                try:
                    result["history"] = parse_naver_history(read_url(url))
                    with self.lock:
                        self.history_cache[symbol] = (time.monotonic(), result["history"])
                except Exception:
                    result["history"] = []
                    result["history_warning"] = "일봉을 불러오지 못했습니다. 차트와 기술 지표는 대기 상태입니다."
        return result

    def quotes(self, symbols):
        def one(symbol):
            try:
                return symbol, self.quote(symbol)
            except Exception:
                return symbol, {"symbol": symbol, "name": CATALOG.get(symbol, symbol),
                                "price": None, "status": "unavailable", "source": self.provider,
                                "history": [], "as_of": None, "change_pct": None,
                                "currency": "KRW", "error": "시세를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."}
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(one, sorted(set(symbols))))

    def search(self, query):
        query = str(query).strip()[:80]
        results = [{"symbol": code, "name": name} for code, name in CATALOG.items()
                   if query.lower() in name.lower() or query in code]
        if re.fullmatch(r"[0-9]{6}", query) and query not in CATALOG and self.provider == "naver":
            quote = self.quote(query)
            results.append({"symbol": query, "name": quote["name"]})
        return results[:30]


def technical_report(quote):
    values = [float(row["close"]) for row in quote["history"]]
    if len(values) < 21:
        raise ValueError("기술 지표를 계산하려면 최소 21개의 일봉이 필요합니다.")
    ma20 = sum(values[-20:]) / 20
    changes = [values[i] - values[i - 1] for i in range(len(values) - 14, len(values))]
    gains = sum(max(0, value) for value in changes) / 14
    losses = sum(max(0, -value) for value in changes) / 14
    rsi = 50 if gains == losses == 0 else 100 if losses == 0 else 100 - 100 / (1 + gains / losses)
    returns = [(values[i] / values[i - 1] - 1) for i in range(len(values) - 20, len(values))]
    mean = sum(returns) / len(returns)
    volatility = (sum((r - mean) ** 2 for r in returns) / len(returns)) ** .5 * 100
    change = (values[-1] / values[-21] - 1) * 100
    metrics = {"ma20": round(ma20), "rsi14": round(rsi, 1), "return20_pct": round(change, 2),
               "volatility_pct": round(volatility, 2), "history_as_of": quote["history"][-1]["date"]}
    source = "합성 데모 일봉" if quote["source"] == "demo" else "NAVER 공개 일봉"
    body = (f"{quote['name']} ({quote['symbol']})\n\n"
            f"자료: {source} · 일봉 기준일 {metrics['history_as_of']}\n"
            f"시세 기준 시각: {quote['as_of']}\n\n"
            f"최근 20거래일 종가 변화는 {change:+.2f}%입니다.\n"
            f"20일 단순 이동평균은 {ma20:,.0f}원입니다.\n"
            f"14일 단순 RSI는 {rsi:.1f}입니다. (Wilder 평활 방식과 다릅니다.)\n"
            f"20일 일간 수익률 표준편차는 {volatility:.2f}%입니다.\n\n"
            "이 문서는 가격 자료를 계산한 관측 메모입니다. AI 분석이나 매수·매도 신호가 아닙니다.\n"
            "재무, 뉴스, 수급, 체결 가능성은 이 메모에서 평가하지 않았습니다.")
    return {"symbol": quote["symbol"], "title": f"{quote['name']} · 가격 관측 메모",
            "body": body, "kind": "technical", "metrics": metrics,
            "created_at": datetime.now(KST).isoformat(), "source": quote["source"]}
