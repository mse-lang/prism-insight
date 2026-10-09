"""Local manual paper ledger. AGPL-3.0 personal extension, 2026-10-09."""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path

KST = timezone(timedelta(hours=9))
DEFAULTS = {"display_name": "나의 투자 데스크", "max_position_pct": 30, "fee_bps": 1.5}
SEEDS = ("005930", "000660", "035420", "035720", "005380", "051910")


def now():
    return datetime.now(KST).isoformat(timespec="seconds")


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("종목코드는 숫자 6자리로 입력해주세요.")
    return value


def numeric(value, lower, upper):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or not lower <= result <= upper:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise ValueError("설정값의 숫자와 허용 범위를 확인해주세요.") from None


class DeskStore:
    def __init__(self, path, provider="demo"):
        if provider not in ("demo", "naver"):
            raise ValueError("지원하지 않는 시세 공급자입니다.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, ordinal INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity>0), cost_basis INTEGER NOT NULL CHECK(cost_basis>=0));
                CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT UNIQUE NOT NULL, signature TEXT NOT NULL, symbol TEXT NOT NULL,
                    name TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                    price INTEGER NOT NULL, gross INTEGER NOT NULL, fee INTEGER NOT NULL,
                    total INTEGER NOT NULL, realized_pnl INTEGER NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL);
            ''')
            db.execute("BEGIN IMMEDIATE")
            existing = self.meta(db, "provider")
            if existing and existing != provider:
                raise ValueError("다른 시세 모드의 계좌입니다. 별도 데이터 폴더를 선택해주세요.")
            if not existing:
                for key, value in {"provider": provider, "cash": "10000000", "initial_cash": "10000000",
                                   "settings": json.dumps(DEFAULTS, ensure_ascii=False)}.items():
                    self.set_meta(db, key, value)
                db.executemany("INSERT OR IGNORE INTO watchlist VALUES (?, ?)", enumerate_seed())
            db.commit()

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def meta(db, key):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def set_meta(db, key, value):
        db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def settings(self):
        with self.connection() as db:
            return json.loads(self.meta(db, "settings"))

    def update_settings(self, payload):
        if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
            raise ValueError("지원하지 않는 설정 항목입니다.")
        with self.connection(write=True) as db:
            settings = json.loads(self.meta(db, "settings"))
            settings.update(payload)
            name = settings["display_name"]
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
                raise ValueError("데스크 이름은 1~60자로 입력해주세요.")
            settings["display_name"] = name.strip()
            settings["max_position_pct"] = float(numeric(settings["max_position_pct"], 1, 100))
            settings["fee_bps"] = float(numeric(settings["fee_bps"], 0, 100))
            self.set_meta(db, "settings", json.dumps(settings, ensure_ascii=False, allow_nan=False))
            return settings

    def watchlist(self):
        with self.connection() as db:
            return [row[0] for row in db.execute("SELECT symbol FROM watchlist ORDER BY ordinal,symbol")]

    def required_symbols(self):
        with self.connection() as db:
            return {row[0] for row in db.execute("SELECT symbol FROM watchlist UNION SELECT symbol FROM positions")}

    def change_watchlist(self, code, action):
        code = symbol(code)
        if action not in ("add", "remove"):
            raise ValueError("관심종목 추가 또는 삭제를 선택해주세요.")
        with self.connection(write=True) as db:
            if action == "remove":
                db.execute("DELETE FROM watchlist WHERE symbol=?", (code,))
            else:
                if db.execute("SELECT count(*) FROM watchlist").fetchone()[0] >= 50:
                    raise ValueError("관심종목은 최대 50개까지 등록할 수 있습니다.")
                ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM watchlist").fetchone()[0]
                db.execute("INSERT OR IGNORE INTO watchlist VALUES (?,?)", (code, ordinal))

    def quote_price(self, quotes, code):
        quote = quotes.get(code, {})
        price = quote.get("price")
        if (quote.get("status") != "ok" or quote.get("source") != self.provider
                or quote.get("symbol") != code or type(price) is not int or not 0 < price <= 10**15):
            raise ValueError("확인된 해당 모드의 시세가 없어 주문할 수 없습니다.")
        return price

    def snapshot(self, quotes):
        with self.connection() as db:
            # Hold one SQLite read snapshot across cash, holdings and the ledger.
            db.execute("BEGIN")
            cash = int(self.meta(db, "cash"))
            initial = int(self.meta(db, "initial_cash"))
            settings = json.loads(self.meta(db, "settings"))
            rows = db.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
            positions, warnings, known_value, unknown = [], [], 0, False
            for row in rows:
                position = dict(row)
                position["average_cost"] = row["cost_basis"] / row["quantity"]
                try:
                    price = self.quote_price(quotes, row["symbol"])
                    value = price * row["quantity"]
                    pnl = value - row["cost_basis"]
                    known_value += value
                except ValueError:
                    price = value = pnl = None
                    unknown = True
                    warnings.append(f"{row['name']} 시세가 없어 총 평가자산을 확인할 수 없습니다.")
                position.update(price=price, market_value=value, unrealized_pnl=pnl)
                positions.append(position)
            equity = None if unknown else cash + known_value
            for position in positions:
                position["weight_pct"] = None if equity is None or equity == 0 or position["market_value"] is None else position["market_value"] / equity * 100
            orders = [public_order(row) for row in db.execute("SELECT * FROM orders ORDER BY id DESC")]
            journal = [dict(row) for row in db.execute("SELECT * FROM journal ORDER BY id DESC")]
            realized = db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM orders").fetchone()[0]
            unrealized = None if unknown else sum(p["unrealized_pnl"] for p in positions)
            return {"initial_cash": initial, "cash": cash, "equity": equity,
                    "realized_pnl": realized, "unrealized_pnl": unrealized,
                    "positions": positions, "orders": orders, "journal": journal,
                    "settings": settings, "warnings": warnings}

    def order(self, payload, quotes, *, guard=None):
        code = symbol(payload.get("symbol"))
        side, quantity, key = payload.get("side"), payload.get("quantity"), payload.get("idempotency_key")
        note = payload.get("note", "")
        if side not in ("buy", "sell") or type(quantity) is not int or not 1 <= quantity <= 1_000_000:
            raise ValueError("매수·매도 종류와 1 이상의 정수 수량을 확인해주세요.")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
            raise ValueError("주문 식별자가 올바르지 않습니다. 주문을 다시 검토해주세요.")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("주문 메모는 최대 1000자로 입력해주세요.")
        signature = hashlib.sha256(json.dumps([code, side, quantity], ensure_ascii=False).encode()).hexdigest()
        with self.connection(write=True) as db:
            existing = db.execute("SELECT * FROM orders WHERE request_key=?", (key,)).fetchone()
            if existing:
                if existing["signature"] != signature:
                    raise ValueError("같은 주문 식별자로 내용을 바꿀 수 없습니다. 새 주문을 검토해주세요.")
                return public_order(existing)
            price = self.quote_price(quotes, code)
            held = db.execute("SELECT * FROM positions WHERE symbol=?", (code,)).fetchone()
            held_qty = held["quantity"] if held else 0
            basis = held["cost_basis"] if held else 0
            settings = json.loads(self.meta(db, "settings"))
            cash = int(self.meta(db, "cash"))
            gross = price * quantity
            fee = int((Decimal(gross) * Decimal(str(settings["fee_bps"])) / 10000).to_integral_value(rounding=ROUND_CEILING))
            realized = 0
            name = str(quotes[code].get("name") or code)[:80]
            if guard is not None:
                # Automation checks share the ledger's write transaction so a
                # concurrent manual order cannot bypass its account constraints.
                guard(db, {"symbol": code, "side": side, "quantity": quantity,
                           "price": price, "fee": fee, "cash": cash,
                           "held_quantity": held_qty, "cost_basis": basis})
            if side == "buy":
                total = gross + fee
                if total > cash:
                    raise ValueError("수수료를 포함한 주문금액이 현금 잔고를 초과합니다.")
                equity = cash
                for row in db.execute("SELECT * FROM positions"):
                    try:
                        equity += self.quote_price(quotes, row["symbol"]) * row["quantity"]
                    except ValueError:
                        raise ValueError("보유 종목의 시세를 모두 확인한 뒤 모의 매수를 진행해주세요.") from None
                cap = Decimal(equity - fee) * Decimal(str(settings["max_position_pct"])) / 100
                if Decimal(price * (held_qty + quantity)) > cap:
                    raise ValueError("매수 후 종목당 최대 자산 비중을 초과합니다. 수량이나 설정을 확인해주세요.")
                db.execute("INSERT INTO positions VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,quantity=excluded.quantity,cost_basis=excluded.cost_basis",
                           (code, name, held_qty + quantity, basis + total))
                cash -= total
            else:
                if quantity > held_qty:
                    raise ValueError("보유 수량보다 많이 매도할 수 없습니다.")
                allocated = basis if quantity == held_qty else int((Decimal(basis) * quantity / held_qty).to_integral_value(rounding=ROUND_HALF_UP))
                total = gross - fee
                realized = total - allocated
                cash += total
                if quantity == held_qty:
                    db.execute("DELETE FROM positions WHERE symbol=?", (code,))
                else:
                    db.execute("UPDATE positions SET quantity=?,cost_basis=? WHERE symbol=?", (held_qty - quantity, basis - allocated, code))
            self.set_meta(db, "cash", cash)
            cursor = db.execute("INSERT INTO orders(request_key,signature,symbol,name,side,quantity,price,gross,fee,total,realized_pnl,created_at,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (key, signature, code, name, side, quantity, price, gross, fee, total, realized, now(), note))
            return public_order(db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone())

    def add_journal(self, payload):
        code, text = payload.get("symbol"), payload.get("text")
        code = symbol(code) if code else None
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 5000:
            raise ValueError("일지는 1~5000자로 입력해주세요.")
        with self.connection(write=True) as db:
            cursor = db.execute("INSERT INTO journal(symbol,text,created_at) VALUES (?,?,?)", (code, text.strip(), now()))
            return dict(db.execute("SELECT * FROM journal WHERE id=?", (cursor.lastrowid,)).fetchone())

    def save_report(self, report):
        result = dict(report)
        symbol(result.get("symbol"))
        body = result.get("body")
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 500000:
            raise ValueError("보고서 본문이 비어 있거나 최대 크기를 초과했습니다.")
        result.setdefault("created_at", now())
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
        with self.connection(write=True) as db:
            cursor = db.execute("INSERT INTO reports(payload) VALUES (?)", (payload,))
            result["id"] = cursor.lastrowid
            return result

    def reports(self):
        with self.connection() as db:
            return [{**json.loads(row["payload"]), "id": row["id"]} for row in db.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 100")]


def enumerate_seed():
    return [(code, index) for index, code in enumerate(SEEDS)]


def public_order(row):
    result = dict(row)
    result.pop("request_key", None)
    result.pop("signature", None)
    return result
