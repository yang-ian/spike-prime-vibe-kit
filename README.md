# SPIKE Prime Vibe Kit

SPIKE Prime Vibe Kit is an open-source starter project for families, classrooms, and young makers who want to write LEGO SPIKE Prime Python code in a real editor such as PyCharm, then either:

- build an `.llsp3` file for the official LEGO SPIKE app
- or upload code directly to a Hub over Bluetooth on macOS

The project is designed for AI-assisted learning. The code stays in normal Python files, and the tooling handles the LEGO packaging work for you.

## Why This Project Exists

The official SPIKE app is friendly, but it is not ideal for larger editing workflows. This repo makes it easier to:

- write code in a full editor
- keep source code in regular `.py` files
- rebuild a runnable SPIKE package automatically
- teach children with clear, commented Python
- use AI coding tools without hiding the project structure

## Features

- Normal Python source files in `src/`
- Automatic `.llsp3` package generation
- macOS Bluetooth upload workflow for fast iteration
- PyCharm-friendly start and stop run configurations
- A tiny beginner starter example that shows `HI` on the Hub

## Platform Support

| Workflow | macOS | Windows | Linux |
| --- | --- | --- | --- |
| Edit Python source | Yes | Yes | Yes |
| Build `.llsp3` package | Yes | Yes | Yes |
| Import `.llsp3` into LEGO SPIKE app | Yes | Yes | Yes |
| Direct Bluetooth upload to Hub | Yes | No | No |

The direct Bluetooth uploader is macOS-only because it uses a native Swift helper app.

## Project Layout

- `src/`: editable Python source files
- `assets/`: SPIKE metadata such as `manifest.json` and `icon.svg`
- `tools/`: build, watch, upload, and helper scripts
- `build/`: generated intermediate files
- `dist/`: generated `.llsp3` package

Do not edit `build/` or `dist/` by hand.

## Requirements

- Python 3.9 or newer
- macOS if you want direct Bluetooth upload
- Xcode Command Line Tools on macOS for the Bluetooth helper build
- LEGO SPIKE Prime Hub and the official SPIKE firmware

## Quick Start

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

## Build an `.llsp3` Package

Build once:

```bash
python3 tools/build_llsp3.py
```

Watch `src/` and rebuild automatically:

```bash
python3 tools/watch_llsp3.py
```

Validate the generated files:

```bash
python3 -m json.tool build/projectbody.json
python3 -m json.tool build/manifest.json
unzip -l dist/spike-prime-vibe-kit.llsp3
```

Then import `dist/spike-prime-vibe-kit.llsp3` into the official LEGO SPIKE app.

## Direct Bluetooth Workflow on macOS

This workflow is faster than repeated manual imports into the SPIKE app.

### 1. Pair your Hub with macOS

Make sure the Hub is powered on and visible to macOS before you try the uploader.

### 2. Update `spike-build.json`

Set the Hub name to match the name shown by macOS.

Example:

```json
"hub": {
    "transport": "macos_helper",
    "default_slot": 0,
    "auto_stop_before_start": true,
    "target_name": "My SPIKE Hub",
    "device_uuid": "",
    "bt_address": ""
}
```

You can leave `device_uuid` empty on the first run. The tool caches it after a successful connection.

### 3. Close the LEGO SPIKE app

Do not keep the LEGO SPIKE app connected while using the direct uploader.

### 4. Upload once

```bash
python3 tools/push_to_hub.py
```

### 5. Start a watch session

```bash
python3 tools/watch_and_run_hub.py
```

Once that session is running, every save in `src/` uploads the latest code to the Hub over the same Bluetooth session.

Stop the running program:

```bash
python3 tools/stop_hub.py
```

Stop the program and disconnect the long-lived Bluetooth session:

```bash
python3 tools/stop_hub.py --shutdown-session
```

## PyCharm Workflow

You can use PyCharm as the main editor for this project.

Shared run configurations are included:

- `SPIKE Start`: start the Bluetooth watch-and-run session
- `SPIKE Stop`: stop the Hub program and disconnect the session

Recommended classroom flow:

1. Open the repo in PyCharm
2. Run `SPIKE Start`
3. Edit `src/main.py`
4. Save changes
5. Watch the Hub update
6. Click PyCharm's red Stop button when done

## The Starter Example

The default starter example lives in `src/main.py`.

It only does one thing:

- display `HI` on the Hub screen

That makes it safe for first-time users. Once the toolchain is working, you can replace the file with motors, sensors, sounds, animations, or your own robot project.

## Typical First Changes

- Replace `HI` with your own message
- Add motor control in `src/main.py`
- Split code into helper modules under `src/`
- Change the package name in `assets/manifest.json`
- Change the output file name in `spike-build.json`

## Troubleshooting

### The Bluetooth uploader cannot find my Hub

- Make sure the Hub is powered on
- Make sure the Hub name in `spike-build.json` matches macOS
- Close the LEGO SPIKE app
- Try `python3 tools/push_to_hub.py` first before using the watcher

### Python crashes when scanning Bluetooth

This repo avoids that macOS problem by using a native Swift helper app. Use the provided scripts instead of trying to replace the uploader with raw Python BLE code.

### My `.llsp3` file does not update

- Make sure you edited files inside `src/`
- Re-run `python3 tools/build_llsp3.py`
- Validate `build/projectbody.json`

### PyCharm stop does not disconnect the Hub

Use the shared `SPIKE Stop` run configuration or run:

```bash
python3 tools/stop_hub.py --shutdown-session
```

## Development Notes

- Keep code in `src/`
- Keep comments beginner-friendly
- Prefer explicit code over clever shortcuts
- Treat this project as a learning template first and a robot project second

## License

MIT
