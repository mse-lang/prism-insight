"use strict";

const SHELL_CACHE = "prism-mydesk-shell-v2-clear-20261010";
const STATIC_PATHS = new Set([
  "/static/app.css", "/static/app.js", "/static/advisor.css", "/static/advisor.js", "/static/desk-mobile.css",
  "/static/login.js", "/manifest.webmanifest", "/static/icon.svg", "/static/icon-192.png", "/static/icon-512.png"
]);
const OFFLINE_PAGE = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#f4f7f9"><title>PRISM · 연결 확인 필요</title><style>body{margin:0;min-height:100vh;background:#f4f7f9;color:#172c3a;font-family:system-ui,'Malgun Gothic',sans-serif;display:grid;place-items:center}main{max-width:360px;margin:25px;padding:32px;border:1px solid #e5edf1;border-radius:14px;background:white}small{letter-spacing:2px;color:#086aee}h1{font-size:25px;margin:22px 0 13px}p{font-size:15px;line-height:1.95;color:#607380}a{display:inline-block;background:#086aee;color:white;border-radius:7px;padding:12px 18px;margin-top:12px;font-size:14px;text-decoration:none}a:focus-visible{outline:3px solid #8ab9fb;outline-offset:4px}</style></head><body><main><small>PRISM / MY DESK</small><h1>PC 서버 연결이 끊겼습니다</h1><p>최신 계좌·제안·주문 상태를 조회할 수 없습니다. 이 화면에서는 주문할 수 없습니다.</p><p>PC 서버와 네트워크 연결을 확인한 후 다시 연결하세요. PC에서 실행 중인 자동매매의 상태도 다시 확인해야 합니다.</p><a href="/">다시 연결</a></main></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await Promise.allSettled([...STATIC_PATHS].map(async (path) => {
      const response = await fetch(path, { cache: "reload", credentials: "same-origin" });
      if (response.ok && !response.redirected) await cache.put(path, response);
    }));
    await self.skipWaiting();
  })());
});
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key.startsWith("prism-mydesk-shell-") && key !== SHELL_CACHE).map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});
self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname === "/api" || url.pathname.startsWith("/api/")) return;
  if (STATIC_PATHS.has(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      try {
        const response = await fetch(request);
        if (response.ok && !response.redirected) await cache.put(url.pathname, response.clone());
        return response;
      } catch (_) {
        const cached = await cache.match(url.pathname);
        return cached || new Response("", { status: 503, headers: { "Cache-Control": "no-store" } });
      }
    })());
    return;
  }
  if (request.mode === "navigate") event.respondWith((async () => {
    try { return await fetch(request); }
    catch (_) { return new Response(OFFLINE_PAGE, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } }); }
  })());
});
