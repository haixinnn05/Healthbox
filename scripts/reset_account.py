"""Forgot the Healthbox password? Remove the local account so a new one can be made.

Needs physical access to this laptop. Only the account (name and password) is removed;
medicines, faces, label photos and history are kept. The next time the page opens it asks
for a new account. Setup is open until then.

    .venv/bin/python scripts/reset_account.py            # asks before removing anything
    .venv/bin/python scripts/reset_account.py --db data/doseguard.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.store import Store  # noqa: E402
from app.timeutil import Clock  # noqa: E402

CONFIRM_WORD = "RESET"


def reset_account(db_path: Path) -> bool:
    """Delete the account row. Returns True when an account was removed."""
    store = Store(db_path, Clock())
    try:
        with store.tx() as conn:
            gone = conn.execute("DELETE FROM account").rowcount > 0
            if gone:
                store.event(conn, "ACCOUNT_RESET", details={"by": "scripts/reset_account.py"}, demo=0)
        return gone
    finally:
        store.conn.close()


def main(argv: list[str] | None = None, ask=input) -> int:
    ap = argparse.ArgumentParser(description="Remove the local Healthbox account (keeps medicines, faces and history).")
    ap.add_argument("--db", type=Path, default=None, help="database file (default: DB_PATH or data/doseguard.db)")
    args = ap.parse_args(argv)
    db = args.db or Settings.from_env().db_path
    if not db.exists():
        print(f"No database at {db}. Nothing to reset.")
        return 1
    store = Store(db, Clock())
    row = store.q1("SELECT name FROM account WHERE account_id=1")
    store.conn.close()
    if not row:
        print("There is no account on this laptop. Nothing to reset.")
        return 0
    print(f"This removes the account for {row['name']!r} from {db}.")
    print("Medicines, faces, label photos and history are kept. A new account can be made in the app.")
    try:
        answer = ask(f"Type {CONFIRM_WORD} to remove the account: ")
    except EOFError:
        answer = ""
    if answer.strip() != CONFIRM_WORD:
        print("Cancelled. Nothing was changed.")
        return 1
    reset_account(db)
    print("The account was removed. Open Healthbox to make a new one. If the app is running, Setup is open until then.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
