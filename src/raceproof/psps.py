"""PSP packs: the plug-in contract between raceproof and a PSP.

A pack is an installed Python package registered under the entry-point group `raceproof.psps`, e.g. in its
pyproject.toml:

    [project.entry-points."raceproof.psps"]
    acme = "raceproof_acme"

lab.yaml's `psp:` names the pack to use (default: demo, the toy pack shipped with raceproof). `raceproof packs` lists
the installed ones.

The pack's module gives the runner everything that differs between PSPs:

  NAME, TITLE                  "acme", "Acme" (TITLE is used in messages: "check its Acme base URL")
  SCENARIOS                    folder of the pack's scenarios (the menu lists it), or None
  FAKE_APP                     the pack's fake PSP as "module:attribute" for uvicorn (`raceproof fake <psp>`)
  FAKE_PORT                    the fake's default port
  SCENARIO_GROUPS              optional: scenario id letters in menu order, e.g. "MCW" lists M1, M2, C1.. before W1..
  is_stray(call, trans_id)     True if a call the fake logged belongs to ANOTHER transaction (an earlier run's straggler)
  describe_call(call)          one line for --trace: which step the app is on
  compute(spec)                the value of a `compute:` step (the pack's own builders); raises ValueError for a spec
                               it does not know
  error_code(text)             the PSP's error code in a response body, for the `error_codes` assertion, or None
  after_http(h, response)      checks after an `http` step (e.g. the app rejected a webhook the fake signed); raise
                               RuntimeError to fail the run
  RACE                         the race the reports measure, or None (then the report shows runs and failures only):
                                 path           the fake's call log path to read
                                 label(call)    what a call there is (e.g. "shopper", "timeout"), or None to ignore it
                                 posted, rival  the two labels; the report times `posted` against `rival`
                                 rival_text     how the report names the rival ("the gateway's timeout")
                                 rival_short    the rival in short (default: rival)
                                 starts_clock(h, body)  True for the http step that posts `posted` (sets its time)

Every fake offers the same control API, which the runner relies on: POST /_control/reset, POST /_control/script (body:
per-queue answers), GET /_control/calls?path=... (list of {"t", "path", "body", "key", ...}).
"""
import os
from importlib import import_module
from importlib.metadata import entry_points

GROUP = "raceproof.psps"
DEFAULT = "demo"


def available():
    """{name: entry point} of the installed packs."""
    return {ep.name.lower(): ep for ep in entry_points(group=GROUP)}


def load(name=None):
    name = (name or DEFAULT).strip().lower()
    if not name.isidentifier():
        raise ValueError(f"psp {name!r} is not a valid name")
    packs = available()
    if name not in packs:
        if name == DEFAULT:                      # running from a source checkout that was never pip-installed
            return import_module("raceproof.demo")
        have = ", ".join(sorted(packs)) or "none"
        raise ValueError(f"unknown psp {name!r}: no installed pack registers it (installed: {have})")
    return packs[name].load()


def scenarios_dir(pack):
    folder = getattr(pack, "SCENARIOS", None)
    return os.path.abspath(folder) if folder else None


def owner_of(path):
    """The NAME of the installed pack whose scenario folder holds this scenario file, or None."""
    here = os.path.dirname(os.path.abspath(path))
    for name in available():
        try:
            pack = load(name)
        except Exception:  # noqa: BLE001 (a broken pack must not stop scenarios of other packs)
            continue
        if scenarios_dir(pack) == here:
            return pack.NAME
    return None
