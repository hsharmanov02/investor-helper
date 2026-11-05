"""Entry point. `InvestorHelper.exe` opens the window; `--once` runs headless."""
from __future__ import annotations

import argparse
import logging
import sys


def main() -> int:
    ap = argparse.ArgumentParser(prog="InvestorHelper", description="Advisory-only stock research helper.")
    ap.add_argument("--once", action="store_true", help="run one refresh, print the report and exit")
    ap.add_argument("--no-chrome", action="store_true", help="skip the live Chrome connection for this run")
    ap.add_argument("--check-chrome", action="store_true", help="test the live Chrome connection and exit")
    args = ap.parse_args()
    if args.check_chrome:
        from .config import load_settings
        from .sources.chrome_live import check_connection
        try:
            print(check_connection(load_settings().chrome_cdp_url))
            return 0
        except Exception as exc:
            print(f"Chrome check failed: {exc}")
            return 1
    if args.once:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        from .config import load_settings
        from .engine import Engine
        s = load_settings()
        if args.no_chrome:
            s.use_chrome_cdp = False
        md = Engine(settings=s).run_cycle()
        sys.stdout.reconfigure(encoding="utf-8")
        print(md)
        return 0
    from .gui.app import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
