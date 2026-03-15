# SPIKE Prime Vibe Kit

SPIKE Prime Vibe Kit is a **PyCharm-first LEGO SPIKE Prime starter kit** for families, classrooms, and young makers.

The main workflow is simple:

1. open the project in PyCharm
2. click `SPIKE Start`
3. edit Python in `src/`
4. save the file
5. watch the Hub hot reload over Bluetooth on macOS

If you prefer the official LEGO SPIKE app, the repo can still build a normal `.llsp3` package as a fallback.

## Best For

- parents teaching children with real Python files
- kids learning Python with AI tools such as Codex or Claude Code
- fast SPIKE Prime iteration in PyCharm instead of editing embedded code strings

## Why This Project Exists

The official SPIKE app is friendly, but it is not ideal for larger editing workflows. This repo makes it easier to:

- write code in a full editor
- keep source code in regular `.py` files
- use PyCharm as the main coding experience
- rebuild and upload code automatically
- keep the code readable and heavily commented for beginners

## Main Features

- PyCharm-first workflow
- direct Bluetooth upload to the Hub on macOS
- save-to-run hot reload during an active watch session
- normal `.llsp3` package generation for the official SPIKE app
- shared `SPIKE Start` and `SPIKE Stop` run configurations
- a tiny beginner starter example that shows `HI` on the Hub

## What “Hot Reload” Means Here

In this project, hot reload means:

- you save a Python file in `src/`
- the watcher rebuilds the SPIKE project automatically
- the latest code is uploaded to the Hub automatically
- the Hub program is restarted automatically

This is not in-process code patching. It is a fast save -> rebuild -> upload -> restart loop designed for SPIKE Prime development.

## Platform Support

| Workflow | macOS | Windows | Linux |
| --- | --- | --- | --- |
| Edit Python source | Yes | Yes | Yes |
| Build `.llsp3` package | Yes | Yes | Yes |
| Import `.llsp3` into LEGO SPIKE app | Yes | Yes | Yes |
| Direct Bluetooth upload | Yes | No | No |
| Save-triggered hot reload | Yes | No | No |

Direct Bluetooth upload and hot reload are macOS-only because they use the included native Swift helper app.

## Quick Start: Recommended PyCharm Workflow

### 1. Clone the repository

```bash
git clone https://github.com/yang-ian/spike-prime-vibe-kit.git
cd spike-prime-vibe-kit
```

### 2. Create a virtual environment

On macOS or Linux:

```bash
./scripts/setup_venv.sh
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.\scripts\setup_venv.ps1
.\.venv\Scripts\Activate.ps1
```

### 3. Open the repo in PyCharm

This repo includes shared run configurations:

- `SPIKE Start`
- `SPIKE Stop`

### 4. Configure your Hub name

Open `spike-build.json` and set:

```json
"target_name": "My SPIKE Hub"
```

Replace that value with the Bluetooth name shown by macOS for your Hub.

You can leave `device_uuid` empty on the first run. The tool caches it after a successful connection.

### 5. Start the Bluetooth session

Close the LEGO SPIKE app first, then run `SPIKE Start` in PyCharm.

That starts a long-lived Bluetooth session and enables hot reload.

### 6. Edit and save

Edit `src/main.py`, save the file, and the Hub should rebuild, re-upload, and restart automatically over Bluetooth.

### 7. Stop cleanly

Use either:

- PyCharm's red Stop button on the running `SPIKE Start` session
- the shared `SPIKE Stop` run configuration
- `python3 tools/stop_hub.py --shutdown-session`

## Bluetooth Support on macOS

Bluetooth is a headline feature of this repo, not an afterthought.

The macOS workflow supports:

- one-time direct upload with `python3 tools/push_to_hub.py`
- long-lived watch sessions with `python3 tools/watch_and_run_hub.py`
- save-triggered hot reload during the session
- clean stop and disconnect behavior

The uploader uses the included Swift helper app because raw command-line Python BLE access is unreliable on this macOS setup.

### Bluetooth setup checklist

1. Pair the Hub with macOS
2. Make sure the Hub is powered on
3. Set the correct `hub.target_name` in `spike-build.json`
4. Close the LEGO SPIKE app
5. Start `SPIKE Start` in PyCharm or run `python3 tools/watch_and_run_hub.py`

## PyCharm Workflow Details

PyCharm is the recommended editor for this repo.

Why PyCharm is the main path:

- it is easier for children to see real files and folders
- it makes AI-assisted coding easier to follow
- the shared Start/Stop buttons reduce terminal friction
- the hot reload loop feels much closer to a real programming environment

Recommended classroom flow:

1. open the repo in PyCharm
2. click `SPIKE Start`
3. edit code in `src/`
4. save to hot reload on the Hub
5. click Stop when the lesson is finished

## Fallback Workflow: Build an `.llsp3` Package

If you want to stay with the official LEGO SPIKE app workflow, this repo still supports it.

Build once:

```bash
python3 tools/build_llsp3.py
```

Watch and rebuild automatically:

```bash
python3 tools/watch_llsp3.py
```

Validate the generated package:

```bash
python3 -m json.tool build/projectbody.json
python3 -m json.tool build/manifest.json
unzip -l dist/spike-prime-vibe-kit.llsp3
```

Then import `dist/spike-prime-vibe-kit.llsp3` into the official LEGO SPIKE app.

This fallback path is useful for users who are not on macOS or who prefer the official app import flow.

## Project Layout

- `src/`: editable Python source files
- `assets/`: SPIKE metadata such as `manifest.json` and `icon.svg`
- `tools/`: build, watch, upload, and helper scripts
- `build/`: generated intermediate files
- `dist/`: generated `.llsp3` package

Do not edit `build/` or `dist/` by hand.

## The Starter Example

The default starter example lives in `src/main.py`.

It only does one thing:

- display `HI` on the Hub screen

That makes it safe for first-time users. Once the toolchain is working, you can replace the file with motors, sensors, sounds, animations, or your own robot project.

## Troubleshooting

### The Bluetooth uploader cannot find my Hub

- make sure the Hub is powered on
- make sure the Hub name in `spike-build.json` matches macOS
- close the LEGO SPIKE app
- try `python3 tools/push_to_hub.py` first before using the watcher

### Hot reload is not working

- make sure `SPIKE Start` is still running
- make sure you are editing files inside `src/`
- make sure the Bluetooth session is not blocked by the LEGO SPIKE app
- try restarting the watcher

### Python crashes when scanning Bluetooth

This repo avoids that macOS problem by using a native Swift helper app. Use the provided scripts instead of trying to replace the uploader with raw Python BLE code.

### I want to use the official SPIKE app instead

Use the `.llsp3` fallback workflow with `python3 tools/build_llsp3.py` or `python3 tools/watch_llsp3.py`.

## Development Notes

- keep code in `src/`
- keep comments beginner-friendly
- prefer explicit code over clever shortcuts
- treat this project as a learning template first and a robot project second

## License

MIT
