"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const wonFormat = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
  const decimalFormat = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
  const palette = ["#8c77c9", "#aba0d8", "#75969d", "#a3b7a7", "#d5c2a6", "#bd92ac"];
  let state = null;
  let selectedSymbol = "";
  let selectedQuote = null;
  let side = "buy";
  let pendingOrder = null;
  let quoteRequest = 0;
  let searchRequest = 0;
  let toastTimer;
  let jobTimer;
  let activeJob = null;
  let firstState = true;
  let autotrade = null;
  let autoConfigInitialized = false;
  let autoConfigDirty = false;
  let autoPollTimer;
  let autoFetching = false;
  let autoOperating = false;
  let autoStopping = false;
  let liveReviewKey = "";
  let globalMessage = "";
  let globalSourceWarning = false;
  const autoModeNames = { paper: "로컬 모의매매", "kis-paper": "한국투자 모의투자", "kis-live": "한국투자 실계좌 · 실제 주문", "toss-live": "토스 실계좌 · 실제 주문" };
  let tossAccounts = [];
  let tossCredentialsVersion = 0;
  let tossAccountsVersion = -1;
  const autoFields = { interval_seconds: "auto-interval", order_budget: "auto-budget", max_daily_buy: "auto-daily-buy", max_daily_orders: "auto-max-orders", max_positions: "auto-max-positions", stop_loss_pct: "auto-stop-loss", daily_loss_pct: "auto-daily-loss" };

  function node(tag, className, value) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined && value !== null) result.textContent = String(value);
    return result;
  }
  function svgNode(tag, attrs) {
    const result = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs || {}).forEach(([key, value]) => result.setAttribute(key, String(value)));
    return result;
  }
  function hasNumber(value) { return value !== null && value !== undefined && value !== "" && typeof value !== "boolean" && Number.isFinite(Number(value)); }
  function number(value) { return hasNumber(value) ? Number(value) : 0; }
  function won(value) { return hasNumber(value) ? wonFormat.format(Number(value)) + "원" : "확인 불가"; }
  function percent(value, signed = true) {
    if (!hasNumber(value)) return "확인 불가";
    const amount = number(value);
    return (signed && amount > 0 ? "+" : "") + amount.toFixed(2) + "%";
  }
  function signedWon(value) { return hasNumber(value) ? (number(value) > 0 ? "+" : "") + won(value) : "확인 불가"; }
  function tone(value) { return number(value) > 0 ? "up" : number(value) < 0 ? "down" : "neutral"; }
  function dateTime(value, compact = false) {
    if (!value) return "기준 시각 없음";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 24);
    return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", ...(compact ? {} : { year: "numeric" }) }).format(parsed);
  }
  function showToast(message) {
    clearTimeout(toastTimer);
    $("toast").textContent = message;
    $("toast").classList.remove("hidden");
    toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 4500);
  }
  function alertMessage(message, sourceWarning = false) {
    globalMessage = message;
    globalSourceWarning = sourceWarning;
    renderGlobalAlert();
  }
  function renderGlobalAlert() {
    const brokerView = currentView() === "autotrade" && autotrade && autotrade.mode !== "paper";
    const message = brokerView && globalSourceWarning ? "" : globalMessage;
    $("global-alert").textContent = message;
    $("global-alert").classList.toggle("hidden", !message);
  }
  async function api(path, payload) {
    const options = { cache: "no-store", credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (payload !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRF-Token"] = state?.csrf_token || "";
      options.body = JSON.stringify(payload);
    }
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw new Error("앱에 연결할 수 없습니다. 연결을 확인한 뒤 다시 시도하세요."); }
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error("서버 응답을 읽을 수 없습니다. 앱의 실행 상태를 확인하세요."); }
    if (!response.ok) throw new Error(body.error || "요청을 처리하지 못했습니다.");
    return body;
  }
  async function busy(button, action) {
    button.disabled = true;
    try { return await action(); }
    finally { button.disabled = false; }
  }
  function currentView() {
    const requested = location.hash.replace("#", "");
    return ["dashboard", "portfolio", "journal", "autotrade", "settings"].includes(requested) ? requested : "dashboard";
  }
  function renderView() {
    const view = currentView();
    const labels = { dashboard: "대시보드", portfolio: "포트폴리오", journal: "거래일지", autotrade: "자동매매", settings: "설정" };
    document.querySelectorAll(".view").forEach((element) => element.classList.toggle("hidden", element.id !== "view-" + view));
    document.querySelectorAll(".nav-link").forEach((element) => {
      element.classList.toggle("active", element.dataset.view === view);
      if (element.dataset.view === view) element.setAttribute("aria-current", "page");
      else element.removeAttribute("aria-current");
    });
    $("view-label").textContent = labels[view];
    document.title = "PRISM · " + labels[view];
    if (state) {
      renderDashboardSummary();
      if (["dashboard", "portfolio"].includes(view)) refreshState(true);
    }
    renderModeBadge();
    if (view === "autotrade" && state) refreshAutotrade();
    else scheduleAutoPoll();
  }

  async function refreshState(quiet = false) {
    try {
      state = !quiet && state && showingConnectedAccount() ? await api("/api/dashboard/refresh", {}) : await api("/api/state");
      renderState();
      if (!selectedSymbol) selectedSymbol = state.watchlist?.[0]?.symbol || currentDashboardAccount().positions?.[0]?.symbol || "";
      // Account snapshots contain lightweight quotes without daily bars.
      // Fetch the selected quote rather than replacing its chart with that list.
      if (selectedSymbol) await selectStock(selectedSymbol);
      else { selectedQuote = null; renderSelected(); }
      alertMessage((state.warnings || []).map((warning) => typeof warning === "string" ? warning : warning.message || "시세 연결을 확인하세요.").join("\n"), true);
      if (!quiet) showToast("시세와 선택한 계좌를 새로 불러왔습니다.");
    } catch (error) {
      if (state && showingConnectedAccount()) {
        state.dashboard_account = { ...currentDashboardAccount(), status: "unavailable", cash: null, equity: null, positions: [],
          unrealized_pnl: null, realized_pnl: null, message: "계좌 조회를 완료하지 못했습니다. 다시 조회하세요." };
        renderDashboardSummary(); renderPositions(); renderModeBadge();
      }
      alertMessage(error.message);
    }
  }
  function renderState() {
    const displayName = state.settings?.display_name || "나의 투자 데스크";
    $("welcome").textContent = displayName;
    $("sidebar-name").textContent = displayName;
    $("sidebar-avatar").textContent = displayName.charAt(0).toUpperCase();
    renderDashboardSummary();
    const demo = state.provider === "demo";
    $("source-tag").textContent = demo ? "합성 데모 시세" : "네이버 공개 시세";
    $("source-description").textContent = demo ? "학습용 합성 데이터입니다. 실제 시장 가격과 다릅니다." : "공개 시세는 지연·누락될 수 있습니다. 종목별 기준 시각을 확인하세요.";
    $("last-updated").textContent = "조회 " + dateTime(new Date().toISOString(), true);
    $("settings-provider").textContent = demo ? "합성 데모 시세 연결" : "네이버 공개 시세 연결";
    $("provider-help").textContent = demo ? "외부 연결 없이 합성 가격으로 분석과 모의 주문을 연습할 수 있습니다. 데모 기록은 공개 시세 계좌와 별도로 저장됩니다." : "기본 연결입니다. 한국 주식의 공개 가격과 일별 종가를 가져옵니다. 실시간 거래소 시세는 아닙니다.";
    $("settings-initial").textContent = won(state.initial_cash);
    $("upstream-section").classList.toggle("hidden", !state.upstream?.enabled);
    if (firstState) {
      populateSettings();
      firstState = false;
    }
    renderWatchlist();
    renderPositions();
    renderOrders();
    renderJournal();
    updateOrder();
    if (state.autotrade) setAutotrade(state.autotrade);
  }

  let accountView = null;
  let accountViewTouched = false;
  function chooseDefaultAccountView() {
    if (!accountViewTouched) accountView = state?.autotrade?.mode && state.autotrade.mode !== "paper" ? "connected" : "paper";
    if (!accountView) accountView = "paper";
  }
  function showingConnectedAccount() { chooseDefaultAccountView(); return accountView === "connected"; }
  function currentDashboardAccount() {
    if (!state) return { status: "unavailable", cash: null, equity: null, positions: [] };
    if (!showingConnectedAccount()) return state;
    return state.dashboard_account || { kind: "broker", mode: state.autotrade?.mode, provider: state.autotrade?.connection?.provider,
      status: "unavailable", account_masked: state.autotrade?.connection?.account_masked || "", cash: null, equity: null,
      positions: [], unrealized_pnl: null, realized_pnl: null, initial_cash: null,
      message: "연결 계좌를 조회하지 못했습니다. 설정한 계좌를 확인하고 다시 조회하세요." };
  }
  function connectedAccountName(account) {
    if (!account.mode || account.mode === "paper") return "연결 계좌";
    return (account.provider === "toss" ? "토스증권" : "한국투자증권") + (account.mode === "kis-paper" ? " 모의투자" : " 실계좌");
  }
  function dashboardMoney(account, field, pnl = false) {
    if (showingConnectedAccount() && account.status !== "ok") return "조회 불가";
    if (!hasNumber(account[field])) return showingConnectedAccount() && pnl ? "증권사 미제공" : "확인 불가";
    return pnl ? signedWon(account[field]) : won(account[field]);
  }
  function renderDashboardSummary() {
    const connected = showingConnectedAccount(), account = currentDashboardAccount();
    const unavailable = connected && account.status !== "ok";
    const toss = connected && (account.provider === "toss" || account.equity_basis === "krw-trading-capital");
    $("stat-equity-label").textContent = toss ? "원화 거래 기준 자산" : "총 평가자산";
    $("stat-cash-label").textContent = toss ? "원화 현금 매수 가능액" : connected ? "계좌 현금 잔고" : "주문 가능 금액";
    $("stat-cash-help").textContent = connected ? "증권사 계좌의 최근 조회 결과" : "모의 계좌의 현금 잔고";
    $("stat-realized-help").textContent = connected ? "미제공 손익은 계산하지 않습니다" : "거래 수수료 반영";
    $("stat-equity").textContent = dashboardMoney(account, "equity");
    $("stat-cash").textContent = dashboardMoney(account, "cash");
    $("stat-unrealized").textContent = dashboardMoney(account, "unrealized_pnl", true);
    $("stat-realized").textContent = dashboardMoney(account, "realized_pnl", true);
    $("stat-unrealized").className = unavailable || !hasNumber(account.unrealized_pnl) ? "neutral" : tone(account.unrealized_pnl);
    $("stat-realized").className = unavailable || !hasNumber(account.realized_pnl) ? "neutral" : tone(account.realized_pnl);
    if (connected) {
      $("stat-return").replaceChildren(document.createTextNode(unavailable ? "계좌 조회 실패 · 다시 조회하세요" : "증권사 조회 기준 " + dateTime(account.as_of || account.fetched_at, true)));
      $("stat-position-count").textContent = unavailable ? "보유 종목 조회 불가" : "계좌 보유 " + (hasNumber(account.total_position_count) ? wonFormat.format(number(account.total_position_count)) : (account.positions || []).length) + "종목";
    } else {
      const change = hasNumber(state.equity) && number(state.initial_cash) > 0 ? (number(state.equity) / number(state.initial_cash) - 1) * 100 : null;
      $("stat-return").replaceChildren(node("span", tone(change), percent(change)), document.createTextNode("초기 모의자금 대비"));
      $("stat-position-count").textContent = "보유 종목 " + (state.positions || []).length + "개";
    }
    $("stats-grid").setAttribute("aria-label", connected ? connectedAccountName(account) + " 현황" : "모의 계좌 현황");
    $("account-view-select").value = accountView;
    $("account-display-name").textContent = connected ? connectedAccountName(account) + (account.account_masked ? " · " + account.account_masked : "") : "로컬 모의매매 계좌";
    $("account-display-help").textContent = unavailable ? account.message || "계좌 조회 불가. 다시 조회하세요." : toss
      ? "원화 현금 매수 가능액 + 국내 주식 평가액 기준입니다. 전체 계좌 순자산과 구분됩니다."
      : connected ? "연결 계좌의 읽기 전용 잔고입니다." : "로컬 모의 잔고와 모의 거래 기록입니다.";
    if (!unavailable && connected && number(account.excluded_positions_count) > 0) $("account-display-help").textContent += " 다른 시장 또는 관리 대상 외 보유 " + account.excluded_positions_count + "종목은 이 보유 표에서 제외됩니다.";
    if (!unavailable && connected && number(account.open_order_count) > 0) $("account-display-help").textContent += " 미체결·조건 주문 " + account.open_order_count + "건은 증권사에서 확인하세요.";
    $("account-paper-button").classList.toggle("hidden", !connected);
    const mainAccountView = ["dashboard", "portfolio"].includes(currentView());
    $("account-display-toolbar").classList.toggle("hidden", !mainAccountView);
    document.querySelector(".sidebar-note strong").textContent = mainAccountView && connected ? connectedAccountName(account) : "모의매매 계좌";
    document.querySelector(".dashboard-grid").classList.toggle("broker-view", connected);
    document.querySelector(".order-panel").classList.toggle("hidden", connected);
    document.querySelector(".recent-panel").classList.toggle("hidden", connected);
    $("orders-table").closest(".panel").classList.toggle("hidden", connected);
    document.querySelectorAll(".export-link").forEach(el => el.classList.toggle("hidden", connected));
    document.querySelector(".holdings-panel .mini-badge").textContent = connected ? connectedAccountName(account) : "모의매매";
  }
  function changeAccountView(value) {
    accountView = value; accountViewTouched = true;
    renderDashboardSummary(); renderPositions(); renderModeBadge();
  }

  function populateSettings() {
    $("settings-name").value = state.settings?.display_name || "나의 투자 데스크";
    $("settings-limit").value = state.settings?.max_position_pct ?? 30;
    $("settings-fee").value = state.settings?.fee_bps ?? 1.5;
  }
  function renderWatchlist() {
    const list = state.watchlist || [];
    $("watch-count").textContent = list.length + "종목";
    const fragment = document.createDocumentFragment();
    list.forEach((quote, index) => {
      const row = node("div", "watch-item" + (quote.symbol === selectedSymbol ? " active" : ""));
      const select = node("button", "watch-select");
      select.type = "button";
      select.setAttribute("aria-label", quote.name + " " + quote.symbol + " 종목 보기");
      select.setAttribute("aria-pressed", String(quote.symbol === selectedSymbol));
      const avatar = node("span", "stock-avatar", quote.name?.slice(0, 2) || "KR");
      avatar.style.color = palette[index % palette.length];
      const name = node("span", "watch-name");
      name.append(node("strong", "", quote.name), node("span", "", quote.symbol));
      const price = node("span", "watch-price");
      price.append(node("strong", "", number(quote.price) > 0 ? wonFormat.format(number(quote.price)) : "확인 불가"), node("span", tone(quote.change_pct), percent(quote.change_pct)));
      select.append(avatar, name, price);
      select.addEventListener("click", () => selectStock(quote.symbol));
      const remove = node("button", "watch-remove", "×");
      remove.type = "button";
      remove.title = "관심종목 삭제";
      remove.setAttribute("aria-label", quote.name + " 관심종목 삭제");
      remove.addEventListener("click", async () => {
        await busy(remove, async () => {
          try {
            await api("/api/watchlist", { symbol: quote.symbol, action: "remove" });
            await refreshState(true);
            showToast(quote.name + "을(를) 관심종목에서 삭제했습니다.");
          } catch (error) { showToast(error.message); }
        });
      });
      row.append(select, remove);
      fragment.append(row);
    });
    if (!list.length) fragment.append(node("p", "empty-text", "관심종목을 추가하고 시장을 살펴보세요."));
    $("watchlist").replaceChildren(fragment);
  }
  async function selectStock(symbol) {
    const request = ++quoteRequest;
    selectedSymbol = symbol;
    selectedQuote = null;
    $("order-button").disabled = true;
    $("analyze-button").disabled = true;
    $("report-area").classList.add("hidden");
    renderWatchlist();
    $("selected-name").textContent = "종목 불러오는 중…";
    $("selected-symbol").textContent = symbol + " · KRW";
    $("quote-source").textContent = "조회 중";
    $("selected-price").textContent = "—";
    $("selected-change").textContent = "—";
    $("selected-change").className = "neutral";
    clearQuoteFacts("조회 중");
    $("chart-area").replaceChildren(node("p", "empty-text", "일별 종가를 불러오고 있습니다."));
    try {
      const response = await api("/api/stock?symbol=" + encodeURIComponent(symbol));
      if (request !== quoteRequest) return;
      selectedQuote = response.quote || response;
      renderSelected();
    } catch (error) {
      if (request !== quoteRequest) return;
      $("selected-name").textContent = "시세를 불러오지 못했습니다";
      $("selected-price").textContent = "확인 불가";
      $("selected-change").textContent = "확인 불가";
      $("quote-source").textContent = "시세 확인 불가";
      clearQuoteFacts("확인 불가");
      $("chart-area").replaceChildren(node("p", "empty-text", error.message));
      updateOrder();
      showToast(error.message);
    }
  }
  function validHistory(quote) {
    return (quote?.history || []).filter((point) => Number.isFinite(Number(point.close)) && Number(point.close) > 0).slice(-60);
  }
  function clearQuoteFacts(label) {
    ["quote-previous", "quote-volume", "quote-time"].forEach((id) => $(id).textContent = label);
    const metrics = document.createDocumentFragment();
    ["20일 이동평균", "20일 수익률", "기간 고점 대비"].forEach((name) => {
      const metric = node("div");
      metric.append(node("span", "", name), node("strong", "neutral", label));
      metrics.append(metric);
    });
    $("metrics-grid").replaceChildren(metrics);
  }
  function renderSelected() {
    const quote = selectedQuote;
    if (!quote) {
      $("selected-name").textContent = "관심종목을 선택하세요";
      $("selected-symbol").textContent = "KOREA EQUITIES";
      $("selected-price").textContent = "—";
      $("selected-change").textContent = "—";
      $("quote-source").textContent = "종목 선택";
      clearQuoteFacts("—");
      $("chart-area").replaceChildren(node("p", "empty-text", "관심종목을 추가하면 가격과 관측 지표를 확인할 수 있습니다."));
      $("analyze-button").disabled = true;
      updateOrder();
      return;
    }
    $("selected-name").textContent = quote.name;
    $("selected-symbol").textContent = quote.symbol + " · KRW";
    const demo = state.provider === "demo" || String(quote.source).toLowerCase().includes("demo");
    $("quote-source").textContent = quote.status === "unavailable" || number(quote.price) <= 0 ? "시세 확인 불가" : demo ? "합성 데모" : quote.status === "stale" ? "지연 / 캐시 시세" : "공개 시세";
    $("selected-price").textContent = number(quote.price) > 0 ? won(quote.price) : "확인 불가";
    $("selected-change").textContent = percent(quote.change_pct);
    $("selected-change").className = tone(quote.change_pct);
    $("quote-previous").textContent = number(quote.previous_close) > 0 ? won(quote.previous_close) : "확인 불가";
    $("quote-volume").textContent = hasNumber(quote.volume) ? wonFormat.format(number(quote.volume)) + "주" : "확인 불가";
    $("quote-time").textContent = dateTime(quote.as_of, true);
    drawChart(quote);
    renderMetrics(quote);
    $("analyze-button").disabled = false;
    $("prism-analysis-button").disabled = Boolean(activeJob);
    renderWatchlist();
    updateOrder();
  }
  function drawChart(quote) {
    const history = validHistory(quote);
    if (history.length < 2) {
      $("chart-area").replaceChildren(node("p", "empty-text", "차트에 필요한 일별 종가가 부족합니다."));
      return;
    }
    const width = 520, height = 180, left = 5, right = 57, top = 13, bottom = 29;
    const prices = history.map((point) => number(point.close));
    const low = Math.min(...prices), high = Math.max(...prices);
    const padding = Math.max((high - low) * .2, high * .012, 1);
    const minimum = low - padding, maximum = high + padding;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const x = (index) => left + index / (history.length - 1) * plotWidth;
    const y = (price) => top + (maximum - price) / (maximum - minimum) * plotHeight;
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": quote.name + " " + history.length + "일 일별 종가. " + won(prices[0]) + "에서 " + won(prices[prices.length - 1]) });
    const title = svgNode("title");
    title.textContent = quote.name + " 일별 종가 차트";
    svg.append(title);
    const defs = svgNode("defs"), gradient = svgNode("linearGradient", { id: "chart-fill", x1: 0, y1: 0, x2: 0, y2: 1 });
    gradient.append(svgNode("stop", { offset: "0%", "stop-color": "#ae99dd", "stop-opacity": ".23" }), svgNode("stop", { offset: "100%", "stop-color": "#ae99dd", "stop-opacity": ".01" }));
    defs.append(gradient);
    svg.append(defs);
    for (let line = 0; line < 4; line++) {
      const price = maximum - (maximum - minimum) * line / 3;
      const position = y(price);
      svg.append(svgNode("line", { x1: left, x2: width - right, y1: position, y2: position, class: "chart-grid" }));
      const text = svgNode("text", { x: width - right + 10, y: position + 3, class: "chart-text" });
      text.textContent = wonFormat.format(price);
      svg.append(text);
    }
    const path = history.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(2)},${y(point.close).toFixed(2)}`).join(" ");
    svg.append(svgNode("path", { d: path + ` L${x(history.length - 1)},${height - bottom} L${left},${height - bottom} Z`, fill: "url(#chart-fill)" }));
    svg.append(svgNode("path", { d: path, fill: "none", stroke: "#9a81d0", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    svg.append(svgNode("circle", { cx: x(history.length - 1), cy: y(prices[prices.length - 1]), r: 3.5, fill: "#967bcb", stroke: "white", "stroke-width": 2 }));
    [0, Math.round((history.length - 1) / 3), Math.round((history.length - 1) * 2 / 3), history.length - 1].forEach((index, position) => {
      const label = svgNode("text", { x: x(index), y: height - 4, class: "chart-text", "text-anchor": position === 0 ? "start" : position === 3 ? "end" : "middle" });
      label.textContent = String(history[index].date).slice(5, 10).replace("-", ".");
      svg.append(label);
    });
    $("chart-area").replaceChildren(svg);
  }
  function renderMetrics(quote) {
    const prices = validHistory(quote).map((point) => number(point.close));
    const recent = prices.slice(-20);
    const average = recent.length === 20 ? recent.reduce((sum, price) => sum + price, 0) / 20 : null;
    const change = prices.length >= 21 ? (prices.at(-1) / prices.at(-21) - 1) * 100 : null;
    const fromHigh = prices.length && number(quote.price) > 0 ? (number(quote.price) / Math.max(...prices) - 1) * 100 : null;
    const fragment = document.createDocumentFragment();
    [["20일 이동평균", average === null ? "자료 부족" : won(average), ""], ["20일 수익률", change === null ? "자료 부족" : percent(change), change === null ? "" : tone(change)], ["기간 고점 대비", fromHigh === null ? "자료 부족" : percent(fromHigh), fromHigh === null ? "" : tone(fromHigh)]].forEach(([label, value, className]) => {
      const metric = node("div");
      metric.append(node("span", "", label), node("strong", className, value));
      fragment.append(metric);
    });
    $("metrics-grid").replaceChildren(fragment);
  }

  function updateOrder() {
    const price = number(selectedQuote?.price);
    const quantity = number($("order-quantity").value);
    const fee = Math.round(price * quantity * number(state?.settings?.fee_bps) / 10000);
    const total = price * quantity + (side === "buy" ? fee : -fee);
    const position = state?.positions?.find((item) => item.symbol === selectedSymbol);
    $("held-quantity").textContent = "보유 " + wonFormat.format(number(position?.quantity)) + "주";
    $("order-price").textContent = price > 0 ? won(price) : "확인 불가";
    $("order-fee").textContent = price > 0 ? won(fee) : "확인 불가";
    $("order-total").textContent = price > 0 && quantity > 0 ? won(total) : "확인 불가";
    $("order-total-label").textContent = side === "buy" ? "예상 주문금액" : "예상 수령금액";
    $("order-button").replaceChildren(document.createTextNode(side === "buy" ? "모의 매수 검토" : "모의 매도 검토"), node("span", "", "→"));
    $("order-button").disabled = !state || !selectedQuote || price <= 0;
    $("order-help").textContent = selectedQuote && price <= 0 ? "시세를 확인할 수 없어 모의 주문이 제한됩니다.\n시세 연결을 확인하고 새로고침하세요." : selectedQuote?.status === "stale" ? "시세가 지연되었거나 캐시된 가격입니다.\n주문 가능 여부는 서버에서 확인합니다." : "현재 시세로 체결되는 모의 거래입니다.\n실제 주식이나 현금은 사용하지 않습니다.";
    document.querySelectorAll("[data-side]").forEach((button) => {
      button.classList.toggle("selected", button.dataset.side === side);
      button.setAttribute("aria-pressed", String(button.dataset.side === side));
    });
  }
  function reviewOrder(event) {
    event.preventDefault();
    if (!selectedQuote || !state) return;
    const quantity = Number($("order-quantity").value);
    if (!Number.isSafeInteger(quantity) || quantity <= 0) { showToast("주문 수량은 1주 이상의 정수로 입력하세요."); return; }
    const input = { symbol: selectedSymbol, side, quantity, note: $("order-note").value.trim() };
    const fingerprint = JSON.stringify(input);
    if (!pendingOrder || pendingOrder.fingerprint !== fingerprint || !pendingOrder.attempted) {
      pendingOrder = { ...input, idempotency_key: crypto.randomUUID(), fingerprint, attempted: false };
    }
    const fee = Math.round(number(selectedQuote.price) * quantity * number(state.settings?.fee_bps) / 10000);
    $("confirm-stock").textContent = selectedQuote.name + " · " + selectedSymbol;
    $("confirm-side").textContent = (side === "buy" ? "매수" : "매도") + " " + wonFormat.format(quantity) + "주";
    const details = document.createDocumentFragment();
    [["현재 기준가", won(selectedQuote.price)], ["예상 수수료", won(fee)], [side === "buy" ? "예상 주문금액" : "예상 수령금액", won(number(selectedQuote.price) * quantity + (side === "buy" ? fee : -fee))], ["주문 메모", input.note || "없음"]].forEach(([label, value]) => {
      const row = node("div");
      row.append(node("span", "", label), node("strong", "", value));
      details.append(row);
    });
    $("confirmation-details").replaceChildren(details);
    $("confirm-error").classList.add("hidden");
    $("confirm-order-button").textContent = pendingOrder.attempted ? "같은 모의 주문 다시 확인" : "모의 주문 확정";
    $("order-dialog").showModal();
  }
  async function confirmOrder() {
    if (!pendingOrder) return;
    await busy($("confirm-order-button"), async () => {
      pendingOrder.attempted = true;
      try {
        const { fingerprint, attempted, ...payload } = pendingOrder;
        await api("/api/orders", payload);
        pendingOrder = null;
        $("order-dialog").close();
        $("order-note").value = "";
        await refreshState(true);
        showToast("모의 주문이 체결되었습니다. 거래내역에서 확인하세요.");
      } catch (error) {
        $("confirm-error").textContent = error.message + " 다시 확인하면 동일 주문 번호를 사용해 중복 체결을 방지합니다.";
        $("confirm-error").classList.remove("hidden");
        $("confirm-order-button").textContent = "같은 모의 주문 다시 확인";
      }
    });
  }

  function stockCell(stock) {
    const cell = node("td");
    const button = node("button", "table-stock-button");
    button.type = "button";
    button.append(node("strong", "", stock.name), node("span", "symbol", stock.symbol));
    button.addEventListener("click", () => { location.hash = "dashboard"; selectStock(stock.symbol); });
    cell.append(button);
    return cell;
  }
  function renderPositions() {
    const account = currentDashboardAccount(), connected = showingConnectedAccount();
    const unavailable = connected && account.status !== "ok";
    const positions = unavailable ? [] : (account.positions || []);
    const emptyText = unavailable ? "연결 계좌 보유 종목을 조회하지 못했습니다. 다시 조회하세요." : connected ? "조회된 국내 보유 종목이 없습니다." : "아직 보유 종목이 없습니다. 첫 모의 주문으로 포트폴리오를 시작하세요.";
    const rows = document.createDocumentFragment();
    const minis = document.createDocumentFragment();
    positions.forEach((position) => {
      const row = node("tr");
      row.append(stockCell(position), node("td", "number", wonFormat.format(number(position.quantity)) + "주"), node("td", "number", won(position.average_cost)), node("td", "number", won(position.price)), node("td", "number", won(position.market_value)), node("td", "number " + tone(position.unrealized_pnl), (hasNumber(position.unrealized_pnl) ? signedWon(position.unrealized_pnl) : "미제공")), node("td", "number", percent(position.weight_pct, false)));
      rows.append(row);
      const item = node("div", "mini-position");
      const label = node("div");
      label.append(node("strong", "", position.name), node("span", "", wonFormat.format(number(position.quantity)) + "주 · " + percent(position.weight_pct, false)));
      const values = node("div");
      values.append(node("strong", "", won(position.market_value)), node("span", "position-pnl " + tone(position.unrealized_pnl), (hasNumber(position.unrealized_pnl) ? signedWon(position.unrealized_pnl) : "미제공")));
      item.append(label, values);
      minis.append(item);
    });
    if (!positions.length) {
      const empty = node("tr");
      const cell = node("td", "empty-cell", emptyText);
      cell.colSpan = 7;
      empty.append(cell);
      rows.append(empty);
      minis.append(node("p", "empty-text", emptyText));
    }
    $("positions-table").replaceChildren(rows);
    $("mini-positions").replaceChildren(minis);
    drawAllocation(positions, account);
  }
  function drawAllocation(positions, account = currentDashboardAccount()) {
    if (!hasNumber(account.equity) || !hasNumber(account.cash) || positions.some((position) => !hasNumber(position.market_value))) {
      $("allocation-chart").replaceChildren(node("p", "empty-text", "자산 구성 확인 불가\n보유 종목의 시세를 확인할 수 없습니다."));
      $("allocation-legend").replaceChildren(node("p", "field-help", "확인되지 않은 평가금액과 자산 비중을 0으로 계산하지 않습니다."));
      return;
    }
    const equity = number(account.equity);
    const items = positions.map((position, index) => ({ name: position.name, value: number(position.market_value), color: palette[index % palette.length] }));
    items.push({ name: showingConnectedAccount() ? "현금 매수 가능액" : "현금", value: number(account.cash), color: "#e7e1ef" });
    const svg = svgNode("svg", { viewBox: "0 0 160 160", role: "img", "aria-label": (showingConnectedAccount() ? "연결 계좌 자산 구성. " : "모의 계좌 자산 구성. ") + items.map((item) => item.name + " " + percent(equity ? item.value / equity * 100 : 0, false)).join(", ") });
    svg.append(svgNode("circle", { cx: 80, cy: 80, r: 61, fill: "none", stroke: "#f2eef6", "stroke-width": 14 }));
    const circumference = 2 * Math.PI * 61;
    let offset = 0;
    items.forEach((item) => {
      const share = equity > 0 ? Math.max(0, item.value / equity) : 0;
      if (share > 0) {
        svg.append(svgNode("circle", { cx: 80, cy: 80, r: 61, fill: "none", stroke: item.color, "stroke-width": 14, "stroke-dasharray": `${Math.max(0, share * circumference - (items.length > 1 ? 2 : 0))} ${circumference}`, "stroke-dashoffset": -offset, transform: "rotate(-90 80 80)" }));
        offset += share * circumference;
      }
    });
    const title = svgNode("text", { x: 80, y: 73, class: "allocation-center", "font-size": 10 });
    title.textContent = showingConnectedAccount() ? (account.equity_basis === "krw-trading-capital" ? "원화 거래 기준 자산" : "연결 계좌 자산") : "모의 총 자산";
    const total = svgNode("text", { x: 80, y: 94, class: "allocation-center", "font-size": 14, "font-weight": 600 });
    total.textContent = equity >= 10000 ? decimalFormat.format(equity / 10000) + "만원" : won(equity);
    svg.append(title, total);
    $("allocation-chart").replaceChildren(svg);
    const legend = document.createDocumentFragment();
    items.forEach((item) => {
      const row = node("div", "legend-row");
      const label = node("span");
      const dot = node("i", "legend-dot");
      dot.style.backgroundColor = item.color;
      label.append(dot, document.createTextNode(item.name));
      row.append(label, node("span", "", percent(equity ? item.value / equity * 100 : 0, false)));
      legend.append(row);
    });
    $("allocation-legend").replaceChildren(legend);
  }
  function renderOrders() {
    const orders = state.orders || [];
    $("recent-count").textContent = orders.length ? orders.length + "건의 모의 거래" : "모의 거래 기록";
    [[$("recent-orders"), orders.slice(0, 5), false], [$("orders-table"), orders, true]].forEach(([target, records, full]) => {
      const fragment = document.createDocumentFragment();
      records.forEach((order) => {
        const row = node("tr");
        const sideCell = node("td");
        sideCell.append(node("span", "side-badge" + (order.side === "sell" ? " sell" : ""), order.side === "sell" ? "매도" : "매수"));
        row.append(stockCell(order), sideCell, node("td", "number", wonFormat.format(number(order.quantity)) + "주"), node("td", "number", won(order.price)));
        if (full) row.append(node("td", "number", won(order.fee)));
        row.append(node("td", "number", won(order.total)), node("td", "", dateTime(order.created_at, true)));
        if (full) row.append(node("td", "order-note-cell", order.note || "—"));
        fragment.append(row);
      });
      if (!records.length) {
        const row = node("tr");
        const cell = node("td", "empty-cell", "아직 거래가 없습니다. 관심종목을 살펴보고 첫 모의 주문을 기록하세요.");
        cell.colSpan = full ? 8 : 6;
        row.append(cell);
        fragment.append(row);
      }
      target.replaceChildren(fragment);
    });
  }
  function renderJournal() {
    const savedSymbol = $("journal-symbol").value;
    const options = [node("option", "", "전체 시장 / 일반 메모")];
    options[0].value = "";
    const stocks = new Map();
    [...(state.watchlist || []), ...(state.positions || [])].forEach((stock) => stocks.set(stock.symbol, stock));
    stocks.forEach((stock) => {
      const option = node("option", "", stock.name + " · " + stock.symbol);
      option.value = stock.symbol;
      options.push(option);
    });
    $("journal-symbol").replaceChildren(...options);
    if (stocks.has(savedSymbol)) $("journal-symbol").value = savedSymbol;
    const fragment = document.createDocumentFragment();
    (state.journal || []).forEach((entry) => {
      const card = node("article", "journal-card");
      const heading = node("div", "journal-top");
      const stock = stocks.get(entry.symbol);
      const label = entry.symbol ? (stock?.name || entry.symbol) + " · " + entry.symbol : "시장 관찰 · 나의 투자 기록";
      const time = node("time", "", dateTime(entry.created_at));
      if (entry.created_at) time.setAttribute("datetime", entry.created_at);
      heading.append(node("strong", "", label), time);
      card.append(heading, node("p", "", entry.text));
      fragment.append(card);
    });
    if (!(state.journal || []).length) {
      const empty = node("div", "panel");
      empty.append(node("p", "empty-text", "아직 작성한 일지가 없습니다.\n오늘의 시장 관찰이나 첫 모의 주문의 이유를 기록해보세요."));
      fragment.append(empty);
    }
    $("journal-list").replaceChildren(fragment);
  }

  async function searchStocks(event) {
    if (event) event.preventDefault();
    const query = $("stock-search").value.trim();
    const request = ++searchRequest;
    if (!query) { $("search-results").replaceChildren(node("p", "empty-text", "종목명 또는 6자리 종목 코드를 입력하세요.")); return; }
    $("search-results").replaceChildren(node("p", "empty-text", "종목을 찾고 있습니다…"));
    try {
      const response = await api("/api/search?q=" + encodeURIComponent(query));
      if (request !== searchRequest) return;
      const fragment = document.createDocumentFragment();
      (response.results || []).forEach((stock) => {
        const row = node("div", "search-result");
        const text = node("div");
        text.append(node("strong", "", stock.name), node("span", "", stock.symbol));
        const exists = state?.watchlist?.some((item) => item.symbol === stock.symbol);
        const add = node("button", "button secondary", exists ? "추가됨" : "+ 추가");
        add.type = "button";
        add.disabled = exists;
        add.setAttribute("aria-label", stock.name + (exists ? " 관심종목에 추가됨" : " 관심종목 추가"));
        add.addEventListener("click", () => busy(add, async () => {
          try {
            await api("/api/watchlist", { symbol: stock.symbol, action: "add" });
            $("search-dialog").close();
            await refreshState(true);
            await selectStock(stock.symbol);
            showToast(stock.name + "을(를) 관심종목에 추가했습니다.");
          } catch (error) { showToast(error.message); }
        }));
        row.append(text, add);
        fragment.append(row);
      });
      if (!(response.results || []).length) fragment.append(node("p", "empty-text", "검색 결과가 없습니다. 종목명이나 코드를 다시 확인하세요."));
      $("search-results").replaceChildren(fragment);
    } catch (error) {
      if (request === searchRequest) $("search-results").replaceChildren(node("p", "empty-text", error.message));
    }
  }
  function showReport(report) {
    $("report-title").textContent = report.title || "종목 관측 보고서";
    $("report-kind").textContent = report.kind === "technical" ? "일별 종가로 계산한 기술 관측 · AI 보고서가 아닙니다 · " + dateTime(report.created_at, true) : "PRISM 분석 엔진 보고서 · " + dateTime(report.created_at, true);
    $("report-body").textContent = typeof report.body === "string" ? report.body : JSON.stringify(report.body || report, null, 2);
    $("report-area").classList.remove("hidden");
  }
  async function analyzeStock() {
    if (!selectedQuote) return;
    const requestedSymbol = selectedSymbol;
    await busy($("analyze-button"), async () => {
      try {
        const response = await api("/api/analyze", { symbol: requestedSymbol });
        if (selectedSymbol === requestedSymbol) showReport(response.report || response);
      } catch (error) { showToast(error.message); }
    });
  }
  async function startPrismAnalysis() {
    if (!selectedQuote || !state.upstream?.enabled || activeJob) return;
    await busy($("prism-analysis-button"), async () => {
      try {
        const response = await api("/api/prism-analysis", { symbol: selectedSymbol });
        activeJob = { id: response.job.id, symbol: selectedSymbol };
        $("job-status").textContent = "분석을 시작했습니다. 보고서가 준비되면 여기에 표시됩니다.";
        $("prism-analysis-button").disabled = true;
        await pollJob();
      } catch (error) { $("job-status").textContent = error.message; }
    });
    $("prism-analysis-button").disabled = Boolean(activeJob);
  }
  async function pollJob() {
    if (!activeJob) return;
    const job = activeJob;
    try {
      const response = await api("/api/jobs?id=" + encodeURIComponent(job.id));
      const result = response.job || response;
      if (["complete", "completed", "done", "succeeded"].includes(result.status)) {
        $("job-status").textContent = "PRISM 분석 보고서가 준비되었습니다.";
        if (result.report && selectedSymbol === job.symbol) showReport(result.report);
        activeJob = null;
        $("prism-analysis-button").disabled = false;
        return;
      }
      if (["failed", "error"].includes(result.status)) {
        $("job-status").textContent = result.error || "분석을 완료하지 못했습니다. 연결과 설정을 확인하세요.";
        activeJob = null;
        $("prism-analysis-button").disabled = false;
        return;
      }
      $("job-status").textContent = result.status === "queued" ? "분석 대기 중입니다. 보고서 작성에는 몇 분이 걸릴 수 있습니다." : "PRISM 분석 엔진이 보고서를 작성하고 있습니다.";
    } catch (error) {
      $("job-status").textContent = error.message + " 분석 상태를 다시 확인하고 있습니다.";
    }
    jobTimer = setTimeout(pollJob, 5000);
  }

  function renderModeBadge() {
    renderGlobalAlert();
    const autoView = currentView() === "autotrade";
    const connected = ["dashboard", "portfolio"].includes(currentView()) && showingConnectedAccount();
    const account = currentDashboardAccount();
    const mode = autoView ? autotrade?.mode || "paper" : connected ? account.mode : "paper";
    const label = connected ? connectedAccountName(account) : autoView ? autoModeNames[mode] || "계좌 확인 필요" : "모의매매";
    const badge = $("header-mode");
    badge.replaceChildren(node("span", "status-dot"), document.createTextNode(label));
    badge.classList.toggle("live-header", isLiveMode(mode));
    $("footer-message").textContent = connected ? "연결 계좌는 읽기 전용으로 표시합니다. 모의매매 연습은 로컬 모의계좌에서 할 수 있습니다."
      : autoView ? "자동매매 계좌와 실행 상태를 확인하세요. 분석과 전략은 수익을 보장하지 않습니다."
      : "분석은 판단을 돕는 참고 자료입니다. 이 화면의 수동 주문은 모의매매로 기록됩니다.";
  }
  function scheduleAutoPoll() {
    clearTimeout(autoPollTimer);
    if (state && (currentView() === "autotrade" || autotrade?.running)) autoPollTimer = setTimeout(refreshAutotrade, 5000);
  }
  async function refreshAutotrade() {
    if (autoFetching || autoOperating || autoStopping) { scheduleAutoPoll(); return; }
    autoFetching = true;
    try { setAutotrade(await api("/api/autotrade")); }
    catch (error) { autoAlert(error.message); }
    finally { autoFetching = false; scheduleAutoPoll(); }
  }
  function autoAlert(message) {
    $("auto-alert").textContent = message;
    $("auto-alert").classList.toggle("hidden", !message);
  }
  function setAutotrade(response) {
    const previousConfig = JSON.stringify(autotrade?.config);
    autotrade = response.autotrade || response;
    if (state) state.autotrade = autotrade;
    if (!autoConfigInitialized) {
      populateAutoConfig();
      $("broker-environment").value = autotrade.connection?.environment || "paper";
      $("broker-provider").value = autotrade.connection?.provider || "kis";
      autoConfigInitialized = true;
    } else if (!autoConfigDirty && previousConfig !== JSON.stringify(autotrade.config)) {
      populateAutoConfig();
    } else if (!autoConfigDirty) populateAutoConfig();
    renderAutotrade();
    scheduleAutoPoll();
  }
  function populateAutoConfig() {
    const config = autotrade?.config || {};
    $("auto-mode").value = config.mode || autotrade?.mode || "paper";
    $("auto-symbols").value = (config.symbols || []).join(", ");
    Object.entries(autoFields).forEach(([field, id]) => $(id).value = config[field] ?? "");
    autoConfigDirty = false;
    updateAutoModeHelp();
  }
  function updateAutoModeHelp() {
    const mode = $("auto-mode").value;
    $("auto-mode-help").textContent = mode === "toss-live" ? "토스증권 API 키와 허용 IP를 설정하고 조회한 실계좌를 선택하세요. 시작 전 저장된 계좌와 한도를 다시 확인합니다." : mode === "kis-live" ? "한국투자 실전투자 앱 키와 실계좌를 연결해야 합니다. 시작 전 저장된 계좌와 한도를 다시 확인합니다." : mode === "kis-paper" ? "한국투자증권 모의투자 앱 키와 모의 계좌가 필요합니다. 증권사의 모의 주문으로 접수합니다." : "이 앱의 로컬 모의 계좌에서 주문을 기록합니다. 증권사 주문을 보내지 않습니다.";
  }
  function isLiveMode(mode) { return mode === "kis-live" || mode === "toss-live"; }
  function expectedBroker(mode) { return mode === "toss-live" ? "toss" : "kis"; }
  function connectionMatches(mode, connection) {
    return (connection.provider || "kis") === expectedBroker(mode) && connection.environment === (isLiveMode(mode) ? "live" : "paper");
  }
  function updateBrokerForm() {
    const provider = $("broker-provider").value;
    const locked = Boolean(autotrade?.running || unresolvedOrders().length || autoOperating || autoStopping);
    $("broker-kis-fields").classList.toggle("hidden", provider !== "kis");
    $("broker-toss-fields").classList.toggle("hidden", provider !== "toss");
    $("broker-kis-fields").querySelectorAll("input, select, button").forEach((element) => element.disabled = locked || provider !== "kis");
    $("broker-toss-fields").querySelectorAll("input, select, button").forEach((element) => element.disabled = locked || provider !== "toss");
    const accountsValid = tossAccountsVersion === tossCredentialsVersion && tossAccounts.length > 0;
    $("toss-account-seq").disabled = locked || provider !== "toss" || !accountsValid;
    $("toss-accounts-button").disabled = locked || provider !== "toss" || !$("toss-client-id").value.trim() || !$("toss-client-secret").value.trim();
    $("broker-connect-button").disabled = locked || (provider === "toss" && (!accountsValid || !$("toss-account-seq").value));
  }
  function clearTossAccounts(message) {
    tossAccounts = [];
    tossAccountsVersion = -1;
    const option = node("option", "", "계좌 목록을 먼저 조회하세요.");
    option.value = "";
    $("toss-account-seq").replaceChildren(option);
    $("toss-accounts-message").textContent = message || "계좌 목록 조회는 읽기 전용이며 키를 저장하거나 주문하지 않습니다.";
    updateBrokerForm();
  }
  async function lookupTossAccounts() {
    if (autoOperating || autoStopping || autotrade?.running || unresolvedOrders().length) return;
    const client_id = $("toss-client-id").value.trim();
    const client_secret = $("toss-client-secret").value.trim();
    if (!client_id || !client_secret) { autoAlert("토스증권 Client ID와 Client Secret을 입력하세요."); return; }
    const version = tossCredentialsVersion;
    autoOperating = true;
    renderAutoControls();
    autoAlert("");
    $("toss-accounts-message").textContent = "연결 가능한 계좌를 읽기 전용으로 조회하고 있습니다.";
    try {
      const response = await api("/api/autotrade/toss/accounts", { client_id, client_secret });
      if (version !== tossCredentialsVersion) { clearTossAccounts("입력한 키가 변경되었습니다. 계좌 목록을 다시 조회하세요."); return; }
      tossAccounts = (response.accounts || []).filter((account) => {
        const seq = account.account_seq;
        return (Number.isSafeInteger(seq) && seq > 0) || (typeof seq === "string" && /^[1-9][0-9]{0,18}$/.test(seq) && BigInt(seq) < 9223372036854775808n);
      }).map((account) => ({ ...account, account_seq: String(account.account_seq) }));
      tossAccountsVersion = version;
      const placeholder = node("option", "", tossAccounts.length ? "연결할 계좌를 선택하세요." : "연결 가능한 계좌가 없습니다.");
      placeholder.value = "";
      const options = tossAccounts.map((account) => {
        const option = node("option", "", (account.account_masked || "마스킹 계좌 정보 없음") + (account.account_type === "BROKERAGE" ? " · 주식 계좌" : ""));
        option.value = String(account.account_seq);
        return option;
      });
      $("toss-account-seq").replaceChildren(placeholder, ...options);
      $("toss-accounts-message").textContent = tossAccounts.length ? tossAccounts.length + "개 계좌를 조회했습니다. 연결할 계좌를 선택하세요. 키 저장과 주문은 진행하지 않았습니다." : "연결 가능한 계좌가 없습니다. 계좌와 API 권한을 확인하세요.";
    } catch (error) { clearTossAccounts("계좌를 조회하지 못했습니다. 입력한 키와 허용 IP를 확인하세요."); autoAlert(error.message); }
    finally { autoOperating = false; renderAutoControls(); scheduleAutoPoll(); }
  }
  function unresolvedOrders() { return (autotrade?.orders || []).filter((order) => ["submitting", "pending", "partial", "uncertain"].includes(order.status)); }
  function renderAutoControls() {
    if (!autotrade) return;
    const running = Boolean(autotrade.running);
    const pending = unresolvedOrders();
    const connection = autotrade.connection || {};
    const mode = autotrade.mode || "paper";
    const brokerMode = mode !== "paper";
    const matched = connectionMatches(mode, connection);
    const ready = !brokerMode || Boolean(connection.ready && matched);
    const canStart = !running && !autoConfigDirty && Boolean(autotrade.configured) && ready && !pending.length;
    const busyState = autoOperating || autoStopping;
    $("auto-start-button").disabled = !canStart || busyState;
    $("auto-start-button").classList.toggle("hidden", running);
    $("auto-stop-button").classList.toggle("hidden", !running);
    $("auto-stop-button").disabled = autoStopping;
    $("auto-stop-button").textContent = autoStopping ? "정지 요청 중…" : "자동매매 정지";
    $("auto-check-button").disabled = busyState;
    $("auto-config-saved").textContent = autoConfigDirty ? "저장 전 · 변경사항 저장 필요" : autotrade.configured ? "저장된 설정" : "최초 설정 저장 필요";
    $("auto-config-saved").classList.toggle("unsaved", autoConfigDirty);
    $("auto-config-form").querySelectorAll("input, select, textarea, button").forEach((element) => element.disabled = running || pending.length > 0 || busyState);
    $("broker-connect-form").querySelectorAll("input, select, button").forEach((element) => element.disabled = running || pending.length > 0 || busyState);
    $("broker-disconnect-button").disabled = !connection.configured || running || pending.length > 0 || busyState;
    updateBrokerForm();
    let help;
    if (running) help = "자동매매가 실행 중입니다. 정지하면 새 주문 제출을 중단합니다.";
    else if (pending.length) help = "미체결 또는 불확실 주문 " + pending.length + "건이 있습니다. 연결된 증권사 앱에서 주문을 확인·취소한 뒤 ‘신호·주문 상태 확인’을 눌러주세요.";
    else if (autoConfigDirty || !autotrade.configured) help = "거래 방식과 예산·한도를 저장한 뒤 시작하세요.";
    else if (!ready) help = "저장된 거래 방식과 같은 증권사·환경의 계좌를 연결하고 읽기 전용 조회를 확인하세요.";
    else help = isLiveMode(mode) ? "실계좌 연결과 저장된 한도를 확인했습니다. 시작 시 실제 주문을 다시 확인합니다." : "저장된 설정으로 자동매매를 시작할 수 있습니다.";
    $("auto-start-help").textContent = help;
  }
  function renderAutotrade() {
    if (!autotrade) return;
    const mode = autotrade.mode || "paper";
    const running = Boolean(autotrade.running);
    const pending = unresolvedOrders();
    ["auto-mode-heading", "auto-control-mode", "auto-account-mode"].forEach((id) => {
      $(id).textContent = autoModeNames[mode] || "계좌 확인 필요";
      $(id).classList.toggle("live-badge", isLiveMode(mode));
    });
    $("auto-status-title").textContent = running ? "자동매매 실행 중" : pending.length ? "정지됨 · 주문 확인 필요" : "자동매매 정지됨";
    $("auto-status-dot").classList.toggle("running", running);
    $("auto-status-dot").classList.toggle("attention", pending.length > 0);
    $("auto-status-message").textContent = autotrade.message || (running ? "설정된 간격으로 조건과 주문 상태를 확인합니다." : "직접 시작하기 전에는 자동 주문을 제출하지 않습니다.");
    $("auto-last-run").textContent = autotrade.last_run ? dateTime(autotrade.last_run, true) : "아직 실행 없음";
    $("auto-next-run").textContent = running && autotrade.next_run ? dateTime(autotrade.next_run, true) : "—";
    const connection = autotrade.connection || {};
    $("broker-connection-badge").textContent = connection.ready ? "조회 확인" : connection.configured ? "저장됨 · 조회 필요" : "미연결";
    $("broker-account").textContent = connection.configured ? (connection.provider === "toss" ? "토스증권 · " : "한국투자 · ") + (connection.environment === "live" ? "실전투자 · " : "모의투자 · ") + (connection.account_masked || "계좌 확인 필요") : "연결된 계좌 없음";
    $("broker-message").textContent = connection.message || "연결할 증권사와 거래 환경을 선택하세요.";
    renderAutoControls();
    renderAutoAccount();
    renderAutoPreview();
    renderAutoOrders();
    renderAutoEvents();
    renderReconciliation();
    renderModeBadge();
  }
  function emptyRow(columns, text) {
    const row = node("tr");
    const cell = node("td", "empty-cell", text);
    cell.colSpan = columns;
    row.append(cell);
    return row;
  }
  function renderAutoAccount() {
    const mode = autotrade.mode || "paper";
    const cached = autotrade.account;
    const account = cached && cached.mode === mode ? cached : mode === "paper" ? state : null;
    $("auto-account-cash").textContent = won(account?.cash);
    $("auto-account-equity").textContent = won(account?.equity);
    const tossBasis = mode === "toss-live" || account?.equity_basis === "krw-trading-capital";
    $("auto-account-equity-label").textContent = tossBasis ? "원화 거래 기준 자산" : "총 평가자산";
    $("auto-account-basis").textContent = tossBasis ? "토스의 비중·일일 손실 한도는 원화 현금 매수 가능액과 국내 보유 주식 평가액을 기준으로 계산합니다. 해외 주식은 거래하지 않으며 계좌 전체 보유 종목 수 한도에는 포함합니다." : "보유 현황은 최근 읽기 전용 조회 결과입니다.";
    if (tossBasis && hasNumber(account?.excluded_positions_count) && account.excluded_positions_count > 0) $("auto-account-basis").textContent += " 다른 시장의 보유 " + account.excluded_positions_count + "종목은 자동매매 관리에서 제외됩니다.";
    const fragment = document.createDocumentFragment();
    (account?.positions || []).forEach((position) => {
      const row = node("tr");
      const managed = number(position.managed_quantity);
      const management = hasNumber(position.managed_quantity) ? managed > 0 ? "자동 관리 " + wonFormat.format(managed) + "주" : "수동 보유 · 관리 제외" : "관리 구분 확인 필요";
      row.append(stockCell({ ...position, name: position.name || position.symbol }), node("td", "number", hasNumber(position.quantity) ? wonFormat.format(number(position.quantity)) + "주" : "확인 불가"), node("td", "number", won(position.average_cost)), node("td", "number", won(position.price)), node("td", "number", won(position.market_value)), node("td", "", management));
      fragment.append(row);
    });
    if (!(account?.positions || []).length) fragment.append(emptyRow(6, account ? "계좌에 보유 종목이 없습니다." : "계좌 연결 또는 읽기 전용 확인 후 보유 현황이 표시됩니다."));
    $("auto-account-positions").replaceChildren(fragment);
  }
  function renderAutoPreview() {
    const labels = { buy: "매수 조건", sell: "매도 조건", hold: "보유", skip: "건너뜀", wait: "대기", BUY: "매수 조건", SELL: "매도 조건", HOLD: "보유" };
    const fragment = document.createDocumentFragment();
    (autotrade.preview || []).forEach((signal) => {
      const row = node("tr");
      row.append(node("td", "", signal.symbol || "—"), node("td", "", labels[signal.action] || signal.action || "조건 없음"), node("td", "", signal.signal_date || "확인 불가"), node("td", "auto-reason-cell", signal.reason || "—"));
      fragment.append(row);
    });
    if (!(autotrade.preview || []).length) fragment.append(emptyRow(4, "‘신호·주문 상태 확인’을 누르면 조건과 건너뛴 이유가 표시됩니다."));
    $("auto-preview").replaceChildren(fragment);
  }
  function renderAutoOrders() {
    const labels = { submitting: "접수 확인 중", pending: "접수 · 미체결", partial: "부분 체결", filled: "체결 확인", uncertain: "응답 불확실", canceled: "취소 확인", rejected: "거절" };
    const fragment = document.createDocumentFragment();
    (autotrade.orders || []).forEach((order) => {
      const statusLabel = order.status === "rejected" && number(order.filled_quantity) > 0 ? "일부 체결 후 거절" : labels[order.status] || "상태 확인 필요";
      const row = node("tr");
      const symbolCell = node("td");
      symbolCell.append(node("strong", "", order.symbol), node("span", "symbol", order.broker_order_id ? "증권사 " + order.broker_order_id : "앱 " + String(order.id).slice(0, 8)));
      const sideCell = node("td");
      sideCell.append(node("span", "side-badge" + (order.side === "sell" ? " sell" : ""), order.side === "sell" ? "매도" : "매수"));
      row.append(symbolCell, node("td", "", autoModeNames[order.mode] || order.mode), sideCell, node("td", "number", wonFormat.format(number(order.quantity)) + "주"), node("td", "number", won(order.price)), node("td", "auto-order-status " + (["uncertain", "submitting"].includes(order.status) ? "attention-text" : ""), statusLabel), node("td", "number", hasNumber(order.filled_quantity) ? wonFormat.format(number(order.filled_quantity)) + "주" + (number(order.filled_quantity) > 0 && hasNumber(order.average_price) ? " · " + won(order.average_price) : "") : "확인 불가"), node("td", "", dateTime(order.created_at, true)));
      fragment.append(row);
    });
    if (!(autotrade.orders || []).length) fragment.append(emptyRow(8, "자동매매 주문 기록이 없습니다."));
    $("auto-orders").replaceChildren(fragment);
  }
  function renderAutoEvents() {
    const fragment = document.createDocumentFragment();
    (autotrade.events || []).forEach((event) => {
      const row = node("div", "auto-event");
      const marker = node("span", "event-marker" + (["error", "uncertain", "blocked"].includes(event.type) ? " attention" : ""));
      const body = node("div");
      body.append(node("p", "", event.message), node("time", "", dateTime(event.created_at, true)));
      row.append(marker, body);
      fragment.append(row);
    });
    if (!(autotrade.events || []).length) fragment.append(node("p", "empty-text", "자동매매를 시작하거나 신호를 확인하면 기록이 표시됩니다."));
    $("auto-events").replaceChildren(fragment);
  }
  function renderReconciliation() {
    const uncertain = (autotrade.orders || []).filter((order) => ["uncertain", "submitting"].includes(order.status));
    const saved = $("reconcile-intent").value;
    const options = uncertain.map((order) => {
      const option = node("option", "", order.symbol + " · " + (order.side === "sell" ? "매도" : "매수") + " " + wonFormat.format(number(order.quantity)) + "주 · " + dateTime(order.created_at, true));
      option.value = order.id;
      return option;
    });
    $("reconcile-intent").replaceChildren(...options);
    if (uncertain.some((order) => order.id === saved)) $("reconcile-intent").value = saved;
    $("auto-reconcile-panel").classList.toggle("hidden", !uncertain.length);
    updateReconciliationProvider();
  }
  function updateReconciliationProvider() {
    const order = (autotrade?.orders || []).find((item) => item.id === $("reconcile-intent").value);
    const toss = order?.mode === "toss-live";
    $("reconcile-organization-field").classList.toggle("hidden", toss);
    $("reconcile-organization").disabled = toss;
    $("reconcile-organization").required = !toss;
  }
  async function autoAction(path, payload) {
    if (autoOperating || autoStopping) return false;
    autoOperating = true;
    renderAutoControls();
    autoAlert("");
    try {
      setAutotrade(await api(path, payload));
      return true;
    } catch (error) { autoAlert(error.message); return false; }
    finally { autoOperating = false; renderAutoControls(); scheduleAutoPoll(); }
  }
  async function stopAutotrade() {
    if (autoStopping || !autotrade?.running) return;
    autoStopping = true;
    renderAutoControls();
    try {
      setAutotrade(await api("/api/autotrade/stop", {}));
      showToast("새 자동 주문을 중단했습니다. 접수된 주문은 증권사에서 확인하세요.");
    } catch (error) { autoAlert(error.message); }
    finally { autoStopping = false; renderAutoControls(); scheduleAutoPoll(); }
  }
  function liveKey() { return JSON.stringify({ config: autotrade?.config, account: autotrade?.connection?.account_masked, provider: autotrade?.connection?.provider || "kis", environment: autotrade?.connection?.environment, review: autotrade?.review_token }); }
  function reviewAutoStart() {
    if (!autotrade || $("auto-start-button").disabled) return;
    if (!isLiveMode(autotrade.mode)) {
      autoAction("/api/autotrade/start", { confirm_live: false }).then((ok) => { if (ok) showToast("자동매매를 시작했습니다."); });
      return;
    }
    const config = autotrade.config;
    liveReviewKey = liveKey();
    const details = document.createDocumentFragment();
    [["연결 증권사", autotrade.mode === "toss-live" ? "토스증권 실계좌" : "한국투자증권 실계좌"], ["연결 실계좌", autotrade.connection.account_masked || "확인 불가"], ["자산·손실 한도 기준", autotrade.mode === "toss-live" ? "원화 현금 매수 가능액 + 국내 주식 평가액" : "계좌 평가자산"], ["자동매매 전략", "확정 종가 20일 돌파 · MA20 이탈 또는 설정 손절"], ["종목당 최대 자산 비중", percent(autotrade.position_limit_pct, false)], ["주문당 매수 예산", won(config.order_budget)], ["일일 최대 매수금액", won(config.max_daily_buy)], ["일일 최대 매수 주문", number(config.max_daily_orders) + "건"], ["계좌 전체 보유 한도", number(config.max_positions) + "종목"], ["종목별 손절 기준", percent(config.stop_loss_pct, false)], ["일일 손실 한도", percent(config.daily_loss_pct, false)], ["대상 종목", (config.symbols || []).join(", ")]].forEach(([label, value]) => {
      const row = node("div");
      row.append(node("span", "", label), node("strong", "", value));
      details.append(row);
    });
    $("live-confirm-details").replaceChildren(details);
    $("live-confirm-checkbox").checked = false;
    $("toss-external-orders-checkbox").checked = false;
    $("toss-external-confirm").classList.toggle("hidden", autotrade.mode !== "toss-live");
    $("live-broker-limits").classList.toggle("hidden", autotrade.mode !== "toss-live");
    $("live-confirm-button").disabled = true;
    $("live-confirm-error").classList.add("hidden");
    $("live-dialog").showModal();
  }
  async function confirmLiveStart() {
    if (!$("live-confirm-checkbox").checked || (autotrade?.mode === "toss-live" && !$("toss-external-orders-checkbox").checked) || autoOperating || autoStopping) return;
    await busy($("live-confirm-button"), async () => {
      try {
        setAutotrade(await api("/api/autotrade"));
        if (liveReviewKey !== liveKey() || !isLiveMode(autotrade.mode) || autotrade.running || !autotrade.connection?.ready || !connectionMatches(autotrade.mode, autotrade.connection) || unresolvedOrders().length || autoConfigDirty) throw new Error("계좌·설정 또는 실행 상태가 변경되었습니다. 창을 닫고 현재 상태를 다시 확인하세요.");
        if (!$("live-dialog").open || !$("live-confirm-checkbox").checked || (autotrade?.mode === "toss-live" && !$("toss-external-orders-checkbox").checked)) throw new Error("현재 계좌와 한도 및 외부 주문 확인에 동의한 뒤 시작하세요.");
        const payload = { confirm_live: true, review_token: autotrade.review_token };
        if (autotrade.mode === "toss-live") payload.confirm_external_orders = $("toss-external-orders-checkbox").checked;
        const ok = await autoAction("/api/autotrade/start", payload);
        if (ok) { $("live-dialog").close(); showToast("실계좌 자동매매를 시작했습니다. 실행 기록을 확인하세요."); }
        else throw new Error($("auto-alert").textContent || "시작 요청을 확인하지 못했습니다.");
      } catch (error) {
        $("live-confirm-error").textContent = error.message;
        $("live-confirm-error").classList.remove("hidden");
      }
    });
    updateLiveConfirmation();
  }

  document.querySelectorAll("[data-side]").forEach((button) => button.addEventListener("click", () => { side = button.dataset.side; updateOrder(); }));
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  }));
  $("order-quantity").addEventListener("input", updateOrder);
  $("order-form").addEventListener("submit", reviewOrder);
  $("confirm-order-button").addEventListener("click", confirmOrder);
  $("account-view-select").addEventListener("change", event => changeAccountView(event.target.value));
  $("account-paper-button").addEventListener("click", () => changeAccountView("paper"));
  $("refresh-button").addEventListener("click", () => busy($("refresh-button"), () => refreshState()));
  $("add-watch-button").addEventListener("click", () => { $("search-dialog").showModal(); $("stock-search").focus(); });
  $("search-form").addEventListener("submit", searchStocks);
  let searchTimer;
  $("stock-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchStocks, 350); });
  $("analyze-button").addEventListener("click", analyzeStock);
  $("prism-analysis-button").addEventListener("click", startPrismAnalysis);
  $("journal-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("journal-text").value.trim();
    if (!text) { showToast("기록할 내용을 입력하세요."); return; }
    const button = event.submitter || $("journal-form").querySelector("button");
    await busy(button, async () => {
      try {
        await api("/api/journal", { symbol: $("journal-symbol").value, text });
        $("journal-text").value = "";
        await refreshState(true);
        showToast("투자 일지를 저장했습니다.");
      } catch (error) { showToast(error.message); }
    });
  });
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = { display_name: $("settings-name").value.trim(), max_position_pct: Number($("settings-limit").value), fee_bps: Number($("settings-fee").value) };
    const button = event.submitter || $("settings-form").querySelector("button");
    await busy(button, async () => {
      try {
        await api("/api/settings", payload);
        await refreshState(true);
        populateSettings();
        showToast("개인 투자 설정을 저장했습니다.");
      } catch (error) { showToast(error.message); }
    });
  });
  $("auto-config-form").addEventListener("input", () => { autoConfigDirty = true; updateAutoModeHelp(); renderAutoControls(); });
  $("auto-config-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const symbols = [...new Set($("auto-symbols").value.split(/[\s,;]+/).filter(Boolean))];
    if (!symbols.length || symbols.length > 20 || symbols.some((symbol) => !/^\d{6}$/.test(symbol))) { autoAlert("1~20개의 6자리 종목 코드를 입력하세요."); return; }
    const payload = { mode: $("auto-mode").value, symbols };
    Object.entries(autoFields).forEach(([field, id]) => payload[field] = Number($(id).value));
    if (payload.order_budget > payload.max_daily_buy) { autoAlert("주문당 매수 예산은 하루 최대 매수금액 이하여야 합니다."); return; }
    if (await autoAction("/api/autotrade/config", payload)) { populateAutoConfig(); renderAutoControls(); showToast("자동매매 설정을 저장했습니다."); }
  });
  $("broker-connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const provider = $("broker-provider").value;
    let payload;
    if (provider === "toss") {
      const account_seq = $("toss-account-seq").value;
      if (tossAccountsVersion !== tossCredentialsVersion || !tossAccounts.some((account) => account.account_seq === account_seq)) { autoAlert("현재 입력한 키로 계좌를 조회한 뒤 연결할 계좌를 선택하세요."); return; }
      payload = { provider: "toss", environment: "live", client_id: $("toss-client-id").value.trim(), client_secret: $("toss-client-secret").value.trim(), account_seq };
    } else {
      payload = { provider: "kis", app_key: $("broker-app-key").value.trim(), app_secret: $("broker-app-secret").value.trim(), account_no: $("broker-account-no").value.trim(), product_code: $("broker-product-code").value.trim(), environment: $("broker-environment").value };
      if (!/^\d{8}$/.test(payload.account_no) || !/^\d{2}$/.test(payload.product_code)) { autoAlert("계좌번호 앞 8자리와 상품코드 2자리를 확인하세요."); return; }
    }
    if (await autoAction("/api/autotrade/connect", payload)) {
      if (provider === "toss") {
        $("toss-client-id").value = "";
        $("toss-client-secret").value = "";
        tossCredentialsVersion++;
        clearTossAccounts("토스 계좌 연결을 저장했습니다. 키 입력은 화면에서 비웠습니다.");
      } else {
        $("broker-app-key").value = "";
        $("broker-app-secret").value = "";
        $("broker-account-no").value = "";
      }
      showToast("계좌 잔고를 읽기 전용으로 확인했습니다. 주문하지 않았습니다.");
    }
  });
  $("broker-provider").addEventListener("change", updateBrokerForm);
  ["toss-client-id", "toss-client-secret"].forEach((id) => $(id).addEventListener("input", () => { tossCredentialsVersion++; clearTossAccounts("현재 입력한 키로 계좌 목록을 조회하세요. 키가 바뀌면 이전 계좌 선택은 사용하지 않습니다."); }));
  $("toss-accounts-button").addEventListener("click", lookupTossAccounts);
  $("toss-account-seq").addEventListener("change", updateBrokerForm);
  $("broker-disconnect-button").addEventListener("click", async () => { if (await autoAction("/api/autotrade/disconnect", {})) showToast("저장된 증권사 연결 정보를 제거했습니다."); });
  $("auto-start-button").addEventListener("click", reviewAutoStart);
  $("auto-stop-button").addEventListener("click", stopAutotrade);
  $("auto-check-button").addEventListener("click", async () => { if (await autoAction("/api/autotrade/check", {})) showToast("신호와 주문 상태를 읽기 전용으로 확인했습니다. 새 주문은 보내지 않았습니다."); });
  function updateLiveConfirmation() { $("live-confirm-button").disabled = !$("live-confirm-checkbox").checked || (autotrade?.mode === "toss-live" && !$("toss-external-orders-checkbox").checked); }
  $("live-confirm-checkbox").addEventListener("change", updateLiveConfirmation);
  $("toss-external-orders-checkbox").addEventListener("change", updateLiveConfirmation);
  $("live-confirm-button").addEventListener("click", confirmLiveStart);
  $("auto-reconcile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = { intent_id: $("reconcile-intent").value, broker_order_id: $("reconcile-order-id").value.trim(), order_date: $("reconcile-date").value.trim() };
    const order = (autotrade?.orders || []).find((item) => item.id === payload.intent_id);
    if (order?.mode !== "toss-live") payload.organization_id = $("reconcile-organization").value.trim();
    if (await autoAction("/api/autotrade/reconcile", payload)) { showToast("해당 증권사 주문의 상태를 확인했습니다. 체결 기록을 확인하세요."); $("reconcile-order-id").value = ""; $("reconcile-organization").value = ""; }
  });
  $("reconcile-intent").addEventListener("change", updateReconciliationProvider);
  window.addEventListener("hashchange", renderView);
  window.addEventListener("beforeunload", () => { clearTimeout(jobTimer); clearTimeout(searchTimer); clearTimeout(autoPollTimer); });
  $("today").textContent = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" }).format(new Date());
  renderView();
  refreshState(true);
})();
