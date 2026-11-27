# SPDX-License-Identifier: Apache-2.0
"""Sandbox de MyFundedPerps simulado para las pruebas: REST de trading y stream de la cuenta, en local.

arrancar(puerto_rest, puerto_ws) los pone en marcha en 127.0.0.1. Cuenta las GET por clave, manda
X-RateLimit-Warning al pasar de MODO["warn_from"] por minuto y, con MODO["429"], responde 429 con Retry-After.
Necesita el paquete `websockets` (solo para las pruebas; KaBor no lo usa).
"""
import asyncio, itertools, json, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import websockets

now = lambda: int(time.time() * 1000)
ids = itertools.count(1)
LOCK = threading.RLock()
ACC = {"id": "acc1", "name": "Demo", "status": "active", "balance": 100000.0, "starting_balance": 100000.0,
       "risk": {"available_balance": 100000.0, "equity": 100000.0, "daily_loss_room": 5000.0,
                "requirements": {"daily_loss_pct": 5}}}
POL = {"trading_halt": False, "restriction": None, "competition": None}
MKT = {"market_id": "binance|BTCUSDT", "size_step": "0.001", "size_decimals": 3, "min_size": "0.001",
       "max_leverage": 20, "trading_enabled": True}
MARKETS = [
    {"market_id": "binance|BTCUSDT", "provider": "binance", "symbol": "BTC", "coin": "BTC", "category": "crypto", "trading_enabled": True},
    {"market_id": "binance|ETHUSDT", "provider": "binance", "symbol": "ETH", "coin": "ETH", "category": "crypto", "trading_enabled": True},
    {"market_id": "binance|XAUUSDT", "provider": "binance", "symbol": "XAU", "coin": "XAUUSDT", "category": "commodities", "trading_enabled": True},
    {"market_id": "hyperliquid|xyz:NAS100", "provider": "hyperliquid", "symbol": "NAS100", "coin": "xyz:NAS100", "category": "indices", "trading_enabled": True},
]
PRICE = {"mid": 60000.0}
ORDERS, POS, CLOSED = {}, {}, {}
GETS = []                       # (hora, clave, ruta)
MODO = {"429": False, "fill_delay": 0.3, "ws_on": True, "warn_from": 30}
STATS = {"ws_connects": 0, "resumes": 0}
CLIENTS = set()
LOOP = None
SEQ = itertools.count(1)


def emit(fr):
    fr.setdefault("time", now())
    if fr["type"] not in ("heartbeat", "hello", "pong"):
        fr["seq"] = next(SEQ)
    if LOOP:
        for ws in list(CLIENTS):
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps(fr)), LOOP)


def pos_de(market_id):
    return next((p for p in POS.values() if p["market_id"] == market_id), None)


def fill_order(oid):
    time.sleep(MODO["fill_delay"])
    with LOCK:
        o = ORDERS[oid]
        o.update(status="filled", fill_price=PRICE["mid"])
        side = 1 if o["side"] == "buy" else -1
        p = pos_de(o["market_id"])
        emit({"type": "fill", "account_id": "acc1", "fill": {"id": "f%d" % next(ids), "order_id": oid,
                                                             "market_id": o["market_id"], "price": PRICE["mid"],
                                                             "size": o["size"]}})
        emit({"type": "order", "event": "filled", "account_id": "acc1", "order": dict(o)})
        time.sleep(0.05)
        if o.get("position_id"):               # cierre (total o parcial)
            p = POS.get(o["position_id"])
            p["size"] = round(p["size"] - o["size"], 6)
            if p["size"] <= 1e-9:
                POS.pop(p["id"])
                p.update(status="closed", realized_pnl=12.5, exit_price=PRICE["mid"], close_reason="manual")
                CLOSED[p["id"]] = p
                for x in [x for x in ORDERS.values() if x.get("reduce_only") and x["status"] == "resting"]:
                    x["status"] = "canceled"
                    emit({"type": "order", "event": "canceled", "account_id": "acc1", "order": dict(x),
                          "cancel_reason": "position_closed"})
                emit({"type": "position", "event": "closed", "account_id": "acc1", "position": dict(p),
                      "close_reason": "manual"})
            else:
                emit({"type": "position", "event": "changed", "account_id": "acc1", "position": dict(p)})
            return
        pid = "p%d" % next(ids)
        p = {"id": pid, "account_id": "acc1", "market_id": o["market_id"], "side": "long" if side == 1 else "short",
             "size": o["size"], "entry_price": PRICE["mid"], "status": "open"}
        POS[pid] = p
        for role, k in (("stop_loss", "stop_loss_price"), ("take_profit", "take_profit_price")):
            if o.get(k):
                x = {"id": "o%d" % next(ids), "account_id": "acc1", "market_id": o["market_id"], "reduce_only": True,
                     "status": "resting", "type": "stop" if role == "stop_loss" else "take_limit",
                     "trigger_price": o[k], "size": o["size"], "side": "sell" if side == 1 else "buy",
                     "bracket_role": role, "parent_order_id": oid}
                ORDERS[x["id"]] = x
                emit({"type": "order", "event": "created", "account_id": "acc1", "order": dict(x)})
        emit({"type": "position", "event": "opened", "account_id": "acc1", "position": dict(p)})


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, headers=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _key(self):
        return (self.headers.get("Authorization") or "")[7:]

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        k = self._key()
        with LOCK:
            t = time.time()
            GETS.append((t, k, u.path))
            n = sum(1 for x in GETS if x[1] == k and t - x[0] < 60)
        if MODO["429"]:
            return self._send(429, {"error": {"code": "rate_limited", "message": "slow down"}},
                              {"Retry-After": "2", "X-RateLimit-Scope": "api_key", "X-RateLimit-Limit": "30"})
        h = {"X-RateLimit-Warning": "see /v1/stream"} if n > MODO["warn_from"] else {}
        with LOCK:
            p = u.path
            if p == "/v1/markets":
                return self._send(200, {"data": MARKETS}, h)
            if p == "/v1/accounts":
                return self._send(200, {"data": [ACC]}, h)
            if p == "/v1/accounts/acc1":
                return self._send(200, {"data": ACC}, h)
            if p == "/v1/accounts/acc1/trading-policy":
                return self._send(200, {"data": POL}, h)
            if p.startswith("/v1/markets/") and p.endswith("/quote"):
                return self._send(200, {"data": {"mid": PRICE["mid"]}}, h)
            if p.startswith("/v1/markets/"):
                return self._send(200, {"data": MKT}, h)
            if p == "/v1/positions":
                if q.get("status") == "closed":
                    return self._send(200, {"data": list(CLOSED.values())}, h)
                return self._send(200, {"data": list(POS.values())}, h)
            if p == "/v1/orders":
                return self._send(200, {"data": [o for o in ORDERS.values() if o["status"] in ("resting", "pending")]}, h)
            m = re.match(r"^/v1/orders/(\w+)$", p)
            if m and m.group(1) in ORDERS:
                return self._send(200, {"data": ORDERS[m.group(1)]}, h)
        self._send(404, {"error": {"code": "not_found", "message": p}})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        p, b = urlparse(self.path).path, self._body()
        with LOCK:
            if p == "/v1/orders":
                oid = "o%d" % next(ids)
                o = dict(b, id=oid, status="pending")
                ORDERS[oid] = o
                threading.Thread(target=fill_order, args=(oid,), daemon=True).start()
                return self._send(201, {"data": dict(o)})
            m = re.match(r"^/v1/positions/(\w+)/close$", p)
            if m and m.group(1) in POS:
                pos = POS[m.group(1)]
                oid = "o%d" % next(ids)
                o = {"id": oid, "status": "pending", "market_id": pos["market_id"], "reduce_only": True,
                     "size": b.get("size") or pos["size"], "side": "sell" if pos["side"] == "long" else "buy",
                     "position_id": pos["id"]}
                ORDERS[oid] = o
                threading.Thread(target=fill_order, args=(oid,), daemon=True).start()
                return self._send(201, {"data": dict(o)})
        self._send(404, {"error": {"code": "not_found", "message": p}})

    def do_PUT(self):
        p, b = urlparse(self.path).path, self._body()
        m = re.match(r"^/v1/positions/(\w+)/exit-orders$", p)
        with LOCK:
            if m and m.group(1) in POS:
                for x in b.get("orders") or b.get("exit_orders") or []:
                    if x.get("order_id") in ORDERS:
                        ORDERS[x["order_id"]]["trigger_price"] = x.get("price")
                        emit({"type": "order", "event": "edited", "account_id": "acc1", "order": dict(ORDERS[x["order_id"]])})
                return self._send(200, {"data": {"ok": True}})
        self._send(404, {"error": {"code": "not_found", "message": p}})


async def ws_handler(ws):
    if not MODO["ws_on"]:
        await ws.close(1013)
        return
    path = ws.request.path if hasattr(ws, "request") else ws.path
    auth = (ws.request.headers.get("Authorization") if hasattr(ws, "request") else ws.request_headers.get("Authorization")) or ""
    if not auth.startswith("Bearer fp_test_"):
        await ws.close(4001)
        return
    STATS["ws_connects"] += 1
    resumed = "resume=" in path
    if resumed:
        STATS["resumes"] += 1
    CLIENTS.add(ws)
    try:
        await ws.send(json.dumps({"type": "hello", "stream_id": "s1", "environment": "sandbox", "resumed": resumed,
                                  "heartbeat_interval_ms": 15000}))
        if not resumed:
            with LOCK:
                fr = {"type": "snapshot", "account_id": "acc1", "account": ACC,
                      "orders": [o for o in ORDERS.values() if o["status"] in ("resting", "pending")],
                      "positions": list(POS.values()), "latest_fill": None, "seq": next(SEQ), "time": now()}
            await ws.send(json.dumps(fr))
            await ws.send(json.dumps({"type": "snapshot_end"}))
        async def hb():
            try:
                while True:
                    await asyncio.sleep(3)
                    await ws.send(json.dumps({"type": "heartbeat", "time": now()}))
            except Exception:
                pass
        t = asyncio.create_task(hb())
        async for msg in ws:
            m = json.loads(msg)
            if m.get("op") == "ping":
                await ws.send(json.dumps({"type": "pong", "id": m.get("id"), "time": now()}))
        t.cancel()
    except Exception:
        pass
    finally:
        CLIENTS.discard(ws)


def cortar_ws():
    for ws in list(CLIENTS):
        asyncio.run_coroutine_threadsafe(ws.close(1012), LOOP)


def arrancar(puerto_rest=8096, puerto_ws=8095):
    global LOOP
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", puerto_rest), H).serve_forever, daemon=True).start()

    def ws_loop():
        global LOOP
        LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(LOOP)

        async def main():
            async with websockets.serve(ws_handler, "127.0.0.1", puerto_ws):
                await asyncio.Future()
        LOOP.run_until_complete(main())
    threading.Thread(target=ws_loop, daemon=True).start()
    time.sleep(0.5)


def gets(desde, clave=None):
    with LOCK:
        return [x for x in GETS if x[0] >= desde and (clave is None or x[1] == clave)]
