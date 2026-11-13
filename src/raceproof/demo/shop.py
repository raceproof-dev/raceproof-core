"""The demo shop: a toy payment gateway with a real race, for trying raceproof without a PSP pack.

  POST /pay                     {"reference"} -> {"id", "status"}: starts a payment at the demo PSP; the shopper must
                                confirm it. If no result comes within `timeout_ms`, the shop's timeout gives up and tells
                                the PSP the payment was abandoned.
  POST /result/{id}             {"result": "authorised"}: the shopper's result, sent on to the PSP.
  GET/PUT /_demo/fixes          {"race": true|false}: switch the fix on or off at runtime.

Both paths send their result to the PSP with the same idempotency key and record the answer. They first check that the
payment is not final yet, but that check and the PSP call are not atomic: when the shopper's result arrives as the
timeout fires, both pass the check. The PSP answers the loser 409 ("a request with this key is in progress").

  fix off: the loser records the 409 as FAILED and tells the merchant; the winner tells the merchant too.
  fix on:  a 409 means the other path owns the step: the loser changes nothing, and only the first final answer counts.

Settings (environment): RACEPROOF_DEMO_PSP (default http://127.0.0.1:9300), RACEPROOF_DEMO_DB (SQLite file, default
raceproof-demo.db), RACEPROOF_DEMO_TIMEOUT_MS (default 2000), RACEPROOF_DEMO_FIX (default true).
"""
import asyncio
import os
import sqlite3

import requests
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PSP = os.environ.get("RACEPROOF_DEMO_PSP", "http://127.0.0.1:9300").rstrip("/")
DB = os.environ.get("RACEPROOF_DEMO_DB", "raceproof-demo.db")
TIMEOUT_MS = int(os.environ.get("RACEPROOF_DEMO_TIMEOUT_MS", "2000"))
FIXES = {"race": os.environ.get("RACEPROOF_DEMO_FIX", "true").lower() != "false"}
FINAL = ("COMPLETED", "REFUSED", "FAILED")


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS notifications (payment_id TEXT, status TEXT)")
    return con


def status_of(pid):
    con = db()
    try:
        row = con.execute("SELECT status FROM payments WHERE id = ?", (pid,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def record(pid, status):
    """Store the final status and tell the merchant. With the fix, only the first final status counts."""
    con = db()
    try:
        if FIXES["race"]:
            changed = con.execute("UPDATE payments SET status = ? WHERE id = ? AND status = 'PENDING'", (status, pid)).rowcount
        else:
            changed = con.execute("UPDATE payments SET status = ? WHERE id = ?", (status, pid)).rowcount
        if changed:
            con.execute("INSERT INTO notifications VALUES (?, ?)", (pid, status))     # "the merchant is told"
        con.commit()
    finally:
        con.close()


async def submit(pid, result):
    """One path (the shopper's result or the timeout) sends its result to the PSP and records the answer."""
    if status_of(pid) in FINAL:            # check ...
        return
    r = await asyncio.to_thread(requests.post, f"{PSP}/payments/complete", json={"payment": pid, "result": result},
                                headers={"Idempotency-Key": f"{pid}-complete"}, timeout=30)     # ... then act
    if r.status_code == 409:
        if FIXES["race"]:
            return                         # the other path submitted this step; its answer decides
        record(pid, "FAILED")
        return
    if r.status_code == 200:
        record(pid, "COMPLETED" if r.json()["status"] == "authorised" else "REFUSED")


async def timeout(pid):
    await asyncio.sleep(TIMEOUT_MS / 1000)
    await submit(pid, "abandoned")


async def pay(request: Request):
    body = await request.json()
    r = await asyncio.to_thread(requests.post, f"{PSP}/payments", json={"reference": body.get("reference"), "amount": 1000},
                                timeout=30)
    if r.status_code != 201:
        return JSONResponse({"error": "the PSP refused the payment", "psp_status": r.status_code}, status_code=502)
    pid = r.json()["id"]
    con = db()
    try:
        con.execute("INSERT INTO payments VALUES (?, 'PENDING')", (pid,))
        con.commit()
    finally:
        con.close()
    asyncio.get_running_loop().create_task(timeout(pid))
    return JSONResponse({"id": pid, "status": "PENDING"}, status_code=201)


async def result(request: Request):
    pid = request.path_params["pid"]
    body = await request.json()
    await submit(pid, body.get("result", "authorised"))
    return JSONResponse({"id": pid, "status": status_of(pid)})


async def fixes(request: Request):
    if request.method == "PUT":
        for k, v in (await request.json()).items():
            if k in FIXES:
                FIXES[k] = bool(v)
    return JSONResponse(FIXES)


app = Starlette(routes=[
    Route("/pay", pay, methods=["POST"]),
    Route("/result/{pid}", result, methods=["POST"]),
    Route("/_demo/fixes", fixes, methods=["GET", "PUT"]),
])
