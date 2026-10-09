"use strict";

(() => {
  function initialize() {
    const root = document.getElementById("view-advisor");
    const refreshButton = document.getElementById("advisor-refresh");
    const toggleButton = document.getElementById("advisor-toggle");
    const status = document.getElementById("advisor-status");
    const items = document.getElementById("advisor-items");
    const history = document.getElementById("advisor-history");
    const installButton = document.getElementById("install-app");
    const formatter = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
    const evidenceFormatter = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
    let data = null;
    let csrfToken = "";
    let loading = false;
    let toggling = false;
    let connected = false;
    let timer;
    let installPrompt = null;
    let requestGeneration = 0;
    const openDetails = new Set();
    const evidenceNames = { close: "확정 종가", level: "돌파 기준가", ma20: "20일 이동평균", ma50: "50일 이동평균", ma50_5_sessions_ago: "5거래일 전 50일 이동평균", previous_close: "이전 종가", previous_ma20: "이전 20일 이동평균", support_upper: "지지 범위 상단", available: "확인한 자료", required: "필요한 자료", excluded: "제외한 자료", signal_date: "확정 일봉 날짜", current_price: "현재가", entry_threshold: "진입 기준가", passed: "충족한 조건", total: "전체 조건" };
    const priceEvidence = new Set(["close", "level", "ma20", "ma50", "ma50_5_sessions_ago", "previous_close", "previous_ma20", "support_upper", "current_price", "entry_threshold"]);
    const countEvidence = new Set(["available", "required", "passed", "total", "excluded"]);

    function element(tag, className, text) {
      const result = document.createElement(tag);
      if (className) result.className = className;
      if (text !== undefined) result.textContent = text;
      return result;
    }
    function scalar(value, fallback = "확인 불가") {
      return ["string", "number", "boolean"].includes(typeof value) ? String(value) : fallback;
    }
    function numeric(value) {
      return value !== null && value !== undefined && value !== "" && typeof value !== "boolean" && Number.isFinite(Number(value));
    }
    function won(value) { return numeric(value) ? formatter.format(Number(value)) + "원" : "확인 불가"; }
    function evidence(value, key = "") {
      if (value === null || value === undefined) return "확인 불가";
      if (Array.isArray(value)) return value.map((point, index) => "종가 " + (index + 1) + ": " + (numeric(point) ? evidenceFormatter.format(Number(point)) + "원" : evidence(point))).join("\n");
      if (typeof value === "object") return Object.entries(value).map(([name, point]) => (evidenceNames[name] || name) + ": " + evidence(point, name)).join("\n");
      if (typeof value === "boolean") return value ? "예" : "아니오";
      if (key === "signal_date" || key.endsWith("_date")) return scalar(value);
      if (numeric(value)) return evidenceFormatter.format(Number(value)) + (priceEvidence.has(key) ? "원" : countEvidence.has(key) ? "개" : "");
      return scalar(value);
    }
    function expired(value) { const until = value ? new Date(value).getTime() : NaN; return Number.isFinite(until) && until < Date.now(); }
    function time(value) {
      if (!value) return "확인 불가";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return scalar(value);
      return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
    }
    function visible() { return Boolean(root && !root.classList.contains("hidden") && !root.hidden && getComputedStyle(root).display !== "none"); }
    function loginRequired(error) {
      if (error?.status === 401 || error?.statusCode === 401 || error?.code === "AUTH_REQUIRED") {
        location.replace("/login");
        return true;
      }
      return false;
    }
    async function fetchJSON(path, body) {
      const options = { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } };
      if (body !== undefined) {
        options.method = "POST";
        options.headers["Content-Type"] = "application/json";
        options.headers["X-CSRF-Token"] = csrfToken;
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      if (response.status === 401) {
        const error = new Error("기기 인증이 필요합니다.");
        error.status = 401;
        loginRequired(error);
        throw error;
      }
      let result;
      try { result = await response.json(); }
      catch (_) { throw new Error("서버 응답을 확인할 수 없습니다."); }
      if (!response.ok) {
        const error = new Error(typeof result.error === "string" ? result.error : "요청을 처리하지 못했습니다.");
        error.status = response.status;
        throw error;
      }
      return result;
    }
    async function request(path, body) {
      try {
        if (typeof window.deskApi === "function") return await window.deskApi(path, body);
        if (body !== undefined && !csrfToken) csrfToken = (await fetchJSON("/api/state")).csrf_token || "";
        return await fetchJSON(path, body);
      } catch (error) { loginRequired(error); throw error; }
    }
    function controls() {
      if (refreshButton) refreshButton.disabled = loading || toggling || !navigator.onLine;
      if (toggleButton) {
        toggleButton.disabled = toggling || !connected || !navigator.onLine;
        toggleButton.textContent = toggling ? "변경 중…" : data?.enabled ? "자율 제안 중지" : "자율 제안 시작";
        toggleButton.setAttribute("aria-pressed", String(Boolean(data?.enabled)));
      }
    }
    function schedule() {
      clearTimeout(timer);
      if (visible()) timer = setTimeout(() => load(false), 5000);
    }
    function connectionError(error) {
      connected = false;
      root?.classList.add("advisor-stale");
      if (status) {
        status.dataset.state = "offline";
        status.textContent = "연결 끊김 · 최신 제안과 실행 상태를 확인할 수 없습니다. 다시 조회하세요. 이 화면에서는 주문을 제출하지 않습니다.";
        if (error?.message && navigator.onLine) status.textContent += " " + error.message;
      }
      controls();
    }
    function checkValue(check) {
      return Array.isArray(check) ? { label: check[0], passed: check[1], value: check[2] } : check || {};
    }
    function renderStrategy(strategy, symbol) {
      const details = element("details", "advisor-strategy");
      details.dataset.advisorKey = JSON.stringify([symbol, "strategy", scalar(strategy.id, scalar(strategy.name, ""))]);
      details.open = openDetails.has(details.dataset.advisorKey);
      const summary = element("summary");
      const label = strategy.error ? "계산 불가" : strategy.entry === true ? "확정 일봉 조건 충족" : strategy.entry === false ? "확정 일봉 조건 미충족" : "조건 확인 불가";
      summary.append(element("strong", "", scalar(strategy.name, "매매 방법")), element("span", "advisor-strategy-state", label));
      details.append(summary);
      const body = element("div", "advisor-strategy-body");
      if (strategy.error) {
        body.append(element("p", "advisor-warning", scalar(strategy.error, "자료를 확인하지 못했습니다.")));
      } else {
        const explanation = Array.isArray(strategy.explanation) ? strategy.explanation.map((text) => scalar(text, "")).filter(Boolean).join("\n") : scalar(strategy.explanation, "");
        if (explanation) body.append(element("p", "advisor-explanation", explanation));
        if (typeof strategy.current_entry === "boolean") body.append(element("p", "advisor-current-entry", strategy.current_entry ? "현재 가격 조건도 충족합니다. 실제 주문 여부는 계좌 한도와 실행 상태를 따릅니다." : "현재 가격 조건은 충족하지 않습니다."));
        const checks = element("ul", "advisor-checks");
        (strategy.checks || []).forEach((raw) => {
          const check = checkValue(raw);
          const result = check.passed === true ? "충족" : check.passed === false ? "미충족" : "확인 불가";
          const row = element("li");
          row.append(element("span", "advisor-check-label", scalar(check.label, "관측 조건")), element("span", "advisor-check-result" + (check.passed === true ? " passed" : ""), result));
          if (check.value !== null && check.value !== undefined) row.append(element("small", "advisor-check-value", evidence(check.value)));
          checks.append(row);
        });
        if (checks.childElementCount) body.append(checks);
      }
      const risks = element("ul", "advisor-strategy-risks");
      (strategy.risks || []).forEach((risk) => risks.append(element("li", "", scalar(typeof risk === "object" && risk !== null ? risk.message || risk.reason : risk))));
      if (risks.childElementCount) body.append(element("h4", "", "이 방법에서 확인할 위험"), risks);
      details.append(body);
      return details;
    }
    function stageName(value) {
      const names = { pass: "통과", passed: "통과", ok: "통과", success: "통과", complete: "완료", completed: "완료", fail: "제한", failed: "제한", blocked: "제한", error: "확인 불가", unknown: "확인 불가", unavailable: "확인 불가", missing: "확인 불가", pending: "대기", watch: "관측", skipped: "건너뜀" };
      return names[value] || (typeof value === "string" && /[가-힣]/.test(value) ? value : "확인 필요");
    }
    function renderItem(item) {
      const actionNames = { buy: "매수 조건 관측", watch: "관찰", hold: "보유 관측", sell: "매도 조건 관측", blocked: "조건 제한" };
      const card = element("article", "panel advisor-card");
      const validUntil = item.valid_until || data?.valid_until;
      const previous = expired(validUntil) || data?.status === "stale";
      card.classList.toggle("advisor-expired", previous);
      const heading = element("div", "advisor-card-heading");
      const title = element("div");
      title.append(element("p", "eyebrow small", scalar(item.symbol, "종목")), element("h2", "", scalar(item.name, scalar(item.symbol, "종목 확인 필요"))));
      heading.append(title, element("span", "advisor-action " + (item.action === "blocked" ? "blocked" : ""), actionNames[item.action] || "판단 확인 필요"));
      card.append(heading, element("p", "advisor-summary", scalar(item.summary, "판단 요약이 제공되지 않았습니다.")));
      const source = element("div", "advisor-source");
      source.append(element("span", "", "참고 가격 " + won(item.price)), element("span", "", "확정 일봉 " + scalar(item.signal_date)), element("span", "", "가격 기준 " + time(item.as_of)));
      card.append(source);
      if (validUntil) card.append(element("p", "advisor-validity" + (previous ? " expired" : ""), previous ? "이전 제안 · 유효시간이 지났습니다. 최신 자료로 다시 계산하세요." : "제안 유효시각 " + time(validUntil)));
      const strategies = element("div", "advisor-strategies");
      (item.strategies || []).forEach((strategy) => strategies.append(renderStrategy(strategy, item.symbol)));
      if (strategies.childElementCount) card.append(strategies);
      const trace = element("details", "advisor-trace");
      trace.dataset.advisorKey = JSON.stringify([item.symbol, "trace"]);
      trace.open = openDetails.has(trace.dataset.advisorKey);
      trace.append(element("summary", "", "판단 과정과 계좌 한도 보기"));
      const traceBody = element("div", "advisor-trace-body");
      const stages = element("ol", "advisor-stages");
      (item.stages || []).forEach((stage) => {
        const row = element("li");
        const header = element("div");
        header.append(element("strong", "", scalar(stage.title, "판단 단계")), element("span", "advisor-stage-state", stageName(stage.status)));
        row.append(header, element("p", "", scalar(stage.detail, "상세 근거가 제공되지 않았습니다.")));
        stages.append(row);
      });
      if (stages.childElementCount) traceBody.append(stages);
      const risk = item.risk || {};
      const metrics = element("dl", "advisor-risk-metrics");
      [["한도 내 계산 수량", numeric(risk.quantity) ? formatter.format(Number(risk.quantity)) + "주" : "확인 불가"], ["계산 예산", won(risk.budget)], ["손절 기준가", won(risk.stop_price)], ["손절 기준 계산 손실", won(risk.estimated_loss)]].forEach(([label, value]) => {
        const row = element("div");
        row.append(element("dt", "", label), element("dd", "", value));
        metrics.append(row);
      });
      traceBody.append(metrics, element("p", "advisor-disclaimer", "계산 수량과 손실은 참고 값입니다. 실제 체결가와 손실은 달라질 수 있으며, 제안 생성은 주문을 제출하지 않습니다."));
      const gates = element("ul", "advisor-gate-reasons");
      (risk.gate_reasons || []).forEach((reason) => gates.append(element("li", "", scalar(typeof reason === "object" && reason !== null ? reason.message || reason.reason : reason))));
      if (gates.childElementCount) traceBody.append(element("h4", "", "현재 확인된 제한"), gates);
      trace.append(traceBody);
      card.append(trace);
      return card;
    }
    function render(result) {
      root.querySelectorAll("details[data-advisor-key]").forEach((details) => {
        if (details.open) openDetails.add(details.dataset.advisorKey);
        else openDetails.delete(details.dataset.advisorKey);
      });
      data = result.advisor || result;
      connected = true;
      root.classList.remove("advisor-stale");
      const enabled = Boolean(data.enabled);
      const statusNames = { idle: "관측 대기", stopped: "중지", running: "계산 중", waiting: "다음 관측 대기", calculating: "제안 계산 중", ready: "관측 완료", stale: "이전 제안", error: "자료 확인 필요", offline: "연결 확인 필요" };
      const detail = typeof data.status === "string" && data.status ? " · " + (statusNames[data.status] || (/[가-힣]/.test(data.status) ? data.status : "상태 확인 필요")) : "";
      const previous = expired(data.valid_until) || data.status === "stale";
      const accountNames = { paper: "로컬 모의 계좌", "kis-paper": "한국투자 모의투자", "kis-live": "한국투자 실계좌", "toss-live": "토스 실계좌" };
      const sourceNames = { demo: "합성 데모 시세", naver: "네이버 공개 시세", kis: "한국투자증권 시세", toss: "토스증권 시세" };
      const sourceValue = typeof data.source === "object" && data.source !== null ? data.source.name || data.source.provider : data.source;
      const sourceName = sourceNames[sourceValue] || scalar(sourceValue, "시세 출처 확인 불가");
      status.dataset.state = previous ? "stale" : data.running ? "running" : enabled ? "waiting" : "stopped";
      status.textContent = (data.running ? "자율 제안 계산 중" : enabled ? "자율 제안 켜짐 · 다음 관측 대기" : "자율 제안 중지됨") + detail + " · 실제 주문 없음" + "\n제안 계좌: " + (accountNames[data.account_mode] || scalar(data.account_mode, "확인 불가")) + " · 자료: " + sourceName + (data.last_run ? "\n최근 계산 " + time(data.last_run) : "") + (enabled && data.next_run ? " · 다음 계산 " + time(data.next_run) : "") + (previous ? "\n이전 제안입니다. 유효시간이 지났으므로 최신 자료로 다시 계산하세요." : "") + (typeof data.message === "string" && data.message ? "\n" + data.message : "");
      const cards = document.createDocumentFragment();
      (data.items || []).forEach((item) => cards.append(renderItem(item)));
      if (!(data.items || []).length) cards.append(element("p", "advisor-empty", "아직 계산한 제안이 없습니다. ‘제안 다시 계산’을 눌러 근거와 계좌 한도를 확인하세요. 이 작업은 주문하지 않습니다."));
      items.replaceChildren(cards);
      const records = document.createDocumentFragment();
      (data.history || []).slice(0, 50).forEach((record) => {
        const row = element("article", "advisor-history-record");
        row.append(element("time", "", time(record.created_at)), element("p", "", scalar(record.summary, "계산 기록")));
        records.append(row);
      });
      if (!(data.history || []).length) records.append(element("p", "advisor-empty", "제안 계산 기록이 없습니다."));
      history.replaceChildren(records);
      controls();
    }
    async function load(force = false) {
      if (!root || !items || !history || !status || loading || toggling) { schedule(); return; }
      if (!navigator.onLine) { connectionError(); schedule(); return; }
      loading = true;
      const generation = ++requestGeneration;
      controls();
      try {
        const result = await request(force ? "/api/advisor/refresh" : "/api/advisor", force ? {} : undefined);
        if (generation === requestGeneration) render(result);
      }
      catch (error) { if (!loginRequired(error) && generation === requestGeneration) connectionError(error); }
      finally { loading = false; controls(); schedule(); }
    }
    async function toggle() {
      if (!data || !connected || toggling || !navigator.onLine) return;
      toggling = true;
      ++requestGeneration;
      controls();
      try { render(await request("/api/advisor/config", { enabled: !Boolean(data.enabled) })); }
      catch (error) { if (!loginRequired(error)) connectionError(error); }
      finally { toggling = false; controls(); schedule(); }
    }
    function routeChanged() {
      if (visible()) load(false);
      else clearTimeout(timer);
    }
    function installMessage(text) {
      let notice = document.getElementById("install-app-notice");
      if (!notice && installButton) {
        notice = element("p", "advisor-install-notice");
        notice.id = "install-app-notice";
        notice.setAttribute("role", "status");
        installButton.insertAdjacentElement("afterend", notice);
      }
      if (notice) notice.textContent = text;
    }
    if (installButton) {
      if (matchMedia("(display-mode: standalone)").matches) installButton.classList.add("hidden");
      window.addEventListener("beforeinstallprompt", (event) => {
        event.preventDefault();
        installPrompt = event;
        installButton.classList.remove("hidden");
      });
      window.addEventListener("appinstalled", () => { installPrompt = null; installButton.classList.add("hidden"); installMessage("앱을 설치했습니다."); });
      installButton.addEventListener("click", async () => {
        if (!installPrompt) { installMessage("Android 브라우저 메뉴에서 설치 항목을 선택하세요. 이 앱은 연결된 PC 서버를 사용합니다."); return; }
        const prompt = installPrompt;
        installPrompt = null;
        try { await prompt.prompt(); await prompt.userChoice; }
        catch (_) { installMessage("브라우저 메뉴에서 앱 설치 항목을 확인하세요."); }
      });
    }
    if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => installMessage("설치 준비를 완료하지 못했습니다. 연결된 서버와 HTTPS 주소를 확인하세요."));
    if (!root || !refreshButton || !toggleButton || !items || !history || !status) return;
    items.classList.add("advisor-grid");
    history.classList.add("advisor-history-list");
    status.setAttribute("aria-live", "polite");
    refreshButton.addEventListener("click", () => load(true));
    toggleButton.addEventListener("click", toggle);
    window.addEventListener("hashchange", routeChanged);
    window.addEventListener("online", routeChanged);
    window.addEventListener("offline", () => { connectionError(); schedule(); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) routeChanged(); });
    window.addEventListener("beforeunload", () => clearTimeout(timer));
    controls();
    routeChanged();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
})();
