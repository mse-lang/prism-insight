"""Private HTTPS proxy access with expiring, desktop-issued device pairing."""
import hashlib
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit


class MobileAccess:
    def __init__(self, origin=None, clock=time.monotonic):
        self.origin = None
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                raise ValueError("모바일 접속 주소는 HTTPS 주소로 지정해주세요.")
            self.origin = "https://" + parsed.netloc.lower()
        self.clock = clock
        self.lock = threading.Lock()
        self.code = None
        self.sessions = {}

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def state(self):
        with self.lock:
            self._prune()
            return {"enabled": bool(self.origin), "origin": self.origin,
                    "paired_devices": len(self.sessions), "session_hours": 12}

    def _prune(self):
        stamp = self.clock()
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > stamp}
        if self.code and self.code[1] <= stamp:
            self.code = None

    def pair(self):
        if not self.origin:
            raise ValueError("모바일 보안 연결 주소를 먼저 설정해주세요.")
        code = f"{secrets.randbelow(100000000):08d}"
        with self.lock:
            self.code = [self.digest(code), self.clock() + 300, 0]
        return {"code": code, "expires_in": 300, "origin": self.origin}

    def login(self, payload):
        code = payload.get("code")
        with self.lock:
            self._prune()
            if not self.origin or not self.code:
                raise ValueError("PC에서 새 연결 코드를 발급해주세요.")
            self.code[2] += 1
            match = isinstance(code, str) and len(code) == 8 and code.isascii() and code.isdigit()
            match = match and secrets.compare_digest(self.digest(code), self.code[0])
            if not match:
                if self.code[2] >= 8:
                    self.code = None
                raise ValueError("연결 코드가 맞지 않거나 만료됐습니다.")
            self.code = None
            token = secrets.token_urlsafe(32)
            if len(self.sessions) >= 10:
                oldest = min(self.sessions, key=self.sessions.get)
                del self.sessions[oldest]
            self.sessions[self.digest(token)] = self.clock() + 12 * 3600
            return token

    def authenticated(self, header):
        try:
            cookie = SimpleCookie()
            cookie.load(header or "")
            token = cookie["prism_device"].value
        except (KeyError, ValueError, CookieError):
            return False
        with self.lock:
            self._prune()
            return self.digest(token) in self.sessions

    def logout(self, header):
        try:
            cookie = SimpleCookie()
            cookie.load(header or "")
            key = self.digest(cookie["prism_device"].value)
        except (KeyError, ValueError, CookieError):
            return
        with self.lock:
            self.sessions.pop(key, None)

    def revoke(self):
        with self.lock:
            self.code = None
            self.sessions.clear()
        return self.state()

    @staticmethod
    def cookie(token, *, clear=False):
        return ("prism_device=" + token + "; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age="
                + ("0" if clear else "43200"))
