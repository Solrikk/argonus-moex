"""Drive the integrated tick at aligned 30-second slots; --dry-run is no-I/O."""
from __future__ import annotations

import argparse
import fcntl
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from argonus.paths import PROJECT_ROOT as ROOT
MOSCOW = ZoneInfo("Europe/Moscow")


def due(now):
    return now.weekday()<5 and "06:50"<=now.strftime("%H:%M")<="23:45"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    lock = (ROOT/"runtime/opening_runner.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print("Candidate loop is already running")
        return 0
    previous_slot = None
    while True:
        now = datetime.now(MOSCOW)
        slot = int(now.timestamp())//30
        if slot != previous_slot and due(now):
            previous_slot = slot
            if args.dry_run:
                print(f"{now.isoformat()}: would run the integrated tick (no broker calls)", flush=True)
            elif not (ROOT/"PAUSE").exists():
                subprocess.run([str(ROOT/"scripts/run_tick.sh")], cwd=ROOT, check=False)
        elif args.once:
            print(f"{now.isoformat()}: outside the weekday session", flush=True)
        if args.once:
            return 0
        time.sleep(max(.2, 30-time.time()%30))


if __name__ == "__main__":
    raise SystemExit(main())
