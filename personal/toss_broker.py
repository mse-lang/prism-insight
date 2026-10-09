"""Toss Securities live, domestic LIMIT/DAY adapter.

Official contract: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
Construction performs no network I/O. Orders are never retried; their accepted
response is not a fill. API order visibility excludes unsupported app types.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from personal.kis_broker import (BrokerError, OrderRejected, OrderUncertain,
                                 KST, _NoRedirect, _code, _decimal, _int, _mapping)

HOST = "https://openapi.tossinvest.com"
_TOKEN_LOCK = threading.RLock()
_TOKENS = {}
_INTERVALS = {"AUTH": .21, "ACCOUNT": 1.01, "ASSET": .21,
              "STOCK": .21, "MARKET_INFO": .34, "MARKET_DATA": .07,
              "CHART": .051, "ORDER": .11, "ORDER_INFO": .34}
_PENDING = {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE", "PARTIAL_FILLED"}
_REJECTIONS = {
    "invalid-request": "토스증권이 주문 조건을 거절했습니다.",
    "confirm-high-value-required": "1억원 이상 주문은 토스 앱에서 직접 확인해주세요.",
    "insufficient-buying-power": "토스증권 주문 가능 금액이 부족합니다.",
    "insufficient-sellable-quantity": "토스증권 매도 가능 수량이 부족합니다.",
    "market-closed": "토스증권 거래 시간이 아닙니다.",
    "idempotency-key-conflict": "이미 사용한 주문 식별자와 주문 조건이 다릅니다.",
}


def _identifier(value, *, client=False):
    maximum = 36 if client else 256
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1," + str(maximum) + "}", value):
        raise BrokerError("토스증권 주문 식별자를 확인할 수 없습니다.")
    return value


def _timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(KST)
    except ValueError:
        raise BrokerError("토스증권 응답의 기준 시각을 확인할 수 없습니다.") from None


def _array(value):
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise BrokerError("토스증권 목록 응답이 불완전합니다.")
    return value


def _name(value):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise BrokerError("토스증권 종목명을 확인할 수 없습니다.")
    return "".join(c for c in value if ord(c) >= 32)[:120]


class TossBroker:
    def __init__(self, config, *, opener=None, clock=None, sleeper=time.sleep):
        if not isinstance(config, dict) or config.get("environment") != "live" or config.get("provider", "toss") != "toss":
            raise BrokerError("토스증권은 공식 실계좌 환경만 지원합니다.")
        allowed = {"provider", "environment", "client_id", "client_secret", "account_seq",
                   "verified_account_identity", "account_masked"}
        if set(config) - allowed:
            raise BrokerError("토스증권 연결 설정에 지원하지 않는 항목이 있습니다.")
        for field in ("client_id", "client_secret"):
            value = config.get(field)
            if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(c.isspace() or ord(c) < 32 for c in value):
                raise BrokerError("토스증권 API 키 설정을 확인해주세요.")
        seq = config.get("account_seq")
        if seq is not None and (type(seq) is not int or not 0 < seq < 2**63):
            raise BrokerError("조회한 토스증권 계좌를 선택해주세요.")
        identity = config.get("verified_account_identity")
        if identity is not None and (not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity)):
            raise BrokerError("저장된 토스증권 계좌 확인 정보가 올바르지 않습니다.")
        masked = config.get("account_masked")
        if masked is not None and (not isinstance(masked, str) or not re.fullmatch(r"[0-9* -]{4,30}", masked)):
            raise BrokerError("저장된 토스증권 계좌 표시 정보가 올바르지 않습니다.")
        self.environment, self.host = "live", HOST
        self._client_id, self._secret = config["client_id"], config["client_secret"]
        self._account_seq = seq
        self.account_identity, self.account_masked = identity, masked
        self._saved_identity, self._verified = identity, False
        self._opener = opener or build_opener(_NoRedirect()).open
        self._clock = clock or (lambda: datetime.now(KST))
        self._sleep = sleeper
        self._wire_lock = threading.RLock()
        self._last_request = {}
        self._proof = None
        # A new token invalidates previous tokens for the same client. Share it
        # across temporary discovery and configured instances in this process.
        token_key = self._client_id + "\0" + self._secret
        if opener is not None:
            token_key += "\0" + str(id(opener))
        self._token_key = hashlib.sha256(token_key.encode()).hexdigest()

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BrokerError("시간대가 있는 현재 시각이 필요합니다.")
        return value.astimezone(KST)

    def _guard_order(self, body):
        now, proof = self._now(), self._proof
        if now.weekday() >= 5 or not 900 <= now.hour * 100 + now.minute < 1520:
            raise OrderRejected("자동주문 허용 시간이 지나 주문을 보내지 않았습니다.")
        if not proof or now > proof["until"] or not proof["start"] <= now < proof["end"]:
            raise OrderRejected("개장 또는 최근 거래 증거의 유효기간이 지나 주문을 보내지 않았습니다.")
        if body.get("symbol") != proof["symbol"] or body.get("price") != str(proof["price"]):
            raise OrderRejected("확인한 최신 종목 시세와 주문이 달라 주문을 보내지 않았습니다.")

    def _wire(self, path, *, group, params=None, body=None, token=None, private=False, order=False, form=False):
        params = params or {}
        data = (urlencode(body).encode() if form else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) if body is not None else None
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if private:
            if not self._verified:
                raise BrokerError("선택한 토스증권 계좌의 소유 확인이 필요합니다.")
            headers["X-Tossinvest-Account"] = str(self._account_seq)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json; charset=utf-8"
        req = Request(HOST + path + ("?" + urlencode(params) if params else ""), data=data,
                      headers=headers, method="POST" if data is not None else "GET")
        try:
            with self._wire_lock:
                self._sleep(max(0, _INTERVALS[group] - (time.monotonic() - self._last_request.get(group, 0))))
                self._last_request[group] = time.monotonic()
                if order:
                    self._guard_order(body)
                try:
                    response = self._opener(req, timeout=10)
                except HTTPError as error:
                    response = error
                try:
                    status = getattr(response, "status", None) or response.getcode()
                    raw = response.read(2_000_001)
                finally:
                    response.close()
            if len(raw) > 2_000_000:
                raise ValueError
            payload = _mapping(json.loads(raw.decode("utf-8-sig")))
            if status != 200:
                code = payload.get("error", {}).get("code") if isinstance(payload.get("error"), dict) else None
                if order and status in (400, 422) and code in _REJECTIONS:
                    raise OrderRejected(_REJECTIONS[code])
                raise ValueError
            if "error" in payload:
                raise ValueError
            return payload
        except OrderRejected:
            raise
        except Exception:
            error = OrderUncertain if order else BrokerError
            message = "주문 접수 여부가 불확실합니다. 재주문하지 말고 토스 앱 주문내역을 확인해주세요." if order else "토스증권 연결 또는 응답을 확인할 수 없습니다. 허용 IP와 API 권한도 확인해주세요."
            raise error(message) from None

    def _token(self):
        with _TOKEN_LOCK:
            cached = _TOKENS.get(self._token_key)
            if cached and time.monotonic() < cached[1]:
                return cached[0]
            payload = self._wire("/oauth2/token", group="AUTH", form=True,
                                 body={"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._secret})
            token, expires = payload.get("access_token"), _int(payload.get("expires_in"), positive=True)
            if payload.get("token_type", "").lower() != "bearer" or not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token) or expires <= 60:
                raise BrokerError("토스증권 인증의 유효기간을 확인할 수 없습니다.")
            _TOKENS[self._token_key] = (token, time.monotonic() + min(expires, 86400) - 60)
            return token

    def _request(self, endpoint, group, params=None, *, body=None, private=False, order=False):
        if private and not self._verified:
            self._verify_account()
        payload = self._wire("/api/v1/" + endpoint, group=group, params=params, body=body,
                             token=self._token(), private=private, order=order)
        if "result" not in payload:
            error = OrderUncertain if order else BrokerError
            raise error("토스증권 응답의 결과를 확인할 수 없습니다.")
        return payload["result"]

    def _accounts(self):
        rows = _array(self._request("accounts", "ACCOUNT"))
        seen = set()
        for row in rows:
            seq, number = row.get("accountSeq"), row.get("accountNo")
            if type(seq) is not int or not 0 < seq < 2**63 or seq in seen or not isinstance(number, str) or not re.fullmatch(r"[0-9]{8,20}", number) or not isinstance(row.get("accountType"), str):
                raise BrokerError("토스증권 계좌 목록을 확인할 수 없습니다.")
            seen.add(seq)
        return rows

    def list_accounts(self):
        return [{"account_seq": row["accountSeq"], "account_masked": "*" * (len(row["accountNo"]) - 4) + row["accountNo"][-4:],
                 "account_type": row["accountType"]} for row in self._accounts() if row["accountType"] == "BROKERAGE"]

    def _verify_account(self):
        if self._account_seq is None:
            raise BrokerError("조회한 토스증권 계좌를 먼저 선택해주세요.")
        self._verified = False
        selected = [r for r in self._accounts() if r["accountSeq"] == self._account_seq and r["accountType"] == "BROKERAGE"]
        if len(selected) != 1:
            raise BrokerError("선택한 토스증권 계좌의 소유를 확인할 수 없습니다.")
        number = selected[0]["accountNo"]
        actual = hashlib.sha256(("toss:live:" + number).encode()).hexdigest()
        if self._saved_identity is not None and actual != self._saved_identity:
            raise BrokerError("저장된 계좌와 현재 토스증권 계좌가 다릅니다. 연결을 다시 확인해주세요.")
        self.account_identity = actual
        self.account_masked = "*" * (len(number) - 4) + number[-4:]
        self._verified = True

    def _cash(self):
        result = _mapping(self._request("buying-power", "ASSET", {"currency": "KRW"}, private=True))
        if result.get("currency") != "KRW":
            raise BrokerError("토스증권 원화 주문 가능 금액을 확인할 수 없습니다.")
        return _int(result.get("cashBuyingPower"))

    def _pages(self, endpoint, key, params):
        rows, cursors, identities = [], set(), set()
        for _ in range(100):
            result = _mapping(self._request(endpoint, "ORDER_INFO", params, private=True))
            page = _array(result.get(key))
            for row in page:
                identity = _identifier(row.get("conditionalOrderId" if key == "conditionalOrders" else "orderId"))
                if identity in identities:
                    raise BrokerError("토스증권 주문 목록에 중복 항목이 있습니다.")
                identities.add(identity)
                rows.append(row)
            more, cursor = result.get("hasNext"), result.get("nextCursor")
            if type(more) is not bool:
                raise BrokerError("토스증권 목록의 연속조회 여부를 확인할 수 없습니다.")
            if not more:
                if cursor is not None:
                    raise BrokerError("토스증권 마지막 페이지를 확인할 수 없습니다.")
                return rows
            if endpoint == "orders" or not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in cursors:
                raise BrokerError("토스증권 주문 목록 조회가 완료되지 않았습니다.")
            cursors.add(cursor)
            params = {**params, "cursor": cursor}
        raise BrokerError("토스증권 주문 목록 조회가 완료되지 않았습니다.")

    def account(self):
        self._verify_account()
        result = _mapping(self._request("holdings", "ASSET", private=True))
        rows = _array(result.get("items"))
        value = _mapping(_mapping(result.get("marketValue")).get("amount"))
        kr_sum, us_sum = Decimal(0), Decimal(0)
        positions, seen, count, excluded = [], set(), 0, 0
        for row in rows:
            country, currency, symbol = row.get("marketCountry"), row.get("currency"), row.get("symbol")
            if (country, currency) not in (("KR", "KRW"), ("US", "USD")) or not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,30}", symbol) or (currency, symbol) in seen:
                raise BrokerError("토스증권 보유 종목의 통화 또는 식별자를 확인할 수 없습니다.")
            seen.add((currency, symbol))
            quantity = _decimal(row.get("quantity"), integer=country == "KR")
            price = _decimal(row.get("lastPrice"), positive=quantity > 0, integer=country == "KR")
            average = _decimal(row.get("averagePurchasePrice"), positive=quantity > 0)
            amount = _decimal(_mapping(row.get("marketValue")).get("amount"), integer=country == "KR")
            if quantity > 0 and amount <= 0:
                raise BrokerError("토스증권 보유 종목 평가 금액을 확인할 수 없습니다.")
            if quantity == 0 and amount != 0:
                raise BrokerError("토스증권 보유 수량과 평가 금액이 다릅니다.")
            if country == "KR":
                kr_sum += amount
                if abs(amount - quantity * price) > 1:
                    raise BrokerError("토스증권 국내 보유 종목 평가 금액이 일치하지 않습니다.")
            else:
                us_sum += amount
            if quantity > 0:
                count += 1
                if country == "KR" and re.fullmatch(r"[0-9]{6}", symbol):
                    positions.append({"symbol": symbol, "name": _name(row.get("name")), "quantity": int(quantity),
                                      "average_cost": float(average), "price": int(price)})
                else:
                    excluded += 1
        if _decimal(value.get("krw"), integer=True) != kr_sum or (us_sum > 0 and _decimal(value.get("usd")) != us_sum) or (us_sum == 0 and value.get("usd") is not None and _decimal(value["usd"]) != 0):
            raise BrokerError("토스증권 보유 종목과 통화별 합계가 일치하지 않습니다.")
        cash = self._cash()
        orders = self._pages("orders", "orders", {"status": "OPEN"})
        for row in orders:
            if row.get("status") not in _PENDING or row.get("side") not in ("BUY", "SELL"):
                raise BrokerError("토스증권 미체결 주문 상태를 확인할 수 없습니다.")
            _decimal(row.get("quantity"), positive=True)
        conditional = self._pages("conditional-orders", "conditionalOrders", {"status": "OPEN", "limit": 100})
        for row in conditional:
            if row.get("type") not in ("SINGLE", "OCO", "OTO") or row.get("status") not in ("WATCHING", "PAUSED", "ORDERING", "ORDERED", "COMPLETED", "EXPIRED"):
                raise BrokerError("토스증권 조건주문 상태를 확인할 수 없습니다.")
            _decimal(row.get("quantity"), positive=True)
        return {"cash": cash, "equity": cash + int(kr_sum), "positions": positions,
                "open_orders": [{"broker_order_id": row["orderId"], "symbol": row.get("symbol"), "status": row["status"]} for row in orders]
                               + [{"conditional_order_id": row["conditionalOrderId"], "symbol": row.get("symbol"), "status": row["status"], "kind": "conditional"} for row in conditional],
                "as_of": self._now().isoformat(), "account_identity": self.account_identity,
                "account_masked": self.account_masked, "environment": "live", "equity_basis": "krw-trading-capital",
                "total_position_count": count, "excluded_positions_count": excluded,
                "order_visibility": "api-supported-types", "venue_scope": "krx-nxt-integrated"}

    def buying_power(self, symbol, limit_price=None):
        code = _code(symbol)
        if type(limit_price) is not int or not 0 < limit_price <= 10**9:
            raise BrokerError("토스증권 주문 가능 수량 확인에는 원 단위 지정가가 필요합니다.")
        cash = self._cash()
        rows = _array(self._request("commissions", "ASSET", private=True))
        today, rates = self._now().date(), []
        for row in rows:
            if row.get("marketCountry") != "KR":
                continue
            try:
                start = datetime.fromisoformat(row["startDate"]).date() if row.get("startDate") is not None else None
                end = datetime.fromisoformat(row["endDate"]).date() if row.get("endDate") is not None else None
            except (ValueError, TypeError):
                raise BrokerError("토스증권 수수료 적용 기간을 확인할 수 없습니다.") from None
            rate = _decimal(row.get("commissionRate"))
            if rate > 1 or (start and end and start > end):
                raise BrokerError("토스증권 수수료를 확인할 수 없습니다.")
            if (start is None or start <= today) and (end is None or today <= end):
                rates.append(rate)
        if len(rates) != 1:
            raise BrokerError("현재 적용되는 토스증권 국내 수수료를 확인할 수 없습니다.")
        # Reserve at least one percent, including won rounding, even during a
        # commission promotion. This is a cash calculation, not broker quantity.
        cost = Decimal(limit_price) * (1 + max(rates[0], Decimal(".01")))
        quantity = int((Decimal(max(0, cash - 1)) / cost).to_integral_value(rounding=ROUND_FLOOR))
        return {"cash": cash, "quantity": quantity, "symbol": code, "limit_price": limit_price,
                "quantity_basis": "cash-derived", "commission_rate": float(rates[0]), "as_of": self._now().isoformat()}

    def history(self, symbol):
        code, params, seen_cursor, candles = _code(symbol), {}, set(), {}
        for _ in range(20):
            result = _mapping(self._request("candles", "CHART", {"symbol": code, "interval": "1d", "count": 200, "adjusted": "true", **params}))
            page = _array(result.get("candles"))
            for row in page:
                stamp = _timestamp(row.get("timestamp"))
                if row.get("currency") != "KRW" or stamp.date() > self._now().date() or (stamp.hour, stamp.minute, stamp.second) != (0, 0, 0):
                    raise BrokerError("토스증권 일봉의 기준일 또는 통화를 확인할 수 없습니다.")
                close = _int(row.get("closePrice"), positive=True)
                _int(row.get("volume"))
                day = stamp.date().isoformat()
                signature = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if day in candles and candles[day][1] != signature:
                    raise BrokerError("토스증권 중복 일봉의 값이 다릅니다.")
                candles[day] = ({"date": day, "close": close}, signature)
            cursor = result.get("nextBefore")
            if cursor is not None and (not isinstance(cursor, str) or not cursor or cursor in seen_cursor):
                raise BrokerError("토스증권 일봉의 다음 페이지를 확인할 수 없습니다.")
            if len(candles) >= 200 or cursor is None:
                if not candles:
                    raise BrokerError("토스증권 일봉을 확인할 수 없습니다.")
                return [candles[day][0] for day in sorted(candles)[-200:]]
            seen_cursor.add(cursor)
            params = {"before": cursor}
        raise BrokerError("토스증권 일봉 조회가 완료되지 않았습니다.")

    def quote(self, symbol):
        self._proof = None
        code, now = _code(symbol), self._now()
        stocks = _array(self._request("stocks", "STOCK", {"symbols": code}))
        prices = _array(self._request("prices", "MARKET_DATA", {"symbols": code}))
        if len(stocks) != 1 or len(prices) != 1 or stocks[0].get("symbol") != code or prices[0].get("symbol") != code:
            raise BrokerError("토스증권 시세 종목이 요청과 다릅니다.")
        stock, price_row = stocks[0], prices[0]
        if stock.get("currency") != "KRW" or stock.get("market") not in ("KOSPI", "KOSDAQ", "KR_ETC") or stock.get("status") != "ACTIVE" or price_row.get("currency") != "KRW":
            raise BrokerError("거래 가능한 국내 원화 종목의 시세가 아닙니다.")
        price, stamp = _int(price_row.get("lastPrice"), positive=True), _timestamp(price_row.get("timestamp"))
        if stamp > now + timedelta(seconds=5):
            raise BrokerError("토스증권 시세 시각이 현재보다 미래입니다.")
        today = _mapping(_mapping(self._request("market-calendar/KR", "MARKET_INFO", {"date": now.date().isoformat()})).get("today"))
        if today.get("date") != now.date().isoformat():
            raise BrokerError("토스증권 거래일 응답이 현재 날짜와 다릅니다.")
        session = None
        integrated = today.get("integrated")
        if integrated is not None:
            regular = _mapping(integrated).get("regularMarket")
            if regular is not None and _mapping(regular).get("singlePriceAuctionStartTime") is not None:
                start, end = _timestamp(regular.get("startTime")), _timestamp(regular.get("singlePriceAuctionStartTime"))
                full_end = _timestamp(regular.get("endTime"))
                if start.date() != now.date() or end.date() != now.date() or not start < end <= full_end:
                    raise BrokerError("토스증권 정규장 시간을 확인할 수 없습니다.")
                session = (start, end)
        trades = _array(self._request("trades", "MARKET_DATA", {"symbol": code, "count": 50}))
        verified_trades = []
        for row in trades:
            trade_stamp = _timestamp(row.get("timestamp"))
            if row.get("currency") != "KRW" or trade_stamp > now + timedelta(seconds=5):
                raise BrokerError("토스증권 최근 거래를 확인할 수 없습니다.")
            verified_trades.append((trade_stamp, _int(row.get("price"), positive=True), _int(row.get("volume"), positive=True)))
        volume = None
        if verified_trades:
            stamp, price, volume = max(verified_trades)
        history = self.history(code)
        previous = [row["close"] for row in history if row["date"] < now.date().isoformat()]
        previous_close = previous[-1] if previous else None
        suspended = _mapping(stock.get("koreanMarketDetail")).get("krxTradingSuspended")
        if type(suspended) is not bool:
            raise BrokerError("토스증권 KRX 거래 정지 여부를 확인할 수 없습니다.")
        current = self._now()
        opened = bool(session and not suspended and now.weekday() < 5 and 900 <= current.hour * 100 + current.minute < 1520
                      and session[0] <= current < session[1] and verified_trades and stamp.date() == current.date()
                      and current - timedelta(seconds=90) <= stamp <= current + timedelta(seconds=5))
        if opened:
            self._proof = {"symbol": code, "price": price, "until": stamp + timedelta(seconds=90), "start": session[0], "end": session[1]}
        return {"symbol": code, "name": _name(stock.get("name")), "price": price, "source": "toss", "status": "ok",
                "as_of": stamp.isoformat(), "timestamp": stamp.isoformat(), "retrieved_at": current.isoformat(),
                "market_status": "OPEN" if opened else "CLOSED", "timestamp_basis": "last_trade" if verified_trades else "last_price",
                "previous_close": previous_close, "change_pct": float((Decimal(price) / previous_close - 1) * 100) if previous_close else None,
                "volume": volume, "venue_scope": "krx-nxt-integrated"}

    def submit(self, symbol, side, quantity, limit_price=None, client_order_id=None):
        code = _code(symbol)
        if side not in ("buy", "sell") or type(quantity) is not int or not 0 < quantity <= 10**9 or type(limit_price) is not int or not 0 < limit_price <= 10**9:
            raise BrokerError("토스증권 주문은 국내 주식의 양의 정수 수량과 원 단위 지정가가 필요합니다.")
        client_id = _identifier(client_order_id, client=True)
        if quantity * limit_price >= 100_000_000:
            raise OrderRejected("1억원 이상 주문은 토스 앱에서 직접 확인해주세요.")
        body = {"symbol": code, "side": side.upper(), "quantity": str(quantity), "price": str(limit_price),
                "orderType": "LIMIT", "timeInForce": "DAY", "clientOrderId": client_id, "confirmHighValueOrder": False}
        self._guard_order(body)
        if side == "buy":
            if quantity > self.buying_power(code, limit_price)["quantity"]:
                raise OrderRejected("토스증권 현금 주문 가능 금액이 부족합니다.")
        else:
            available = _mapping(self._request("sellable-quantity", "ASSET", {"symbol": code}, private=True))
            if quantity > _int(available.get("sellableQuantity")):
                raise OrderRejected("토스증권 매도 가능 수량이 부족합니다.")
        attempt_at = self._now()
        result = self._request("orders", "ORDER", body=body, private=True, order=True)
        try:
            result = _mapping(result)
            order_id = _identifier(result.get("orderId"))
            if result.get("clientOrderId") != client_id:
                raise BrokerError("주문 식별자가 다릅니다.")
        except BrokerError:
            raise OrderUncertain("토스증권 주문 접수 응답이 불완전합니다. 재전송하지 말고 주문내역을 확인해주세요.") from None
        return {"broker_order_id": order_id, "order_date": attempt_at.strftime("%Y%m%d"), "symbol": code,
                "side": side, "quantity": quantity, "limit_price": limit_price, "client_order_id": client_id,
                "account_identity": self.account_identity, "environment": "live", "status": "accepted",
                "attempt_at": attempt_at.isoformat(), "accepted_at": self._now().isoformat()}

    def fills(self, receipt):
        receipt = _mapping(receipt)
        self._verify_account()
        identity = _identifier(receipt.get("broker_order_id"))
        code, side = _code(receipt.get("symbol")), receipt.get("side")
        quantity, price = receipt.get("quantity"), receipt.get("limit_price")
        if receipt.get("account_identity") != self.account_identity or receipt.get("environment") != "live" or side not in ("buy", "sell") or type(quantity) is not int or quantity <= 0 or type(price) is not int or price <= 0:
            raise BrokerError("토스증권 주문의 계좌와 주문 조건을 확인할 수 없습니다.")
        _identifier(receipt.get("client_order_id"), client=True)
        attempt = _timestamp(receipt.get("attempt_at", receipt.get("created_at")))
        row = _mapping(self._request("orders/" + identity, "ORDER_INFO", private=True))
        ordered = _timestamp(row.get("orderedAt"))
        if row.get("orderId") != identity or row.get("symbol") != code or row.get("side") != side.upper() or row.get("currency") != "KRW" or row.get("orderType") != "LIMIT" or row.get("timeInForce") != "DAY" or _int(row.get("quantity"), positive=True) != quantity or _int(row.get("price"), positive=True) != price or ordered.strftime("%Y%m%d") != receipt.get("order_date") or ordered < attempt - timedelta(seconds=5) or ordered > self._now() + timedelta(seconds=5):
            raise BrokerError("토스증권 주문 상세가 저장한 주문과 일치하지 않습니다.")
        if "clientOrderId" in row and row["clientOrderId"] != receipt["client_order_id"]:
            raise BrokerError("토스증권 주문 상세의 주문 식별자가 다릅니다.")
        execution = _mapping(row.get("execution"))
        filled = _int(execution.get("filledQuantity"))
        if filled > quantity:
            raise BrokerError("토스증권 체결 수량이 주문 수량보다 큽니다.")
        average, amount, filled_at = None, None, None
        if filled:
            average = _decimal(execution.get("averageFilledPrice"), positive=True)
            amount = _int(execution.get("filledAmount"), positive=True)
            filled_at = _timestamp(execution.get("filledAt"))
            if abs(average * filled - amount) > 1 or not ordered <= filled_at <= self._now() + timedelta(seconds=5) or (side == "buy" and average > price) or (side == "sell" and average < price):
                raise BrokerError("토스증권 체결 가격 또는 시각을 확인할 수 없습니다.")
        else:
            for field in ("averageFilledPrice", "filledAmount"):
                if execution.get(field) is not None and _decimal(execution[field]) != 0:
                    raise BrokerError("토스증권 미체결 주문의 금액이 일치하지 않습니다.")
            if execution.get("filledAt") is not None:
                raise BrokerError("토스증권 미체결 주문의 체결 시각이 존재합니다.")
        status = row.get("status")
        if status == "FILLED":
            if filled != quantity:
                raise BrokerError("토스증권 전량 체결 수량이 일치하지 않습니다.")
            normalized, terminal = "filled", True
        elif status in ("CANCELED", "REJECTED"):
            normalized, terminal = ("canceled" if status == "CANCELED" else "rejected"), True
        elif status in _PENDING:
            if filled >= quantity:
                raise BrokerError("토스증권 대기 주문의 체결 수량을 확인할 수 없습니다.")
            normalized, terminal = ("partial" if filled else "pending"), False
        else:
            raise BrokerError("토스증권 주문 상태가 변경되어 수동 확인이 필요합니다.")
        return {"status": normalized, "terminal": terminal, "filled_quantity": filled,
                "remaining_quantity": quantity - filled, "average_price": float(average) if average is not None else None,
                "filled_amount": amount, "commission": float(_decimal(execution["commission"])) if execution.get("commission") is not None else None,
                "tax": float(_decimal(execution["tax"])) if execution.get("tax") is not None else None,
                "filled_at": filled_at.isoformat() if filled_at else None, "broker_order_id": identity,
                "broker_status": status, "as_of": self._now().isoformat()}

    def health(self):
        account = self.account()
        return {"status": "ok", "provider": "toss", "environment": "live", "account_identity": self.account_identity,
                "account_masked": self.account_masked, "as_of": account["as_of"],
                "order_visibility": "api-supported-types", "venue_scope": "krx-nxt-integrated"}
