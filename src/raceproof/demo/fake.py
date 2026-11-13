"""The demo PSP: a toy payment API with idempotency keys, enough to show a race and the control API every pack offers.

  POST /payments                {"reference", "amount"} -> {"id", "status": "action_required"} (the shopper must confirm)
  POST /payments/complete       {"payment", "result": "authorised" | "abandoned"}, header Idempotency-Key
                                -> 200 {"id", "status": "authorised" | "refused"}; the answer takes `latency_ms`.
                                The same key while the first call is in progress -> 409 {"code": "in_progress"}; after it
                                -> the first answer replayed. A new key for a final payment -> 422 {"code": "already_final"}.
  GET  /payments/{id}           {"id", "status"}

Control API (used by the runner): POST /_control/reset, POST /_control/script {"latency_ms": N},
GET /_control/calls?path=/payments/complete -> [{"t", "path", "body", "key"}].
This toy is not modelled on any real PSP.
"""
import asyncio
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DEFAULT_LATENCY_MS = 150


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.payments = {}
        self.keys = {}            # idempotency key -> None while in progress, else (status code, body)
        self.calls = []
        self.latency_ms = DEFAULT_LATENCY_MS


S = State()


def _log(path, body, key=None):
    S.calls.append({"t": time.time(), "path": path, "body": body, "key": key})


async def create(request: Request):
    body = await request.json()
    _log("/payments", body)
    pid = "pay_" + uuid.uuid4().hex[:16]
    S.payments[pid] = {"id": pid, "status": "action_required", "reference": body.get("reference")}
    return JSONResponse({"id": pid, "status": "action_required"}, status_code=201)


async def complete(request: Request):
    body = await request.json()
    key = request.headers.get("idempotency-key")
    _log("/payments/complete", body, key)
    if key and key in S.keys:
        done = S.keys[key]
        if done is None:
            return JSONResponse({"code": "in_progress", "message": "a request with this key is in progress"}, status_code=409)
        return JSONResponse(done[1], status_code=done[0])
    if key:
        S.keys[key] = None
    await asyncio.sleep(S.latency_ms / 1000)
    p = S.payments.get(body.get("payment"))
    if p is None:
        answer = (404, {"code": "not_found", "message": "no such payment"})
    elif p["status"] != "action_required":
        answer = (422, {"code": "already_final", "status": p["status"]})
    else:
        p["status"] = "authorised" if body.get("result") == "authorised" else "refused"
        answer = (200, {"id": p["id"], "status": p["status"]})
    if key:
        S.keys[key] = answer
    return JSONResponse(answer[1], status_code=answer[0])


async def get_payment(request: Request):
    p = S.payments.get(request.path_params["pid"])
    return JSONResponse(p, status_code=200) if p else JSONResponse({"code": "not_found"}, status_code=404)


async def reset(request: Request):
    S.reset()
    return JSONResponse({"ok": True})


async def script(request: Request):
    body = await request.json()
    if "latency_ms" in body:
        S.latency_ms = int(body["latency_ms"])
    return JSONResponse({"ok": True, "latency_ms": S.latency_ms})


async def calls(request: Request):
    path = request.query_params.get("path")
    return JSONResponse([c for c in S.calls if not path or c["path"] == path])


app = Starlette(routes=[
    Route("/payments", create, methods=["POST"]),
    Route("/payments/complete", complete, methods=["POST"]),
    Route("/payments/{pid}", get_payment, methods=["GET"]),
    Route("/_control/reset", reset, methods=["POST"]),
    Route("/_control/script", script, methods=["POST"]),
    Route("/_control/calls", calls, methods=["GET"]),
])
