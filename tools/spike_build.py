from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class BuildError(Exception):
    pass


@dataclass(frozen=True)
class HubConfig:
    transport: str
    default_slot: int
    auto_stop_before_start: bool
    target_name: str
    device_uuid: str
    bt_address: str


@dataclass(frozen=True)
class BuildConfig:
    config_file: Path
    entrypoint: Path
    source_root: Path
    manifest_file: Path
    icon_file: Path
    build_dir: Path
    dist_file: Path
    hub: HubConfig | None


@dataclass(frozen=True)
class ModuleSource:
    name: str
    path: Path
    source: str
    is_package: bool


@dataclass(frozen=True)
class BuildResult:
    manifest_path: Path
    projectbody_path: Path
    hub_program_path: Path
    dist_path: Path
    watched_files: tuple[Path, ...]


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT_DIR / "spike-build.json"

BOOTSTRAP_TEMPLATE = """import sys

try:
    import builtins as _spike_builtins
except ImportError:
    import __builtin__ as _spike_builtins

_SPIKE_BUNDLED_SOURCES = {sources}
_SPIKE_BUNDLED_PACKAGES = set({packages})
_SPIKE_BUNDLED_NAMES = set(_SPIKE_BUNDLED_SOURCES) | _SPIKE_BUNDLED_PACKAGES
_SPIKE_ORIGINAL_IMPORT = _spike_builtins.__import__


def _spike_resolve(name, package, level):
    if level == 0:
        return name
    if not package:
        raise ImportError("relative import outside of bundled package")
    package_bits = package.split(".")
    if level > len(package_bits) + 1:
        raise ImportError("attempted relative import beyond top-level package")
    resolved = package_bits[: len(package_bits) + 1 - level]
    if name:
        resolved.append(name)
    return ".".join(resolved)


def _spike_has_bundled(name):
    if name in _SPIKE_BUNDLED_NAMES:
        return True
    prefix = name + "."
    for candidate in _SPIKE_BUNDLED_NAMES:
        if candidate.startswith(prefix):
            return True
    return False


def _spike_bind_child(name):
    parent_name, separator, child_name = name.rpartition(".")
    if not separator:
        return
    parent = sys.modules.get(parent_name)
    child = sys.modules.get(name)
    if parent is not None and child is not None:
        setattr(parent, child_name, child)


def _spike_make_module(name, is_package):
    module = sys.modules.get(name)
    if module is None:
        module = type(sys)(name)
        sys.modules[name] = module
    module.__name__ = name
    if is_package:
        module.__package__ = name
        module.__path__ = []
        module.__file__ = name.replace(".", "/") + "/__init__.py"
    else:
        module.__package__ = name.rpartition(".")[0]
        module.__file__ = name.replace(".", "/") + ".py"
    return module


def _spike_load(name):
    if not _spike_has_bundled(name):
        raise ImportError(name)

    module = sys.modules.get(name)
    if module is not None and getattr(module, "__spike_loaded__", False):
        return module

    is_package = name in _SPIKE_BUNDLED_PACKAGES
    parent_name, separator, _ = name.rpartition(".")
    if separator and _spike_has_bundled(parent_name):
        _spike_load(parent_name)

    module = _spike_make_module(name, is_package)
    _spike_bind_child(name)
    if getattr(module, "__spike_loading__", False):
        return module

    module.__spike_loading__ = True
    source = _SPIKE_BUNDLED_SOURCES.get(name)
    if source is not None:
        exec(source, module.__dict__)
    module.__spike_loading__ = False
    module.__spike_loaded__ = True
    return module


def _spike_import(name, globals=None, locals=None, fromlist=(), level=0):
    package = None
    if globals:
        package = globals.get("__package__") or globals.get("__name__")
    absolute_name = _spike_resolve(name, package, level)
    if _spike_has_bundled(absolute_name):
        target = _spike_load(absolute_name)
        if fromlist:
            for item in fromlist:
                if item == "*":
                    continue
                child_name = absolute_name + "." + item
                if _spike_has_bundled(child_name):
                    _spike_load(child_name)
            return target
        if "." in absolute_name:
            return sys.modules[absolute_name.split(".", 1)[0]]
        return target
    return _SPIKE_ORIGINAL_IMPORT(name, globals, locals, fromlist, level)


_spike_builtins.__import__ = _spike_import

{entry_source}
"""


def load_config(config_path: Path | None = None) -> BuildConfig:
    config_file = (config_path or DEFAULT_CONFIG).resolve()
    raw = json.loads(config_file.read_text(encoding="utf-8"))
    base_dir = config_file.parent
    config = BuildConfig(
        config_file=config_file,
        entrypoint=(base_dir / raw["entrypoint"]).resolve(),
        source_root=(base_dir / raw["source_root"]).resolve(),
        manifest_file=(base_dir / raw["manifest_file"]).resolve(),
        icon_file=(base_dir / raw["icon_file"]).resolve(),
        build_dir=(base_dir / raw["build_dir"]).resolve(),
        dist_file=(base_dir / raw["dist_file"]).resolve(),
        hub=_load_hub_config(raw.get("hub")),
    )
    validate_config(config)
    return config


def _load_hub_config(raw_hub: object) -> HubConfig | None:
    if raw_hub is None:
        return None
    if not isinstance(raw_hub, dict):
        raise BuildError("The `hub` config must be an object.")

    transport = raw_hub.get("transport", "macos_helper")
    if transport in {"bluetooth", "bleak"}:
        # Keep the older spellings working so existing configs do not break.
        transport = "macos_helper"
    if transport != "macos_helper":
        raise BuildError("`hub.transport` must be `macos_helper`.")

    default_slot = raw_hub.get("default_slot", 0)
    if not isinstance(default_slot, int) or not (0 <= default_slot <= 19):
        raise BuildError("`hub.default_slot` must be an integer from 0 to 19.")

    auto_stop_before_start = raw_hub.get("auto_stop_before_start", True)
    if not isinstance(auto_stop_before_start, bool):
        raise BuildError("`hub.auto_stop_before_start` must be true or false.")

    target_name = raw_hub.get("target_name", "")
    if not isinstance(target_name, str):
        raise BuildError("`hub.target_name` must be a string.")

    device_uuid = raw_hub.get("device_uuid", "")
    if not isinstance(device_uuid, str):
        raise BuildError("`hub.device_uuid` must be a string.")

    bt_address = raw_hub.get("bt_address", "")
    if not isinstance(bt_address, str):
        raise BuildError("`hub.bt_address` must be a string.")

    return HubConfig(
        transport=transport,
        default_slot=default_slot,
        auto_stop_before_start=auto_stop_before_start,
        target_name=target_name.strip(),
        device_uuid=device_uuid.strip(),
        bt_address=bt_address.strip(),
    )


def validate_config(config: BuildConfig) -> None:
    if not config.entrypoint.exists():
        raise BuildError(f"Entrypoint not found: {config.entrypoint}")
    if not config.source_root.exists():
        raise BuildError(f"Source root not found: {config.source_root}")
    if config.entrypoint.parent != config.source_root and config.source_root not in config.entrypoint.parents:
        raise BuildError("Entrypoint must be inside source_root")
    if not config.manifest_file.exists():
        raise BuildError(f"Manifest not found: {config.manifest_file}")
    if not config.icon_file.exists():
        raise BuildError(f"Icon not found: {config.icon_file}")


def _iter_python_files(source_root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if any(part.startswith(".") for part in path.relative_to(source_root).parts):
            continue
        paths.append(path)
    return paths


def discover_modules(config: BuildConfig) -> list[ModuleSource]:
    modules: list[ModuleSource] = []
    for path in _iter_python_files(config.source_root):
        if path == config.entrypoint:
            continue
        relative_path = path.relative_to(config.source_root)
        is_package = relative_path.name == "__init__.py"
        module_name = module_name_from_path(relative_path)
        source = path.read_text(encoding="utf-8")
        validate_python_source(path, source)
        modules.append(
            ModuleSource(
                name=module_name,
                path=path,
                source=source,
                is_package=is_package,
            )
        )
    return modules


def module_name_from_path(relative_path: Path) -> str:
    if relative_path.name == "__init__.py":
        if len(relative_path.parts) == 1:
            raise BuildError("src/__init__.py is not supported; use named packages under src/")
        return ".".join(relative_path.parts[:-1])
    return ".".join(relative_path.with_suffix("").parts)


def build_script(config: BuildConfig) -> tuple[str, list[ModuleSource]]:
    entry_source = config.entrypoint.read_text(encoding="utf-8").rstrip() + "\n"
    validate_python_source(config.entrypoint, entry_source)
    modules = discover_modules(config)
    bundled_sources = {module.name: module.source for module in modules}
    bundled_packages = sorted(module.name for module in modules if module.is_package)
    script = BOOTSTRAP_TEMPLATE.format(
        sources=json.dumps(bundled_sources, ensure_ascii=False, sort_keys=True),
        packages=json.dumps(bundled_packages, ensure_ascii=False),
        entry_source=entry_source,
    )
    validate_python_source(config.build_dir / "projectbody.py", script)
    return script, modules


def watch_files_for_config(config: BuildConfig, modules: list[ModuleSource] | None = None) -> tuple[Path, ...]:
    if modules is None:
        modules = discover_modules(config)
    return tuple(
        sorted(
            {
                config.config_file,
                config.entrypoint,
                config.manifest_file,
                config.icon_file,
                *(module.path for module in modules),
            }
        )
    )


def collect_watch_files(config_path: Path | None = None) -> list[Path]:
    config = load_config(config_path)
    return list(watch_files_for_config(config))


def _build_manifest(manifest_file: Path, main_script: str) -> dict:
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["lastsaved"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    manifest["size"] = len(main_script.encode("utf-8"))
    manifest["extraFiles"] = []
    return manifest


def validate_python_source(path: Path, source: str) -> None:
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        raise BuildError(f"Syntax error in {path}: line {exc.lineno}: {exc.msg}") from exc


def write_project_files(config: BuildConfig, script: str) -> tuple[Path, Path, Path]:
    config.build_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = config.build_dir / "manifest.json"
    projectbody_path = config.build_dir / "projectbody.json"
    hub_program_path = config.build_dir / "hub_program.py"
    manifest = _build_manifest(config.manifest_file, script)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    projectbody_path.write_text(
        json.dumps({"main": script}, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    hub_program_path.write_text(script, encoding="utf-8")
    return manifest_path, projectbody_path, hub_program_path


def package_llsp3(config: BuildConfig, manifest_path: Path, projectbody_path: Path) -> None:
    config.dist_file.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(config.dist_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(manifest_path, "manifest.json")
        archive.write(projectbody_path, "projectbody.json")
        archive.write(config.icon_file, "icon.svg")


def build_project_from_config(config: BuildConfig) -> BuildResult:
    script, modules = build_script(config)
    manifest_path, projectbody_path, hub_program_path = write_project_files(config, script)
    package_llsp3(config, manifest_path, projectbody_path)
    return BuildResult(
        manifest_path=manifest_path,
        projectbody_path=projectbody_path,
        hub_program_path=hub_program_path,
        dist_path=config.dist_file,
        watched_files=watch_files_for_config(config, modules),
    )


def build_project(config_path: Path | None = None) -> BuildResult:
    try:
        config = load_config(config_path)
        return build_project_from_config(config)
    except BuildError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
        raise BuildError(str(exc)) from exc
