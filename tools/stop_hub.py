from __future__ import annotations

import argparse
import os
import platform
import sys
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

from spike_build import BuildError
from spike_hub import stop_hub_program


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop the currently running program on the Hub over Bluetooth")
    parser.add_argument("--config", type=Path, default=None, help="Path to spike-build.json")
    parser.add_argument(
        "--shutdown-session",
        action="store_true",
        help="Also shut down the long-lived Bluetooth development session after stopping the program",
    )
    args = parser.parse_args()

    try:
        result = stop_hub_program(args.config, shutdown_session=args.shutdown_session)
    except BuildError as exc:
        print(f"Hub stop failed: {exc}")
        return 1

    if args.shutdown_session:
        print(f"Stopped slot {result.slot} on {result.hub_name} ({result.device_identifier}) and shut down the session")
    else:
        print(f"Stopped slot {result.slot} on {result.hub_name} ({result.device_identifier})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
