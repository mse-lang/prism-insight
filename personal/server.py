"""Loopback personal desk; broker automation needs explicit local user activation."""
import argparse
import csv
import io
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .engine import DeskStore
from .market import Market, technical_report, valid_symbol
from .prism_bridge import PrismJobs
from .autotrade import AutoTrader
from .advisor import Advisor
from .mobile import MobileAccess

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/static/app.css": ("app.css", "text/css; charset=utf-8"),
                "/static/app.js": ("app.js", "text/javascript; charset=utf-8")}
STATIC_FILES.update({
    "/static/advisor.js": ("advisor.js", "text/javascript; charset=utf-8"),
    "/static/advisor.css": ("advisor.css", "text/css; charset=utf-8"),
    "/static/desk-mobile.css": ("desk-mobile.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
    "/static/icon-192.png": ("icon-192.png", "image/png"),
    "/static/icon-512.png": ("icon-512.png", "image/png"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/static/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/login": ("login.html", "text/html; charset=utf-8"),
    "/static/login.js": ("login.js", "text/javascript; charset=utf-8"),
})


class Desk:
    def __init__(self, data_path, provider="demo", enable_prism=False):
        self.store = DeskStore(data_path, provider=provider)
        self.market = Market(provider)
        self.csrf = secrets.token_urlsafe(32)
        self.jobs = PrismJobs(self.store, enabled=enable_prism)
        self.provider = provider
        self.autotrade = AutoTrader(self.store, self.market)
        self.advisor = Advisor(self.autotrade)
        self.mobile = MobileAccess()

    def state(self, *, refresh_account=False):
        quotes = self.market.quotes(self.store.required_symbols())
        state = self.store.snapshot(quotes)
        state.update({"csrf_token": self.csrf, "provider": self.provider, "mode": "paper",
                      "watchlist": [quotes[symbol] for symbol in self.store.watchlist()],
                      "upstream": {"revision": "53afbbb", "enabled": self.jobs.enabled,
                                   "reason": self.jobs.reason}})
        warnings = list(state.get("warnings", []))
        if self.provider == "demo":
            warnings.append("데모 시세와 차트는 합성 예시입니다. 실제 시장 가격이 아닙니다.")
        else:
            warnings.append("NAVER 공개 시세의 기준 시각으로 모의 체결합니다. 장 마감 후에는 마지막 시세가 사용됩니다.")
        if any(q.get("status") != "ok" for q in quotes.values()):
            warnings.append("일부 시세를 확인하지 못했습니다. 해당 종목 주문은 차단됩니다.")
        state["warnings"] = list(dict.fromkeys(warnings))
        state["dashboard_account"] = self.autotrade.dashboard_snapshot(refresh=refresh_account)
        state["autotrade"] = self.autotrade.state()
        state["mobile"] = self.mobile.state()
        return state


class DeskHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def make_server(desk, port=8866, *, mobile=False):
    if mobile and not desk.mobile.origin:
        raise ValueError("모바일 HTTPS 주소를 지정해주세요.")
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def discard_rejected_body(self):
            # Closing a socket with unread request bytes can reset the response
            # on Windows. Drain only a small declared body, with a bounded wait.
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 65536 and not self.headers.get("Transfer-Encoding"):
                    self.connection.settimeout(2)
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            finally:
                self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            # No URLs, request bodies or credentials in access logs.
            pass

        def send_headers(self, code, content_type, length):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Service-Worker-Allowed", "/")
            if getattr(self, "response_cookie", None):
                self.send_header("Set-Cookie", self.response_cookie)
                self.response_cookie = None
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()

        def send_json(self, value, code=200):
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_headers(code, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)

        def allowed_host(self):
            actual_port = self.server.server_port
            hosts = [f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"]
            if mobile:
                hosts.append(urlsplit(desk.mobile.origin).netloc)
            return self.headers.get("Host") in hosts

        def mobile_authorized(self):
            return not mobile or desk.mobile.authenticated(self.headers.get("Cookie"))

        def do_GET(self):
            if not self.allowed_host():
                self.send_json({"error": "로컬 주소로 접속해주세요."}, 403)
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if mobile and not self.mobile_authorized() and url.path not in STATIC_FILES:
                    self.send_json({"error": "기기 연결이 필요합니다.", "code": "AUTH_REQUIRED"}, 401)
                    return
                if url.path in STATIC_FILES:
                    file, mime = STATIC_FILES[url.path]
                    if mobile and not self.mobile_authorized() and url.path in ("/", "/index.html"):
                        file = "login.html"
                    data = (STATIC / file).read_bytes()
                    self.send_headers(200, mime, len(data))
                    self.wfile.write(data)
                elif url.path == "/api/health":
                    self.send_json({"ok": True, "mode": "paper", "provider": desk.provider})
                elif url.path == "/api/state":
                    self.send_json(desk.state())
                elif url.path == "/api/stock":
                    self.send_json(desk.market.quote(query.get("symbol", [""])[0], history=True))
                elif url.path == "/api/search":
                    self.send_json({"results": desk.market.search(query.get("q", [""])[0])})
                elif url.path == "/api/reports":
                    self.send_json({"reports": desk.store.reports()})
                elif url.path == "/api/jobs":
                    self.send_json({"job": desk.jobs.get(query.get("id", [""])[0])})
                elif url.path == "/api/autotrade":
                    self.send_json(desk.autotrade.state())
                elif url.path == "/api/advisor":
                    self.send_json(desk.advisor.state())
                elif url.path == "/api/mobile":
                    self.send_json(desk.mobile.state())
                elif url.path == "/api/export.csv":
                    orders = desk.store.snapshot(desk.market.quotes(desk.store.required_symbols()))["orders"]
                    output = io.StringIO(newline="")
                    writer = csv.writer(output)
                    columns = ["created_at", "symbol", "name", "side", "quantity", "price", "fee", "total", "realized_pnl", "note"]
                    writer.writerow(columns)
                    for order in orders:
                        row = []
                        for key in columns:
                            value = order.get(key, "")
                            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                                value = "'" + value
                            row.append(value)
                        writer.writerow(row)
                    data = output.getvalue().encode("utf-8-sig")
                    self.send_headers(200, "text/csv; charset=utf-8", len(data))
                    self.wfile.write(data)
                else:
                    self.send_json({"error": "찾을 수 없는 주소입니다."}, 404)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception:
                self.send_json({"error": "요청을 처리하지 못했습니다. 시세 연결 상태를 확인해주세요."}, 503)

        def do_POST(self):
            origin = self.headers.get("Origin")
            path = urlsplit(self.path).path
            origins = (desk.mobile.origin,) if mobile else (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            if mobile and path == "/api/mobile/login":
                if not self.allowed_host() or origin != desk.mobile.origin:
                    self.discard_rejected_body()
                    self.send_json({"error": "모바일 HTTPS 주소로 연결해주세요."}, 403)
                    self.close_connection = True
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("연결 코드 형식을 확인해주세요.")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or set(payload) != {"code"}:
                        raise ValueError("연결 코드 형식을 확인해주세요.")
                    token = desk.mobile.login(payload)
                    self.response_cookie = desk.mobile.cookie(token)
                    self.send_json({"ok": True})
                except (ValueError, UnicodeError):
                    self.send_json({"error": "연결 코드가 맞지 않거나 만료됐습니다. PC에서 새 코드를 발급해주세요."}, 400)
                    self.close_connection = True
                return
            if mobile and not self.mobile_authorized():
                self.discard_rejected_body()
                self.send_json({"error": "기기 연결이 필요합니다.", "code": "AUTH_REQUIRED"}, 401)
                self.close_connection = True
                return
            if mobile and (origin != desk.mobile.origin or path in ("/api/mobile/pair", "/api/mobile/revoke", "/api/autotrade/connect", "/api/autotrade/toss/accounts", "/api/autotrade/disconnect")):
                self.discard_rejected_body()
                self.send_json({"error": "증권사 인증정보와 기기 연결은 PC에서 관리해주세요."}, 403)
                self.close_connection = True
                return
            if (not self.allowed_host() or (origin and origin not in origins)
                    or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), desk.csrf)):
                self.discard_rejected_body()
                self.send_json({"error": "페이지를 새로고침한 뒤 다시 시도해주세요."}, 403)
                self.close_connection = True
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.discard_rejected_body()
                self.send_json({"error": "JSON 요청만 지원합니다."}, 415)
                self.close_connection = True
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("요청 크기가 올바르지 않습니다.")
                def invalid_constant(_):
                    raise ValueError("유효한 숫자를 입력해주세요.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=invalid_constant)
                if not isinstance(payload, dict):
                    raise ValueError("요청 형식이 올바르지 않습니다.")
                path = urlsplit(self.path).path
                if path == "/api/orders":
                    symbol = valid_symbol(payload.get("symbol"))
                    quotes = desk.market.quotes(set(desk.store.required_symbols()) | {symbol})
                    try:
                        quotes[symbol] = desk.market.quote(symbol, fresh=True)
                    except Exception:
                        raise ValueError("최신 시세를 확인하지 못해 주문하지 않았습니다.") from None
                    self.send_json({"order": desk.store.order(payload, quotes)})
                elif path == "/api/watchlist":
                    symbol = valid_symbol(payload.get("symbol"))
                    if payload.get("action") == "add":
                        desk.market.quote(symbol)
                    desk.store.change_watchlist(symbol, payload.get("action"))
                    self.send_json({"ok": True})
                elif path == "/api/settings":
                    self.send_json({"settings": desk.store.update_settings(payload)})
                elif path == "/api/journal":
                    self.send_json({"entry": desk.store.add_journal(payload)})
                elif path == "/api/analyze":
                    quote = desk.market.quote(valid_symbol(payload.get("symbol")), history=True)
                    self.send_json({"report": desk.store.save_report(technical_report(quote))})
                elif path == "/api/prism-analysis":
                    quote = desk.market.quote(valid_symbol(payload.get("symbol")))
                    self.send_json({"job": desk.jobs.start(quote["symbol"], quote["name"])}, 202)
                elif path == "/api/dashboard/refresh":
                    if payload:
                        raise ValueError("계좌 새로고침에는 추가 입력이 필요하지 않습니다.")
                    self.send_json(desk.state(refresh_account=True))
                elif path == "/api/advisor/refresh":
                    self.send_json(desk.advisor.request(payload), 202)
                elif path == "/api/advisor/config":
                    self.send_json(desk.advisor.configure(payload))
                elif path == "/api/mobile/pair":
                    if payload:
                        raise ValueError("기기 연결에는 추가 입력이 필요하지 않습니다.")
                    self.send_json(desk.mobile.pair())
                elif path == "/api/mobile/revoke":
                    if payload:
                        raise ValueError("기기 해제에는 추가 입력이 필요하지 않습니다.")
                    self.send_json(desk.mobile.revoke())
                elif path == "/api/mobile/logout":
                    desk.mobile.logout(self.headers.get("Cookie"))
                    self.response_cookie = desk.mobile.cookie("", clear=True)
                    self.send_json({"ok": True})
                elif path == "/api/autotrade/config":
                    self.send_json(desk.autotrade.update_config(payload))
                elif path == "/api/autotrade/connect":
                    self.send_json(desk.autotrade.connect(payload))
                elif path == "/api/autotrade/toss/accounts":
                    self.send_json(desk.autotrade.toss_accounts(payload))
                elif path == "/api/autotrade/disconnect":
                    self.send_json(desk.autotrade.disconnect())
                elif path == "/api/autotrade/start":
                    self.send_json(desk.autotrade.start(payload))
                elif path == "/api/autotrade/stop":
                    self.send_json(desk.autotrade.stop())
                elif path == "/api/autotrade/check":
                    self.send_json(desk.autotrade.check())
                elif path == "/api/autotrade/reconcile":
                    self.send_json(desk.autotrade.resolve(payload))
                else:
                    self.send_json({"error": "찾을 수 없는 주소입니다."}, 404)
            except (ValueError, UnicodeError) as exc:
                self.send_json({"error": str(exc) if isinstance(exc, ValueError) else "UTF-8 형식으로 보내주세요."}, 400)
                self.close_connection = True
            except Exception:
                self.send_json({"error": "처리하지 못했습니다. 거래 기록을 확인한 뒤 다시 시도해주세요."}, 503)

    return DeskHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="PRISM 개인 한국 주식 분석·모의매매·자동매매 데스크")
    parser.add_argument("--provider", choices=("naver", "demo"), default="naver")
    parser.add_argument("--port", type=int, default=8866)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "runtime" / "personal")
    parser.add_argument("--enable-prism", action="store_true", help="원본 AI 분석을 요청할 수 있게 허용합니다. 별도 API 설정이 필요합니다.")
    parser.add_argument("--mobile-origin", help="비공개 HTTPS 프록시 주소. 예: https://my-pc.example.ts.net")
    parser.add_argument("--mobile-port", type=int, default=8868)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("포트는 1024~65535 범위로 지정해주세요.")
    if args.mobile_origin and (not 1024 <= args.mobile_port <= 65535 or args.mobile_port == args.port):
        parser.error("모바일 포트는 PC 포트와 다른 1024~65535 포트로 지정해주세요.")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    desk = Desk(args.data_dir / f"{args.provider}.sqlite", args.provider, args.enable_prism)
    desk.mobile = MobileAccess(args.mobile_origin)
    server = make_server(desk, args.port)
    mobile_server = make_server(desk, args.mobile_port, mobile=True) if args.mobile_origin else None
    if mobile_server:
        threading.Thread(target=mobile_server.serve_forever, daemon=True, name="personal-mobile").start()
    print(f"PRISM MY DESK · {args.provider} · 자동매매 기본 중지\nhttp://127.0.0.1:{args.port}\n종료: Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if mobile_server:
            mobile_server.shutdown()
            mobile_server.server_close()
        desk.advisor.shutdown()
        desk.jobs.shutdown()
        desk.autotrade.shutdown()


if __name__ == "__main__":
    main()
