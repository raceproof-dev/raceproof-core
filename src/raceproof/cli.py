"""The raceproof command.

  raceproof menu [...]          pick scenarios by number and run them, with a summary (raceproof menu --help)
  raceproof run SCENARIO [...]  run one scenario (raceproof run --help)
  raceproof fake [PSP] [--port N]   start a pack's fake PSP (default: lab.yaml's psp)
  raceproof demo [--port N]     start the demo PSP and the demo shop, and write raceproof-demo.yaml for them
  raceproof packs               list the installed PSP packs
"""
import argparse
import asyncio
import os
import sys

from raceproof import __version__, psps

DEMO_CONFIG = "raceproof-demo.yaml"


def _uvicorn():
    try:
        import uvicorn
    except ImportError:
        sys.exit("this needs uvicorn: pip install uvicorn")
    return uvicorn


def cmd_fake(argv):
    ap = argparse.ArgumentParser(prog="raceproof fake", description="Start a PSP pack's fake PSP.")
    ap.add_argument("psp", nargs="?", help="the pack (default: lab.yaml's psp, else demo)")
    ap.add_argument("--port", type=int, help="default: the pack's own port")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    from raceproof.menu import configured_psp
    try:
        pack = psps.load(args.psp or configured_psp())
    except ValueError as e:
        sys.exit(str(e))
    app = getattr(pack, "FAKE_APP", None)
    if not app:
        sys.exit(f"the {pack.NAME} pack has no fake")
    port = args.port or getattr(pack, "FAKE_PORT", 9100)
    print(f"{pack.TITLE} fake on http://{args.host}:{port}", flush=True)
    _uvicorn().run(app, host=args.host, port=port, timeout_keep_alive=120, log_level="warning")


def demo_config(db_path, psp_port, shop_port, timeout_ms):
    db = os.path.abspath(db_path).replace(os.sep, "/")
    return f"""# raceproof demo: the demo shop and the demo PSP, both started by `raceproof demo`.
psp: demo
fake: http://127.0.0.1:{psp_port}
gateway:
  base_url: http://127.0.0.1:{shop_port}
db:
  dsn: sqlite:///{db}
  table: payments
  id_column: id
  status_column: status
  key: ${{trans_id}}
status:
  pending_challenge: PENDING
  completed: COMPLETED
  refused: REFUSED
  failed: FAILED
timing:
  watchdog_ms: {timeout_ms}          # the shop's timeout
queries:
  merchant_final_count: "SELECT count(*) FROM notifications WHERE payment_id = '${{trans_id}}'"
  is_final: "SELECT count(*) FROM payments WHERE id = '${{trans_id}}' AND status <> 'PENDING'"
"""


def cmd_demo(argv):
    ap = argparse.ArgumentParser(prog="raceproof demo",
                                 description="Start the demo PSP and the demo shop (a toy gateway with a race).")
    ap.add_argument("--port", type=int, default=9300, help="the demo PSP's port; the shop uses the next one")
    ap.add_argument("--timeout-ms", type=int, default=2000, help="the shop's timeout")
    ap.add_argument("--fix", choices=["on", "off"], default="on", help="start with the shop's race fix on or off")
    ap.add_argument("--db", default="raceproof-demo.db", help="the shop's SQLite file")
    args = ap.parse_args(argv)
    shop_port = args.port + 1
    os.environ.update(RACEPROOF_DEMO_PSP=f"http://127.0.0.1:{args.port}", RACEPROOF_DEMO_DB=os.path.abspath(args.db),
                      RACEPROOF_DEMO_TIMEOUT_MS=str(args.timeout_ms), RACEPROOF_DEMO_FIX="true" if args.fix == "on" else "false")
    with open(DEMO_CONFIG, "w", encoding="utf-8") as f:
        f.write(demo_config(args.db, args.port, shop_port, args.timeout_ms))
    uvicorn = _uvicorn()
    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_keep_alive=120))
               for app, port in (("raceproof.demo.fake:app", args.port), ("raceproof.demo.shop:app", shop_port))]
    print(f"demo PSP  http://127.0.0.1:{args.port}\ndemo shop http://127.0.0.1:{shop_port}   (race fix {args.fix})\n"
          f"wrote {DEMO_CONFIG}\n\nIn another terminal, in this folder:\n"
          f"  raceproof menu --config {DEMO_CONFIG}\n"
          f"Switch the shop's fix off or on while it runs:\n"
          f"  curl -X PUT -H 'Content-Type: application/json' -d '{{\"race\": false}}' http://127.0.0.1:{shop_port}/_demo/fixes\n"
          f"Ctrl+C stops both.", flush=True)

    async def serve():
        await asyncio.gather(*(s.serve() for s in servers))
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


def cmd_packs(argv):
    packs = psps.available()
    if not packs:
        print("no PSP packs installed (the demo pack runs from the source tree)")
    for name in sorted(packs):
        try:
            pack = packs[name].load()
            print(f"  {name:<10} {pack.TITLE}  ({packs[name].value})")
        except Exception as e:  # noqa: BLE001
            print(f"  {name:<10} cannot load: {e}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] in ("-V", "--version"):
        print(f"raceproof {__version__}")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "menu":
        from raceproof import menu
        return menu.main(rest)
    if cmd == "run":
        from raceproof import runner
        runner.cli(rest)
        return 0
    if cmd == "fake":
        return cmd_fake(rest)
    if cmd == "demo":
        return cmd_demo(rest)
    if cmd == "packs":
        return cmd_packs(rest)
    print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
