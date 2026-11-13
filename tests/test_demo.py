"""End to end: the demo PSP and the demo shop in this process, the runner and the menu as subprocesses."""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import requests
import uvicorn

from raceproof import cli, psps
from raceproof.demo import fake, shop

DEMO = psps.load("demo")
R1 = os.path.join(DEMO.SCENARIOS, "R1_result_vs_timeout.yaml")
R2 = os.path.join(DEMO.SCENARIOS, "R2_same_key_selftest.yaml")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server


class Demo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.psp_port, cls.shop_port = free_port(), free_port()
        shop.PSP = f"http://127.0.0.1:{cls.psp_port}"
        shop.DB = os.path.join(cls.dir.name, "shop.db")
        shop.TIMEOUT_MS = 1500
        cls.servers = [serve(fake.app, cls.psp_port), serve(shop.app, cls.shop_port)]
        cls.cfg = os.path.join(cls.dir.name, "demo.yaml")
        with open(cls.cfg, "w", encoding="utf-8") as f:
            f.write(cli.demo_config(shop.DB, cls.psp_port, cls.shop_port, shop.TIMEOUT_MS))

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.should_exit = True
        time.sleep(0.3)
        cls.dir.cleanup()

    def setUp(self):
        shop.FIXES["race"] = True

    def run_cli(self, *args, timeout=240):
        p = subprocess.run([sys.executable, "-m", "raceproof", *args], cwd=self.dir.name, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr

    def test_with_the_fix_on_every_run_passes_and_the_race_was_hit(self):
        code, out = self.run_cli("run", R1, "--config", self.cfg, "--repeat", "10")
        self.assertEqual(code, 0, out)
        self.assertIn("runs: 10   runs with failures: 0", out)
        self.assertRegex(out, r"inside the race window: [1-9]")

    def test_with_the_fix_off_the_merchant_is_told_twice(self):
        shop.FIXES["race"] = False
        code, out = self.run_cli("run", R1, "--config", self.cfg, "--repeat", "20")
        self.assertEqual(code, 1, out)
        self.assertIn("merchant_final_count = 2, expected 1", out)
        self.assertRegex(out, r"reached the fake: (result then timeout|timeout then result) \(\d+ ms apart\)")

    def test_the_selftest_shows_the_psps_same_key_rule_and_runs_by_its_id(self):
        code, out = self.run_cli("run", "R2", "--config", self.cfg, "--repeat", "5")
        self.assertEqual(code, 0, out)
        self.assertIn("== R2 demo PSP same-key race (self-test) ==", out)
        code, out = self.run_cli("run", "R9", "--config", self.cfg)
        self.assertNotEqual(code, 0)
        self.assertIn("no scenario 'R9'", out)

    def test_trace_shows_the_runner_and_the_fake_on_one_clock(self):
        code, out = self.run_cli("run", R1, "--config", self.cfg, "--repeat", "1", "--trace")
        self.assertIn("timeline (seconds from the first event)", out)
        self.assertRegex(out, r"runner +start http pay")
        self.assertRegex(out, r"FAKE +POST /payments/complete pay_\w+ result=abandoned")

    def test_sweep_runs_every_value_and_prints_a_table(self):
        code, out = self.run_cli("run", R1, "--config", self.cfg, "--repeat", "1", "--sweep", "timing.near_watchdog_ms=1100:1300:100")
        self.assertIn("== sweep of timing.near_watchdog_ms ==", out)
        for v in ("1100", "1200", "1300"):
            self.assertRegex(out, rf"(?m)^\s+{v}\s+1\s")

    def test_the_menu_runs_the_pack_scenarios_and_summarises(self):
        code, out = self.run_cli("menu", "--config", self.cfg, "--pick", "all", "--repeat", "3")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"R1 shopper's result arrives as the shop's timeout fires\s+3\s+0\s+0\s+\S+\s+passed")
        self.assertNotIn("R2 demo PSP", out.split("Summary")[1])           # the self-test is not in "all"

    def test_an_app_that_never_calls_the_fake_stops_early_as_inconclusive(self):
        sc = os.path.join(self.dir.name, "never.yaml")
        with open(sc, "w", encoding="utf-8") as f:
            f.write("name: never\nrepeat: 10\nsteps:\n  - reset: {}\n  - wait_calls: {path: /nothing, min: 1, timeout_ms: 200}\n")
        code, out = self.run_cli("run", sc, "--config", self.cfg)
        self.assertEqual(code, 3, out)
        self.assertIn("stopping early", out)
        self.assertIn("runs: 3", out)

    def test_an_unreachable_fake_is_reported(self):
        sc = os.path.join(self.dir.name, "quick.yaml")
        with open(sc, "w", encoding="utf-8") as f:
            f.write("name: quick\nsteps: [{sleep_ms: 1}]\n")
        code, out = self.run_cli("run", sc, "--config", self.cfg, "--fake", f"http://127.0.0.1:{free_port()}")
        self.assertIn("cannot reach the fake", out)

    def test_the_demo_psp_answers_like_its_docstring_says(self):
        base = f"http://127.0.0.1:{self.psp_port}"
        requests.post(f"{base}/_control/reset")
        pid = requests.post(f"{base}/payments", json={"reference": "r"}).json()["id"]
        first = requests.post(f"{base}/payments/complete", json={"payment": pid, "result": "authorised"},
                              headers={"Idempotency-Key": "k1"})
        replay = requests.post(f"{base}/payments/complete", json={"payment": pid, "result": "abandoned"},
                               headers={"Idempotency-Key": "k1"})
        other = requests.post(f"{base}/payments/complete", json={"payment": pid, "result": "abandoned"},
                              headers={"Idempotency-Key": "k2"})
        self.assertEqual((first.status_code, first.json()["status"]), (200, "authorised"))
        self.assertEqual((replay.status_code, replay.json()), (200, first.json()))
        self.assertEqual((other.status_code, other.json()["code"]), (422, "already_final"))
        self.assertEqual(len(requests.get(f"{base}/_control/calls", params={"path": "/payments/complete"}).json()), 3)


class Cli(unittest.TestCase):
    def test_packs_version_and_help(self):
        run = lambda *a: subprocess.run([sys.executable, "-m", "raceproof", *a], capture_output=True, text=True, timeout=60)
        self.assertIn("demo", run("packs").stdout)
        self.assertRegex(run("--version").stdout, r"raceproof \d+\.\d+\.\d+")
        self.assertIn("raceproof menu", run().stdout)
        self.assertEqual(run("nope").returncode, 2)

    def test_the_demo_config_names_every_query_the_demo_scenarios_use(self):
        import yaml
        from raceproof import runner
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(cli.demo_config("x.db", 1, 2, 2000))
            cfg = runner.load_config(path)
        for sc in (R1, R2):
            text = open(sc, encoding="utf-8").read()
            for named in set(__import__("re").findall(r"named: (\w+)", text)):
                self.assertIn(named, cfg["queries"], f"{os.path.basename(sc)}: {named}")
            yaml.safe_load(text)


if __name__ == "__main__":
    unittest.main()
