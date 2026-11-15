"""Scenario menu: pick scenarios by number and run them one after another, with a summary at the end.

Usage:  raceproof menu                          interactive: list the scenarios, ask which to run (and how many runs)
        raceproof menu --list                   only print the list
        raceproof menu --pick "1-3,7" [--repeat N]   no questions (also --pick all)
        Any of --config / --fake / --db / --max-inconclusive is passed on to `raceproof run`.

The list is the scenario folder of the pack that lab.yaml's `psp:` names (default demo), or --scenarios.
Each scenario runs as its own runner process, so memory is freed between scenarios and a crash in one does not stop
the rest. The output goes to the console and to results/<timestamp>/<scenario>.log.

Scenario keys read here (the runner ignores them):
  needs: [..]           short requirements shown before the scenario runs (e.g. the gateway restarted with some setting)
  run_all: false        leave it out of "all" (it can still be picked by number); run_all_note says why
  applies_if: [..]      (read by the runner) config keys of an optional gateway feature; unset -> "not applicable"
  settle_after_s: N     pause before the NEXT scenario (default 3 s), e.g. to let a circuit breaker close again
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time

import yaml

from raceproof import psps

DEFAULT_PSP = psps.DEFAULT
DEFAULT_SETTLE_S = 3
SUMMARY = re.compile(r"runs: (\d+)\s+runs with failures: (\d+)(?:\s+inconclusive: (\d+))?")


def configured_psp(config_path=None):
    """lab.yaml's `psp:` (default demo): the menu lists that pack's scenarios."""
    path = config_path or "lab.yaml"
    if not os.path.exists(path):
        return DEFAULT_PSP
    with open(path, encoding="utf-8") as f:
        return str((yaml.safe_load(f) or {}).get("psp") or DEFAULT_PSP).strip().lower()


def discover(scenario_dir=None, psp=None):
    """The scenarios with the menu's metadata, in file-name order (numbers compared as numbers). A pack's
    SCENARIO_GROUPS (e.g. "MCW") puts ids starting with those letters first, in that order."""
    out = []
    pack = None
    if not scenario_dir:
        try:
            pack = psps.load(psp)
        except ValueError as e:
            if (psp or DEFAULT_PSP).strip().lower() in psps.available():
                sys.exit(str(e))            # the pack is installed but refuses to load (e.g. a trial build that ended)
            return []
        scenario_dir = psps.scenarios_dir(pack)
        if not scenario_dir:
            return []
    flow = {g.lower(): i for i, g in enumerate(getattr(pack, "SCENARIO_GROUPS", "") or "")}

    def natural(p):
        name = os.path.basename(p).lower()
        group = re.match(r"([a-z])\d", name)
        rank = flow.get(group.group(1), len(flow)) if group else len(flow)
        return [rank] + [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]
    for path in sorted(glob.glob(os.path.join(scenario_dir, "*.yaml")), key=natural):
        with open(path, encoding="utf-8") as f:
            sc = yaml.safe_load(f) or {}
        needs = sc.get("needs") or []
        out.append({
            "path": path,
            "file": os.path.basename(path),
            "name": sc.get("name", os.path.basename(path)),
            "repeat": int(sc.get("repeat", 1)),
            "needs": [needs] if isinstance(needs, str) else list(needs),
            "run_all": bool(sc.get("run_all", True)),
            "run_all_note": sc.get("run_all_note", ""),
            "settle_after_s": float(sc.get("settle_after_s", DEFAULT_SETTLE_S)),
        })
    return out


def parse_pick(text, scenarios):
    """'3', '1-4,7', 'a' / 'all' -> the chosen scenarios, in list order, each once. Raises ValueError on bad input."""
    text = text.strip().lower()
    if text in ("a", "all"):
        return [s for s in scenarios if s["run_all"]]
    chosen = set()
    for part in filter(None, (p.strip() for p in text.split(","))):
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise ValueError(f"not a number or range: {part!r}")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo > hi:
            lo, hi = hi, lo
        for n in range(lo, hi + 1):
            if not 1 <= n <= len(scenarios):
                raise ValueError(f"there is no scenario {n} (1-{len(scenarios)})")
            chosen.add(n - 1)
    if not chosen:
        raise ValueError("nothing picked")
    return [scenarios[i] for i in sorted(chosen)]


def print_list(scenarios):
    print(f"\n {'#':>3}  {'scenario':<62} {'runs':>5}")
    for i, s in enumerate(scenarios, 1):
        mark = "*" if not s["run_all"] else " "
        print(f" {i:>2}{mark}  {s['name'][:62]:<62} {s['repeat']:>5}")
        if not s["run_all"]:
            print(f"        * not in 'all'{': ' + s['run_all_note'] if s['run_all_note'] else ''}")
        for n in s["needs"]:
            print(f"        needs: {n}")
    excluded = sum(not s["run_all"] for s in scenarios)
    print(f"   a  all{f' except the {excluded} marked * (pick those by number)' if excluded else ''}\n")


def parse_summary(output):
    """(runs, failed, inconclusive) from the runner's report, or None if it never got that far."""
    m = None
    for m in SUMMARY.finditer(output):
        pass
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def verdict(code, summary):
    if code == 4:
        return "not applicable (an optional feature your lab.yaml does not configure)"
    if summary is None:
        return "ERROR (no report: setup problem or crash, see the log)"
    if code == 0:
        return "passed"
    if code == 1:
        return "FAILED"
    if code == 3:
        return "UNTRUSTED (too many inconclusive runs)"
    return f"ERROR (exit {code})"


def run_one(s, repeat, passthrough, log_path):
    cmd = [sys.executable, "-u", "-m", "raceproof.runner", s["path"], *passthrough]
    if repeat:
        cmd += ["--repeat", str(repeat)]
    chunks = []
    with open(log_path, "w", encoding="utf-8") as log, \
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as p:
        try:
            for raw in iter(lambda: p.stdout.read1(4096), b""):
                text = raw.decode("utf-8", "replace")
                sys.stdout.write(text)
                sys.stdout.flush()
                log.write(text)
                chunks.append(text)
            code = p.wait()
        except KeyboardInterrupt:
            p.terminate()
            p.wait()
            raise
    return code, parse_summary("".join(chunks))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="raceproof menu", description="Pick scenarios by number and run them.")
    ap.add_argument("--list", action="store_true", help="print the scenarios and exit")
    ap.add_argument("--pick", help='e.g. "3", "1-4,7" or "all"; skips the questions')
    ap.add_argument("--repeat", type=int, help="runs per scenario (default: each scenario's own repeat)")
    ap.add_argument("--scenarios", default=None, help="scenario folder (default: the scenarios of lab.yaml's psp)")
    ap.add_argument("--results", default="results", help="where the logs go (default: ./results)")
    for opt in ("--config", "--fake", "--db", "--max-inconclusive"):
        ap.add_argument(opt)
    args = ap.parse_args(argv)
    passthrough = [x for opt in ("config", "fake", "db", "max_inconclusive") if getattr(args, opt) is not None
                   for x in (f"--{opt.replace('_', '-')}", str(getattr(args, opt)))]

    psp = configured_psp(args.config)
    scenarios = discover(args.scenarios, psp)
    if not scenarios:
        sys.exit(f"no scenarios found in {args.scenarios or f'the {psp} pack (installed: ' + ', '.join(sorted(psps.available())) + ')'}")
    if not args.scenarios:
        print(f"\nPSP pack: {psp} (lab.yaml psp:)")
    print_list(scenarios)
    if args.list:
        return 0

    repeat = args.repeat
    if args.pick:
        chosen = parse_pick(args.pick, scenarios)
    else:
        while True:
            try:
                answer = input("Pick (e.g. 3, 1-4,7, a; q to quit): ")
            except EOFError:
                return 0
            if answer.strip().lower() in ("q", "quit", ""):
                return 0
            try:
                chosen = parse_pick(answer, scenarios)
                break
            except ValueError as e:
                print(f"  {e}")
        if repeat is None:
            r = input("Runs per scenario (blank = each scenario's own): ").strip()
            repeat = int(r) if r.isdigit() and int(r) > 0 else None

    out_dir = os.path.join(args.results, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    rows, started = [], time.time()
    try:
        for i, s in enumerate(chosen):
            if i:
                pause = chosen[i - 1]["settle_after_s"]
                if pause > 0:
                    print(f"\n(settling {pause:g} s after {chosen[i - 1]['file']})")
                    time.sleep(pause)
            print(f"\n=== [{i + 1}/{len(chosen)}] {s['name']}  ({s['file']}, {repeat or s['repeat']} runs)")
            for n in s["needs"]:
                print(f"    needs: {n}")
            t0 = time.time()
            code, summary = run_one(s, repeat, passthrough, os.path.join(out_dir, s["file"].replace(".yaml", ".log")))
            rows.append((s, code, summary, time.time() - t0))
    except KeyboardInterrupt:
        print("\n\nstopped (Ctrl+C); summary of the scenarios that finished:")

    print(f"\n{'=' * 100}\nSummary ({len(rows)} of {len(chosen)} scenario(s), {(time.time() - started) / 60:.1f} min)  logs: {out_dir}\n")
    print(f"  {'scenario':<58} {'runs':>5} {'failed':>7} {'inconcl.':>9} {'time':>7}  result")
    for s, code, summary, took in rows:
        runs, failed, incon = summary or ("-", "-", "-")
        print(f"  {s['name'][:58]:<58} {runs:>5} {failed:>7} {incon:>9} {took / 60:>5.1f}m  {verdict(code, summary)}")
    return exit_code(rows, len(chosen))


def exit_code(rows, n_chosen):
    """1 if any scenario failed, else 2 if any had no report, else 3 if any was untrusted, else 130 if stopped early, else 0."""
    verdicts = [verdict(code, summary) for _, code, summary, _ in rows]
    for prefix, code in (("FAILED", 1), ("ERROR", 2), ("UNTRUSTED", 3)):
        if any(v.startswith(prefix) for v in verdicts):
            return code
    return 130 if len(rows) < n_chosen else 0


if __name__ == "__main__":
    sys.exit(main())
