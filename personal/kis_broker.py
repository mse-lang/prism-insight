"""Isolated, stdlib KIS domestic cash adapter; construction never sends an order.

REST fields/TR IDs follow Korea Investment's official open-trading-api samples.
Credentials arrive as a controller-owned dict and are never loaded from upstream
configuration. An accepted order is not a fill. Orders are never HTTP-retried.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

KST = timezone(timedelta(hours=9))
HOSTS = {"paper": "https://openapivts.koreainvestment.com:29443",
         "live": "https://openapi.koreainvestment.com:9443"}
TR_IDS = {"balance": "TTTC8434R", "power": "TTTC8908R",
          "daily": "TTTC0081R", "buy": "TTTC0012U", "sell": "TTTC0011U"}
API_ROOT = "/uapi/domestic-stock/v1/"


class BrokerError(ValueError):
    """A read/authentication response could not be verified."""


class OrderRejected(BrokerError):
    """A definite rejection; no accepted order is inferred."""


class OrderUncertain(BrokerError):
    """An order may have reached the broker; reconcile, never resend blindly."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a token/app secret to a redirect target.


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise BrokerError("종목코드는 숫자 6자리여야 합니다.")
    return value


def _decimal(value, *, positive=False, integer=False, signed=False):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError
        result = Decimal(str(value).strip())
        if not result.is_finite() or abs(result) > Decimal("1e15"):
            raise ValueError
        if (positive and result <= 0) or (not signed and result < 0):
            raise ValueError
        if integer and result != result.to_integral_value():
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise BrokerError("증권사 응답의 금액 또는 수량을 확인할 수 없습니다.") from None


def _int(value, *, positive=False):
    return int(_decimal(value, positive=positive, integer=True))


def _mapping(value):
    if not isinstance(value, dict):
        raise BrokerError("증권사 응답 형식을 확인할 수 없습니다.")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise BrokerError("증권사 주문 식별자를 확인할 수 없습니다.")
    return value


def _same_id(left, right):
    return _id(left).lstrip("0") == _id(right).lstrip("0")


def _date(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
            raise ValueError
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise BrokerError("증권사 주문 또는 시세 기준일을 확인할 수 없습니다.") from None


class KISBroker:
    def __init__(self, config, *, opener=None, clock=None, sleeper=time.sleep):
        if not isinstance(config, dict) or config.get("environment") not in HOSTS:
            raise BrokerError("증권사 환경은 paper 또는 live로 설정해주세요.")
        for field in ("app_key", "app_secret"):
            value = config.get(field)
            if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(c.isspace() or ord(c) < 32 for c in value):
                raise BrokerError("증권사 앱 키와 비밀키 설정을 확인해주세요.")
        if not isinstance(config.get("account_no"), str) or not re.fullmatch(r"[0-9]{8}", config["account_no"]):
            raise BrokerError("증권사 계좌번호는 앞 8자리로 설정해주세요.")
        if not isinstance(config.get("product_code"), str) or not re.fullmatch(r"[0-9]{2}", config["product_code"]):
            raise BrokerError("증권사 계좌 상품코드는 숫자 2자리로 설정해주세요.")
        self.environment = config["environment"]
        self.host = HOSTS[self.environment]
        self._app_key, self._app_secret = config["app_key"], config["app_secret"]
        self._account_no, self._product_code = config["account_no"], config["product_code"]
        self.account_identity = hashlib.sha256(f"kis:{self.environment}:{self._account_no}:{self._product_code}".encode()).hexdigest()
        self._opener = opener or build_opener(_NoRedirect()).open
        self._clock = clock or (lambda: datetime.now(KST))
        self._sleep = sleeper
        self._token_lock, self._wire_lock, self._calendar_lock = threading.Lock(), threading.Lock(), threading.Lock()
        self._token, self._token_until, self._last_request = None, 0.0, 0.0
        self._calendar_day, self._calendar_open = None, None
        self._session_valid_until = None
        self._session_symbol, self._session_price = None, None

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BrokerError("시간대가 있는 현재 시각이 필요합니다.")
        return value.astimezone(KST)

    def _tr(self, key):
        value = TR_IDS[key]
        return "V" + value[1:] if self.environment == "paper" else value

    def _account_params(self):
        return {"CANO": self._account_no, "ACNT_PRDT_CD": self._product_code}

    def _wire(self, path, *, headers, params, post=False, order=False):
        body = json.dumps(params, ensure_ascii=False, allow_nan=False).encode() if post else None
        url = self.host + path + ("" if post else "?" + urlencode(params))
        req = Request(url, data=body, headers=headers, method="POST" if post else "GET")
        error_type = OrderUncertain if order else BrokerError
        try:
            with self._wire_lock:
                interval = 0.55 if self.environment == "paper" else 0.06
                self._sleep(max(0, interval - (time.monotonic() - self._last_request)))
                self._last_request = time.monotonic()
                if order:
                    current = self._now()
                    if current.weekday() >= 5 or not 900 <= current.hour * 100 + current.minute < 1520:
                        raise OrderRejected("자동주문 허용 시간이 지나 주문을 보내지 않았습니다.")
                    if self._calendar_open is False or self._session_valid_until is None or current > self._session_valid_until:
                        raise OrderRejected("개장 또는 최근 거래 증거의 유효기간이 지나 주문을 보내지 않았습니다.")
                    if params.get("PDNO") != self._session_symbol:
                        raise OrderRejected("주문 종목의 최근 시세를 먼저 확인해주세요.")
                    if params.get("ORD_DVSN") == "00" and params.get("ORD_UNPR") != str(self._session_price):
                        raise OrderRejected("확인한 최신 시세와 주문 단가가 달라 주문을 보내지 않았습니다.")
                try:
                    response = self._opener(req, timeout=10)
                except HTTPError as error:
                    response = error
                try:
                    status = getattr(response, "status", None) or response.getcode()
                    raw = response.read(2_000_001)
                    response_headers = {str(k).lower(): str(v).strip() for k, v in response.headers.items()}
                finally:
                    response.close()
            if len(raw) > 2_000_000:
                raise ValueError
            payload = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError
            if status != 200:
                raise ValueError
            if order and isinstance(payload.get("rt_cd"), str) and re.fullmatch(r"[0-9]+", payload["rt_cd"]) and int(payload["rt_cd"]) != 0:
                raise OrderRejected("증권사가 주문을 거절했습니다. 주문 조건과 계좌 상태를 확인해주세요.")
            return payload, response_headers
        except OrderRejected:
            raise
        except Exception:
            message = "주문 접수 여부가 불확실합니다. 재주문하지 말고 증권사 주문내역을 확인해주세요." if order else "증권사 연결 또는 응답을 확인할 수 없습니다."
            raise error_type(message) from None

    def _access_token(self):
        with self._token_lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            payload, _ = self._wire("/oauth2/tokenP", headers={"Content-Type": "application/json; charset=utf-8"},
                                    params={"grant_type": "client_credentials", "appkey": self._app_key, "appsecret": self._app_secret}, post=True)
            token = payload.get("access_token")
            if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token):
                raise BrokerError("증권사 인증을 확인할 수 없습니다.")
            lifetime = _int(payload.get("expires_in"), positive=True)
            if lifetime <= 60:
                raise BrokerError("증권사 인증의 유효기간을 확인할 수 없습니다.")
            self._token, self._token_until = token, time.monotonic() + min(lifetime, 86400) - 60
            return token

    def _request(self, endpoint, tr_id, params, *, post=False, order=False, continuation=""):
        headers = {"Content-Type": "application/json; charset=utf-8", "authorization": "Bearer " + self._access_token(),
                   "appkey": self._app_key, "appsecret": self._app_secret, "tr_id": tr_id,
                   "custtype": "P", "tr_cont": continuation}
        payload, response_headers = self._wire(API_ROOT + endpoint, headers=headers, params=params, post=post, order=order)
        if payload.get("rt_cd") != "0":
            if order:
                raise OrderUncertain("증권사 주문 응답을 확인할 수 없습니다. 주문내역에서 접수 여부를 확인해주세요.")
            raise BrokerError("증권사 조회가 거절되었거나 응답이 불완전합니다.")
        return payload, response_headers

    def _pages(self, endpoint, tr_id, params):
        rows, summaries, seen, continuation = [], [], set(), ""
        params = {**params, "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        for _ in range(20):
            payload, headers = self._request(endpoint, tr_id, params, continuation=continuation)
            page = payload.get("output1")
            if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
                raise BrokerError("증권사 목록 응답이 불완전합니다.")
            rows.extend(page)
            summaries.append(payload.get("output2"))
            if "tr_cont" not in headers:
                raise BrokerError("증권사 목록의 연속조회 여부를 확인할 수 없습니다.")
            marker = headers["tr_cont"].upper()
            if marker not in ("M", "F"):
                if marker not in ("", "D", "E"):
                    raise BrokerError("증권사 목록의 연속조회 여부를 확인할 수 없습니다.")
                return rows, summaries
            fk, nk = payload.get("ctx_area_fk100"), payload.get("ctx_area_nk100")
            if not isinstance(fk, str) or not isinstance(nk, str) or not (fk.strip() or nk.strip()) or (fk, nk) in seen:
                raise BrokerError("증권사 목록의 다음 페이지를 확인할 수 없습니다.")
            seen.add((fk, nk))
            params.update(CTX_AREA_FK100=fk, CTX_AREA_NK100=nk)
            continuation = "N"
        raise BrokerError("증권사 목록 조회가 완료되지 않았습니다.")

    def buying_power(self, symbol, limit_price=None):
        code = _code(symbol)
        if limit_price is not None and (type(limit_price) is not int or not 0 < limit_price <= 10**9):
            raise BrokerError("주문 단가는 양의 원 단위 정수여야 합니다.")
        variants = [("01", "0")] if limit_price is None else [("00", str(limit_price)), ("01", str(limit_price))]
        amounts, quantities = [], []
        for division, price in variants:
            payload, _ = self._request("trading/inquire-psbl-order", self._tr("power"),
                                      {**self._account_params(), "PDNO": code, "ORD_UNPR": price, "ORD_DVSN": division,
                                       "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
            output = _mapping(payload.get("output"))
            amounts.append(min(_int(output.get("ord_psbl_cash")), _int(output.get("nrcvb_buy_amt"))))
            quantities.append(_int(output.get("nrcvb_buy_qty")))
        return {"cash": min(amounts), "quantity": min(quantities), "symbol": code,
                "as_of": self._now().isoformat(timespec="seconds"), "account_identity": self.account_identity,
                "environment": self.environment}

    def _daily_orders(self, day, *, code="", side="00", order_id="", organization=""):
        return self._pages("trading/inquire-daily-ccld", self._tr("daily"),
                           {**self._account_params(), "INQR_STRT_DT": day, "INQR_END_DT": day,
                            "SLL_BUY_DVSN_CD": side, "PDNO": code, "CCLD_DVSN": "00", "INQR_DVSN": "00",
                            "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "ORD_GNO_BRNO": organization,
                            "ODNO": order_id, "EXCG_ID_DVSN_CD": "KRX"})[0]

    def account(self):
        rows, summaries = self._pages("trading/inquire-balance", self._tr("balance"),
                                     {**self._account_params(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                                      "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00"})
        cash, equity, positions, seen = None, None, [], set()
        for summary in summaries:
            if not isinstance(summary, list) or len(summary) != 1:
                raise BrokerError("증권사 계좌 평가금액을 확인할 수 없습니다.")
            item = _mapping(summary[0])
            current = (_int(item.get("ord_psbl_cash")), _int(item.get("tot_evlu_amt")))
            if cash is not None and current != (cash, equity):
                raise BrokerError("계좌 조회 중 평가금액이 변경되었습니다. 다시 조회해주세요.")
            cash, equity = current
        for row in rows:
            code, quantity = _code(row.get("pdno")), _int(row.get("hldg_qty"))
            if code in seen:
                raise BrokerError("증권사 보유 종목 응답이 중복되었습니다.")
            seen.add(code)
            if quantity:
                positions.append({"symbol": code, "name": str(row.get("prdt_name") or code)[:80], "quantity": quantity,
                                  "average_cost": float(_decimal(row.get("pchs_avg_pric"), positive=True)),
                                  "price": _int(row.get("prpr"), positive=True)})
        day = self._now().strftime("%Y%m%d")
        open_orders, ids = [], set()
        for row in self._daily_orders(day):
            parsed = self._fill_row(row)
            identity = (parsed["order_date"], parsed["broker_order_id"], parsed["organization_id"])
            if identity in ids or parsed["order_date"] != day:
                raise BrokerError("증권사 주문 목록이 중복되거나 기준일이 일치하지 않습니다.")
            ids.add(identity)
            if not parsed["terminal"]:
                open_orders.append(parsed)
        return {"cash": cash, "equity": equity, "positions": positions, "open_orders": open_orders,
                "as_of": self._now().isoformat(timespec="seconds"), "account_identity": self.account_identity,
                "environment": self.environment}

    def _market_status(self):
        current = self._now()
        if current.weekday() >= 5 or not (900 <= current.hour * 100 + current.minute < 1530):
            return "CLOSED", None
        day = current.strftime("%Y%m%d")
        with self._calendar_lock:
            if self._calendar_day != day:
                self._calendar_day, self._calendar_open = day, None
                try:
                    # Official samples transform CTCA0903R to VTCA0903R in paper mode.
                    tr_id = "VTCA0903R" if self.environment == "paper" else "CTCA0903R"
                    payload, _ = self._request("quotations/chk-holiday", tr_id,
                                              {"BASS_DT": day, "CTX_AREA_FK": "", "CTX_AREA_NK": ""})
                    output = payload.get("output")
                    if not isinstance(output, list):
                        raise BrokerError("개장일을 확인할 수 없습니다.")
                    matches = [row for row in output if isinstance(row, dict) and row.get("bass_dt") == day]
                    if len(matches) != 1 or matches[0].get("opnd_yn") not in ("Y", "N"):
                        raise BrokerError("개장일을 확인할 수 없습니다.")
                    self._calendar_open = matches[0]["opnd_yn"] == "Y"
                except BrokerError:
                    pass  # Unknown calendar prevents trading and is explicitly surfaced.
            if self._calendar_open is None:
                if self._session_valid_until is not None and current <= self._session_valid_until:
                    return "OPEN", None
                return "CLOSED", "증권사 개장일을 확인할 수 없어 자동주문을 차단했습니다."
            return ("OPEN", None) if self._calendar_open else ("CLOSED", None)

    def quote(self, symbol):
        code = _code(symbol)
        self._session_valid_until = None
        self._session_symbol, self._session_price = None, None
        payload, _ = self._request("quotations/inquire-price", "FHKST01010100",
                                  {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        item = _mapping(payload.get("output"))
        if item.get("stck_shrn_iscd") != code or item.get("temp_stop_yn") != "N":
            raise BrokerError("종목 일치 또는 거래 정지 상태를 확인할 수 없습니다.")
        price = _int(item.get("stck_prpr"), positive=True)
        change = _decimal(item.get("prdy_vrss"), signed=True)
        sign = item.get("prdy_vrss_sign")
        if sign not in ("1", "2", "3", "4", "5"):
            raise BrokerError("현재가의 전일대비 부호를 확인할 수 없습니다.")
        change = abs(change) * (-1 if sign in ("4", "5") else 1)
        previous = _int(Decimal(price) - change, positive=True)
        status, reason = self._market_status()
        current = self._now()
        bars = self.history(code)
        last_day = datetime.strptime(bars[-1]["date"], "%Y-%m-%d").date()
        # A retrieval clock is not a trade clock. Holidays and stale trades must
        # never become fresh OPEN quotes simply because this HTTP call succeeded.
        timestamp = datetime.combine(last_day, datetime.min.time(), KST).isoformat(timespec="seconds")
        timestamp_basis = "daily_bar_date"
        if status == "OPEN" or (reason is not None and self._calendar_open is None):
            intraday, _ = self._request("quotations/inquire-time-itemchartprice", "FHKST03010200",
                                        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                                         "FID_INPUT_HOUR_1": current.strftime("%H%M%S"),
                                         "FID_PW_DATA_INCU_YN": "N", "FID_ETC_CLS_CODE": ""})
            minute_rows = intraday.get("output2")
            if not isinstance(minute_rows, list) or not minute_rows:
                raise BrokerError("당일 분봉의 거래 기준일을 확인할 수 없습니다.")
            today_bars = [_mapping(row) for row in minute_rows if _date(_mapping(row).get("stck_bsop_date")) == current.date()]
            positive_today = any(_int(row.get("cntg_vol")) > 0 for row in today_bars)
            if last_day != current.date() or not positive_today:
                status, reason = "CLOSED", "당일 시세 기준일이 없어 자동주문을 차단했습니다."
            else:
                trades, _ = self._request("quotations/inquire-ccnl", "FHKST01010300",
                                          {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
                rows = trades.get("output")
                if not isinstance(rows, list) or not rows:
                    raise BrokerError("최근 체결 시각을 확인할 수 없습니다.")
                candidates = []
                for raw in rows:
                    row = _mapping(raw)
                    hour = row.get("stck_cntg_hour")
                    try:
                        if not isinstance(hour, str) or not re.fullmatch(r"[0-9]{6}", hour):
                            raise ValueError
                        clock_time = datetime.strptime(hour, "%H%M%S").time()
                    except ValueError:
                        raise BrokerError("최근 체결 시각을 확인할 수 없습니다.") from None
                    stamp = datetime.combine(current.date(), clock_time, KST)
                    candidates.append((stamp, _int(row.get("stck_prpr"), positive=True)))
                stamp, trade_price = max(candidates, key=lambda item: item[0])
                age = current - stamp
                if age < -timedelta(seconds=5) or age > timedelta(seconds=90):
                    raise BrokerError("최근 체결 시세가 오래되어 주문 기준으로 사용할 수 없습니다.")
                price, timestamp = trade_price, stamp.isoformat(timespec="seconds")
                timestamp_basis = "last_trade"
                status, reason = "OPEN", None
                self._session_valid_until = stamp + timedelta(seconds=90)
                self._session_symbol, self._session_price = code, trade_price
        return {"symbol": code, "name": str(item.get("hts_kor_isnm") or code)[:80], "price": price,
                "previous_close": previous, "change_pct": float((Decimal(price) / previous - 1) * 100),
                "volume": _int(item.get("acml_vol")), "source": "kis", "as_of": timestamp,
                "retrieved_at": current.isoformat(timespec="seconds"), "timestamp_basis": timestamp_basis, "currency": "KRW", "status": "ok",
                "market_status": status, "market_status_reason": reason, "history": [],
                "source_url": "https://apiportal.koreainvestment.com/"}

    def history(self, symbol):
        code, current = _code(symbol), self._now()
        payload, _ = self._request("quotations/inquire-daily-itemchartprice", "FHKST03010100",
                                  {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                                   "FID_INPUT_DATE_1": (current - timedelta(days=180)).strftime("%Y%m%d"),
                                   "FID_INPUT_DATE_2": current.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        rows = payload.get("output2")
        if not isinstance(rows, list) or not rows:
            raise BrokerError("증권사 일별 시세가 없습니다.")
        result, seen = [], set()
        for raw in rows:
            row = _mapping(raw)
            day = _date(row.get("stck_bsop_date"))
            if day in seen or day > current.date():
                raise BrokerError("증권사 시세 기준일이 중복되거나 미래입니다.")
            seen.add(day)
            result.append({"date": day.isoformat(), "close": _int(row.get("stck_clpr"), positive=True)})
        return sorted(result, key=lambda row: row["date"])

    def submit(self, symbol, side, quantity, limit_price=None):
        code = _code(symbol)
        if side not in ("buy", "sell") or type(quantity) is not int or not 1 <= quantity <= 1_000_000:
            raise OrderRejected("주문 방향과 정수 수량을 확인해주세요.")
        if limit_price is not None and (type(limit_price) is not int or not 0 < limit_price <= 10**9):
            raise OrderRejected("주문 단가는 양의 원 단위 정수여야 합니다.")
        market_status, _ = self._market_status()
        if market_status != "OPEN":
            raise OrderRejected("정규장 개장 여부를 확인할 수 없어 주문을 보내지 않았습니다.")
        payload, _ = self._request("trading/order-cash", self._tr(side),
                                  {**self._account_params(), "PDNO": code, "ORD_DVSN": "00" if limit_price is not None else "01",
                                   "ORD_QTY": str(quantity), "ORD_UNPR": str(limit_price) if limit_price is not None else "0",
                                   "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""},
                                  post=True, order=True)
        try:
            output = _mapping(payload.get("output"))
            order_id, organization = _id(output.get("ODNO")), _id(output.get("KRX_FWDG_ORD_ORGNO"))
            if not re.fullmatch(r"[0-9]{6}", str(output.get("ORD_TMD", ""))):
                raise BrokerError("접수 시각을 확인할 수 없습니다.")
        except BrokerError:
            raise OrderUncertain("주문이 접수되었을 수 있으나 식별자가 불완전합니다. 증권사 주문내역을 확인해주세요.") from None
        return {"broker_order_id": order_id, "organization_id": organization, "order_date": self._now().strftime("%Y%m%d"),
                "symbol": code, "side": side, "quantity": quantity, "limit_price": limit_price, "status": "accepted",
                "account_identity": self.account_identity, "environment": self.environment,
                "accepted_at": self._now().isoformat(timespec="seconds")}

    def _fill_row(self, row):
        row = _mapping(row)
        for field, expected in (("cano", self._account_no), ("acnt_prdt_cd", self._product_code)):
            if field in row and row[field] != expected:
                raise BrokerError("다른 계좌의 주문 응답이어서 처리할 수 없습니다.")
        code, order_id, organization = _code(row.get("pdno")), _id(row.get("odno")), _id(row.get("ord_gno_brno"))
        date = _date(row.get("ord_dt")).strftime("%Y%m%d")
        side = {"01": "sell", "02": "buy"}.get(row.get("sll_buy_dvsn_cd"))
        if side is None or row.get("cncl_yn") not in ("Y", "N"):
            raise BrokerError("주문 방향 또는 취소 여부를 확인할 수 없습니다.")
        quantity = _int(row.get("ord_qty"), positive=True)
        filled, remaining = _int(row.get("tot_ccld_qty")), _int(row.get("rmn_qty"))
        canceled, rejected = _int(row.get("cnc_cfrm_qty")), _int(row.get("rjct_qty"))
        active = quantity - filled - canceled - rejected
        if active < 0 or (remaining != active and not (row["cncl_yn"] == "Y" and remaining == quantity - filled and active == 0)):
            raise BrokerError("체결·취소·거부 수량과 잔여 수량이 일치하지 않습니다.")
        if (canceled > 0 and row["cncl_yn"] != "Y") or (row["cncl_yn"] == "Y" and not canceled and filled < quantity):
            raise BrokerError("주문 취소 수량을 확인할 수 없습니다.")
        average = float(_decimal(row.get("avg_prvs"), positive=True)) if filled else None
        amount = _int(row.get("tot_ccld_amt"), positive=True) if filled else 0
        terminal = active == 0
        status = ("filled" if filled == quantity else "canceled" if terminal and canceled else
                  "rejected" if terminal and rejected else "partial" if filled else "pending")
        return {"broker_order_id": order_id, "organization_id": organization, "order_date": date,
                "symbol": code, "side": side, "quantity": quantity, "status": status, "terminal": terminal,
                "filled_quantity": filled, "average_price": average, "filled_amount": amount,
                "remaining_quantity": active, "canceled_quantity": canceled, "rejected_quantity": rejected,
                "account_identity": self.account_identity, "environment": self.environment}

    def fills(self, order):
        order = _mapping(order)
        if order.get("account_identity") != self.account_identity or order.get("environment") != self.environment:
            raise BrokerError("현재 계좌와 일치하는 주문만 조회할 수 있습니다.")
        code, identifier, organization = _code(order.get("symbol")), _id(order.get("broker_order_id")), _id(order.get("organization_id"))
        day = _date(order.get("order_date"))
        if day > self._now().date() or self._now().date() - day > timedelta(days=89):
            raise BrokerError("주문 조회 가능 기간을 벗어났습니다.")
        if order.get("side") not in ("buy", "sell") or type(order.get("quantity")) is not int or order["quantity"] <= 0:
            raise BrokerError("저장된 주문 정보를 확인할 수 없습니다.")
        rows = self._daily_orders(day.strftime("%Y%m%d"), code=code, side="02" if order["side"] == "buy" else "01",
                                  order_id=identifier, organization=organization)
        matches = []
        for row in rows:
            if _same_id(row.get("odno"), identifier):
                parsed = self._fill_row(row)
                if (parsed["symbol"] != code or parsed["side"] != order["side"] or parsed["quantity"] != order["quantity"]
                        or parsed["order_date"] != day.strftime("%Y%m%d") or not _same_id(parsed["organization_id"], organization)):
                    raise BrokerError("접수된 주문과 체결 조회 응답이 일치하지 않습니다.")
                matches.append(parsed)
        if len(matches) > 1:
            raise BrokerError("주문 체결 응답이 중복되어 확정할 수 없습니다.")
        if not matches:
            return {"status": "pending", "terminal": False, "filled_quantity": None, "average_price": None,
                    "remaining_quantity": None, "reason": "주문이 아직 조회되지 않아 접수·체결을 확정할 수 없습니다."}
        result = matches[0]
        result["as_of"] = self._now().isoformat(timespec="seconds")
        return result

    def health(self):
        account = self.account()
        return {"status": "ok", "environment": self.environment, "account_identity": self.account_identity,
                "as_of": account["as_of"], "open_order_count": len(account["open_orders"])}
