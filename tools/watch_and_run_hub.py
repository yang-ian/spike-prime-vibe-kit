from __future__ import annotations

import argparse
import os
import platform
import signal
import sys
import time
from pathlib import Path


def ensure_project_python() -> None:
    root_dir = Path(__file__).resolve().parents[1]
    venv_dir = root_dir / ".venv"
    if platform.system() == "Windows":
        project_python = venv_dir / "Scripts" / "python.exe"
    else:
        project_python = venv_dir / "bin" / "python"

    if os.environ.get("SPIKE_RUNNING_IN_PROJECT_VENV") == "1":
        return
    if not project_python.exists():
        return
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return

    env = os.environ.copy()
    env["SPIKE_RUNNING_IN_PROJECT_VENV"] = "1"
    os.execve(
        str(project_python),
        [str(project_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        env,
    )


ensure_project_python()

from spike_build import BuildError, collect_watch_files
from spike_hub import ensure_hub_session_started, push_to_hub_in_session, shutdown_hub_session


def snapshot(paths: list[Path]) -> dict[Path, tuple[int, int]]:
    state: dict[Path, tuple[int, int]] = {}
    for path in paths:
        if not path.exists():
            continue
        stat = path.stat()
        state[path] = (stat.st_mtime_ns, stat.st_size)
    return state


def install_stop_signal_handlers() -> None:
    # PyCharm's red Stop button usually sends SIGTERM instead of a keyboard
    # interrupt. Translate both common stop signals into KeyboardInterrupt so
    # the watcher can shut down the Hub session cleanly either from the IDE or
    # from a normal Ctrl+C in the terminal.
    def handle_stop_signal(_: int, __: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch src/ and push the program to the Hub over Bluetooth")
    parser.add_argument("--config", type=Path, default=None, help="Path to spike-build.json")
    parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds")
    parser.add_argument("--debounce", type=float, default=0.5, help="Quiet period before rebuilding")
    args = parser.parse_args()

    install_stop_signal_handlers()

    try:
        session = ensure_hub_session_started(args.config)
        print(f"Bluetooth session connected to {session.hub_name} ({session.device_identifier})")
        result = push_to_hub_in_session(args.config)
        print(f"Initial Hub upload complete for slot {result.slot}")
    except BuildError as exc:
        print(f"Initial Hub upload failed: {exc}")

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
                result = push_to_hub_in_session(args.config)
            except BuildError as exc:
                print(f"Hub upload failed: {exc}")
            else:
                current = snapshot(collect_watch_files(args.config))
                print(f"Uploaded and started slot {result.slot}")
            dirty_since = None
    except KeyboardInterrupt:
        try:
            shutdown_hub_session(args.config, stop_running=True)
            print("\nStopped Hub program and disconnected Bluetooth session.")
        except BuildError as exc:
            print(f"\nWatcher stopped, but Hub cleanup failed: {exc}")
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
