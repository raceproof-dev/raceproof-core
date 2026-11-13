"""The scenario menu: picking, metadata, the summary and exit codes. No app, DB or fake needed."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from unittest import mock

from raceproof import menu as lab, psps


def write(d, name, text):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)


class Psp(unittest.TestCase):
    def test_the_menu_lists_the_scenarios_of_the_configured_pack(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(lab.configured_psp(os.path.join(d, "missing.yaml")), "demo")
            write(d, "lab.yaml", "psp: Acme\n")
            self.assertEqual(lab.configured_psp(os.path.join(d, "lab.yaml")), "acme")
            write(d, "none.yaml", "fake: x\n")
            self.assertEqual(lab.configured_psp(os.path.join(d, "none.yaml")), "demo")
        self.assertEqual({os.path.dirname(s["path"]) for s in lab.discover(psp="demo")}, {psps.load("demo").SCENARIOS})
        self.assertEqual(lab.discover(psp="no_such_psp"), [])

class Pick(unittest.TestCase):
    S = [{"name": f"s{i}", "run_all": i != 3} for i in range(1, 6)]

    def names(self, text):
        return [s["name"] for s in lab.parse_pick(text, self.S)]

    def test_numbers_ranges_and_duplicates(self):
        self.assertEqual(self.names("3"), ["s3"])
        self.assertEqual(self.names("4, 1-2,2"), ["s1", "s2", "s4"])   # list order, each once
        self.assertEqual(self.names("5-4"), ["s4", "s5"])

    def test_all_leaves_out_the_marked_ones_but_they_can_be_picked_by_number(self):
        self.assertEqual(self.names("a"), ["s1", "s2", "s4", "s5"])
        self.assertEqual(self.names("ALL"), ["s1", "s2", "s4", "s5"])
        self.assertEqual(self.names("3"), ["s3"])

    def test_bad_input(self):
        for bad in ("0", "6", "x", "1-9", ",", "1;2"):
            with self.assertRaises(ValueError, msg=bad):
                lab.parse_pick(bad, self.S)


class Discover(unittest.TestCase):
    def test_metadata_defaults_and_natural_order(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "C10_x.yaml", "name: ten\nrepeat: 7\nsteps: []\n")
            write(d, "C9_x.yaml", "name: nine\nneeds: one thing\nrun_all: false\nrun_all_note: why\nsettle_after_s: 12\nsteps: []\n")
            write(d, "C9b_x.yaml", "name: nine b\nneeds: [a, b]\nsteps: []\n")
            got = lab.discover(d)
        self.assertEqual([s["name"] for s in got], ["nine", "nine b", "ten"])
        nine, nine_b, ten = got
        self.assertEqual((nine["needs"], nine["run_all"], nine["run_all_note"], nine["settle_after_s"]), (["one thing"], False, "why", 12))
        self.assertEqual(nine_b["needs"], ["a", "b"])
        self.assertEqual((ten["repeat"], ten["needs"], ten["run_all"], ten["settle_after_s"]), (7, [], True, lab.DEFAULT_SETTLE_S))

    def test_a_packs_scenario_groups_come_first_in_their_order(self):
        import types
        with tempfile.TemporaryDirectory() as d:
            for name in ("W1_x", "C2_x", "selftest", "M1_x", "C1_x"):
                write(d, name + ".yaml", f"name: {name}\nsteps: []\n")
            pack = types.SimpleNamespace(NAME="acme", SCENARIOS=d, SCENARIO_GROUPS="MCW")
            with mock.patch.object(psps, "load", return_value=pack):
                got = [s["name"] for s in lab.discover(psp="acme")]
            plain = [s["name"] for s in lab.discover(d)]
        self.assertEqual(got, ["M1_x", "C1_x", "C2_x", "W1_x", "selftest"])
        self.assertEqual(plain, ["C1_x", "C2_x", "M1_x", "selftest", "W1_x"])

    def test_the_demo_scenarios_parse_and_the_selftest_is_left_out_of_all(self):
        got = {s["file"]: s for s in lab.discover(psp="demo")}
        self.assertEqual(sorted(got), ["R1_result_vs_timeout.yaml", "R2_same_key_selftest.yaml"])
        self.assertTrue(got["R1_result_vs_timeout.yaml"]["run_all"])
        self.assertFalse(got["R2_same_key_selftest.yaml"]["run_all"])

class Report(unittest.TestCase):
    def test_summary_is_read_from_the_runner_report(self):
        self.assertEqual(lab.parse_summary("\r  run 3/3 ...\n== X ==\nruns: 50   runs with failures: 0   inconclusive: 14\n"), (50, 0, 14))
        self.assertEqual(lab.parse_summary("== X ==\nruns: 20   runs with failures: 2\n"), (20, 2, 0))
        self.assertIsNone(lab.parse_summary("cannot use the database from db.dsn"))

    def test_verdicts_and_exit_code_precedence(self):
        ok, failed, untrusted, error = (None, 0, (5, 0, 0), 0), (None, 1, (5, 1, 0), 0), (None, 3, (5, 0, 4), 0), (None, 1, None, 0)
        self.assertEqual(lab.exit_code([ok, ok], 2), 0)
        self.assertEqual(lab.exit_code([ok, untrusted, error, failed], 4), 1)
        self.assertEqual(lab.exit_code([ok, untrusted, error], 3), 2)
        self.assertEqual(lab.exit_code([ok, untrusted], 2), 3)
        self.assertEqual(lab.exit_code([ok], 2), 130)                 # stopped with Ctrl+C
        self.assertTrue(lab.verdict(1, None).startswith("ERROR"))     # sys.exit("message") is also exit 1: no report = setup error


class EndToEnd(unittest.TestCase):
    def test_runs_the_picked_scenarios_through_runner_and_summarises(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as res:
            write(d, "A_pass.yaml", "name: passes\nrepeat: 2\nsettle_after_s: 0\nsteps:\n  - sleep_ms: 1\n")
            write(d, "B_fail.yaml", "name: fails\nrepeat: 1\nsteps:\n  - no_such_step: {}\n")
            write(d, "C_skip.yaml", "name: skipped\nrun_all: false\nsteps:\n  - sleep_ms: 1\n")
            write(res, "lab.yaml", "fake: http://127.0.0.1:1\n")        # not the local lab.yaml: no app, no DB
            out = io.StringIO()
            with redirect_stdout(out):
                code = lab.main(["--scenarios", d, "--pick", "all", "--config", os.path.join(res, "lab.yaml"),
                                 "--results", os.path.join(res, "out")])
            text = out.getvalue()
            out_dir = os.path.join(res, "out")
            logs = sorted(os.listdir(os.path.join(out_dir, os.listdir(out_dir)[0])))
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"passes\s+2\s+0\s+0\s+\S+\s+passed")
        self.assertRegex(text, r"fails\s+1\s+1\s+0\s+\S+\s+FAILED")
        self.assertNotIn("=== [3/", text)                              # the run_all: false one was not run
        self.assertEqual(logs, ["A_pass.log", "B_fail.log"])


if __name__ == "__main__":
    unittest.main()
