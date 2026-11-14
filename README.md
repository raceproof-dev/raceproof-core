# raceproof

Race conditions in 3-D Secure payment flows only show up when two things happen at exactly the wrong moment: the
shopper's challenge result arrives as your gateway's timeout gives up, a success webhook overtakes a lost API answer,
a retry reuses an idempotency key while the first call is still in progress. raceproof makes those moments happen on
purpose, hundreds of times, against a fake PSP, and checks what your gateway did each time.

- **Scenarios** in YAML: the steps of a payment flow, the race to aim at, and what a correct gateway must have done
  (one final status, the merchant told once, no stray refusal markers).
- **A runner** that repeats a scenario with jitter, tells failed runs from inconclusive ones (the environment was not
  ready), measures whether each run actually hit the race window, and can sweep a timing setting to find it.
- **A menu** that runs a set of scenarios and summarises them, with logs per scenario.
- **PSP packs**: a fake PSP and its scenarios, plugged in through a small contract (`raceproof/psps.py`). raceproof
  ships with a demo pack; packs for real PSPs are separate products.

## Try it in two minutes

```
pip install raceproof
raceproof demo                                   # terminal 1: a demo PSP and a demo shop with a real race
raceproof menu --config raceproof-demo.yaml      # terminal 2: pick R1 and run it
```

The demo shop's two paths (the shopper's result and its own timeout) both send a result to the PSP with the same
idempotency key. With its fix on, R1 passes every run. Switch the fix off and run R1 again:

```
curl -X PUT -H 'Content-Type: application/json' -d '{"race": false}' http://127.0.0.1:9301/_demo/fixes
```

Now some runs fail: the merchant is told two final statuses for one payment. The report says how many runs hit the
race window and in what order the two completions reached the PSP.

## Commands

| Command | What it does |
|---|---|
| `raceproof menu [--list] [--pick 1-3] [--config lab.yaml]` | list the scenarios of the configured pack, run the picked ones, summarise |
| `raceproof run SCENARIO [--repeat N] [--trace] [--sweep timing.x=a:b:step]` | run one scenario: a file, or its id such as `R1` |
| `raceproof fake [PSP] [--port N]` | start a pack's fake PSP |
| `raceproof demo` | start the demo PSP and shop, write `raceproof-demo.yaml` |
| `raceproof packs` | list the installed packs |

Exit codes of `run`: 0 passed, 1 failed, 3 too many inconclusive runs to trust the result, 4 not applicable (the
scenario tests an optional feature your config does not set, see `applies_if`).

## PSP packs

The demo pack shows the idea on a toy shop. The races of real PSPs need a fake that answers as the real PSP does
(measured in its sandbox: idempotency replays and "already in progress" errors, webhook timing and retries). That is
what the commercial packs add:

| Pack | Fake | Scenarios |
|---|---|---|
| `raceproof-adyen` | Adyen's native 3DS2 (`/payments`, `/payments/details`, the issuer's ACS, signed webhooks) | M1 (method step), C1-C5 (challenge), W1-W4 (webhooks) |
| `raceproof-stripe` | Stripe PaymentIntents with Stripe's 3DS page, signed webhooks | S1-S9 |

Each pack ships an example config for [paygate-demo](https://github.com/raceproof-dev/paygate-demo), an open-source
gateway with two of these races (C3 and S1) behind runtime switches, so you can watch a scenario fail and pass before
pointing it at your own gateway. A time-limited trial of the packs is available on request.

## Your gateway

Everything specific to your system lives in one config file (`lab.yaml`): your gateway's URLs, how to read a
payment's status from your database, what your status values are, your timeout. Scenarios refer to it by name
(`${gateway.base_url}`, `tx_status: {is: refused}`, `named: merchant_final_count`), so the same scenario runs against
any gateway. See the docstring of `raceproof/runner.py` for every step and assertion.

## Writing a PSP pack

A pack is a Python package registered under the entry-point group `raceproof.psps`. It provides the fake PSP (with
the control API the runner uses), its scenarios, and a few hooks; `raceproof/psps.py` documents the contract and
`raceproof/demo/` is a complete small example.

## License

Apache-2.0. See [LICENSE](https://github.com/raceproof-dev/raceproof-core/blob/main/LICENSE).
