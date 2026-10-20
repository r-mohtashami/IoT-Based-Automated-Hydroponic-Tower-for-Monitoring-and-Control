"""Explicit reset utility for simulator-owned persistent virtual device state."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def reset_state(path: str) -> bool:
    state_path = Path(path)
    if not state_path.exists():
        return False
    state_path.unlink()
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Explicitly remove simulator-owned persistent virtual Tower state."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Required confirmation that simulator state should be removed.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.reset:
        raise SystemExit("Refusing to modify simulator state without explicit --reset.")
    path = os.getenv("SIM_STATE_PATH", "/simulator-state/state.json")
    removed = reset_state(path)
    print("Simulator state reset." if removed else "Simulator state already empty.")
    print(f"state_path={path}")


if __name__ == "__main__":
    main()
