# Repository Guidelines

## Project Structure & Module Organization
`src/` is the editable source tree. `src/main.py` is the starter entrypoint, and additional modules may live anywhere under `src/`. `assets/` holds the SPIKE metadata inputs. `tools/` contains the build and Hub-upload tooling. `build/` and `dist/` are generated outputs and must not be edited by hand.

Keep publishable source in `src/`, `assets/`, `tools/`, and top-level docs/config files. Do not commit local build output, virtual environments, or personal Hub identifiers.

## Build, Test, and Development Commands
Use these commands:

- `python3 tools/build_llsp3.py`
- `python3 tools/watch_llsp3.py`
- `python3 tools/push_to_hub.py`
- `python3 tools/stop_hub.py`
- `python3 tools/stop_hub.py --shutdown-session`
- `python3 tools/watch_and_run_hub.py`
- `python3 -m json.tool build/projectbody.json`
- `python3 -m json.tool build/manifest.json`
- `unzip -l dist/spike-prime-vibe-kit.llsp3`

Create a local virtual environment with `./scripts/setup_venv.sh` on macOS/Linux or `.\scripts\setup_venv.ps1` on Windows.

## Coding Style & Naming Conventions
Use 4-space indentation and straightforward Python. Favor simple control flow, descriptive names, and educational comments over compact tricks. This repo is meant for children and parents, so comments in `src/` should explain what the code is doing and why.

Keep beginner-facing examples small. If you add a more advanced example, keep the default starter path easy to understand and safe to run.

## Product Positioning
This repo is PyCharm-first. Treat PyCharm as the main recommended editor and `SPIKE Start` / `SPIKE Stop` as the primary child-friendly controls.

The main user-facing story is:

- edit in PyCharm
- start one Bluetooth session
- save files in `src/`
- hot reload on the Hub over Bluetooth

Keep `.llsp3` generation available, but present it as a fallback workflow rather than the primary development path.

## Bluetooth Workflow Expectations
Bluetooth on macOS is a headline feature of this project. Preserve clear support for:

- direct upload
- long-lived watch sessions
- save-triggered hot reload
- clean stop and disconnect behavior

The watcher should keep a long-lived BLE session open during a coding session, and it should shut down cleanly when the user stops the watcher from PyCharm or the terminal.

The public template config must stay sanitized:

- `hub.target_name` should remain a placeholder in repo-tracked defaults
- `hub.device_uuid` should stay empty in committed files
- `hub.bt_address` should stay empty in committed files

Never commit personal Hub names, UUIDs, or Bluetooth addresses.

## Documentation Maintenance
If you change the project structure, setup steps, build flow, Bluetooth workflow, hot reload behavior, or user-facing defaults, update both `README.md` and `AGENTS.md` in the same change.

## Testing Guidelines
At minimum, validate:

- `python3 tools/build_llsp3.py`
- `python3 -m py_compile tools/*.py`
- `python3 -m json.tool build/projectbody.json`
- `python3 -m json.tool build/manifest.json`
- `unzip -l dist/spike-prime-vibe-kit.llsp3`

If the Bluetooth path changes, also test `tools/push_to_hub.py`, `tools/watch_and_run_hub.py`, and `tools/stop_hub.py --shutdown-session` on macOS.

## Commit & Pull Request Guidelines
Use short, imperative commit messages. Pull requests should explain what changed for learners, mention touched files or workflows, and note how the change was tested.
