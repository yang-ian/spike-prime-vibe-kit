from __future__ import annotations

import argparse
from pathlib import Path

from spike_build import BuildError, build_project


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a LEGO SPIKE .llsp3 package from src/")
    parser.add_argument("--config", type=Path, default=None, help="Path to spike-build.json")
    args = parser.parse_args()

    try:
        result = build_project(args.config)
    except BuildError as exc:
        print(f"Build failed: {exc}")
        return 1

    print(f"Wrote {result.hub_program_path}")
    print(f"Wrote {result.projectbody_path}")
    print(f"Wrote {result.manifest_path}")
    print(f"Wrote {result.dist_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
