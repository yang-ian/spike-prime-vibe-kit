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
from spike_hub import push_to_hub


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the project and upload it to the Hub over Bluetooth")
    parser.add_argument("--config", type=Path, default=None, help="Path to spike-build.json")
    args = parser.parse_args()

    try:
        result = push_to_hub(args.config)
    except BuildError as exc:
        print(f"Hub upload failed: {exc}")
        return 1

    print(f"Wrote {result.build_result.hub_program_path}")
    print(f"Uploaded slot {result.slot} on {result.hub_name} ({result.device_identifier})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
