"""The runner without a fake: config, variables, statuses, databases, named queries, sweeps, pack selection."""
import asyncio, os, sqlite3, subprocess, sys, tempfile, textwrap, threading, time, unittest
from unittest import mock

from raceproof import psps, runner

RUN = [sys.executable, "-m", "raceproof.runner"]

CFG = """
fake: http://localhost:9100
gateway:
  base_url: http://gw
  method_url: ${gateway.base_url}/method
  challenge_url: ""
db:
  dsn: sqlite:///%(db)s
  table: txs
  id_column: txid
  status_column: state
payloads:
  start_payment: payloads/p.json
status:
  pending_challenge: WAIT_CH
  refused: REJ
  completed: DONE
timing: {watchdog_ms: 2000}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.d.name, "t.db")
        con = sqlite3.connect(self.db); con.execute("create table txs (txid text, state text)")
        con.execute("insert into txs values ('abc', 'WAIT_CH')"); con.commit(); con.close()
        self.cfg_path = os.path.join(self.d.name, "lab.yaml")
        open(self.cfg_path, "w").write(CFG % {"db": self.db})
        self.cfg = runner.load_config(self.cfg_path)

    def tearDown(self): self.d.cleanup()

    def set_state(self, v):
        con = sqlite3.connect(self.db); con.execute("update txs set state=?", (v,)); con.commit(); con.close()

    def run_scenario(self, steps, asserts=(), **vars_):
        sc = {"name": "t", "steps": steps, "assert": list(asserts), "vars": {"trans_id": "abc", **vars_}, "_dir": self.d.name}
        async def go():
            r = runner.Run(sc, "http://127.0.0.1:1", runner.make_db(self.cfg["db"]["dsn"]), self.cfg)
            await r.execute(); return await r.check()
        return asyncio.run(go())


class Vars(Base):
    def test_dotted_nested_and_computed_values(self):
        v = {**self.cfg, "fake": "F"}
        self.assertEqual(runner.subst("${gateway.method_url}|${timing.near_watchdog_ms}|${timing.wait_watchdog_ms}", v),
                         "http://gw/method|1700|17000")
        self.assertEqual(runner.subst("[${gateway.challenge_url}]", v), "[]")            # empty = defined, means 'use default'
        self.assertEqual(self.cfg["payloads"]["start_payment"], os.path.join(self.d.name, "payloads/p.json"))

    def test_undefined_variable_is_a_clear_error(self):
        with self.assertRaises(ValueError) as c:
            runner.check_defined(runner.subst({"url": "${gateway.nope}/${trans_id}"}, {**self.cfg}), " in step http")
        self.assertIn("${gateway.nope}", str(c.exception)); self.assertIn("lab.yaml", str(c.exception))
        self.assertIn("undefined variable", str(runner_run_error(self)))

    def test_json_file_from_config_path_with_run_seq(self):
        os.makedirs(os.path.join(self.d.name, "payloads"))
        open(os.path.join(self.d.name, "payloads", "p.json"), "w").write('{"id": "11111111-1111-4111-8111-1111111${run_seq}"}')
        sc = {"name": "t", "steps": [], "_dir": self.d.name}
        r = runner.Run(sc, "http://x", None, self.cfg)
        import json
        body = runner.subst(json.load(open(self.cfg["payloads"]["start_payment"])), r.vars)
        self.assertRegex(body["id"], r"^11111111-1111-4111-8111-1111111\d{5}$")


def runner_run_error(t):
    try: t.run_scenario([{"sleep_ms": 0}, {"http": {"name": "x", "method": "GET", "url": "${gateway.start_payment_url}"}}])
    except ValueError as e: return e
    return ""


class Status(Base):
    def test_wait_status_and_tx_status_use_logical_names(self):
        self.assertEqual(self.run_scenario([{"wait_status": {"is": "pending_challenge", "timeout_ms": 500}}],
                                           [{"tx_status": {"is": "pending_challenge"}}, {"tx_status": {"in": ["refused", "pending_challenge"]}}]), [])

    def test_wait_status_sees_a_change_that_happens_later(self):
        threading.Timer(0.4, self.set_state, ["REJ"]).start()
        t0 = time.time()
        self.assertEqual(self.run_scenario([{"wait_status": {"is": "refused", "timeout_ms": 5000}}], [{"tx_status": {"is": "refused"}}]), [])
        self.assertGreater(time.time() - t0, 0.3)

    def test_assertion_failure_names_both_sides(self):
        self.set_state("DONE")
        f = self.run_scenario([], [{"tx_status": {"is": "refused"}}])
        self.assertEqual(len(f), 1); self.assertIn("'DONE' (completed)", f[0]); self.assertIn("expected refused (REJ)", f[0])

    def test_unknown_db_value_is_flagged_not_hidden(self):
        self.set_state("SOMETHING_ELSE")
        f = self.run_scenario([], [{"tx_status": {"is": "completed"}}])
        self.assertIn("NOT in the configured status list", f[0])
        with self.assertRaises(TimeoutError) as c:
            self.run_scenario([{"wait_status": {"is": "completed", "timeout_ms": 300}}])
        self.assertIn("NOT in the configured status list", str(c.exception))

    def test_unknown_logical_name_and_missing_db_config_are_errors(self):
        with self.assertRaises(ValueError) as c: self.run_scenario([{"wait_status": {"is": "typo"}}])
        self.assertIn("known: ['completed', 'pending_challenge', 'refused']", str(c.exception))
        self.cfg["db"].pop("table")
        with self.assertRaises(ValueError) as c: self.run_scenario([{"wait_status": {"is": "refused"}}])
        self.assertIn("db.table", str(c.exception))

    def test_identifiers_are_validated(self):
        self.cfg["db"]["table"] = "txs; drop table txs"
        with self.assertRaises(ValueError): self.run_scenario([{"wait_status": {"is": "refused"}}])


class Key(Base):
    """The lookup column need not be the primary key: id is a bigint, the UUID lives in another column."""
    def setUp(self):
        super().setUp()
        con = sqlite3.connect(self.db)
        con.execute("create table meta (id integer primary key, transaction_id text, state text)")
        con.execute("insert into meta values (7, '11111111-1111-4111-8111-111111100042', 'WAIT_CH')"); con.commit(); con.close()
        self.cfg["db"].update(table="meta", id_column="transaction_id", status_column="state",
                              key="${ids.transaction_id}")
        self.cfg["ids"] = {"transaction_id": "11111111-1111-4111-8111-1111111${run_seq}"}

    def run_with_seq(self, seq, asserts):
        sc = {"name": "t", "steps": [{"wait_status": {"is": "pending_challenge", "timeout_ms": 300}}], "assert": asserts,
              "vars": {"trans_id": "fake-uuid-not-in-db"}, "_dir": self.d.name}
        async def go():
            r = runner.Run(sc, "http://x", runner.make_db(self.cfg["db"]["dsn"]), self.cfg)
            r.vars["run_seq"] = seq
            await r.execute(); return await r.check()
        return asyncio.run(go())

    def test_lookup_by_own_transaction_id_not_the_fake_uuid(self):
        self.assertEqual(self.run_with_seq("00042", [{"tx_status": {"is": "pending_challenge"}}]), [])

    def test_wrong_key_finds_nothing_and_says_what_it_looked_for(self):
        with self.assertRaises(TimeoutError): self.run_with_seq("99999", [])

    def test_unresolved_key_is_a_clear_error(self):
        self.cfg["db"]["key"] = "${reference}"            # templates save it, but this scenario did not
        with self.assertRaises(ValueError) as c: self.run_with_seq("00042", [])
        self.assertIn("db.key resolved to", str(c.exception))


class Marker(Base):
    def setUp(self):
        super().setUp()
        con = sqlite3.connect(self.db)
        con.execute("create table markers (transaction_id text, created text)")
        con.execute("insert into markers values ('abc', 'now')"); con.commit(); con.close()
        self.cfg["db"]["refusal_marker"] = {"table": "markers", "id_column": "transaction_id"}

    def test_marker_count_is_asserted_per_transaction(self):
        self.assertEqual(self.run_scenario([], [{"refusal_marker": {"count": 1}}]), [])
        f = self.run_scenario([], [{"refusal_marker": {"count": 0}}])
        self.assertEqual(f, ["refusal markers for this transaction = 1, expected 0"])

    def test_other_transactions_do_not_count(self):
        self.cfg["db"]["key"] = "${trans_id}"
        self.assertEqual(self.run_scenario([], [{"refusal_marker": {"count": 0}}], trans_id="someone-else"), [])

    def test_default_expectation_is_zero_and_config_is_validated(self):
        self.assertEqual(self.run_scenario([], [{"refusal_marker": {}}], trans_id="nobody"), [])
        self.cfg["db"]["refusal_marker"].pop("table")
        with self.assertRaises(ValueError) as c: self.run_scenario([], [{"refusal_marker": {"count": 0}}])
        self.assertIn("db.refusal_marker.table", str(c.exception))
        self.cfg["db"]["refusal_marker"] = {"table": "markers; drop table markers", "id_column": "transaction_id"}
        with self.assertRaises(ValueError): self.run_scenario([], [{"refusal_marker": {"count": 0}}])


class OtherDatabase(Base):
    """The marker table lives in a different database (another service's), reached with its own dsn."""
    def setUp(self):
        super().setUp()
        self.db2 = os.path.join(self.d.name, "other.db")
        con = sqlite3.connect(self.db2); con.execute("create table markers (transaction_id text)")
        con.execute("insert into markers values ('abc')"); con.commit(); con.close()
        self.cfg["db"]["refusal_marker"] = {"table": "markers", "id_column": "transaction_id", "dsn": f"sqlite:///{self.db2}"}

    def test_marker_is_read_from_its_own_database_not_the_main_one(self):
        self.assertEqual(self.run_scenario([], [{"refusal_marker": {"count": 1}}]), [])      # main db has no 'markers' table at all
        f = self.run_scenario([], [{"refusal_marker": {"count": 0}}])
        self.assertEqual(f, ["refusal markers for this transaction = 1, expected 0"])

    def test_sql_steps_and_asserts_can_name_their_own_database(self):
        os.environ["RACEPROOF_TEST_OTHER_DSN"] = f"sqlite:///{self.db2}"
        self.addCleanup(os.environ.pop, "RACEPROOF_TEST_OTHER_DSN", None)
        q = "SELECT count(*) FROM markers"
        steps = [{"sql": {"name": "n", "query": q, "dsn": "env:RACEPROOF_TEST_OTHER_DSN"}},
                 {"wait_sql": {"query": q, "equals": 1, "dsn": "env:RACEPROOF_TEST_OTHER_DSN", "timeout_ms": 500}}]
        asserts = [{"sql_equals": {"name": "n", "value": 1}},
                   {"sql_equals": {"query": q, "value": 1, "dsn": "env:RACEPROOF_TEST_OTHER_DSN"}},
                   {"sql_equals": {"query": "SELECT count(*) FROM txs", "value": 1}}]          # no dsn: the main database
        self.assertEqual(self.run_scenario(steps, asserts), [])

    def test_marker_dsn_from_environment_and_no_main_db_needed(self):
        os.environ["RACEPROOF_TEST_OTHER_DSN"] = f"sqlite:///{self.db2}"
        self.addCleanup(os.environ.pop, "RACEPROOF_TEST_OTHER_DSN", None)
        self.cfg["db"]["refusal_marker"]["dsn"] = "env:RACEPROOF_TEST_OTHER_DSN"
        sc = {"name": "t", "steps": [], "assert": [{"refusal_marker": {"count": 1}}], "vars": {"trans_id": "abc"}, "_dir": self.d.name}
        async def go():
            r = runner.Run(sc, "http://x", None, self.cfg)          # no default database at all
            return await r.check()
        self.assertEqual(asyncio.run(go()), [])

    def test_helpers(self):
        self.assertEqual(runner.mask_dsn("postgresql://postgres:s3cret@localhost:5433/db"), "postgresql://postgres:***@localhost:5433/db")
        self.assertEqual(runner.config_dsns(self.cfg), [("db.dsn", self.cfg["db"]["dsn"]), ("db.refusal_marker.dsn", f"sqlite:///{self.db2}")])
        with self.assertRaises(ValueError): runner.resolve_dsn("env:LAB_SURELY_NOT_SET_12345")

    def test_cli_checks_every_database_up_front_and_never_prints_the_password(self):
        bad = "sqlite:////nonexistent_dir_xyz/other.db"
        cfg = open(self.cfg_path).read() + f"\n" ; cfg = cfg.replace("db:\n", f"db:\n  refusal_marker: {{table: m, id_column: transaction_id, dsn: \"{bad}\"}}\n", 1)
        open(self.cfg_path, "w").write(cfg)
        sc = os.path.join(self.d.name, "s.yaml")
        open(sc, "w").write("name: x\nvars: {trans_id: abc}\nsteps: [{sleep_ms: 1}]\nassert: [{refusal_marker: {count: 0}}]\n")
        p = subprocess.run([*RUN, sc], cwd=self.d.name, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("db.refusal_marker.dsn", p.stdout + p.stderr); self.assertIn("cannot use the database", p.stdout + p.stderr)
        # a scenario that needs no database must still run
        open(sc, "w").write("name: x\nsteps: [{sleep_ms: 1}]\n")
        p = subprocess.run([*RUN, sc], cwd=self.d.name, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class Duplicates(unittest.TestCase):
    def test_shared_status_values_are_detected(self):
        cfg = {"status": {"pending_challenge": "WAITING", "pending_method": "WAITING", "refused": "refused"}}
        self.assertEqual(runner.duplicate_statuses(cfg), {"WAITING": ["pending_challenge", "pending_method"]})
        self.assertEqual(runner.duplicate_statuses({"status": {"a": "x", "b": "y"}}), {})


class PspSelection(unittest.TestCase):
    def test_load_defaults_to_the_demo_pack_and_names_an_unknown_psp(self):
        self.assertEqual(psps.load(None).NAME, "demo")
        self.assertEqual(psps.load(" Demo ").NAME, "demo")
        for bad in ("nope", "bad name", "../x"):
            with self.assertRaises(ValueError, msg=bad):
                psps.load(bad)
        self.assertIn("demo", psps.available())

    def test_a_scenario_belongs_to_the_pack_whose_folder_holds_it(self):
        demo = psps.load("demo")
        self.assertEqual(runner.scenario_psp(os.path.join(demo.SCENARIOS, "R1_result_vs_timeout.yaml")), "demo")
        self.assertIsNone(runner.scenario_psp(os.path.join(tempfile.gettempdir(), "x.yaml")))

    def test_cli_refuses_an_unknown_psp_and_a_scenario_of_another_psp(self):
        with tempfile.TemporaryDirectory() as d:
            sc = os.path.join(d, "s.yaml")
            open(sc, "w").write("name: x\nsteps: [{sleep_ms: 1}]\n")
            cfg = os.path.join(d, "lab.yaml")
            open(cfg, "w").write("psp: nope\nfake: http://127.0.0.1:1\n")
            p = subprocess.run([*RUN, sc, "--config", cfg], cwd=d, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("unknown psp 'nope'", p.stdout + p.stderr)
            open(cfg.replace("lab.yaml", "ok.yaml"), "w").write("fake: http://127.0.0.1:1\n")
            with mock.patch.object(runner, "scenario_psp", return_value="acme"):
                with self.assertRaises(SystemExit) as e:
                    asyncio.run(runner.main([sc, "--config", cfg.replace("lab.yaml", "ok.yaml")]))
            self.assertIn("is a acme scenario, but lab.yaml has psp: demo", str(e.exception))

    def test_a_scenario_for_an_optional_feature_is_not_applicable_without_its_config_key(self):
        sc = {"name": "x", "applies_if": ["gateway.skip_method_url"]}
        self.assertEqual(runner.not_applicable(sc, {"gateway": {"method_url": "u"}}), ["gateway.skip_method_url"])
        self.assertEqual(runner.not_applicable(sc, {"gateway": {"skip_method_url": ""}}), ["gateway.skip_method_url"])
        self.assertEqual(runner.not_applicable(sc, {"gateway": {"skip_method_url": "http://x/skip"}}), [])
        self.assertEqual(runner.not_applicable({"name": "y"}, {}), [])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.yaml")
            open(path, "w").write("name: optional\napplies_if: gateway.skip_method_url\nsteps: [{sleep_ms: 1}]\n")
            cfg = os.path.join(d, "lab.yaml")
            open(cfg, "w").write("fake: http://127.0.0.1:1\n")
            p = subprocess.run([*RUN, path, "--config", cfg], cwd=d, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, runner.NOT_APPLICABLE, p.stdout + p.stderr)
            self.assertIn("NOT APPLICABLE", p.stdout)

    def test_a_psp_without_a_race_runs_and_reports(self):
        import types
        quiet = types.SimpleNamespace(NAME="none", TITLE="None", RACE=None, is_stray=lambda c, t: False,
                                      describe_call=str, compute=lambda c: 1, error_code=lambda t: None,
                                      after_http=lambda h, r: None)
        res = asyncio.run(runner.run_once({"name": "x", "steps": [{"sleep_ms": 1}]}, "http://127.0.0.1:1", None, {}, psp=quiet))
        self.assertEqual((res["fails"], res["pat"], res["offset"]), ([], [], None))


class Cli(Base):
    def test_env_dsn_and_default_config_discovery(self):
        sc = os.path.join(self.d.name, "s.yaml")
        open(sc, "w").write(textwrap.dedent("""\
            name: cli
            vars: {trans_id: abc}
            steps: [{wait_status: {is: pending_challenge, timeout_ms: 300}}]
            assert: [{tx_status: {is: pending_challenge}}]
        """))
        cfg = open(self.cfg_path).read().replace(f"sqlite:///{self.db}", "env:LAB_DB_DSN")
        open(self.cfg_path, "w").write(cfg)
        env = {**os.environ, "LAB_DB_DSN": f"sqlite:///{self.db}"}
        p = subprocess.run([*RUN, sc], cwd=self.d.name, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)       # found ./lab.yaml, resolved env:LAB_DB_DSN
        self.assertIn("all assertions held", p.stdout)
        env.pop("LAB_DB_DSN")
        p = subprocess.run([*RUN, sc], cwd=self.d.name, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0); self.assertIn("LAB_DB_DSN", p.stdout + p.stderr)


class NamedQueries(Base):
    """A scenario names a query (`named: x`); lab.yaml `queries:` says how to run it on your system."""
    def setUp(self):
        super().setUp()
        self.cfg["queries"] = {"tx_count": "SELECT count(*) FROM txs WHERE txid = '${trans_id}'",
                               "nothing": {"sql": "SELECT count(*) FROM txs WHERE txid = 'none'"}}

    def test_named_query_works_in_sql_wait_sql_and_sql_equals(self):
        steps = [{"sql": {"name": "n", "named": "tx_count"}},
                 {"wait_sql": {"named": "tx_count", "equals": 1, "timeout_ms": 500}}]
        asserts = [{"sql_equals": {"name": "n", "value": 1}},
                   {"sql_equals": {"named": "tx_count", "value": 1}},
                   {"sql_equals": {"named": "nothing", "value": 0}}]
        self.assertEqual(self.run_scenario(steps, asserts), [])

    def test_a_failure_names_the_query_not_its_sql(self):
        self.assertEqual(self.run_scenario([], [{"sql_equals": {"named": "tx_count", "value": 2}}]),
                         ["tx_count = 1, expected 2"])

    def test_an_undefined_query_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, r"no queries\.missing.*known: \['nothing', 'tx_count'\]"):
            self.run_scenario([], [{"sql_equals": {"named": "missing", "value": 0}}])

    def test_a_named_query_can_read_another_database(self):
        db2 = os.path.join(self.d.name, "other.db")
        con = sqlite3.connect(db2); con.execute("create table m (x)"); con.execute("insert into m values (1)"); con.commit(); con.close()
        self.cfg["queries"]["other"] = {"sql": "SELECT count(*) FROM m", "dsn": f"sqlite:///{db2}"}   # main db has no table m
        self.assertEqual(self.run_scenario([], [{"sql_equals": {"named": "other", "value": 1}}]), [])
        self.assertIn(("queries.other.dsn", f"sqlite:///{db2}"), runner.config_dsns(self.cfg))    # checked up front by the CLI


RACE = {"path": "/x", "label": lambda c: None, "posted": "CRes", "rival": "watchdog U", "rival_text": "the watchdog's U",
        "rival_short": "U", "starts_clock": lambda h, b: False}


class Sweep(unittest.TestCase):
    def test_set_path_overrides_a_dotted_key_on_a_copy(self):
        base = {"timing": {"watchdog_ms": 4000, "near_watchdog_ms": 100}, "x": 1}
        out = runner.set_path(base, "timing.near_watchdog_ms", 5400)
        self.assertEqual(out["timing"], {"watchdog_ms": 4000, "near_watchdog_ms": 5400})
        self.assertEqual(base["timing"]["near_watchdog_ms"], 100)           # original untouched
        self.assertEqual(runner.set_path({}, "a.b.c", 1), {"a": {"b": {"c": 1}}})

    def test_parse_sweep_includes_the_stop_value_and_rejects_nonsense(self):
        self.assertEqual(runner.parse_sweep("timing.near_watchdog_ms=4600:5000:200"), ("timing.near_watchdog_ms", [4600, 4800, 5000]))
        self.assertEqual(runner.parse_sweep("a=5:5:1")[1], [5])
        for bad in ("a", "a=1:2", "a=2:1:1", "a=1:2:0", "a=x:y:z", "=1:2:1", "a=0:100000:1"):
            with self.assertRaises(SystemExit, msg=bad):
                runner.parse_sweep(bad)

    def test_race_window_is_1_5_s_before_to_0_5_s_after_the_u(self):
        self.assertTrue(all(runner.in_window(o) for o in (-0.5, 0, 1.5, 0.7)))
        self.assertFalse(any(runner.in_window(o) for o in (1.51, -0.51, 5, -3)))

    def test_sweep_table_counts_and_picks_the_value_that_tests_the_race_most(self):
        import io, contextlib
        def res(v, pat, offset=None, fails=(), inc=None):
            return {"value": v, "pat": pat, "offset": offset, "fails": list(fails), "inconclusive": inc,
                    "strays": 0, "key": None, "fake_id": None}
        one, two = [("CRes", 1.0)], [("watchdog U", 1.0), ("CRes", 1.3)]
        results = [res(100, one), res(100, [("watchdog U", 1.0)], 5.0),
                   res(200, two, 0.2), res(200, two, -0.3, fails=["boom"]), res(200, one, inc="not ready")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.sweep_report("timing.x", [100, 200], results, RACE)
        out = buf.getvalue()
        rows = {l.split()[0]: l.split() for l in out.splitlines() if l.strip()[:1].isdigit()}
        # value runs failed inconcl. CRes-only U-only 2-submissions in-window
        self.assertEqual(rows["100"][1:], ["2", "0", "0", "1", "1", "0", "0"])
        self.assertEqual(rows["200"][1:], ["3", "1", "1", "0", "0", "2", "2"])   # the inconclusive run is excluded from the counts
        self.assertIn("timing.x = 200", out)

    def test_a_pack_without_a_race_gets_runs_and_failures_only(self):
        import io, contextlib
        r = {"value": 1, "pat": [], "offset": None, "fails": ["x"], "inconclusive": None, "strays": 0, "key": None, "fake_id": None}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.sweep_report("timing.x", [1], [r])
            runner.report("t", [r])
        out = buf.getvalue()
        self.assertRegex(out, r"(?m)^\s+1\s+1\s+1\s+0$")
        self.assertNotIn("window", out)
        self.assertIn("failure: x", out)

    def test_sweep_says_so_when_no_value_reached_the_race(self):
        import io, contextlib
        r = {"value": 1, "pat": [("CRes", 1.0)], "offset": None, "fails": [], "inconclusive": None, "strays": 0, "key": None, "fake_id": None}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.sweep_report("timing.x", [1], [r], RACE)
        self.assertIn("no value in this range produced a run that tested the race", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
