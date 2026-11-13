"""The demo pack: a toy PSP (fake.py) and a toy shop with a race (shop.py), so raceproof can be tried without a PSP pack.

Start both with `raceproof demo`, then run the scenarios with `raceproof menu --config raceproof-demo.yaml`.
"""
import os

NAME = "demo"
TITLE = "demo PSP"
SCENARIOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")
FAKE_APP = "raceproof.demo.fake:app"
FAKE_PORT = 9300
SHOP_APP = "raceproof.demo.shop:app"
SHOP_PORT = 9301
SCENARIO_GROUPS = "R"


def is_stray(c, trans_id):
    """A completion of ANOTHER payment than this run's."""
    pid = (c.get("body") or {}).get("payment")
    return bool(trans_id and pid and pid != trans_id)


def describe_call(c):
    body = c.get("body") or {}
    if c["path"] == "/payments/complete":
        return f"POST /payments/complete {body.get('payment')} result={body.get('result')} (key {(c.get('key') or '')[:12]})"
    return f"POST {c['path']}"


def compute(c):
    raise ValueError("the demo pack has no compute builders")


def error_code(text):
    import json
    try:
        return (json.loads(text) or {}).get("code")
    except (ValueError, AttributeError):
        return None


def after_http(h, r):
    return None


def _label(c):
    result = (c.get("body") or {}).get("result")
    return {"authorised": "result", "abandoned": "timeout"}.get(result)


RACE = {
    "path": "/payments/complete",
    "label": _label,
    "posted": "result",
    "rival": "timeout",
    "rival_text": "the shop's timeout",
    "what": "completions",
    "starts_clock": lambda h, body: "/result/" in h.get("url", ""),
}
