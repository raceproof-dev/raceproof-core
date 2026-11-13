"""Scenario runner.

Usage:  raceproof run <scenario.yaml | id, e.g. R1> [--config lab.yaml] [--fake URL] [--db DSN] [--repeat N] [--trace] [--sweep PATH=start:stop:step] [--max-inconclusive PCT]

`precondition: true` on a wait marks it as 'the environment must be ready before the scenario means anything'
(wait_calls: default true; wait_status / wait_sql: default false). If it times out the run is INCONCLUSIVE, not failed: it is
reported separately and left out of the race statistics. Too many inconclusive runs (> --max-inconclusive, default 20 %)
make the exit code 3, so a stalled environment can never look like a pass.

Sweep:  --sweep timing.near_watchdog_ms=4600:6400:200   runs the scenario for every value (start:stop:step, stop included),
`--repeat` = rounds per value, values interleaved in random order so drift hits all of them equally, then prints one table
(runs, failures, how many runs got two submissions / fell inside the race window per value) and the best value.

PSP: lab.yaml's `psp:` (default demo) picks the installed PSP pack (its fake, `compute` builders, how its calls are
read). A scenario from a pack's own scenario folder only runs with that psp. See raceproof/psps.py.

Config (lab.yaml, see the pack's example config): everything specific to YOUR system lives there, never in a scenario:
  gateway: {base_url, start_payment_url, method_url, challenge_url, webhook_url}
  payloads: {start_payment: path}   db: {dsn, table, id_column, status_column, key, refusal_marker: {table, id_column, reconciled_column?}}
  status: {pending_challenge: <value stored in your DB>, refused: ..., completed: ...}   timing: {watchdog_ms}
  queries: {<name>: "SQL returning one value" | {sql, dsn?}}   how to read a fact from YOUR system, e.g.
           merchant_final_count = how many final statuses the merchant was told about for this transaction
Scenarios refer to it as ${gateway.method_url}, ${timing.watchdog_ms}, ... to statuses by logical name, and to SQL by
query name (`named: merchant_final_count`), so a scenario never contains your table names.

Scenario keys: name, repeat, jitter_ms: [min, max], vars: {k: v}, max_inconclusive_pct (default 20), steps: [...], assert: [...]
  (needs, run_all, run_all_note, settle_after_s are for the menu, lab.py, and ignored here)
Every string may use ${var} or ${section.key} (config values and scenario `vars`). An undefined one is an error. Built-in vars: ${fake}, ${run_id}, ${run_seq} (5 digits, fits inside a UUID). `save` and `compute` add more.

Steps
  - reset: {}                                   clear the fake's state
  - script: {payments: [...], fingerprint: [...], challenge: [...]}
  - http: {name, method?, url, headers?, json? | json_file?, form?, expect?: 2xx | 201 | [200, 201], save?: {var: dotted.path}}
                                                expect: fail the run right here, naming the step, status and body, if the status differs
                                                (steps that `save` fail on any status >= 400 automatically)
                                                json_file: path relative to the scenario file; ${var} (e.g. ${run_id}) works inside it
  - compute: {var: x, <builder>: {...}}        a value built by the PSP pack, e.g. a signed
                                                challenge result (the builders: the pack's module)
  - sleep_ms: 500
  - sql: {name, query | named, dsn?}            first column of first row -> results[name]
                                                named: a query from lab.yaml `queries:` instead of inline SQL (its dsn too),
                                                in sql, wait_sql and sql_equals alike
                                                dsn (sql, wait_sql, sql_equals): another database, e.g. "${db.other_dsn}"
                                                (a config value such as env:OTHER_DB_DSN); default db.dsn
  - wait_calls: {path: /payments/details, min: 1, timeout_ms?, precondition?: true}   wait until the FAKE has logged that many calls to the path
                                                                  (e.g. your app sent the fingerprint and got ChallengeShopper)
  - wait_sql: {query | named, equals, timeout_ms?, interval_ms?, dsn?}
  - wait_status: {is: refused, timeout_ms?, interval_ms?, precondition?: false}   wait until the transaction's DB status equals the value
                                                              that config `status.refused` maps to (needs db + trans_id var)
  - parallel: [step, step, ...]                 children start at independent random offsets (jitter_ms)
  - sequence: [step, ...], jitter_ms?: [a, b]   (as a parallel child) runs its steps in order after ONE offset; its own
                                                jitter_ms overrides the scenario's, e.g. [0, 0] to start right away

Assertions
  - calls: {path: /payments/details, max|min|equals: N}
  - status_counts: {names: [a, b], counts: {200: 1, 422: 1}}
  - error_codes: {names: [a, b], codes: ["704"]}   errorCode values found across those responses
  - sql_equals: {query: "...", value: X, dsn?}  or {named: <lab.yaml query>, value: X}  or {name: <earlier sql step>, value: X}
  - tx_status: {is: refused}  or  {in: [refused, completed]}   logical names from config `status`
  - refusal_marker: {count: 0}                  rows in config db.refusal_marker.table for this transaction (default expectation 0)
  - refusal_marker: {unreconciled: 0}           ...of those, rows whose db.refusal_marker.reconciled_column is still NULL
"""
import argparse
import asyncio
import collections
import itertools
import json
import os
import random
import re
import sys
import time

import requests
import yaml

from raceproof import psps


_VAR = re.compile(r"\$\{([\w.]+)\}")
_MISSING = object()


class Inconclusive(Exception):
    """A precondition of the scenario was not met (the environment was not ready), so this run proves nothing either way."""


def get_var(vars_, name):
    cur = vars_
    for part in name.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def subst(obj, vars_):
    if isinstance(obj, str):
        for _ in range(4):          # a config value may itself contain ${...}, e.g. method_url: ${gateway.base_url}/x
            def one(m):
                v = get_var(vars_, m.group(1))
                return m.group(0) if v is _MISSING or isinstance(v, (dict, list)) or v is None else str(v)
            new = _VAR.sub(one, obj)
            if new == obj:
                break
            obj = new
        return obj
    if isinstance(obj, dict):
        return {k: subst(v, vars_) for k, v in obj.items()}
    if isinstance(obj, list):
        return [subst(v, vars_) for v in obj]
    return obj


def check_defined(obj, where=""):
    left = sorted(set(_VAR.findall(json.dumps(obj))))
    if left:
        raise ValueError(f"undefined variable(s) {', '.join('${' + n + '}' for n in left)}{where}: "
                         f"set them in lab.yaml (see lab.example.yaml) or in the scenario's `vars`")


def psp_for(config):
    """The PSP pack that lab.yaml's `psp:` names (default demo)."""
    return psps.load((config or {}).get("psp"))


def scenario_psp(path):
    """The pack a scenario belongs to (it lies in that pack's scenario folder), else None."""
    return psps.owner_of(path)


def duplicate_statuses(cfg):
    """{db value: [logical names]} for values that more than one logical status maps to."""
    seen = {}
    for name, val in (cfg.get("status") or {}).items():
        seen.setdefault(str(val), []).append(name)
    return {v: n for v, n in seen.items() if len(n) > 1}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    base = os.path.dirname(os.path.abspath(path))
    for k, v in (cfg.get("payloads") or {}).items():          # payload paths are relative to the config file
        if v and not os.path.isabs(v):
            cfg["payloads"][k] = os.path.join(base, v)
    t = cfg.setdefault("timing", {})
    wd = int(t.get("watchdog_ms", 5000))
    t.setdefault("watchdog_ms", wd)
    t.setdefault("near_watchdog_ms", max(wd - 300, 0))        # "just before the watchdog fires"
    t.setdefault("wait_watchdog_ms", wd + 15000)              # how long to wait for the watchdog to act
    return cfg


_IDENT = re.compile(r"^[A-Za-z_][\w.]*$")


def lookup(obj, dotted):
    for part in dotted.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


_SEQ = itertools.count(int(time.time()) % 100000)     # 5-digit run counter, unique per run in this process


class Run:
    def __init__(self, scenario, fake, db, config=None, psp=None):
        self.s, self.fake, self.db = scenario, fake.rstrip("/"), db
        self.psp = psp or psp_for(config)
        self.results: dict = {}
        self.events: list = []
        self.strays = 0
        self.cres_posted_at = None      # when the runner told the fake to post the shopper's CRes to the gateway
        self.vars = {**(config or {}), **scenario.get("vars", {}), "fake": self.fake,
                     "run_id": f"{int(time.time() * 1000)}-{random.randint(0, 9999)}",
                     "run_seq": f"{next(_SEQ) % 100000:05d}"}

    async def step(self, st, jitter=None):
        if "sequence" in st:            # a parallel child that runs steps in order; its own jitter_ms overrides the scenario's
            jitter = st.get("jitter_ms", jitter)
        if jitter:
            await asyncio.sleep(random.uniform(*jitter) / 1000)
        if "sequence" in st:            # substituted step by step, so later steps can use vars saved by earlier ones
            for x in st["sequence"]:
                await self.step(x)
            return
        if "parallel" in st:            # children substitute themselves when they start
            jit = self.s.get("jitter_ms", [0, 0])
            await asyncio.gather(*(self.step(x, jitter=jit) for x in st["parallel"]))
            return
        st = subst(st, self.vars)
        check_defined(st, f" in step {list(st)[0]}")
        self._mark(f"start {self._label(st)}")
        if "reset" in st:
            await asyncio.to_thread(requests.post, f"{self.fake}/_control/reset", timeout=30)
        elif "script" in st:
            await asyncio.to_thread(requests.post, f"{self.fake}/_control/script", json=st["script"], timeout=30)
        elif "sleep_ms" in st:
            await asyncio.sleep(int(st["sleep_ms"]) / 1000)
        elif "http" in st:
            h = st["http"]
            body = h.get("json")
            race = self.psp.RACE
            if self.cres_posted_at is None and race and race["starts_clock"](h, body):
                self.cres_posted_at = time.time()
            if "json_file" in h:        # request body kept in its own file, ${vars} substituted per run
                with open(os.path.join(self.s.get("_dir", "."), h["json_file"]), encoding="utf-8") as f:
                    body = subst(json.load(f), self.vars)
                check_defined(body, f" in {h['json_file']}")
            r = await asyncio.to_thread(
                requests.request, h.get("method", "POST"), h["url"], headers=h.get("headers"),
                json=body, data=h.get("form"), timeout=h.get("timeout", 30))
            self.results[h["name"]] = {"status": r.status_code, "text": r.text}
            # the fake's own /_control/ API refusing a call is a lab misconfiguration (e.g. no FAKE_HMAC_KEY), never a result
            exp = h.get("expect", "4xx5xx-is-error" if h.get("save") or "/_control/" in h["url"] else None)
            if exp is not None:
                if exp == "4xx5xx-is-error":
                    ok = r.status_code < 400
                elif isinstance(exp, str) and exp.endswith("xx"):
                    ok = str(r.status_code)[0] == exp[0]
                else:
                    ok = r.status_code in ([exp] if isinstance(exp, int) else [int(x) for x in exp])
                if not ok:
                    want = "a status < 400" if exp == "4xx5xx-is-error" else exp
                    raise RuntimeError(f"step {h['name']!r}: {h.get('method', 'POST')} {h['url']} returned HTTP "
                                       f"{r.status_code}, expected {want}. Response: {r.text[:300]!r}")
            self.psp.after_http(h, r)       # e.g. the app rejected a webhook the fake signed: a lab misconfiguration
            for var, path in (h.get("save") or {}).items():
                self.vars[var] = lookup(r.json(), path)
        elif "compute" in st:           # the pack's own builders
            c = st["compute"]
            self.vars[c["var"]] = self.psp.compute(c)
        elif "sql" in st:
            s = self._named(st["sql"], f"sql step {st['sql'].get('name')!r}")
            db = self._db_for(s)
            self.results[s["name"]] = await asyncio.to_thread(db, s["query"])
        elif "wait_sql" in st:
            w = self._named(st["wait_sql"], "wait_sql")
            db = self._db_for(w)
            deadline = time.time() + int(w.get("timeout_ms", 15000)) / 1000
            while True:
                got = await asyncio.to_thread(db, w["query"])
                if str(got) == str(w["equals"]):
                    break
                if time.time() > deadline:
                    what = f" {w['named']}" if "named" in w else ""
                    raise (Inconclusive if w.get("precondition") else TimeoutError)(
                        f"wait_sql{what}: wanted {w['equals']!r}, last saw {got!r}")
                await asyncio.sleep(w.get("interval_ms", 200) / 1000)
        elif "wait_calls" in st:
            w = st["wait_calls"]
            deadline = time.time() + int(w.get("timeout_ms", 15000)) / 1000
            while True:
                resp = await asyncio.to_thread(requests.get, f"{self.fake}/_control/calls", params={"path": w["path"]}, timeout=30)
                n = len(resp.json())
                if n >= int(w.get("min", 1)):
                    break
                if time.time() > deadline:
                    raise (Inconclusive if w.get("precondition", True) else TimeoutError)(
                        f"wait_calls: wanted {w.get('min', 1)} call(s) to {w['path']} at the fake, saw {n}. "
                        f"Your app has not sent that request to the fake (check its {self.psp.TITLE} base URL and its log)")
                await asyncio.sleep(int(w.get("interval_ms", 100)) / 1000)
        elif "wait_status" in st:
            self._need_db()
            w = st["wait_status"]
            want, query = self._status_value(w["is"]), self._status_query()
            deadline = time.time() + int(w.get("timeout_ms", 15000)) / 1000
            while True:
                got = await asyncio.to_thread(self.db, query)
                if str(got) == str(want):
                    break
                if time.time() > deadline:
                    raise (Inconclusive if w.get("precondition") else TimeoutError)(
                        f"wait_status: wanted {w['is']} ({want!r}), last saw {self._status_name(got)}")
                await asyncio.sleep(int(w.get("interval_ms", 200)) / 1000)
        else:
            raise ValueError(f"unknown step: {list(st)}")
        self._mark(f"done  {self._label(st)}")

    def _mark(self, label):
        self.events.append((time.time(), label))

    @staticmethod
    def _label(st):
        k = next(iter(st))
        v = st[k]
        if k == "http":
            return f"http {v.get('name')}"
        if k == "sleep_ms":
            return f"sleep {v} ms"
        if k in ("wait_status", "wait_calls", "wait_sql"):
            return f"{k} {v.get('is') or v.get('path') or v.get('named') or ''}".rstrip()
        return k

    async def challenge_pattern(self):
        """What reached the fake as a challenge submission in this run: [(label, t), ...] in time order."""
        race = self.psp.RACE
        self.strays = 0
        if not race:
            return []
        calls = (await asyncio.to_thread(requests.get, f"{self.fake}/_control/calls", params={"path": race["path"]},
                                         timeout=30)).json()
        out, mine = [], str(self.vars.get("trans_id", ""))
        for c in calls:
            if self.psp.is_stray(c, mine):     # a late call from some OTHER transaction (earlier run) landed in this log
                self.strays += 1
                continue
            kind = race["label"](c)
            if kind:
                out.append((kind, c["t"]))
        return sorted(out, key=lambda x: x[1])

    async def trace(self):
        """Timeline of this run: what the runner did and what the fake received, on one clock (--trace)."""
        calls = (await asyncio.to_thread(requests.get, f"{self.fake}/_control/calls", timeout=30)).json()
        rows = [(t, "runner", lbl) for t, lbl in self.events if not lbl.startswith("done  parallel")]
        for c in calls:
            rows.append((c["t"], "FAKE", self.psp.describe_call(c)))
        rows.sort(key=lambda r: r[0])
        t0 = rows[0][0] if rows else 0
        print("\n  timeline (seconds from the first event):")
        for t, who, lbl in rows:
            print(f"   +{t - t0:7.3f}  {who:<6} {lbl}")

    def _need_db(self):
        if not self.db:
            raise RuntimeError("this scenario uses SQL; set db.dsn in lab.yaml or pass --db")

    def _named(self, spec, where):
        """`named: X` -> the same spec with the query (and dsn, unless the spec sets one) from lab.yaml `queries.X`."""
        if "named" not in spec:
            return spec
        name = spec["named"]
        queries = self.vars.get("queries") or {}
        if name not in queries:
            raise ValueError(f"{where} uses named query {name!r}, but lab.yaml has no queries.{name} "
                             f"(see lab.example.yaml; known: {sorted(queries) or 'none'})")
        q = queries[name]
        q = {"sql": q} if isinstance(q, str) else dict(q or {})
        if not q.get("sql"):
            raise ValueError(f"lab.yaml queries.{name} has no sql")
        out = {**spec, "query": subst(q["sql"], self.vars)}
        if q.get("dsn") and not spec.get("dsn"):
            out["dsn"] = subst(q["dsn"], self.vars)
        check_defined(out, f" in lab.yaml queries.{name}")
        return out

    def _db_for(self, spec):
        """The query function for a sql / wait_sql / sql_equals: its own `dsn` (another service's database), else db.dsn."""
        if spec.get("dsn"):
            return get_db(resolve_dsn(spec["dsn"]))
        self._need_db()
        return self.db

    def _status_value(self, name):
        m = self.vars.get("status") or {}
        if name not in m:
            raise ValueError(f"unknown status {name!r}; add it under `status:` in lab.yaml (known: {sorted(m)})")
        return m[name]

    def _status_name(self, got):
        """DB value -> 'value (logical name)', or flag a value that is not in the configured status list."""
        for k, v in (self.vars.get("status") or {}).items():
            if str(v) == str(got):
                return f"{got!r} ({k})"
        return f"{got!r} (NOT in the configured status list)"

    def _status_query(self):
        d = self.vars.get("db") or {}
        miss = [k for k in ("table", "id_column", "status_column") if not d.get(k)]
        if miss:
            raise ValueError(f"lab.yaml db.{', db.'.join(miss)} missing (needed for wait_status / tx_status)")
        for k in ("table", "id_column", "status_column"):
            if not _IDENT.match(str(d[k])):
                raise ValueError(f"db.{k} {d[k]!r} is not a plain identifier")
        # The value to look the row up by. Default: the id the fake generated (threeDSServerTransID). If your table keys on
        # something else (your own transactionId, the merchantReference), set db.key, e.g. "${ids.transaction_id}" or "${reference}".
        tid = self._tx_key()
        return f"SELECT {d['status_column']} FROM {d['table']} WHERE {d['id_column']} = '{tid}'"

    def _tx_key(self):
        d = self.vars.get("db") or {}
        tid = subst(str(d.get("key") or "${trans_id}"), self.vars)
        if _VAR.search(tid) or not tid or "'" in tid:
            raise ValueError(f"db.key resolved to {tid!r}: it must be a plain value. Define the variable it names "
                             f"(`trans_id` and `reference` are saved by the templates' txn step; `ids.*` come from lab.yaml)")
        return tid

    def _marker_query(self, unreconciled=False):
        m = (self.vars.get("db") or {}).get("refusal_marker") or {}
        need = ("table", "id_column") + (("reconciled_column",) if unreconciled else ())
        miss = [k for k in need if not m.get(k)]
        if miss:
            raise ValueError(f"lab.yaml db.refusal_marker.{', db.refusal_marker.'.join(miss)} missing (needed for refusal_marker)")
        for k in need:
            if not _IDENT.match(str(m[k])):
                raise ValueError(f"db.refusal_marker.{k} {m[k]!r} is not a plain identifier")
        q = f"SELECT count(*) FROM {m['table']} WHERE {m['id_column']} = '{self._tx_key()}'"
        return q + f" AND {m['reconciled_column']} IS NULL" if unreconciled else q

    async def execute(self):
        for st in self.s["steps"]:
            await self.step(st)

    async def check(self):
        failures = []
        for a in self.s.get("assert", []):
            a = subst(a, self.vars)
            check_defined(a, " in an assertion")
            if "refusal_marker" in a:
                mdsn = ((self.vars.get("db") or {}).get("refusal_marker") or {}).get("dsn")
                if not mdsn:
                    self._need_db()
                rm = a["refusal_marker"] or {}
                mdb = get_db(resolve_dsn(mdsn)) if mdsn else self.db     # the marker may live in another service's database
                if "unreconciled" in rm:   # markers no late PSP report has reconciled yet (needs db.refusal_marker.reconciled_column)
                    want = int(rm["unreconciled"])
                    got = await asyncio.to_thread(mdb, self._marker_query(unreconciled=True))
                    if int(got or 0) != want:
                        failures.append(f"unreconciled refusal markers for this transaction = {got}, expected {want}")
                if "count" in rm or "unreconciled" not in rm:
                    want = int(rm.get("count", 0))
                    got = await asyncio.to_thread(mdb, self._marker_query())
                    if int(got or 0) != want:
                        failures.append(f"refusal markers for this transaction = {got}, expected {want}")
            elif "tx_status" in a:
                self._need_db()
                e = a["tx_status"]
                names = e["in"] if "in" in e else [e["is"]]
                want = [str(self._status_value(n)) for n in names]
                got = await asyncio.to_thread(self.db, self._status_query())
                if str(got) not in want:
                    failures.append(f"tx status = {self._status_name(got)}, expected {' or '.join(names)} ({', '.join(want)})")
            elif "calls" in a:
                c = a["calls"]
                r = await asyncio.to_thread(requests.get, f"{self.fake}/_control/calls",
                                            params={"path": c["path"]}, timeout=30)
                # calls of OTHER transactions (stragglers from an earlier run) do not count
                n = sum(1 for x in r.json() if not self.psp.is_stray(x, str(self.vars.get("trans_id", ""))))
                if "max" in c and n > c["max"]: failures.append(f"{c['path']} called {n} times > max {c['max']}")
                if "min" in c and n < c["min"]: failures.append(f"{c['path']} called {n} times < min {c['min']}")
                if "equals" in c and n != c["equals"]: failures.append(f"{c['path']} called {n} times != {c['equals']}")
            elif "status_counts" in a:
                sc = a["status_counts"]
                got = dict(collections.Counter(self.results[n]["status"] for n in sc["names"]))
                want = {int(k): v for k, v in sc["counts"].items()}
                if got != want: failures.append(f"status counts {got} != {want}")
            elif "error_codes" in a:
                ec = a["error_codes"]
                got = [code for code in (self.psp.error_code(self.results[n]["text"]) for n in ec["names"]) if code is not None]
                want = sorted(str(c) for c in ec["codes"])
                if sorted(got) != want: failures.append(f"errorCodes {sorted(got)} != {want}")
            elif "sql_equals" in a:
                e = self._named(a["sql_equals"], "sql_equals")
                if "query" in e:
                    got = await asyncio.to_thread(self._db_for(e), e["query"])
                    label = e.get("named") or e["query"]
                else:
                    got, label = self.results.get(e["name"]), e["name"]
                if str(got) != str(e["value"]): failures.append(f"{label} = {got!r}, expected {e['value']!r}")
            else:
                failures.append(f"unknown assertion {list(a)}")
        return failures


def resolve_dsn(dsn):
    """`env:NAME` -> the value of that environment variable (keeps passwords out of lab.yaml)."""
    if dsn and dsn.startswith("env:"):
        v = os.environ.get(dsn[4:])
        if not v:
            raise ValueError(f"environment variable {dsn[4:]} is not set")
        return v
    return dsn


def mask_dsn(dsn):
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", dsn or "")


_DBS = {}


def get_db(dsn):
    """Cached query function for a DSN (one per distinct database)."""
    if dsn not in _DBS:
        _DBS[dsn] = make_db(dsn)
    return _DBS[dsn]


def config_dsns(config, override=None):
    """[(where it is configured, raw dsn)] for every database the config mentions."""
    d = config.get("db") or {}
    out = []
    if override or d.get("dsn"):
        out.append(("db.dsn" if not override else "--db", override or d["dsn"]))
    m = d.get("refusal_marker") or {}
    if m.get("dsn"):
        out.append(("db.refusal_marker.dsn", m["dsn"]))
    for name, q in (config.get("queries") or {}).items():      # a named query may read another service's database
        if isinstance(q, dict) and q.get("dsn"):
            raw = subst(q["dsn"], config)
            if raw not in [r for _, r in out]:
                out.append((f"queries.{name}.dsn", raw))
    return out


def make_db(dsn):
    """sqlite:///path.db or a postgresql:// DSN. Returns fn(query) -> first column of first row."""
    if not dsn:
        return None
    if dsn.startswith("sqlite:///"):
        import sqlite3
        path = dsn[len("sqlite:///"):]

        def q(query):
            con = sqlite3.connect(path)
            try:
                row = con.execute(query).fetchone()
                con.commit()      # a query may write (e.g. an UPDATE ... RETURNING); psycopg below commits on exit
                return row[0] if row else None
            finally:
                con.close()
        return q
    import psycopg  # pip install "psycopg[binary]"

    def q(query):
        with psycopg.connect(dsn) as con:
            row = con.execute(query).fetchone()
            return row[0] if row else None
    return q


def set_path(config, dotted, value):
    """Deep-copy `config` and set config[a][b] = value for 'a.b'."""
    import copy
    cfg = copy.deepcopy(config)
    cur = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    return cfg


def parse_sweep(spec):
    try:
        name, rng = spec.split("=", 1)
        start, stop, step = (int(x) for x in rng.split(":"))
        assert step > 0 and stop >= start and name.strip()
    except Exception:  # noqa: BLE001
        sys.exit(f"--sweep expects PATH=start:stop:step with whole numbers, e.g. timing.near_watchdog_ms=4600:6400:200 (got {spec!r})")
    values = list(range(start, stop + 1, step))
    if len(values) > 60:
        sys.exit(f"--sweep would run {len(values)} values; use a larger step (max 60 values)")
    return name.strip(), values


async def run_once(scenario, fake_url, db, config, trace=False, label=None, psp=None):
    """One run -> a plain dict of everything the reports need."""
    run = Run(scenario, fake_url, db, config, psp)
    rival = (run.psp.RACE or {}).get("rival")
    res = {"fails": [], "inconclusive": None, "pat": [], "strays": 0, "offset": None, "key": None, "fake_id": None, "value": label}
    try:
        await run.execute()
        res["fails"] = await run.check()
    except Inconclusive as e:
        res["inconclusive"] = str(e)
    except Exception as e:  # noqa: BLE001
        res["fails"] = [f"exception: {e}"]
    try:
        res["pat"] = await run.challenge_pattern()
        res["strays"] = run.strays
        u = next((t for k, t in res["pat"] if k == rival), None)
        if u is not None and run.cres_posted_at is not None:
            res["offset"] = u - run.cres_posted_at          # > 0: the CRes was posted BEFORE the watchdog's U reached the fake
    except Exception:  # noqa: BLE001 (the fake may be down; the run already reports that)
        pass
    if trace:
        await run.trace()
    d = (run.vars.get("db") or {}) if isinstance(run.vars.get("db"), dict) else {}
    try:
        res["key"] = subst(str(d.get("key") or "${trans_id}"), run.vars)
    except Exception:  # noqa: BLE001
        res["key"] = None
    res["fake_id"] = run.vars.get("trans_id")
    return res


def describe_pattern(pat, race=None):
    what = " then ".join(k for k, _ in pat) or f"none of the {(race or {}).get('what', 'racing calls')} reached the fake"
    if len(pat) >= 2:
        what += f" ({round((pat[1][1] - pat[0][1]) * 1000)} ms apart)"
    return what


def in_window(o):
    return -0.5 <= o <= 1.5


def report(name, results, race=None):
    """The standard report. Returns (n_failed_runs, n_inconclusive)."""
    if race:
        posted, rival_text, rival_short = race["posted"], race["rival_text"], race.get("rival_short", race["rival"])
    n = len(results)
    failed = [(i, r) for i, r in enumerate(results, 1) if r["fails"]]
    incon = [(i, r) for i, r in enumerate(results, 1) if r["inconclusive"]]
    ok = [r for r in results if not r["inconclusive"]]
    failures, first = collections.Counter(), {}
    for i, r in failed:
        for f in r["fails"]:
            failures[f] += 1
            first.setdefault(f, i)
    print(f"\n== {name} ==")
    print(f"runs: {n}   runs with failures: {len(failed)}" + (f"   inconclusive: {len(incon)}" if incon else ""))
    for f, c in failures.most_common():
        print(f"  FAIL x{c}: {f}   (first at run {first[f]})")
    reasons = collections.Counter(r["inconclusive"] for _, r in incon)
    for why, c in reasons.most_common():
        print(f"  INCONCLUSIVE x{c}: {why}")
    if incon:
        print(f"  ({len(incon)} run(s) did not reach the scenario's starting point, so they prove nothing either way and are "
              f"left out of the statistics below; look for stalls or errors in your app log at those times.)")
    if not failures:
        print("  all assertions held in every conclusive run" if incon else "  all assertions held in every run")
    offsets = [r["offset"] for r in ok if r["offset"] is not None]
    if offsets:
        early, win, late = sum(o > 1.5 for o in offsets), sum(in_window(o) for o in offsets), sum(o < -0.5 for o in offsets)
        print(f"  {posted} timing in the {len(offsets)} run(s) where {rival_text} reached the fake "
              f"(race window = {posted} posted from 1.5 s before to 0.5 s after the {rival_short}):")
        print(f"    more than 1.5 s before the {rival_short}: {early}   |   inside the race window: {win}   |   "
              f"more than 0.5 s after the {rival_short}: {late}")
        print("    (runs inside the window are the ones that test the race, even if your app stops the second submission "
              "before it reaches the fake)")
    if failed:
        print("  failed runs (DB key / what reached the fake / why it failed), to find the rows and log lines:")
        for i, r in failed[:10]:
            reached = f"\n            reached the fake: {describe_pattern(r['pat'], race)}" if race else ""
            print(f"    run {i}: {r['key']}{reached}\n            failure: {'; '.join(r['fails'])[:140]}")
    if incon:
        print("  inconclusive runs (DB key / why):")
        for i, r in incon[:10]:
            print(f"    run {i}: {r['key']}\n            {r['inconclusive'][:140]}")
    strays = sum(r["strays"] for r in results)
    if strays:
        print(f"  note: {strays} call(s) at the fake belonged to OTHER transactions (stragglers from earlier runs) "
              f"and were left out of the counts below. Your app was still working on a previous transaction when the next run started.")
    patterns = collections.defaultdict(list)
    for r in ok:
        patterns[tuple(k for k, _ in r["pat"])].append([round((b[1] - a[1]) * 1000) for a, b in zip(r["pat"], r["pat"][1:])])
    if race and any(patterns):
        print(f"  {race.get('what', 'racing calls')} that reached the fake, per run (a race needs two, close together):")
        for pat, gaps in sorted(patterns.items(), key=lambda kv: -len(kv[1])):
            pname = " then ".join(pat) if pat else "none"
            extra = ""
            if len(pat) >= 2:
                g = sorted(x[0] for x in gaps)
                extra = f"   gap between the first two: min {g[0]} ms, median {g[len(g) // 2]} ms, max {g[-1]} ms"
            print(f"    {len(gaps):>4} x {pname}{extra}")
    return len(failed), len(incon)


def sweep_report(path, values, results, race=None):
    if not race:
        print(f"\n== sweep of {path} ==")
        print(f"{'value':>8} {'runs':>5} {'failed':>7} {'inconcl.':>9}")
        for v in values:
            rs = [r for r in results if r["value"] == v]
            print(f"{v:>8} {len(rs):>5} {sum(bool(r['fails']) for r in rs):>7} {sum(bool(r['inconclusive']) for r in rs):>9}")
        return
    posted, rival, rival_short = race["posted"], race["rival"], race.get("rival_short", race["rival"])
    rows = []
    for v in values:
        rs = [r for r in results if r["value"] == v]
        ok = [r for r in rs if not r["inconclusive"]]
        two = sum(len(r["pat"]) >= 2 for r in ok)
        win = sum(r["offset"] is not None and in_window(r["offset"]) for r in ok)
        cres_only = sum([k for k, _ in r["pat"]] == [posted] for r in ok)
        u_only = sum([k for k, _ in r["pat"]] == [rival] for r in ok)
        rows.append((v, len(rs), sum(bool(r["fails"]) for r in rs), sum(bool(r["inconclusive"]) for r in rs), cres_only, u_only, two, win))
    print(f"\n== sweep of {path} ==")
    print(f"{'value':>8} {'runs':>5} {'failed':>7} {'inconcl.':>9} {posted + ' only':>10} {rival_short + ' only':>7} "
          f"{'2 submissions':>14} {posted + ' in window':>15}")
    for r in rows:
        print(f"{r[0]:>8} {r[1]:>5} {r[2]:>7} {r[3]:>9} {r[4]:>10} {r[5]:>7} {r[6]:>14} {r[7]:>15}")
    best = max(rows, key=lambda r: (r[7] + r[6], r[6]))
    if best[7] + best[6] > 0:
        print(f"  most runs that tested the race: {path} = {best[0]}  ({best[6]} with two submissions, {best[7]} with the {posted} inside the window)")
        print(f"  -> set it in lab.yaml (e.g. under timing:) and run the scenario with a higher --repeat. '{posted} in window' counts runs where "
              f"{race['rival_text']} reached the fake; a value with 0 everywhere means the race was never reached at that setting.")
    else:
        print(f"  no value in this range produced a run that tested the race. Widen the range or check the watchdog (is it firing at all?).")


def resolve_scenario(arg, psp):
    """A scenario file, or the id of one of the pack's scenarios: 'R1' (or 'R1_result_vs_timeout') for R1_*.yaml."""
    if os.path.exists(arg):
        return arg
    folder = psps.scenarios_dir(psp)
    if folder and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            stem = name[:-5] if name.endswith(".yaml") else None
            if stem and arg.lower() in (stem.lower(), stem.split("_")[0].lower()):
                return os.path.join(folder, name)
    sys.exit(f"no scenario {arg!r}: not a file, and no scenario of the {psp.NAME} pack has that id")


async def main(argv=None):
    ap = argparse.ArgumentParser(prog="raceproof run")
    ap.add_argument("scenario")
    ap.add_argument("--fake", default=None, help="the fake PSP's URL (default: `fake` in the config, else the pack's default port)")
    ap.add_argument("--db", default=None, help="DSN; overrides db.dsn from the config")
    ap.add_argument("--config", default=None, help="lab.yaml (default: ./lab.yaml if it exists)")
    ap.add_argument("--repeat", type=int, default=None, help="runs (or, with --sweep, rounds per value)")
    ap.add_argument("--trace", action="store_true", help="print a timeline of every run (use with --repeat 1 or 2)")
    ap.add_argument("--sweep", default=None, metavar="PATH=start:stop:step",
                    help="run the scenario for every value of a config setting, e.g. timing.near_watchdog_ms=4600:6400:200")
    ap.add_argument("--max-inconclusive", type=float, default=None, metavar="PCT",
                    help="exit code 3 if more than this %% of runs are inconclusive (default: the scenario's "
                         "max_inconclusive_pct, else 20)")
    args = ap.parse_args(argv)

    cfg_path = args.config or ("lab.yaml" if os.path.exists("lab.yaml") else None)
    config = load_config(cfg_path) if cfg_path else {}
    try:
        psp = psp_for(config)
    except ValueError as e:
        sys.exit(f"lab.yaml psp: {e}")
    args.scenario = resolve_scenario(args.scenario, psp)
    with open(args.scenario, encoding="utf-8") as f:
        scenario = yaml.safe_load(f)
    scenario["_dir"] = os.path.dirname(os.path.abspath(args.scenario))
    sweep = parse_sweep(args.sweep) if args.sweep else None
    repeat = args.repeat or (5 if sweep else scenario.get("repeat", 1))
    owner = scenario_psp(args.scenario)
    if owner and owner != psp.NAME:
        sys.exit(f"{args.scenario} is a {owner} scenario, but lab.yaml has psp: {psp.NAME}. "
                 f"Use a config for {owner} (--config) or a scenario of the {psp.NAME} pack")
    for val, names in duplicate_statuses(config).items():
        print(f"note: status {val!r} is shared by {', '.join(names)}; wait_status cannot tell these apart "
              f"(the templates also wait for the fake to see your app's fingerprint call)", file=sys.stderr)
    uses_db = any(k in json.dumps(scenario) for k in ("wait_status", "tx_status", "refusal_marker", "wait_sql", "sql_equals", '"sql"'))
    dsn = args.db or (config.get("db") or {}).get("dsn")
    if uses_db:                                    # check every configured database BEFORE running, with a readable error
        for where, raw in config_dsns(config, args.db):
            try:
                get_db(resolve_dsn(raw))("SELECT 1")
            except Exception as e:  # noqa: BLE001
                sys.exit(f"cannot use the database from {where} ({mask_dsn(raw)}): {e}")
    try:
        db = get_db(resolve_dsn(dsn)) if dsn else None
    except ValueError as e:
        sys.exit(f"db.dsn: {e}")
    fake_url = args.fake or config.get("fake") or f"http://127.0.0.1:{getattr(psp, 'FAKE_PORT', 9100)}"
    # Preflight: a slow or unreachable fake ruins every timing-sensitive scenario, so say so before running.
    t0 = time.time()
    try:
        requests.get(f"{fake_url.rstrip('/')}/_control/calls", timeout=10)
        took = time.time() - t0
        if took > 0.5:
            print(f"WARNING: one request to the fake took {took * 1000:.0f} ms (it should take a few ms). On Windows "
                  f"'localhost' often costs ~2 s per call because ::1 is tried first. Use 127.0.0.1 in `fake:` and in the "
                  f"gateway URLs (and in your app's {psp.TITLE} base URL). Timing scenarios are NOT meaningful until this is fixed.",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: cannot reach the fake at {fake_url}: {e}", file=sys.stderr)

    # The plan: (config to use, sweep value or None) per run. With --sweep the values are interleaved and shuffled
    # in every round, so slow drift in the environment affects all values alike.
    plan = []
    if sweep:
        path, values = sweep
        for _ in range(repeat):
            order = values[:]
            random.shuffle(order)
            plan += [(set_path(config, path, v), v) for v in order]
        print(f"sweep: {path} in {values[0]}..{values[-1]} step {values[1] - values[0] if len(values) > 1 else 0} "
              f"= {len(values)} values x {repeat} rounds = {len(plan)} runs", file=sys.stderr)
    else:
        plan = [(config, None)] * repeat

    results = []
    for i, (cfg, label) in enumerate(plan):
        res = await run_once(scenario, fake_url, db, cfg, args.trace, label, psp)
        results.append(res)
        bad = sum(bool(r["fails"]) for r in results)
        inc = sum(bool(r["inconclusive"]) for r in results)
        if len(plan) > 1:
            print(f"\r  run {i + 1}/{len(plan)}   failed so far: {bad}   inconclusive: {inc}", end="", file=sys.stderr, flush=True)
        # A setup problem (wrong URL, app not calling the fake, DB unreachable) fails identically every time:
        # do not spend 300 runs on it.
        if i == 2 and all(r["inconclusive"] or any(f.startswith("exception:") for f in r["fails"]) for r in results):
            print("\n  the first 3 runs all stopped before the scenario could start, so the setup is probably wrong; stopping early.",
                  file=sys.stderr)
            break
    if len(plan) > 1:
        print(file=sys.stderr)

    if sweep:
        sweep_report(sweep[0], sweep[1], results, psp.RACE)
    n_failed, n_incon = report(scenario["name"], results, psp.RACE)
    if n_failed:
        sys.exit(1)
    max_incon = args.max_inconclusive if args.max_inconclusive is not None else float(scenario.get("max_inconclusive_pct", 20))
    if results and n_incon * 100.0 / len(results) > max_incon:
        print(f"\n  EXIT 3: {n_incon} of {len(results)} runs were inconclusive (more than {max_incon:g} %), "
              f"so this result cannot be trusted. Fix the environment (stalls, slow status updates) and rerun.")
        sys.exit(3)
    sys.exit(0)


def cli(argv=None):
    asyncio.run(main(argv))


if __name__ == "__main__":
    cli()
