/* SPDX-License-Identifier: Apache-2.0 · Copyright 2026 Borislav Vladimirov Petrakiev */
/* KaBor Quant — service worker mínimo (7.3).
   No guarda datos ni respuestas de la API: el panel siempre muestra lo que dice la Raspberry en ese momento.
   Solo sirve para que el panel se pueda instalar como app y para enseñar una pantalla clara cuando no hay
   conexión con la Raspberry, en vez del error del navegador. */
const VERSION = "kq-7.3";
const OFFLINE = `<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0A1430"><title>KaBor Quant · sin conexión</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0A1430;color:#E8ECF5;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:24px;box-sizing:border-box}
main{max-width:360px;text-align:center}img{width:84px;height:84px;border-radius:20px}h1{font-size:1.3rem;margin:18px 0 6px}p{color:#A9B3C9;margin:0 0 18px}
button{font:inherit;font-weight:600;background:#E6B84A;color:#0A1430;border:0;border-radius:10px;padding:11px 22px;cursor:pointer}</style></head>
<body><main><img src="/icono-192.png" alt=""><h1>Sin conexión con la Raspberry</h1>
<p>No llega la conexión. Comprueba la wifi o Tailscale y que el programa esté en marcha. Los bots siguen funcionando en la Raspberry aunque no los veas.</p>
<button onclick="location.reload()">Reintentar</button></main></body></html>`;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(["/icono-192.png"])).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url);
  if (u.origin !== location.origin) return;
  if (r.mode === "navigate") {
    e.respondWith(fetch(r).catch(() => new Response(OFFLINE, {status: 503, headers: {"Content-Type": "text/html; charset=utf-8"}})));
    return;
  }
  if (u.pathname === "/icono-192.png") e.respondWith(fetch(r).catch(() => caches.match(r)));
});
