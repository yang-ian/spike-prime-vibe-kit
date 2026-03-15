from __future__ import annotations

import argparse
import time
from pathlib import Path

from spike_build import BuildError, build_project, collect_watch_files


def snapshot(paths: list[Path]) -> dict[Path, tuple[int, int]]:
    state: dict[Path, tuple[int, int]] = {}
    for path in paths:
        if not path.exists():
            continue
        stat = path.stat()
        state[path] = (stat.st_mtime_ns, stat.st_size)
    return state

def main() -> int:
    parser = argparse.ArgumentParser(description="Watch src/ and rebuild dist/car.llsp3")
    parser.add_argument("--config", type=Path, default=None, help="Path to spike-build.json")
    parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds")
    parser.add_argument("--debounce", type=float, default=0.5, help="Quiet period before rebuilding")
    args = parser.parse_args()

    try:
        result = build_project(args.config)
        print(f"Initial build: {result.dist_path}")
    except BuildError as exc:
        print(f"Initial build failed: {exc}")

    watch_paths = collect_watch_files(args.config)
    current = snapshot(watch_paths)
    dirty_since: float | None = None

    print("Watching for changes. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(args.interval)
            watch_paths = collect_watch_files(args.config)
            latest = snapshot(watch_paths)
            if latest != current:
                current = latest
                dirty_since = time.monotonic()
                continue
            if dirty_since is None:
                continue
            if time.monotonic() - dirty_since < args.debounce:
                continue
            try:
                result = build_project(args.config)
            except BuildError as exc:
                print(f"Build failed: {exc}")
            else:
                print(f"Rebuilt {result.dist_path}")
            dirty_since = None
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
