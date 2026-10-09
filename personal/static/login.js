"use strict";

(() => {
  const form = document.getElementById("mobile-login-form");
  const input = document.getElementById("mobile-login-code");
  const button = document.getElementById("mobile-login-button");
  const message = document.getElementById("mobile-login-message");
  if (!form || !input || !button || !message) return;
  let busy = false;
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  const secure = location.protocol === "https:" || loopback;
  function controls() {
    button.disabled = busy || !secure || !navigator.onLine;
    button.textContent = busy ? "기기 연결 확인 중…" : "투자 데스크에 연결";
  }
  function connectionMessage() {
    if (!secure) message.textContent = "PC에서 안내한 HTTPS 주소로 접속하세요. 이 주소에서는 연결 코드를 보내지 않습니다.";
    else if (!navigator.onLine) message.textContent = "네트워크 연결이 끊겼습니다. PC 서버와 연결을 확인한 후 다시 시도하세요.";
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !secure || !navigator.onLine) { connectionMessage(); return; }
    const code = input.value.trim();
    if (!/^\d{8}$/.test(code)) { message.textContent = "PC에서 발급한 8자리 숫자 코드를 입력하세요."; input.focus(); return; }
    busy = true;
    message.textContent = "";
    controls();
    try {
      const response = await fetch("/api/mobile/login", { method: "POST", cache: "no-store", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify({ code }) });
      if (response.ok) {
        input.value = "";
        location.replace("/");
        return;
      }
      message.textContent = response.status === 429 ? "연결 확인 요청이 많습니다. 잠시 후 다시 시도하세요." : response.status === 403 ? "이 연결에서 기기 인증을 완료할 수 없습니다. PC에서 안내한 주소와 설정을 확인하세요." : "연결 코드를 확인하세요. 만료되었거나 이미 사용한 코드라면 PC에서 새 코드를 발급하세요.";
    } catch (_) { message.textContent = "PC 서버에 연결할 수 없습니다. 서버와 네트워크 연결을 확인하세요."; }
    finally { busy = false; controls(); }
  });
  window.addEventListener("offline", () => { connectionMessage(); controls(); });
  window.addEventListener("online", () => { if (secure) message.textContent = ""; controls(); });
  controls();
  connectionMessage();
})();
