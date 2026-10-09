"""Explicitly started personal automation; no upstream AI or broker side effects on import."""
import base64
import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from .engine import KST, now, numeric, symbol

POLICY = "personal-breakout-20-v1"
DEFAULTS = {"mode": "paper", "interval_seconds": 60, "order_budget": 1000000,
            "max_daily_buy": 3000000, "max_daily_orders": 10, "max_positions": 3,
            "stop_loss_pct": 5, "daily_loss_pct": 2, "symbols": ["005930", "000660", "035420"]}


def validate_config(payload, previous=None):
    if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
        raise ValueError("지원하지 않는 자동매매 설정입니다.")
    result = {**copy.deepcopy(previous or DEFAULTS), **payload}
    if result["mode"] not in ("paper", "kis-paper", "kis-live", "toss-live"):
        raise ValueError("자동매매 계좌 종류를 확인해주세요.")
    for key, lower, upper in (("interval_seconds", 30, 3600), ("order_budget", 10000, 100000000),
                             ("max_daily_buy", 10000, 1000000000), ("max_daily_orders", 1, 100),
                             ("max_positions", 1, 20)):
        value = numeric(result[key], lower, upper)
        if value != value.to_integral_value():
            raise ValueError("금액·주기·주문수·보유 종목수는 정수로 입력해주세요.")
        result[key] = int(value)
    for key in ("stop_loss_pct", "daily_loss_pct"):
        result[key] = float(numeric(result[key], Decimal("0.1"), 20))
    if result["order_budget"] > result["max_daily_buy"]:
        raise ValueError("1회 주문 예산은 일일 매수 한도 이하여야 합니다.")
    codes = result["symbols"]
    if not isinstance(codes, list) or not 1 <= len(codes) <= 20:
        raise ValueError("자동매매 대상은 1~20개 종목을 지정해주세요.")
    result["symbols"] = list(dict.fromkeys(symbol(code) for code in codes))
    return result


def signal(history, at, *, synthetic=False):
    """Use only completed, unique, ordered daily closes, never today's candle."""
    if not isinstance(history, list):
        raise ValueError("확정 일봉이 없습니다.")
    rows, seen = [], set()
    for row in history:
        day = date.fromisoformat(row["date"])
        if day > at.date() or day.weekday() >= 5:
            raise ValueError("일봉 기준일이 올바르지 않습니다.")
        if day in seen:
            raise ValueError("일봉 날짜가 중복되었습니다.")
        seen.add(day)
        close = numeric(row["close"], Decimal("0.0001"), Decimal("1e15"))
        if day < at.date():
            rows.append((day, close))
    if rows != sorted(rows) or len(rows) < 21:
        raise ValueError("시간순으로 확인된 확정 일봉이 21개 이상 필요합니다.")
    if not synthetic and (at.date() - rows[-1][0]).days > 7:
        raise ValueError("확정 일봉이 오래되어 판단을 보류합니다.")
    values = [row[1] for row in rows]
    ma = sum(values[-20:]) / 20
    breakout = values[-1] > max(values[-21:-1]) and values[-1] > ma
    return {"signal_date": rows[-1][0].isoformat(), "close": float(values[-1]), "breakout_level": float(max(values[-21:-1])),
            "ma20": float(ma), "breakout": breakout, "exit": values[-1] < ma,
            "bars": [{"date": day.isoformat(), "close": float(close)} for day, close in rows[-21:]]}


def tradable_quote(quote, source, at):
    if (quote.get("source") != source or quote.get("status") != "ok"
            or type(quote.get("price")) is not int or not 0 < quote["price"] <= 10**15):
        raise ValueError("확인된 거래 계좌의 시세가 없습니다.")
    symbol(quote.get("symbol"))
    if source == "demo":
        return
    stamp = datetime.fromisoformat(quote.get("as_of", ""))
    if stamp.tzinfo is None or not -5 <= (at - stamp).total_seconds() <= 90:
        raise ValueError("최신 장중 시세가 아니어서 주문을 보류합니다.")
    minutes = at.hour * 60 + at.minute
    if (at.weekday() >= 5 or not 9 * 60 <= minutes < 15 * 60 + 20
            or quote.get("market_status") != "OPEN"):
        raise ValueError("정규장 자동매매 시간(09:00~15:20)이 아니거나 거래 가능 상태가 아닙니다.")


class SecretFile:
    """Windows DPAPI binds secrets to the current OS user. Unix file mode is 0600."""
    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _dpapi(data, decrypt=False):
        import ctypes
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(data)
        incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = Blob()
        operation = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
        if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise ValueError("PC 사용자 인증정보 보관소를 사용할 수 없습니다.")
        try:
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(outgoing.pbData)

    def save(self, config):
        data = json.dumps(config, ensure_ascii=False).encode()
        if os.name == "nt":
            data = b"DPAPI1:" + base64.b64encode(self._dpapi(data))
        else:
            data = b"LOCAL1:" + data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def load(self):
        if not self.path.exists():
            return None
        data = self.path.read_bytes()
        if data.startswith(b"DPAPI1:") and os.name == "nt":
            data = self._dpapi(base64.b64decode(data[7:]), decrypt=True)
        elif data.startswith(b"LOCAL1:") and os.name != "nt":
            if self.path.stat().st_mode & 0o077:
                raise ValueError("인증정보 파일의 접근 권한을 확인해주세요.")
            data = data[7:]
        else:
            raise ValueError("이 PC의 인증정보를 읽을 수 없습니다. 연결 설정을 다시 입력해주세요.")
        return json.loads(data)

    def delete(self):
        self.path.unlink(missing_ok=True)


class RunLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        if self.handle:
            return
        handle = open(self.path, "a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            raise ValueError("다른 개인 서버가 이 계좌의 자동매매를 실행 중입니다.") from None
        self.handle = handle

    def release(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class AutoTrader:
    def __init__(self, store, market, *, broker_factory=None, toss_broker_factory=None, clock=None):
        self.store, self.market = store, market
        self.clock = clock or (lambda: datetime.now(KST))
        if broker_factory is None:
            from .kis_broker import KISBroker
            broker_factory = KISBroker
        self.broker_factory = broker_factory
        self.toss_broker_factory = toss_broker_factory
        self.secrets = SecretFile(store.path.parent / "kis-connection.local")
        self.toss_secrets = SecretFile(store.path.parent / "toss-connection.local")
        self.run_lock = RunLock(store.path.parent / "personal-autotrade.lock")
        self.broker_run_lock = None
        self.client_run_lock = None
        self.client_locks = {}
        self.lock, self.state_lock = threading.RLock(), threading.Lock()
        self.running, self.wake, self.closed = threading.Event(), threading.Event(), threading.Event()
        self.broker = self.connection_config = None
        self.broker_ready = False
        self.account = None
        self.dashboard_cache = None
        self.preview = []
        self.info = {"status": "stopped", "message": "자동매매는 중지 상태입니다. 설정을 확인한 뒤 시작해주세요.",
                     "last_run": None, "next_run": None}
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS auto_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, type TEXT NOT NULL, message TEXT NOT NULL, symbol TEXT);
                CREATE TABLE IF NOT EXISTS auto_positions(namespace TEXT NOT NULL, symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL, cost_basis REAL NOT NULL, PRIMARY KEY(namespace,symbol));
                CREATE TABLE IF NOT EXISTS auto_intents(id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    mode TEXT NOT NULL, signal_date TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                    quantity INTEGER NOT NULL, price INTEGER NOT NULL, planned_amount INTEGER NOT NULL,
                    status TEXT NOT NULL, broker TEXT, filled_quantity INTEGER NOT NULL DEFAULT 0,
                    average_price REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(namespace,signal_date,symbol,side));
            ''')
        with store.connection(write=True) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(auto_intents)")}
            for column, definition in (("policy", "TEXT"), ("decision", "TEXT"), ("risk_config", "TEXT")):
                if column not in columns:
                    db.execute(f"ALTER TABLE auto_intents ADD COLUMN {column} {definition}")
            if not store.meta(db, "auto_config"):
                store.set_meta(db, "auto_config", json.dumps(DEFAULTS))
        try:
            with store.connection() as db:
                active_provider = store.meta(db, "auto_connection_provider") or "kis"
            config = self._secret_file(active_provider).load() if active_provider in ("kis", "toss") else None
            if config:
                config = {**config, "provider": active_provider}
                self.broker = self._build_broker(config)
                self.connection_config = config
        except Exception:
            self._info(message="저장한 연결을 읽지 못했습니다. 인증정보를 다시 연결해주세요.")
        self.thread = threading.Thread(target=self._loop, name="personal-autotrade", daemon=True)
        # The thread starts only after an explicit start, never on server import/load.

    def _info(self, **changes):
        with self.state_lock:
            self.info.update(changes)

    def _event(self, kind, message, code=None):
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO auto_events(created_at,type,message,symbol) VALUES (?,?,?,?)", (now(), kind, message[:500], code))
            db.execute("DELETE FROM auto_events WHERE id NOT IN (SELECT id FROM auto_events ORDER BY id DESC LIMIT 1000)")

    def config(self):
        with self.store.connection() as db:
            return json.loads(self.store.meta(db, "auto_config"))

    @staticmethod
    def _provider(config):
        return (config or {}).get("provider", "kis")

    def _secret_file(self, provider):
        return self.toss_secrets if provider == "toss" else self.secrets

    def _build_broker(self, config):
        if not isinstance(config, dict) or config.get("provider", "kis") not in ("kis", "toss"):
            raise ValueError("연결할 증권사를 확인해주세요.")
        if self._provider(config) == "toss":
            if config.get("environment") != "live":
                raise ValueError("토스증권 연결은 실계좌만 지원합니다.")
            if self.toss_broker_factory is not None:
                return self.toss_broker_factory(config)
            from .toss_broker import TossBroker
            return TossBroker(config)
        return self.broker_factory(config)

    def _connection_mode(self):
        if not self.connection_config:
            return None
        return self._provider(self.connection_config) + "-" + self.connection_config["environment"]

    def _broker_source(self):
        return self._provider(self.connection_config)

    def _review_token(self, config=None, position_limit_pct=None):
        config = config or self.config()
        value = {"config": config, "policy": POLICY,
                 "position_limit_pct": self.store.settings()["max_position_pct"] if position_limit_pct is None else position_limit_pct,
                 "namespace": self._namespace(config["mode"])}
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _namespace(self, mode=None):
        mode = mode or self.config()["mode"]
        if mode == "paper":
            value = "paper:" + str(self.store.path.resolve())
        elif self.connection_config and mode == self._connection_mode():
            if self._provider(self.connection_config) == "toss":
                if not self.broker or not self.broker.account_identity:
                    return "unconnected"
                value = "toss:live:" + self.broker.account_identity
            else:
                # Preserve legacy KIS namespaces and their ownership/baselines.
                value = ":".join([self.connection_config["environment"], self.connection_config["account_no"], self.connection_config["product_code"]])
        else:
            return "unconnected"
        return hashlib.sha256(value.encode()).hexdigest()[:32]

    def state(self):
        with self.store.connection() as db:
            db.execute("BEGIN")
            config = json.loads(self.store.meta(db, "auto_config"))
            position_limit_pct = json.loads(self.store.meta(db, "settings"))["max_position_pct"]
        with self.state_lock:
            info = copy.deepcopy(self.info)
            account, preview = copy.deepcopy(self.account), copy.deepcopy(self.preview)
        connection = self.connection_config
        provider = self._provider(connection) if connection else None
        masked = ""
        if connection:
            if provider == "toss":
                masked = (getattr(self.broker, "account_masked", "") or connection.get("account_masked")
                          or "계좌 선택 " + str(connection.get("account_seq", "")))
            else:
                masked = connection["account_no"][:2] + "****" + connection["account_no"][-2:] + "-" + connection["product_code"]
        if account and account.get("mode") == config["mode"]:
            owned = self._owned(self._namespace(config["mode"]))
            for position in account.get("positions", []):
                managed = owned.get(position["symbol"], {}).get("quantity", 0)
                position["managed_quantity"] = managed
                position["auto_owned"] = managed > 0
                if type(position.get("price")) is int and type(position.get("quantity")) is int:
                    position["market_value"] = position["price"] * position["quantity"]
        with self.store.connection() as db:
            events = [dict(row) for row in db.execute("SELECT * FROM auto_events ORDER BY id DESC LIMIT 100")]
            orders = []
            for row in db.execute("SELECT * FROM auto_intents ORDER BY created_at DESC LIMIT 100"):
                item = {key: row[key] for key in ("id", "mode", "signal_date", "symbol", "side", "quantity", "price", "status", "filled_quantity", "average_price", "created_at", "updated_at")}
                receipt = json.loads(row["broker"] or "{}")
                item["broker_order_id"] = receipt.get("broker_order_id")
                orders.append(item)
            configured = self.store.meta(db, "auto_configured") == "yes"
        return {**info, "running": self.running.is_set(), "mode": config["mode"], "config": config,
                "configured": configured, "policy": POLICY, "events": events, "orders": orders,
                "position_limit_pct": position_limit_pct, "review_token": self._review_token(config, position_limit_pct),
                "preview": preview, "account": account if account and account.get("mode") == config["mode"] else None,
                "connection": {"configured": bool(connection), "provider": provider,
                               "environment": connection["environment"] if connection else None,
                               "account_masked": masked,
                               "ready": bool(connection and self.broker_ready), "message": "증권사 계좌 조회를 확인했습니다." if connection and self.broker_ready else "증권사 API 연결과 계좌 조회 확인이 필요합니다."}}

    def dashboard_snapshot(self, *, refresh=False):
        """Read balances without running signals, reconciling intents or ordering.

        The local paper ledger remains separate. An unavailable broker response
        never becomes a paper balance, a zero estimate or an unlabelled old value.
        """
        mode = self.config()["mode"]
        if mode == "paper":
            self.dashboard_cache = None
            return None
        provider = "toss" if mode == "toss-live" else "kis"
        base = {"kind": "broker", "mode": mode, "provider": provider, "status": "unavailable",
                "cash": None, "equity": None, "positions": [], "initial_cash": None,
                "unrealized_pnl": None, "realized_pnl": None, "as_of": None, "fetched_at": None,
                "total_position_count": None, "excluded_positions_count": None,
                "equity_basis": "krw-trading-capital" if provider == "toss" else "account-equity",
                "account_masked": self.state()["connection"]["account_masked"],
                "message": "설정한 증권사 계좌를 연결한 뒤 잔고를 다시 조회하세요."}
        # A trading cycle may hold this lock for broker I/O. Keep the page usable
        # rather than waiting indefinitely or issuing a concurrent client token.
        if not self.lock.acquire(timeout=2):
            return {**base, "message": "다른 계좌 점검이 진행 중입니다. 잠시 후 새로고침하세요."}
        try:
            if self.config()["mode"] != mode:
                return {**base, "message": "계좌 설정이 변경됐습니다. 다시 조회하세요."}
            if not self.broker or not self.connection_config or self._connection_mode() != mode:
                self.dashboard_cache = None
                return base
            key = (mode, self._namespace(mode), id(self.broker))
            cached = self.dashboard_cache
            if not refresh and cached and cached[0] == key and time.monotonic() < cached[1]:
                return copy.deepcopy(cached[2])
            try:
                with self._exclusive():
                    expected = self.connection_config.get("verified_account_identity") or self.broker.account_identity
                    account = self.broker.account()
                    # Open orders block trading, but must not hide account balances.
                    self._validate_account({**account, "open_orders": []})
                    if not expected or account.get("account_identity") != expected or self.broker.account_identity != expected or account.get("environment") != self.broker.environment:
                        raise ValueError("계좌 조회 identity가 일치하지 않습니다.")
                    stamp = datetime.fromisoformat(account["as_of"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError("계좌 조회 시각을 확인하지 못했습니다.")
                    positions = []
                    equity = float(numeric(account["equity"], 0, Decimal("1e15")))
                    for row in account["positions"]:
                        if row["quantity"] == 0:
                            continue
                        quantity, price = row["quantity"], float(numeric(row["price"], 1, Decimal("1e15")))
                        value = price * quantity
                        average = row.get("average_cost")
                        average = float(numeric(average, 0, Decimal("1e15"))) if average is not None else None
                        positions.append({"symbol": row["symbol"], "name": row.get("name") or row["symbol"],
                                          "quantity": quantity, "price": price, "market_value": value,
                                          "average_cost": average,
                                          "unrealized_pnl": (price - average) * quantity if average is not None else None,
                                          "weight_pct": value / equity * 100 if equity else None})
                    view = {**base, "status": "ok", "cash": account["cash"], "equity": account["equity"],
                            "positions": positions, "as_of": account["as_of"], "fetched_at": self.clock().isoformat(),
                            "total_position_count": self._position_count(account),
                            "excluded_positions_count": account.get("excluded_positions_count", 0),
                            "open_order_count": len(account.get("open_orders") or []),
                            "equity_basis": account.get("equity_basis") or base["equity_basis"],
                            "message": "증권사에서 읽기 전용으로 조회한 잔고입니다."}
                    self.broker_ready = True
                    with self.state_lock:
                        self.account = {"mode": mode, **{field: account.get(field) for field in
                            ("cash", "equity", "positions", "as_of", "total_position_count", "equity_basis",
                             "excluded_positions_count", "order_visibility", "venue_scope")}}
            except Exception:
                view = {**base, "message": "증권사 잔고를 조회하지 못했습니다. 연결과 허용 IP를 확인하고 다시 조회하세요."}
            self.dashboard_cache = (key, time.monotonic() + 30, copy.deepcopy(view))
            return view
        finally:
            self.lock.release()

    def _pending(self, db=None):
        sql = "SELECT * FROM auto_intents WHERE status IN ('submitting','pending','partial','uncertain') ORDER BY created_at"
        if db:
            return [dict(row) for row in db.execute(sql)]
        with self.store.connection() as connection:
            return self._pending(connection)

    @contextmanager
    def _exclusive(self):
        already_owned = self.run_lock.handle is not None
        self.run_lock.acquire()
        broker_lock, broker_owned, client_lock, client_owned = None, False, None, False
        try:
            if self.connection_config and self._provider(self.connection_config) == "toss":
                client_lock = self._client_lock(self.connection_config)
                client_owned = client_lock.handle is not None
                client_lock.acquire()
            if self.config()["mode"] != "paper" and self.connection_config:
                broker_lock = self._broker_lock()
                broker_owned = broker_lock.handle is not None
                broker_lock.acquire()
            yield
        finally:
            if broker_lock and not broker_owned:
                broker_lock.release()
            if client_lock and not client_owned:
                client_lock.release()
            if not already_owned:
                self.run_lock.release()

    def _broker_lock(self):
        namespace = self._namespace(self._connection_mode())
        root = Path.home() / ".prism-personal" / "locks"
        root.mkdir(parents=True, exist_ok=True)
        path = root / (namespace + ".lock")
        if not self.broker_run_lock or self.broker_run_lock.path != path:
            if self.broker_run_lock and self.broker_run_lock.handle:
                raise ValueError("실행 중에는 증권사 계좌를 바꿀 수 없습니다.")
            self.broker_run_lock = RunLock(path)
        return self.broker_run_lock

    def _client_lock(self, config):
        identifier = config.get("client_id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("토스증권 클라이언트 ID를 확인해주세요.")
        namespace = hashlib.sha256(("toss-client:" + identifier).encode()).hexdigest()
        root = Path.home() / ".prism-personal" / "locks"
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("toss-client-" + namespace + ".lock")
        if self.client_run_lock and self.client_run_lock.path == path:
            return self.client_run_lock
        if path not in self.client_locks:
            self.client_locks[path] = RunLock(path)
        return self.client_locks[path]

    @contextmanager
    def _toss_client_lease(self, config):
        client_lock = self._client_lock(config)
        owned = client_lock.handle is not None
        client_lock.acquire()
        try:
            yield
        finally:
            if not owned:
                client_lock.release()

    def _release_locks(self):
        self.run_lock.release()
        if self.broker_run_lock:
            self.broker_run_lock.release()
        if self.client_run_lock:
            self.client_run_lock.release()

    def update_config(self, payload):
        with self.lock, self._exclusive():
            if self.running.is_set() or self._pending():
                raise ValueError("자동매매를 중지하고 미체결·불확실 주문을 확인한 뒤 설정을 변경해주세요.")
            previous = self.config()
            result = validate_config(payload, previous)
            if result["mode"] != previous["mode"] and self._owned():
                raise ValueError("현재 모드의 자동 보유분을 정리한 뒤 계좌 종류를 변경해주세요.")
            with self.store.connection(write=True) as db:
                self.store.set_meta(db, "auto_config", json.dumps(result, allow_nan=False))
                self.store.set_meta(db, "auto_configured", "yes")
            if result != previous:
                if result["mode"] != previous["mode"]:
                    self.dashboard_cache = None
                with self.state_lock:
                    self.preview = []
                    if result["mode"] != previous["mode"]:
                        self.account = None
                self._info(last_run=None, next_run=None, message="새 설정을 저장했습니다. 신호·계좌를 다시 점검해주세요.")
            self._event("settings", "자동매매 설정을 저장했습니다.")
            return self.state()

    def toss_accounts(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"client_id", "client_secret"}:
            raise ValueError("토스증권 클라이언트 ID와 비밀키를 입력해주세요.")
        config = {**payload, "provider": "toss", "environment": "live"}
        with self.lock, self._exclusive():
            if self.running.is_set() or self._pending():
                raise ValueError("자동매매를 중지하고 주문 상태를 확인한 뒤 계좌를 조회해주세요.")
            with self._toss_client_lease(config):
                if (self.connection_config and self._provider(self.connection_config) == "toss"
                        and all(self.connection_config.get(key) == config[key] for key in payload)):
                    broker = self.broker
                else:
                    broker = self._build_broker(config)
                # Preserve int64 accountSeq values across JavaScript JSON parsing.
                accounts = broker.list_accounts()
                return {"accounts": [{**row, "account_seq": str(row["account_seq"]) if row["account_seq"] > 2**53 - 1 else row["account_seq"]} for row in accounts]}

    def connect(self, config):
        if not isinstance(config, dict):
            raise ValueError("증권사 연결 정보를 확인해주세요.")
        config = {**config, "provider": config.get("provider", "kis")}
        if self._provider(config) == "toss" and set(config) != {"provider", "environment", "client_id", "client_secret", "account_seq"}:
            raise ValueError("토스증권 연결 정보 형식을 확인해주세요.")
        if self._provider(config) == "toss":
            seq = config.get("account_seq")
            if isinstance(seq, str) and re.fullmatch(r"[1-9][0-9]{0,18}", seq):
                seq = int(seq)
            if type(seq) is not int or not 0 < seq < 2**63:
                raise ValueError("조회한 토스증권 계좌를 선택해주세요.")
            config["account_seq"] = seq
        with self.lock, self._exclusive():
            if self.running.is_set() or self._pending():
                raise ValueError("자동매매를 중지하고 주문 상태를 확인한 뒤 연결해주세요.")
            old = self.connection_config
            old_owned = old and self._owned(self._namespace(self._connection_mode()))
            if old_owned:
                identity = ("environment", "account_no", "product_code") if self._provider(old) == "kis" else ("environment", "account_seq", "client_id")
                if self._provider(config) != self._provider(old) or any(config.get(key) != old.get(key) for key in identity):
                    raise ValueError("자동 보유분이 남은 계좌는 다른 계좌로 바꿀 수 없습니다.")
            # Authentication/account reads never submit an order. Toss clients
            # are leased before auth because a new token revokes their old one.
            if self._provider(config) == "toss":
                with self._toss_client_lease(config):
                    broker = self._build_broker(config)
                    account = broker.account()
            else:
                broker = self._build_broker(config)
                account = broker.account()
            self._validate_account(account)
            if old_owned and broker.account_identity != self.broker.account_identity:
                raise ValueError("자동 보유분이 남은 실제 계좌와 연결 정보가 다릅니다.")
            if self._provider(config) == "toss":
                config.update(verified_account_identity=broker.account_identity,
                              account_masked=getattr(broker, "account_masked", ""))
            self._secret_file(self._provider(config)).save(config)
            with self.store.connection(write=True) as db:
                self.store.set_meta(db, "auto_connection_provider", self._provider(config))
            self.broker, self.connection_config = broker, dict(config)
            self.dashboard_cache = None
            self.broker_ready = True
            with self.state_lock:
                self.preview = []
                expected = self._connection_mode()
                self.account = {**account, "mode": expected} if self.config()["mode"] == expected else None
            name = "토스증권" if self._provider(config) == "toss" else "한국투자증권"
            self._event("connected", name + " 계좌를 읽기 전용으로 확인했습니다. 자동매매는 중지 상태입니다.")
            return self.state()

    def disconnect(self):
        with self.lock, self._exclusive():
            if (self.running.is_set() or self._pending() or self._owned()
                    or (self.connection_config and self._owned(self._namespace(self._connection_mode())))):
                raise ValueError("자동매매 중지 및 주문·자동 보유분 정리 후 연결을 해제해주세요.")
            if self.connection_config:
                self._secret_file(self._provider(self.connection_config)).delete()
            with self.store.connection(write=True) as db:
                self.store.set_meta(db, "auto_connection_provider", "none")
            self.broker = self.connection_config = None
            self.dashboard_cache = None
            self.broker_ready = False
            with self.state_lock:
                self.account = None
                self.preview = []
            return self.state()

    def _owned(self, namespace=None):
        namespace = namespace or self._namespace()
        with self.store.connection() as db:
            return {row["symbol"]: dict(row) for row in db.execute("SELECT * FROM auto_positions WHERE namespace=? AND quantity>0", (namespace,))}

    @staticmethod
    def _validate_account(account):
        if not isinstance(account, dict) or not isinstance(account.get("positions"), list):
            raise ValueError("계좌 잔고를 확인하지 못했습니다.")
        numeric(account.get("cash"), 0, Decimal("1e15"))
        numeric(account.get("equity"), 0, Decimal("1e15"))
        seen = set()
        for position in account["positions"]:
            code = symbol(position.get("symbol"))
            if code in seen or type(position.get("quantity")) is not int or position["quantity"] < 0:
                raise ValueError("계좌 보유 수량이 불명확합니다.")
            seen.add(code)
            numeric(position.get("price"), 1, Decimal("1e15"))
        if account.get("open_orders"):
            raise ValueError("계좌에 미체결 주문이 있습니다. 증권사에서 먼저 확인해주세요.")
        if "total_position_count" in account:
            count = account["total_position_count"]
            if type(count) is not int or count < sum(p["quantity"] > 0 for p in account["positions"]):
                raise ValueError("계좌 전체 보유 종목 수를 확인하지 못했습니다.")

    @staticmethod
    def _position_count(account):
        return account.get("total_position_count", sum(p["quantity"] > 0 for p in account["positions"]))

    def _account(self, config, quotes=None):
        if config["mode"] == "paper":
            quotes = quotes or self.market.quotes(self.store.required_symbols())
            account = self.store.snapshot(quotes)
        else:
            if not self.broker or not self.connection_config:
                raise ValueError("거래할 증권사 계좌를 먼저 연결해주세요.")
            if self._connection_mode() != config["mode"]:
                raise ValueError("연결한 증권사·모의·실계좌 종류와 자동매매 설정이 다릅니다.")
            try:
                account = self.broker.account()
                self._validate_account(account)
                self.broker_ready = True
            except Exception:
                self.broker_ready = False
                raise
        self._validate_account(account)
        with self.state_lock:
            # Local paper and broker accounts have separate ledgers and UI labels.
            self.account = {"mode": config["mode"], **{key: account.get(key) for key in ("cash", "equity", "positions", "as_of", "total_position_count", "equity_basis", "excluded_positions_count", "order_visibility", "venue_scope")}}
        return account

    def start(self, payload=None):
        payload = payload or {}
        with self.lock:
            if self.closed.is_set():
                raise ValueError("서버를 다시 시작해주세요.")
            if self.running.is_set():
                return self.state()
            config = self.config()
            if self._pending():
                raise ValueError("미체결·불확실 주문을 점검한 뒤 시작해주세요.")
            with self.store.connection() as db:
                saved = self.store.meta(db, "auto_configured") == "yes"
            if config["mode"] != "paper" and not saved:
                raise ValueError("거래 한도와 대상 종목을 직접 저장한 뒤 시작해주세요.")
            if config["mode"] in ("kis-live", "toss-live") and payload.get("confirm_live") is not True:
                raise ValueError("실계좌·전략·주문 한도 확인이 필요합니다.")
            if config["mode"] == "toss-live" and payload.get("confirm_external_orders") is not True:
                raise ValueError("토스 앱의 전체 미체결·예약·조건 주문 확인이 필요합니다.")
            if config["mode"] in ("kis-live", "toss-live") and payload.get("review_token") != self._review_token(config):
                raise ValueError("계좌·전략·한도가 변경되었거나 확인 정보가 없습니다. 시작 화면을 다시 확인해주세요.")
            self.run_lock.acquire()
            try:
                if config["mode"] != "paper":
                    if not self.connection_config:
                        raise ValueError("거래할 증권사 계좌를 먼저 연결해주세요.")
                    if self._connection_mode() != config["mode"]:
                        raise ValueError("연결한 증권사와 자동매매 설정이 다릅니다.")
                    if self._provider(self.connection_config) == "toss":
                        self.client_run_lock = self._client_lock(self.connection_config)
                        self.client_run_lock.acquire()
                    self._broker_lock().acquire()
                account = self._account(config)
                if numeric(account["equity"], 0, Decimal("1e15")) <= 0:
                    raise ValueError("연결 계좌의 거래 기준 자산이 없어 자동매매를 시작할 수 없습니다.")
                if config["mode"] in ("kis-live", "toss-live") and payload.get("review_token") != self._review_token(config):
                    raise ValueError("실제 조회 계좌가 시작 전 확인 정보와 다릅니다. 계좌를 다시 확인해주세요.")
                with self.store.connection(write=True) as db:
                    self._daily(db, self._namespace(config["mode"]), account, self.clock())
                self.running.set()
                self._info(status="running", message="자동매매를 시작했습니다. 시세·신호·계좌 한도를 점검합니다.")
                self._event("started", f"{config['mode']} 자동매매 시작 · {POLICY}")
                if not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._loop, name="personal-autotrade", daemon=True)
                    self.thread.start()
                self.wake.set()
            except Exception:
                self.running.clear()
                self._release_locks()
                raise
            return self.state()

    def stop(self):
        # Clear before waiting for the in-flight cycle lock. After this returns,
        # no submission can start. Already accepted broker orders remain active.
        self.running.clear()
        self.wake.set()
        with self.lock:
            self.running.clear()
            self._release_locks()
            self._info(status="stopped", next_run=None, message="자동매매 중지. 이미 접수된 주문은 증권사에서 미체결·취소 상태를 확인해주세요.")
            self._event("stopped", "새 자동 주문을 중지했습니다. 접수된 주문은 자동 취소하지 않습니다.")
            return self.state()

    def shutdown(self):
        self.stop()
        self.closed.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=20)

    def check(self):
        with self.lock, self._exclusive():
            self._cycle(execute=False)
            return self.state()

    def _loop(self):
        while not self.closed.is_set():
            self.wake.wait(timeout=1 if not self.running.is_set() else self.config()["interval_seconds"])
            self.wake.clear()
            if self.running.is_set() and not self.closed.is_set():
                with self.lock:
                    if self.running.is_set():
                        try:
                            self._cycle(execute=True)
                        except Exception:
                            self._info(status="waiting", message="계좌·시세·주문 확인에 실패했습니다. 새 주문은 보내지 않았습니다.")
                            self._event("error", "점검 실패. 인증정보와 증권사 주문내역을 확인해주세요.")
            if self.running.is_set():
                self._info(next_run=(self.clock() + timedelta(seconds=self.config()["interval_seconds"])).isoformat())

    def _reconcile(self):
        for intent in self._pending():
            if intent["mode"] == "paper":
                with self.store.connection() as db:
                    row = db.execute("SELECT * FROM orders WHERE request_key=?", ("auto-" + intent["id"],)).fetchone()
                if row:
                    self._settle(intent, {"status": "filled", "filled_quantity": row["quantity"], "average_price": row["price"]})
                else:
                    # Local SQLite submission has no remote uncertainty: absent
                    # committed ledger row means no fill. Never retry its intent.
                    self._status(intent["id"], "rejected")
            elif self.broker and intent["namespace"] == self._namespace(intent["mode"]) and intent["broker"]:
                receipt = json.loads(intent["broker"])
                if receipt.get("broker_order_id"):
                    self._settle(intent, self.broker.fills(receipt))

    def _status(self, identifier, status, broker=None):
        with self.store.connection(write=True) as db:
            db.execute("UPDATE auto_intents SET status=?,broker=COALESCE(?,broker),updated_at=? WHERE id=?",
                       (status, json.dumps(broker) if broker is not None else None, now(), identifier))

    def _settle(self, intent, fill):
        status, quantity = fill.get("status"), fill.get("filled_quantity")
        if status not in ("pending", "partial", "filled", "canceled", "rejected") or type(quantity) is not int or not 0 <= quantity <= intent["quantity"]:
            raise ValueError("주문 체결 수량을 확인할 수 없습니다.")
        terminal = status in ("filled", "canceled", "rejected")
        if status == "filled" and quantity != intent["quantity"]:
            raise ValueError("전체 체결 수량과 주문 수량이 다릅니다.")
        price = float(numeric(fill.get("average_price"), 1, Decimal("1e15"))) if quantity else None
        with self.store.connection() as db:
            current = db.execute("SELECT * FROM auto_intents WHERE id=?", (intent["id"],)).fetchone()
        if not current or current["status"] in ("filled", "canceled", "rejected"):
            return
        if quantity < current["filled_quantity"] or (status == "rejected" and quantity and intent["mode"] != "toss-live"):
            raise ValueError("체결 수량이 이전 확인보다 줄었거나 거절 상태와 맞지 않습니다.")
        if quantity and ((intent["side"] == "buy" and price > intent["price"]) or (intent["side"] == "sell" and price < intent["price"])):
            raise ValueError("지정가 조건과 체결 가격이 달라 정산을 보류합니다.")
        if not terminal:
            with self.store.connection(write=True) as db:
                db.execute("UPDATE auto_intents SET status=?,filled_quantity=?,average_price=?,updated_at=? WHERE id=?",
                           (status, quantity, price, now(), intent["id"]))
            return
        metadata = json.loads(current["broker"] or "{}")
        before = metadata.get("pre_order_quantity")
        if type(before) is not int or before < 0:
            raise ValueError("주문 전 계좌 수량 기록이 없어 정산을 보류합니다.")
        if intent["mode"] == "paper":
            account = self.store.snapshot(self.market.quotes(self.store.required_symbols()))
        else:
            account = self.broker.account()
        self._validate_account(account)
        expected = before + quantity if intent["side"] == "buy" else before - quantity
        actual = next((row["quantity"] for row in account["positions"] if row["symbol"] == intent["symbol"]), 0)
        if actual != expected or expected < 0:
            raise ValueError("체결 내역과 계좌 보유 수량이 달라 정산을 보류합니다.")
        with self.state_lock:
            self.account = {"mode": intent["mode"], **{key: account.get(key) for key in ("cash", "equity", "positions", "as_of", "total_position_count", "equity_basis", "excluded_positions_count", "order_visibility", "venue_scope")}}
        with self.store.connection(write=True) as db:
            current = db.execute("SELECT * FROM auto_intents WHERE id=?", (intent["id"],)).fetchone()
            if current["status"] in ("filled", "canceled", "rejected"):
                return
            owned = db.execute("SELECT * FROM auto_positions WHERE namespace=? AND symbol=?", (intent["namespace"], intent["symbol"])).fetchone()
            old_quantity = owned["quantity"] if owned else 0
            basis = owned["cost_basis"] if owned else 0
            if quantity:
                if intent["side"] == "buy":
                    db.execute("INSERT INTO auto_positions VALUES (?,?,?,?) ON CONFLICT(namespace,symbol) DO UPDATE SET quantity=excluded.quantity,cost_basis=excluded.cost_basis",
                               (intent["namespace"], intent["symbol"], old_quantity + quantity, basis + price * quantity))
                else:
                    if quantity > old_quantity:
                        raise ValueError("자동매매 소유 수량과 체결 수량이 맞지 않습니다.")
                    remaining = old_quantity - quantity
                    if remaining:
                        db.execute("UPDATE auto_positions SET quantity=?,cost_basis=? WHERE namespace=? AND symbol=?", (remaining, basis * remaining / old_quantity, intent["namespace"], intent["symbol"]))
                    else:
                        db.execute("DELETE FROM auto_positions WHERE namespace=? AND symbol=?", (intent["namespace"], intent["symbol"]))
            db.execute("UPDATE auto_intents SET status=?,filled_quantity=?,average_price=?,updated_at=? WHERE id=?", (status, quantity, price, now(), intent["id"]))
        self._event("settled", f"{intent['symbol']} {status} 확인 · 체결 {quantity}주", intent["symbol"])

    def _daily(self, db, namespace, account, at):
        day = at.date().isoformat()
        key = "auto_baseline:" + namespace + ":" + day
        baseline = self.store.meta(db, key)
        if baseline is None:
            baseline = account["equity"]
            self.store.set_meta(db, key, baseline)
        rows = db.execute("SELECT * FROM auto_intents WHERE namespace=? AND substr(created_at,1,10)=?", (namespace, day)).fetchall()
        buys = sum(row["planned_amount"] for row in rows if row["side"] == "buy"
                   and (row["status"] != "rejected" or row["filled_quantity"] > 0))
        return {"orders": sum(row["side"] == "buy" for row in rows), "buys": buys, "baseline": float(baseline)}

    def _reserve(self, config, account, code, side, quantity, price, signal_date, at, *, decision=None, quote=None):
        namespace = self._namespace(config["mode"])
        identifier = uuid.uuid4().hex
        planned = int((Decimal(price * quantity) * Decimal("1.01")).to_integral_value(rounding=ROUND_CEILING)) if side == "buy" else 0
        with self.store.connection(write=True) as db:
            daily = self._daily(db, namespace, account, at)
            if side == "buy":
                if daily["orders"] >= config["max_daily_orders"]:
                    raise ValueError("일일 자동 매수 주문 수 한도에 도달했습니다. 청산 점검은 계속합니다.")
                if account["equity"] <= daily["baseline"] * (1 - config["daily_loss_pct"] / 100):
                    raise ValueError("일일 손실 한도에 도달해 신규 매수를 중단합니다.")
                if daily["buys"] + planned > config["max_daily_buy"]:
                    raise ValueError("일일 자동 매수 금액 한도에 도달했습니다.")
            if db.execute("SELECT 1 FROM auto_intents WHERE namespace=? AND signal_date=? AND symbol=? AND side=?", (namespace, signal_date, code, side)).fetchone():
                raise ValueError("같은 확정 신호로 이미 주문을 시도했습니다.")
            before = next((row["quantity"] for row in account["positions"] if row["symbol"] == code), 0)
            metadata = {"pre_order_quantity": before, "limit_price": price}
            if config["mode"] == "toss-live":
                metadata.update({"client_order_id": identifier, "symbol": code, "side": side, "quantity": quantity,
                                 "order_date": at.strftime("%Y%m%d"), "attempt_at": at.isoformat(), "account_identity": self.broker.account_identity,
                                 "environment": "live"})
            db.execute("INSERT INTO auto_intents(id,namespace,mode,signal_date,symbol,side,quantity,price,planned_amount,status,broker,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identifier, namespace, config["mode"], signal_date, code, side, quantity, price, planned, "submitting", json.dumps(metadata), at.isoformat(), now()))
            evidence = {"signal": decision, "quote": {key: (quote or {}).get(key) for key in ("source", "as_of", "retrieved_at", "timestamp_basis", "market_status", "price")}}
            limits = {"automation": config, "account": {"cash": account["cash"], "equity": account["equity"]},
                      "daily": daily, "ledger": self.store.settings()}
            db.execute("UPDATE auto_intents SET policy=?,decision=?,risk_config=? WHERE id=?",
                       (POLICY, json.dumps(evidence, allow_nan=False), json.dumps(limits, allow_nan=False), identifier))
        return {"id": identifier, "namespace": namespace, "mode": config["mode"], "symbol": code, "side": side, "quantity": quantity, "price": price}

    def _cycle(self, *, execute):
        config, at = self.config(), self.clock()
        self._info(last_run=at.isoformat())
        self._reconcile()
        if self._pending():
            self._info(status="blocked", message="미체결·부분체결·불확실 주문이 남아 새 주문을 중단했습니다. 연결 증권사에서 확인 후 다시 점검해주세요.")
            return
        account = self._account(config)
        with self.store.connection(write=True) as db:
            self._daily(db, self._namespace(config["mode"]), account, at)
        owned = self._owned()
        held = {row["symbol"]: row for row in account["positions"] if row["quantity"] > 0}
        if any(code not in held or held[code]["quantity"] < position["quantity"] for code, position in owned.items()):
            self._info(status="blocked", next_run=None, message="자동 보유 수량과 계좌가 다릅니다. 수동 거래 내역을 확인해주세요.")
            self.running.clear()
            self._release_locks()
            return
        preview = []
        # Exit decisions precede entries; selected symbols may change without
        # orphaning an existing owned position's protection checks.
        codes = list(dict.fromkeys(list(owned) + config["symbols"]))
        for code in codes:
            if execute and not self.running.is_set():
                break
            try:
                if config["mode"] == "paper":
                    quote = self.market.quote(code, fresh=True, history=True)
                    source = self.market.provider
                else:
                    quote = self.broker.quote(code)
                    source = self._broker_source()
                tradable_quote(quote, source, self.clock())
                own = owned.get(code)
                stop_hit = bool(own and quote["price"] <= own["cost_basis"] / own["quantity"] * (1 - config["stop_loss_pct"] / 100))
                try:
                    history = quote["history"] if config["mode"] == "paper" else self.broker.history(code)
                    decision = signal(history, at, synthetic=source == "demo")
                except Exception:
                    if not stop_hit:
                        raise ValueError("확정 일봉을 확인하지 못해 신호 판단을 보류합니다.") from None
                    # A confirmed fresh stop price must not depend on optional
                    # chart availability. It still needs broker cash/shares gates.
                    decision = {"signal_date": at.date().isoformat(), "close": None, "ma20": None, "exit": False, "breakout": False}
                price, side, reason, quantity = quote["price"], None, "돌파·청산 조건이 없습니다.", 0
                if own:
                    average = own["cost_basis"] / own["quantity"]
                    if price <= average * (1 - config["stop_loss_pct"] / 100):
                        side, reason = "sell", "주문 시점 시세가 설정 손절선에 도달했습니다."
                    elif decision["exit"]:
                        side, reason = "sell", "확정 종가가 20일 평균 아래입니다."
                    quantity = own["quantity"] if side else 0
                elif code in held:
                    reason = "기존 수동 보유분은 자동매매가 관리하지 않습니다."
                elif decision["breakout"]:
                    if price <= decision["breakout_level"] or price <= decision["ma20"]:
                        raise ValueError("현재 시세에서 돌파·상승 추세가 유지되지 않아 매수를 보류합니다.")
                    side, reason = "buy", "확정 종가가 앞선 20일 최고 종가를 돌파했습니다."
                    if self._position_count(account) >= config["max_positions"]:
                        raise ValueError("계좌의 최대 보유 종목 수 한도입니다.")
                    budget = min(config["order_budget"], account["cash"])
                    # Reserve 1% within the configured budget, then the ledger
                    # or broker orderability gate checks actual fees/cash too.
                    quantity = int(Decimal(str(budget)) / (Decimal(price) * Decimal("1.01")))
                    cap = account["equity"] * self.store.settings()["max_position_pct"] / 100
                    quantity = min(quantity, int(cap * 0.99 / price))
                    if config["mode"] != "paper":
                        power = self.broker.buying_power(code, limit_price=price)
                        quantity = min(quantity, 1000000, int(numeric(power.get("quantity"), 0, Decimal("1e15"))), int(numeric(power.get("cash"), 0, Decimal("1e15")) / (Decimal(price) * Decimal("1.01"))))
                    if quantity < 1:
                        raise ValueError("예산·현금·계좌 비중 한도 내에서 1주를 주문할 수 없습니다.")
                preview.append({"symbol": code, "action": side or "wait", "reason": reason, "signal_date": decision["signal_date"], "quantity": quantity, "price": price, "policy": POLICY,
                                "close": decision["close"], "ma20": decision["ma20"]})
                if side and execute and self.running.is_set():
                    self._execute(config, account, quote, side, quantity, decision, at)
                    # One submission per cycle. Account and fills must be fresh
                    # before considering another candidate on the next cycle.
                    break
            except (ValueError, KeyError, TypeError) as exc:
                # Domain validation errors contain only local, safe messages.
                reason = str(exc) if isinstance(exc, ValueError) else "시세·확정 일봉이 불완전합니다."
                preview.append({"symbol": code, "action": "wait", "reason": reason[:300]})
            except Exception:
                preview.append({"symbol": code, "action": "wait", "reason": "증권사·시세 조회 실패. 주문을 보류합니다."})
        with self.state_lock:
            self.preview = preview
        if self._pending():
            self._info(status="blocked", message="접수·체결 상태를 다음 점검에서 확인합니다. 확인 전에는 추가 주문을 보내지 않습니다.")
        else:
            self._info(status="running" if self.running.is_set() else "stopped", message="점검 완료. " + (preview[0]["reason"] if preview else "대상 종목을 확인해주세요."))
        if execute:
            for item in preview:
                self._event("decision", f"{item['symbol']} {item['action']} · {item['reason']}", item["symbol"])

    def _execute(self, config, account, quote, side, quantity, decision, at):
        # Refresh immediately before reserving and sending; never execute with a
        # cached quote or with the prior daily close as the order price.
        code = quote["symbol"]
        fresh = self.market.quote(code, fresh=True) if config["mode"] == "paper" else self.broker.quote(code)
        tradable_quote(fresh, self.market.provider if config["mode"] == "paper" else self._broker_source(), self.clock())
        price = fresh["price"]
        if price != quote["price"]:
            raise ValueError("점검 중 시세가 바뀌어 이번 주문을 보류합니다.")
        account = self._account(config)
        held = {row["symbol"]: row for row in account["positions"] if row["quantity"]}
        cap = account["equity"] * self.store.settings()["max_position_pct"] / 100
        if side == "buy" and (code in held or self._position_count(account) >= config["max_positions"] or price * quantity * 1.01 > min(config["order_budget"], account["cash"]) or price * quantity > cap * 0.99):
            raise ValueError("계좌·현금 한도가 바뀌어 매수를 보류합니다.")
        if side == "buy" and config["mode"] != "paper":
            power = self.broker.buying_power(code, limit_price=price)
            if quantity > numeric(power.get("quantity"), 0, Decimal("1e15")) or price * quantity * Decimal("1.01") > numeric(power.get("cash"), 0, Decimal("1e15")):
                raise ValueError("증권사 매수 가능 금액·수량이 바뀌어 매수를 보류합니다.")
        if side == "sell" and (code not in held or quantity > held[code]["quantity"]):
            raise ValueError("확인된 보유 수량이 부족해 매도를 보류합니다.")
        tradable_quote(fresh, self.market.provider if config["mode"] == "paper" else self._broker_source(), self.clock())
        if not self.running.is_set():
            return
        signal_date = at.date().isoformat() if side == "sell" else decision["signal_date"]
        intent = self._reserve(config, account, code, side, quantity, price, signal_date, at, decision=decision, quote=fresh)
        self._event("intent", f"{code} {side} {quantity}주 · 지정가 {price:,}원 · 의도 {intent['id'][:8]}", code)
        if config["mode"] == "paper":
            quotes = self.market.quotes(self.store.required_symbols() | {code})
            quotes[code] = fresh
            def guard(db, order):
                if not self.running.is_set():
                    raise ValueError("중지 요청으로 주문하지 않았습니다.")
                if side == "buy":
                    count = db.execute("SELECT count(*) FROM positions").fetchone()[0]
                    if order["held_quantity"] or count >= config["max_positions"]:
                        raise ValueError("수동 주문으로 계좌 보유 한도가 바뀌었습니다.")
                    if order["price"] * quantity + order["fee"] > config["order_budget"]:
                        raise ValueError("수수료를 포함한 자동 주문 예산을 초과합니다.")
                    equity = order["cash"]
                    for row in db.execute("SELECT * FROM positions"):
                        equity += self.store.quote_price(quotes, row["symbol"]) * row["quantity"]
                    baseline = self.store.meta(db, "auto_baseline:" + intent["namespace"] + ":" + at.date().isoformat())
                    if baseline and equity <= float(baseline) * (1 - config["daily_loss_pct"] / 100):
                        raise ValueError("수동 거래 이후 일일 손실 한도가 바뀌어 매수를 중단합니다.")
                elif order["held_quantity"] < quantity:
                    raise ValueError("수동 거래로 보유 수량이 바뀌었습니다.")
            try:
                result = self.store.order({"symbol": code, "side": side, "quantity": quantity,
                                           "idempotency_key": "auto-" + intent["id"], "note": "자동매매 " + POLICY}, quotes, guard=guard)
            except Exception:
                self._status(intent["id"], "rejected")
                raise
            self._settle(intent, {"status": "filled", "filled_quantity": result["quantity"], "average_price": result["price"]})
        else:
            from .kis_broker import OrderRejected
            try:
                if not self.running.is_set():
                    self._status(intent["id"], "rejected")
                    return
                if config["mode"] == "toss-live":
                    receipt = self.broker.submit(code, side, quantity, limit_price=price, client_order_id=intent["id"])
                else:
                    receipt = self.broker.submit(code, side, quantity, limit_price=price)
                before = next((row["quantity"] for row in account["positions"] if row["symbol"] == code), 0)
                self._status(intent["id"], "pending", {**receipt, "symbol": code, "side": side, "quantity": quantity,
                                                    "pre_order_quantity": before, "limit_price": price,
                                                    **({"client_order_id": intent["id"], "attempt_at": at.isoformat()} if config["mode"] == "toss-live" else {})})
                self._event("accepted", f"{code} 지정가 주문 접수. 체결 확인 대기 중입니다.", code)
            except OrderRejected as exc:
                self._status(intent["id"], "rejected")
                self._event("rejected", f"{code} 주문 거절 · {str(exc)[:250]}", code)
            except Exception:
                self._status(intent["id"], "uncertain")
                self.running.clear()
                self._info(next_run=None)
                self._release_locks()
                self._event("uncertain", f"{code} 주문 응답을 확정하지 못했습니다. 자동 재전송하지 않습니다. 증권사 주문내역을 확인해주세요.", code)

    def resolve(self, payload):
        """Attach a user-supplied exact broker identity; read-only verification."""
        with self.lock, self._exclusive():
            if self.running.is_set() or not self.broker:
                raise ValueError("자동매매 중지와 증권사 연결 확인이 필요합니다.")
            with self.store.connection() as db:
                row = db.execute("SELECT * FROM auto_intents WHERE id=? AND status IN ('uncertain','submitting')", (payload.get("intent_id"),)).fetchone()
            if not row or row["namespace"] != self._namespace(row["mode"]):
                raise ValueError("확인할 불확실 주문과 연결 계좌가 맞지 않습니다.")
            intent = dict(row)
            receipt = {**json.loads(row["broker"] or "{}"), "broker_order_id": str(payload.get("broker_order_id", "")), "organization_id": str(payload.get("organization_id", "")),
                       "order_date": str(payload.get("order_date", "")), "symbol": row["symbol"], "side": row["side"], "quantity": row["quantity"],
                       "account_identity": self.broker.account_identity, "environment": self.broker.environment}
            if row["mode"] == "toss-live":
                if receipt.get("order_date") != datetime.fromisoformat(row["created_at"]).astimezone(KST).strftime("%Y%m%d"):
                    raise ValueError("저장된 주문 시도 일자와 확인 일자가 다릅니다.")
                receipt.update(client_order_id=row["id"], limit_price=row["price"], attempt_at=row["created_at"])
            fill = self.broker.fills(receipt)
            self._status(intent["id"], "pending", receipt)
            self._settle(intent, fill)
            return self.state()
