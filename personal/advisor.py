"""Observable rule-based suggestions. No broker submit, fills or automation start."""
import copy
import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal

from .engine import KST
from .strategies import catalog, evaluate
from .autotrade import signal, tradable_quote


class Advisor:
    def __init__(self, auto, *, clock=None, interval=60):
        self.auto, self.store, self.market = auto, auto.store, auto.market
        self.clock = clock or (lambda: datetime.now(KST))
        self.interval = interval
        self.lock = threading.Lock()
        self.wake, self.closed = threading.Event(), threading.Event()
        self.enabled = False
        self.refresh_requested = False
        self.thread = None
        self.info = {"running": False, "status": "idle", "last_run": None,
                     "next_run": None, "items": [], "history": [], "message": ""}
        self.info["message"] = "제안을 계산하거나 자율 제안을 시작하세요. 주문은 보내지 않습니다."
        with self.store.connection(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS advisor_runs (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, result TEXT NOT NULL)")
        with self.store.connection() as db:
            last = db.execute("SELECT * FROM advisor_runs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            try:
                self.info.update(json.loads(last["result"]))
                self.info.update(running=False, next_run=None, status="stale", message="이전 계산입니다. 최신 제안을 다시 계산하세요.")
            except (ValueError, TypeError):
                pass

    def state(self):
        with self.lock:
            value = copy.deepcopy(self.info)
            value["enabled"] = self.enabled
        with self.store.connection() as db:
            rows = db.execute("SELECT created_at,result FROM advisor_runs ORDER BY id DESC LIMIT 20").fetchall()
        value["history"] = [{"created_at": row["created_at"], "summary": json.loads(row["result"])["summary"]} for row in rows]
        value["strategies"] = catalog()
        return value

    def request(self, payload=None):
        if payload:
            raise ValueError("제안 계산에는 추가 입력이 필요하지 않습니다.")
        with self.lock:
            self.refresh_requested = True
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True, name="personal-advisor")
                self.thread.start()
        self.wake.set()
        return self.state()

    def configure(self, payload):
        if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
            raise ValueError("자율 제안 실행 여부를 확인해주세요.")
        with self.lock:
            self.enabled = payload["enabled"]
            if not self.enabled:
                self.info["next_run"] = None
        if payload["enabled"]:
            return self.request()
        return self.state()

    def _loop(self):
        while not self.closed.is_set():
            self.wake.clear()
            with self.lock:
                compute = self.refresh_requested or self.enabled
                self.refresh_requested = False
            if compute:
                with self.lock:
                    self.info.update(running=True, status="calculating", message="시세와 조건을 확인하고 있습니다. 주문은 보내지 않습니다.")
                try:
                    result = self.calculate()
                    with self.store.connection(write=True) as db:
                        db.execute("INSERT INTO advisor_runs(created_at,result) VALUES (?,?)", (result["last_run"], json.dumps(result, ensure_ascii=False, allow_nan=False)))
                        db.execute("DELETE FROM advisor_runs WHERE id NOT IN (SELECT id FROM advisor_runs ORDER BY id DESC LIMIT 100)")
                    with self.lock:
                        self.info.update(result, running=False, status="ready")
                except Exception:
                    with self.lock:
                        self.info.update(running=False, status="error", items=[], message="자료를 확인하지 못했습니다. 다시 계산해주세요.")
            with self.lock:
                self.info["next_run"] = (self.clock() + timedelta(seconds=self.interval)).isoformat() if self.enabled else None
            self.wake.wait(self.interval)

    def calculate(self):
        at, config = self.clock(), self.auto.config()
        if config["mode"] == "paper":
            account = self.store.snapshot(self.market.quotes(self.store.required_symbols()))
            account_ready = account.get("equity") is not None
        else:
            account = self.auto.dashboard_snapshot(refresh=True)
            account_ready = account.get("status") == "ok"
        with self.auto.state_lock:
            # Freeze a stopped/running observation; suggestions never change it.
            trading_running = self.auto.running.is_set()
        owned = self.auto._owned(self.auto._namespace(config["mode"]))
        codes = list(dict.fromkeys(config["symbols"] + list(owned)))
        items = []
        for code in codes:
            if self.closed.is_set():
                break
            try:
                quote = self.market.quote(code, fresh=True, history=True)
                if quote.get("status") != "ok" or quote.get("source") != self.market.provider or type(quote.get("price")) is not int or not 0 < quote["price"] <= 10**15:
                    raise ValueError("새로운 공개 시세를 확인하지 못했습니다.")
                strategies = []
                for strategy in catalog():
                    try:
                        result = evaluate(quote["history"], at, strategy["id"], synthetic=self.market.provider == "demo", current_price=quote["price"])
                        strategies.append({"id": strategy["id"], **result})
                    except ValueError as exc:
                        strategies.append({"id": strategy["id"], "name": strategy["name"], "error": str(exc)})
                selected = next(row for row in strategies if row["id"] == config.get("strategy", "breakout20"))
                held = next((row for row in account.get("positions", []) if row["symbol"] == code and row["quantity"] > 0), None)
                own = owned.get(code)
                reasons = []
                buy_reasons = []
                if not account_ready:
                    reasons.append("계좌 현금과 자산을 확인하지 못했습니다.")
                if self.auto._pending():
                    reasons.append("미체결·불확실 주문을 확인해야 합니다.")
                try:
                    tradable_quote(quote, self.market.provider, at)
                except ValueError as exc:
                    reasons.append(str(exc))
                if config["mode"] != "paper":
                    reasons.append("공개 시세 제안입니다. 실행에는 증권사의 최신 시세와 매수 가능 수량 검사가 필요합니다.")
                quantity = 0
                stop = quote["price"] * (1 - config["stop_loss_pct"] / 100)
                if account_ready:
                    cash, equity = account["cash"], account["equity"]
                    cap = equity * self.store.settings()["max_position_pct"] / 100
                    with self.store.connection() as db:
                        namespace, day = self.auto._namespace(config["mode"]), at.date().isoformat()
                        rows = db.execute("SELECT * FROM auto_intents WHERE namespace=? AND substr(created_at,1,10)=?", (namespace, day)).fetchall()
                        baseline = self.store.meta(db, "auto_baseline:" + namespace + ":" + day)
                    used = sum(row["planned_amount"] for row in rows if row["side"] == "buy" and (row["status"] != "rejected" or row["filled_quantity"] > 0))
                    budget = max(0, min(config["order_budget"], cash, config["max_daily_buy"] - used))
                    quantity = min(int(Decimal(str(budget)) / (Decimal(quote["price"]) * Decimal("1.01"))), int(cap * .99 / quote["price"]))
                    if sum(row["side"] == "buy" for row in rows) >= config["max_daily_orders"]:
                        buy_reasons.append("일일 매수 주문 수 한도에 도달했습니다.")
                    if baseline is None:
                        buy_reasons.append("일일 손실 기준선은 자동매매 점검에서 확인해야 합니다.")
                    elif equity <= float(baseline) * (1 - config["daily_loss_pct"] / 100):
                        buy_reasons.append("일일 손실 한도에 도달했습니다.")
                    count = account.get("total_position_count")
                    if count is None:
                        count = sum(row["quantity"] > 0 for row in account["positions"])
                    if not held and count >= config["max_positions"]:
                        buy_reasons.append("계좌 보유 종목 수 한도에 도달했습니다.")
                    if quantity < 1:
                        buy_reasons.append("현금·일일 예산·비중 한도 내에서 1주를 살 수 없습니다.")
                candidate = bool(selected.get("current_entry"))
                action, summary = ("buy", "선택 전략의 진입 조건을 충족했습니다.") if candidate else ("watch", "선택 전략의 진입 조건을 기다립니다.")
                if selected.get("error"):
                    buy_reasons.append(selected["error"])
                    action, summary = "blocked", "완료 일봉 자료가 부족해 판단을 보류합니다."
                if held:
                    action, summary, quantity = "hold", "현재 보유 종목입니다. 신규 중복 매수를 제안하지 않습니다.", 0
                    if own:
                        stop = own["cost_basis"] / own["quantity"] * (1 - config["stop_loss_pct"] / 100)
                        try:
                            exit_signal = signal(quote["history"], at, synthetic=self.market.provider == "demo")["exit"]
                        except ValueError:
                            exit_signal = False
                        if quote["price"] <= stop or exit_signal:
                            action, summary, quantity = "sell", "자동 소유분의 기존 손절 또는 MA20 이탈 조건을 확인했습니다.", own["quantity"]
                    else:
                        summary = "수동 보유분은 자동 청산 대상이 아닙니다. 보유 판단을 직접 검토하세요."
                if not account_ready:
                    action, quantity = "blocked", 0
                if action in ("buy", "watch"):
                    reasons.extend(buy_reasons)
                if action in ("buy", "sell") and reasons:
                    action, quantity = "blocked", 0
                    summary += " 현재 실행 조건은 미충족이므로 보류합니다."
                if action == "blocked":
                    quantity = 0
                data_ready = not selected.get("error") or bool(own and quote["price"] <= stop)
                item = {"symbol": code, "name": quote.get("name", code), "action": action,
                        "summary": summary, "price": quote["price"], "as_of": quote.get("as_of"),
                        "signal_date": selected.get("signal_date"), "strategies": strategies,
                        "selected_strategy": config.get("strategy", "breakout20"),
                        "risk": {"quantity": quantity, "budget": quote["price"] * quantity,
                                 "stop_price": stop, "estimated_loss": max(0, quote["price"] - stop) * quantity,
                                 "gate_reasons": reasons, "assumption": "비용·갭·슬리피지·미체결은 포함하지 않은 계획값입니다."},
                        "stages": [{"title": "자료 확인", "status": "ok" if data_ready else "blocked", "detail": "현재 시세로 자동 소유분의 손절 조건을 확인합니다. 신규 진입 자료 결측은 손절 검토를 막지 않습니다." if own and selected.get("error") and data_ready else "공개 일봉의 오늘 봉을 제외하고 완료된 종가만 계산합니다. " + (selected.get("error") or "기준일 " + selected.get("signal_date", ""))},
                                   {"title": "전략 비교", "status": "ok", "detail": "돌파·눌림 반등·평균선 회복·조건 합의를 동일 시각의 자료로 비교합니다."},
                                   {"title": "위험 점검", "status": "blocked" if reasons else "ok", "detail": " / ".join(reasons) if reasons else "관측한 현금·비중·보유 종목·일일 한도 안에서 계획 수량을 계산했습니다."},
                                   {"title": "최종 제안", "status": "blocked" if reasons else "ok", "detail": summary + " 제안 계산으로 주문을 보내지 않습니다."}]}
                items.append(item)
            except Exception:
                items.append({"symbol": code, "name": code, "action": "blocked", "summary": "시세 또는 완료 일봉을 확인하지 못해 제안을 보류합니다.", "strategies": [], "stages": [], "risk": {"gate_reasons": ["자료 연결을 다시 확인해주세요."]}})
        if self.auto.config() != config:
            raise ValueError("계좌나 전략 설정이 변경되어 제안을 다시 계산해야 합니다.")
        return {"last_run": at.isoformat(), "valid_until": (at + timedelta(minutes=5)).isoformat(), "items": items, "summary": f"{len(items)}종목 비교 · 매수 조건 {sum(row['action'] == 'buy' for row in items)} · 주문 없음",
                "message": "규칙에 근거한 제안입니다. 수익성이나 성공 확률을 의미하지 않습니다.",
                "trading_running": trading_running, "account_mode": config["mode"], "source": self.market.provider}

    def shutdown(self):
        self.closed.set()
        self.wake.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=15)
