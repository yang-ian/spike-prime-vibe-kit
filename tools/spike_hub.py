from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spike_build import BuildConfig, BuildError, BuildResult, HubConfig, build_project_from_config, load_config


@dataclass(frozen=True)
class PushResult:
    build_result: BuildResult
    slot: int
    hub_name: str
    device_identifier: str


@dataclass(frozen=True)
class StopResult:
    slot: int
    hub_name: str
    device_identifier: str


@dataclass(frozen=True)
class SessionInfo:
    session_dir: Path
    commands_dir: Path
    responses_dir: Path
    metadata_file: Path
    helper_pid: int
    hub_name: str
    device_identifier: str


def push_to_hub(config_path: Path | None = None) -> PushResult:
    config = load_config(config_path)
    hub_config = require_hub_config(config)
    build_result = build_project_from_config(config)

    helper_app = ensure_helper_app(config)
    session = get_active_session(config)
    if session is not None:
        helper_result = send_session_upload(session, build_result.hub_program_path, hub_config)
    else:
        helper_result = run_helper_upload_with_retry(helper_app, build_result.hub_program_path, hub_config)

    cache_device_identifier(config.config_file, helper_result["deviceIdentifier"], helper_result["hubName"], hub_config)

    return PushResult(
        build_result=build_result,
        slot=hub_config.default_slot,
        hub_name=helper_result["hubName"],
        device_identifier=helper_result["deviceIdentifier"],
    )


def push_to_hub_in_session(config_path: Path | None = None) -> PushResult:
    config = load_config(config_path)
    hub_config = require_hub_config(config)
    build_result = build_project_from_config(config)

    helper_app = ensure_helper_app(config)
    session = _ensure_hub_session(config, hub_config, helper_app)
    helper_result = send_session_upload_with_retry(config, hub_config, helper_app, session, build_result.hub_program_path)

    cache_device_identifier(config.config_file, helper_result["deviceIdentifier"], helper_result["hubName"], hub_config)

    return PushResult(
        build_result=build_result,
        slot=hub_config.default_slot,
        hub_name=helper_result["hubName"],
        device_identifier=helper_result["deviceIdentifier"],
    )


def stop_hub_program(config_path: Path | None = None, shutdown_session: bool = False) -> StopResult:
    config = load_config(config_path)
    hub_config = require_hub_config(config)
    helper_app = ensure_helper_app(config)

    session = get_active_session(config)
    created_temporary_session = False
    if session is None:
        # Stopping through the long-lived session path is more reliable on this
        # Mac than launching a one-shot "stop" helper command. If no session is
        # active, create a temporary one, stop the program, then disconnect it.
        session = _ensure_hub_session(config, hub_config, helper_app)
        created_temporary_session = True

    helper_result = send_session_stop_with_retry(config, hub_config, helper_app, session)
    cache_device_identifier(config.config_file, helper_result["deviceIdentifier"], helper_result["hubName"], hub_config)

    if shutdown_session or created_temporary_session:
        shutdown_hub_session(config_path=config.config_file, stop_running=False)

    return StopResult(
        slot=hub_config.default_slot,
        hub_name=helper_result["hubName"],
        device_identifier=helper_result["deviceIdentifier"],
    )


def ensure_hub_session_started(config_path: Path | None = None) -> SessionInfo:
    config = load_config(config_path)
    hub_config = require_hub_config(config)
    helper_app = ensure_helper_app(config)
    return _ensure_hub_session(config, hub_config, helper_app)


def shutdown_hub_session(config_path: Path | None = None, stop_running: bool = True) -> None:
    config = load_config(config_path)
    session = get_active_session(config)
    if session is None:
        return

    hub_config = require_hub_config(config)
    try:
        send_session_command(
            session,
            {
                "command": "shutdown",
                "slot": hub_config.default_slot,
                "stopRunning": stop_running,
            },
            timeout_seconds=30.0,
        )
    except BuildError:
        terminate_helper_process(session.helper_pid)
    finally:
        wait_for_process_exit(session.helper_pid, timeout_seconds=5.0)
        cleanup_session_dir(session.session_dir)


def require_hub_config(config: BuildConfig) -> HubConfig:
    if config.hub is None:
        raise BuildError("Missing `hub` config in spike-build.json.")
    if config.hub.transport != "macos_helper":
        raise BuildError(
            "This workflow is configured for the macOS helper uploader only. Set `hub.transport` to `macos_helper`."
        )
    if platform.system() != "Darwin":
        raise BuildError("The macOS Bluetooth helper workflow only runs on macOS.")
    if not config.hub.device_uuid and not config.hub.target_name:
        raise BuildError(
            "Set either `hub.device_uuid` or `hub.target_name` in spike-build.json so the uploader can find the Hub."
        )
    return config.hub


def ensure_helper_app(config: BuildConfig) -> Path:
    source_path = config.config_file.parent / "tools" / "spike_ble_helper.swift"
    plist_path = config.config_file.parent / "tools" / "spike_ble_helper.plist"
    binary_path = config.build_dir / "spike_ble_helper_bin"
    app_dir = config.build_dir / "SPIKE BLE Helper.app"
    app_executable = app_dir / "Contents" / "MacOS" / "spike_ble_helper"

    if helper_is_fresh(app_executable, (source_path, plist_path)):
        return app_dir

    config.build_dir.mkdir(parents=True, exist_ok=True)
    run_command(
        ["swiftc", "-parse-as-library", str(source_path), "-o", str(binary_path)],
        "compile the macOS BLE helper",
    )

    app_contents = app_dir / "Contents"
    app_macos = app_contents / "MacOS"
    app_macos.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary_path, app_executable)
    shutil.copy2(plist_path, app_contents / "Info.plist")

    run_command(["codesign", "--force", "--deep", "--sign", "-", str(app_dir)], "codesign the macOS BLE helper")
    return app_dir


def helper_is_fresh(executable_path: Path, source_paths: tuple[Path, ...]) -> bool:
    if not executable_path.exists():
        return False
    executable_mtime = executable_path.stat().st_mtime
    return all(path.exists() and path.stat().st_mtime <= executable_mtime for path in source_paths)


def run_helper_upload(helper_app: Path, program_path: Path, hub_config: HubConfig) -> dict[str, str]:
    args = [
        "upload",
        "--program",
        str(program_path),
        "--slot",
        str(hub_config.default_slot),
    ]
    payload = run_helper_command(helper_app, args, hub_config)
    return validate_helper_result(payload, "upload")


def run_helper_upload_with_retry(helper_app: Path, program_path: Path, hub_config: HubConfig) -> dict[str, str]:
    try:
        return run_helper_upload(helper_app, program_path, hub_config)
    except BuildError as exc:
        if not should_retry_after_transient_bluetooth_error(exc, hub_config):
            raise

        print("Bluetooth warm-up scan before retrying upload...")
        run_helper_scan(helper_app, hub_config)
        return run_helper_upload(helper_app, program_path, hub_config)


def run_helper_stop(helper_app: Path, hub_config: HubConfig) -> dict[str, str]:
    args = [
        "stop",
        "--slot",
        str(hub_config.default_slot),
    ]
    payload = run_helper_command(helper_app, args, hub_config)
    return validate_helper_result(payload, "stop")


def run_helper_stop_with_retry(helper_app: Path, hub_config: HubConfig) -> dict[str, str]:
    try:
        return run_helper_stop(helper_app, hub_config)
    except BuildError as exc:
        if not should_retry_after_transient_bluetooth_error(exc, hub_config):
            raise

        print("Bluetooth warm-up scan before retrying stop...")
        run_helper_scan(helper_app, hub_config)
        return run_helper_stop(helper_app, hub_config)


def run_helper_scan(helper_app: Path, hub_config: HubConfig) -> list[dict[str, str]]:
    args = ["scan"]
    if hub_config.target_name:
        args.extend(["--target-name", hub_config.target_name])
    payload = run_helper_command(helper_app, args, hub_config)
    if not isinstance(payload, list):
        raise BuildError("The macOS BLE helper returned an unexpected scan response format.")
    return payload


def should_retry_after_transient_bluetooth_error(error: BuildError, hub_config: HubConfig) -> bool:
    message = str(error)
    return (
        bool(hub_config.target_name)
        and (
            ("Timed out while waiting for Bluetooth to power on" in message and "unknown" in message)
            or "Timed out while preparing the Hub connection." in message
        )
    )


def run_helper_command(helper_app: Path, args: list[str], hub_config: HubConfig) -> object:
    with tempfile.NamedTemporaryFile(prefix="spike-hub-", suffix=".json", delete=False) as handle:
        output_path = Path(handle.name)

    args = [*args, "--output", str(output_path)]
    if hub_config.target_name:
        args.extend(["--target-name", hub_config.target_name])
    if hub_config.device_uuid:
        args.extend(["--device-identifier", hub_config.device_uuid])
    if args and args[0] == "upload" and not hub_config.auto_stop_before_start:
        args.append("--no-auto-stop")

    try:
        completed = subprocess.run(
            ["open", "-n", "-W", str(helper_app), "--args", *args],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BuildError(f"Could not launch the macOS BLE helper: {exc}") from exc

    try:
        if not output_path.exists():
            message = completed.stderr.strip() or completed.stdout.strip() or "The helper did not return a result."
            raise BuildError(message)

        raw_output = output_path.read_text(encoding="utf-8")
        if not raw_output.strip():
            message = completed.stderr.strip() or completed.stdout.strip() or "The helper returned an empty result file."
            raise BuildError(message)

        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            message = completed.stderr.strip() or completed.stdout.strip() or "The helper returned invalid JSON."
            raise BuildError(f"{message} Raw helper output: {raw_output!r}") from exc
    finally:
        output_path.unlink(missing_ok=True)

    if isinstance(payload, dict) and payload.get("error"):
        raise BuildError(str(payload["error"]))
    return payload


def validate_helper_result(payload: object, action: str) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise BuildError(f"The macOS BLE helper returned an unexpected {action} response format.")
    if not payload.get("deviceIdentifier"):
        raise BuildError(f"The macOS BLE helper did not report which Hub it used while trying to {action}.")
    if "hubName" not in payload:
        raise BuildError(f"The macOS BLE helper did not report the Hub name while trying to {action}.")
    return payload


def cache_device_identifier(config_file: Path, device_identifier: str, hub_name: str, hub_config: HubConfig) -> None:
    raw = json.loads(config_file.read_text(encoding="utf-8"))
    hub = raw.setdefault("hub", {})
    changed = False

    if hub.get("device_uuid") != device_identifier:
        hub["device_uuid"] = device_identifier
        changed = True

    if hub_name and hub.get("target_name") != hub_name:
        hub["target_name"] = hub_name
        changed = True

    if hub_config.bt_address and not hub.get("bt_address"):
        hub["bt_address"] = hub_config.bt_address
        changed = True

    if changed:
        config_file.write_text(json.dumps(raw, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        print(f"Cached Hub UUID `{device_identifier}` in {config_file.name}.")


def _ensure_hub_session(config: BuildConfig, hub_config: HubConfig, helper_app: Path) -> SessionInfo:
    session = get_active_session(config)
    if session is not None:
        return session
    return start_hub_session(config, hub_config, helper_app)


def get_active_session(config: BuildConfig) -> SessionInfo | None:
    metadata_file = session_metadata_file(config)
    if not metadata_file.exists():
        return None

    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        cleanup_session_dir(session_root(config))
        return None

    try:
        session = SessionInfo(
            session_dir=session_root(config),
            commands_dir=session_commands_dir(config),
            responses_dir=session_responses_dir(config),
            metadata_file=metadata_file,
            helper_pid=int(payload["pid"]),
            hub_name=str(payload["hubName"]),
            device_identifier=str(payload["deviceIdentifier"]),
        )
    except (KeyError, TypeError, ValueError):
        cleanup_session_dir(session_root(config))
        return None

    if not process_is_alive(session.helper_pid):
        cleanup_session_dir(session.session_dir)
        return None

    if not session.commands_dir.exists() or not session.responses_dir.exists():
        cleanup_session_dir(session.session_dir)
        return None

    return session


def start_hub_session(config: BuildConfig, hub_config: HubConfig, helper_app: Path) -> SessionInfo:
    try:
        return _start_hub_session_once(config, hub_config, helper_app)
    except BuildError as exc:
        if not should_retry_after_transient_bluetooth_error(exc, hub_config):
            raise

        print("Bluetooth warm-up scan before retrying session start...")
        run_helper_scan(helper_app, hub_config)
        return _start_hub_session_once(config, hub_config, helper_app)


def _start_hub_session_once(config: BuildConfig, hub_config: HubConfig, helper_app: Path) -> SessionInfo:
    root = session_root(config)
    cleanup_session_dir(root)
    session_commands_dir(config).mkdir(parents=True, exist_ok=True)
    session_responses_dir(config).mkdir(parents=True, exist_ok=True)

    args = [
        "session",
        "--session-dir",
        str(root),
        "--slot",
        str(hub_config.default_slot),
    ]
    if hub_config.target_name:
        args.extend(["--target-name", hub_config.target_name])
    if hub_config.device_uuid:
        args.extend(["--device-identifier", hub_config.device_uuid])
    if not hub_config.auto_stop_before_start:
        args.append("--no-auto-stop")

    metadata_file = session_metadata_file(config)
    try:
        launcher = subprocess.Popen(
            ["open", "-n", "-W", str(helper_app), "--args", *args, "--output", str(metadata_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        raise BuildError(f"Could not launch the macOS BLE helper session: {exc}") from exc

    if launcher.poll() not in (None, 0):
        cleanup_session_dir(root)
        raise BuildError("The helper session did not launch.")

    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        if metadata_file.exists():
            raw_output = metadata_file.read_text(encoding="utf-8")
            if not raw_output.strip():
                time.sleep(0.2)
                continue

            payload = json.loads(raw_output)
            if isinstance(payload, dict) and payload.get("error"):
                cleanup_session_dir(root)
                raise BuildError(str(payload["error"]))

            session = get_active_session(config)
            if session is not None:
                return session

        if launcher.poll() not in (None, 0):
            if metadata_file.exists():
                raw_output = metadata_file.read_text(encoding="utf-8")
                if raw_output.strip():
                    payload = json.loads(raw_output)
                    if isinstance(payload, dict) and payload.get("error"):
                        cleanup_session_dir(root)
                        raise BuildError(str(payload["error"]))

            cleanup_session_dir(root)
            raise BuildError("The BLE session helper exited before it became ready.")

        time.sleep(0.2)

    cleanup_session_dir(root)
    raise BuildError("Timed out while waiting for the BLE session helper to become ready.")


def send_session_upload_with_retry(
    config: BuildConfig,
    hub_config: HubConfig,
    helper_app: Path,
    session: SessionInfo,
    program_path: Path,
) -> dict[str, str]:
    try:
        payload = send_session_command(
            session,
            {
                "command": "upload_and_start",
                "programPath": str(program_path),
                "slot": hub_config.default_slot,
                "autoStopBeforeStart": hub_config.auto_stop_before_start,
            },
            timeout_seconds=120.0,
        )
        return validate_helper_result(payload, "upload")
    except BuildError:
        cleanup_session_dir(session.session_dir)
        restarted = _ensure_hub_session(config, hub_config, helper_app)
        payload = send_session_command(
            restarted,
            {
                "command": "upload_and_start",
                "programPath": str(program_path),
                "slot": hub_config.default_slot,
                "autoStopBeforeStart": hub_config.auto_stop_before_start,
            },
            timeout_seconds=120.0,
        )
        return validate_helper_result(payload, "upload")


def send_session_stop_with_retry(
    config: BuildConfig,
    hub_config: HubConfig,
    helper_app: Path,
    session: SessionInfo,
) -> dict[str, str]:
    try:
        payload = send_session_command(
            session,
            {
                "command": "stop",
                "slot": hub_config.default_slot,
            },
            timeout_seconds=60.0,
        )
        return validate_helper_result(payload, "stop")
    except BuildError:
        cleanup_session_dir(session.session_dir)
        restarted = _ensure_hub_session(config, hub_config, helper_app)
        payload = send_session_command(
            restarted,
            {
                "command": "stop",
                "slot": hub_config.default_slot,
            },
            timeout_seconds=60.0,
        )
        return validate_helper_result(payload, "stop")


def send_session_command(session: SessionInfo, command: dict[str, Any], timeout_seconds: float) -> object:
    if not process_is_alive(session.helper_pid):
        cleanup_session_dir(session.session_dir)
        raise BuildError("The Bluetooth session helper is no longer running.")

    request_id = uuid.uuid4().hex
    request_path = session.commands_dir / f"request-{request_id}.json"
    response_path = session.responses_dir / f"response-{request_id}.json"
    payload = {"id": request_id, **command}
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if response_path.exists():
            raw_output = response_path.read_text(encoding="utf-8")
            response_path.unlink(missing_ok=True)
            request_path.unlink(missing_ok=True)
            response = json.loads(raw_output)
            if isinstance(response, dict) and not response.get("ok", True):
                raise BuildError(str(response.get("error") or "The Bluetooth session command failed."))
            return response

        if not process_is_alive(session.helper_pid):
            cleanup_session_dir(session.session_dir)
            raise BuildError("The Bluetooth session helper exited unexpectedly.")

        time.sleep(0.2)

    request_path.unlink(missing_ok=True)
    raise BuildError("Timed out while waiting for the Bluetooth session helper to respond.")


def session_root(config: BuildConfig) -> Path:
    return config.build_dir / "hub_session"


def session_metadata_file(config: BuildConfig) -> Path:
    return session_root(config) / "session.json"


def session_commands_dir(config: BuildConfig) -> Path:
    return session_root(config) / "commands"


def session_responses_dir(config: BuildConfig) -> Path:
    return session_root(config) / "responses"


def cleanup_session_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_helper_process(pid: int) -> None:
    if not process_is_alive(pid):
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return


def wait_for_process_exit(pid: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return
        time.sleep(0.1)


def run_command(command: list[str], description: str) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode == 0:
        return
    message = completed.stderr.strip() or completed.stdout.strip() or "Unknown command failure."
    raise BuildError(f"Failed to {description}: {message}")
