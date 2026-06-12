"""YAML configuration loading and validation.

``load_config(path)`` returns an immutable :class:`AgentConfig` mirroring the
YAML schema documented in jiopc-agent.yaml and design.md. All defaults are
applied here, in one place; validation errors raise :class:`ConfigError`
with file/key context so the CLI can print them and exit 2.

Nothing in the agent reads YAML anywhere else, and no app names/URLs are
hardcoded in code — the config is the single source of truth (brief §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiopc_agent.results import Result


class ConfigError(Exception):
    """Raised for a missing, malformed, or invalid configuration file."""


# ---------------------------------------------------------------------------
# Defaults (every YAML knob has one; documented in design.md §schema)
# ---------------------------------------------------------------------------

DEFAULT_LOG_DIR = "~/.local/share/jiopc/agent/"
DEFAULT_PROMPT_FILE = "./prompts/analyse_log.txt"
DEFAULT_PART_ORDER = ("A", "B", "C")
DEFAULT_FAIL_ON = ("FAIL", "MISSING", "MISPLACED", "ERROR")
DEFAULT_APPLICATIONS_DIRS = ("/usr/share/applications", "~/.local/share/applications")
DEFAULT_DESKTOP_DIR = "~/Desktop"
DEFAULT_BOT_MARKERS = (
    "just a moment",
    "are you human",
    "verify you are",
    "access denied",
    "attention required",
)

VALID_PARTS = ("A", "B", "C")


# ---------------------------------------------------------------------------
# Dataclasses mirroring the YAML
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementCheck:
    """A CSS-selector presence check for Part A."""

    selector: str
    description: str


@dataclass(frozen=True)
class WebApp:
    """One Part A target."""

    name: str
    url: str
    load_time_threshold_ms: int = 8000
    bot_detection_expected: bool = False
    elements: tuple[ElementCheck, ...] = ()


@dataclass(frozen=True)
class NativeApp:
    """One Part B target."""

    name: str
    process_name: str
    desktop_file: Path | None = None  # explicit path; standard dirs searched otherwise
    launch_timeout_s: float = 10.0


@dataclass(frozen=True)
class PresenceApp:
    """One Part C target (two records: desktop_folder + start_menu)."""

    name: str
    desktop_id: str
    desktop_folder: str
    start_menu_category: str


@dataclass(frozen=True)
class EmailConfig:
    """Opt-in SMTP summary email (bonus; see notify.py)."""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    from_addr: str = ""
    to_addr: str = ""
    use_tls: bool = True


@dataclass(frozen=True)
class PathsConfig:
    """Filesystem sources for Part C; overridable so tests can use fixtures."""

    applications_dirs: tuple[Path, ...] = tuple(
        Path(p).expanduser() for p in DEFAULT_APPLICATIONS_DIRS
    )
    desktop_dir: Path = Path(DEFAULT_DESKTOP_DIR).expanduser()


@dataclass(frozen=True)
class AgentSettings:
    """The ``agent:`` block — global knobs."""

    log_dir: Path
    llm_prompt_file: Path
    part_order: tuple[str, ...] = DEFAULT_PART_ORDER
    fail_on: frozenset[str] = frozenset(DEFAULT_FAIL_ON)
    cooldown_s: float = 2.0
    poll_interval_ms: int = 500
    parallel: bool = False
    term_grace_s: float = 5.0
    element_timeout_ms: int = 5000
    bot_detection_markers: tuple[str, ...] = DEFAULT_BOT_MARKERS
    email: EmailConfig = field(default_factory=EmailConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


@dataclass(frozen=True)
class AgentConfig:
    """Fully validated configuration; the only state injected into the parts."""

    path: Path
    agent: AgentSettings
    web_apps: tuple[WebApp, ...]
    native_apps: tuple[NativeApp, ...]
    desktop_presence: tuple[PresenceApp, ...]


# ---------------------------------------------------------------------------
# Typed extraction helpers (all errors carry file/key context)
# ---------------------------------------------------------------------------


def _err(path: Path, where: str, msg: str) -> ConfigError:
    return ConfigError(f"{path}: {where}: {msg}")


def _require_map(path: Path, where: str, value: Any) -> dict:
    if not isinstance(value, dict):
        raise _err(path, where, f"expected a mapping, got {type(value).__name__}")
    return value


def _require_list(path: Path, where: str, value: Any) -> list:
    if not isinstance(value, list):
        raise _err(path, where, f"expected a list, got {type(value).__name__}")
    return value


def _get_str(path: Path, where: str, mapping: dict, key: str, default: str | None = None) -> str:
    value = mapping.get(key, default)
    if value is None:
        raise _err(path, f"{where}.{key}", "missing required key")
    if not isinstance(value, str) or not value.strip():
        raise _err(path, f"{where}.{key}", "expected a non-empty string")
    return value


def _get_num(
    path: Path, where: str, mapping: dict, key: str, default: float, minimum: float = 0
) -> float:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(path, f"{where}.{key}", "expected a number")
    if value < minimum:
        raise _err(path, f"{where}.{key}", f"must be >= {minimum}")
    return float(value)


def _get_bool(path: Path, where: str, mapping: dict, key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise _err(path, f"{where}.{key}", "expected true/false")
    return value


def _expand(value: str) -> Path:
    return Path(value).expanduser()


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_email(path: Path, raw: Any) -> EmailConfig:
    where = "agent.email"
    mapping = _require_map(path, where, raw if raw is not None else {})
    return EmailConfig(
        enabled=_get_bool(path, where, mapping, "enabled", False),
        smtp_host=str(mapping.get("smtp_host", "") or ""),
        smtp_port=int(_get_num(path, where, mapping, "smtp_port", 587)),
        from_addr=str(mapping.get("from", "") or ""),
        to_addr=str(mapping.get("to", "") or ""),
        use_tls=_get_bool(path, where, mapping, "use_tls", True),
    )


def _parse_paths(path: Path, raw: Any) -> PathsConfig:
    where = "agent.paths"
    mapping = _require_map(path, where, raw if raw is not None else {})
    dirs_raw = mapping.get("applications_dirs", list(DEFAULT_APPLICATIONS_DIRS))
    dirs = _require_list(path, f"{where}.applications_dirs", dirs_raw)
    app_dirs: list[Path] = []
    for i, entry in enumerate(dirs):
        if not isinstance(entry, str) or not entry.strip():
            raise _err(path, f"{where}.applications_dirs[{i}]", "expected a path string")
        app_dirs.append(_expand(entry))
    desktop = mapping.get("desktop_dir", DEFAULT_DESKTOP_DIR)
    if not isinstance(desktop, str) or not desktop.strip():
        raise _err(path, f"{where}.desktop_dir", "expected a path string")
    return PathsConfig(applications_dirs=tuple(app_dirs), desktop_dir=_expand(desktop))


def _parse_agent(path: Path, raw: Any) -> AgentSettings:
    where = "agent"
    mapping = _require_map(path, where, raw if raw is not None else {})

    part_order_raw = _require_list(
        path, f"{where}.part_order", mapping.get("part_order", list(DEFAULT_PART_ORDER))
    )
    part_order = tuple(str(p).upper() for p in part_order_raw)
    if sorted(part_order) != sorted(set(part_order)) or not set(part_order) <= set(VALID_PARTS):
        raise _err(
            path, f"{where}.part_order", f"must be unique values from {list(VALID_PARTS)}"
        )

    fail_on_raw = _require_list(
        path, f"{where}.fail_on", mapping.get("fail_on", list(DEFAULT_FAIL_ON))
    )
    valid_results = {r.value for r in Result}
    fail_on = frozenset(str(v).upper() for v in fail_on_raw)
    unknown = fail_on - valid_results
    if unknown:
        raise _err(
            path,
            f"{where}.fail_on",
            f"unknown result(s) {sorted(unknown)}; valid: {sorted(valid_results)}",
        )

    markers_raw = _require_list(
        path,
        f"{where}.bot_detection_markers",
        mapping.get("bot_detection_markers", list(DEFAULT_BOT_MARKERS)),
    )
    markers = tuple(str(m).lower() for m in markers_raw)

    return AgentSettings(
        log_dir=_expand(_get_str(path, where, mapping, "log_dir", DEFAULT_LOG_DIR)),
        llm_prompt_file=_expand(
            _get_str(path, where, mapping, "llm_prompt_file", DEFAULT_PROMPT_FILE)
        ),
        part_order=part_order,
        fail_on=fail_on,
        cooldown_s=_get_num(path, where, mapping, "cooldown_s", 2.0),
        poll_interval_ms=int(_get_num(path, where, mapping, "poll_interval_ms", 500, minimum=50)),
        parallel=_get_bool(path, where, mapping, "parallel", False),
        term_grace_s=_get_num(path, where, mapping, "term_grace_s", 5.0),
        element_timeout_ms=int(
            _get_num(path, where, mapping, "element_timeout_ms", 5000, minimum=100)
        ),
        bot_detection_markers=markers,
        email=_parse_email(path, mapping.get("email")),
        paths=_parse_paths(path, mapping.get("paths")),
    )


def _parse_web_apps(path: Path, raw: Any) -> tuple[WebApp, ...]:
    apps: list[WebApp] = []
    for i, entry in enumerate(_require_list(path, "web_apps", raw if raw is not None else [])):
        where = f"web_apps[{i}]"
        mapping = _require_map(path, where, entry)
        elements: list[ElementCheck] = []
        for j, el in enumerate(
            _require_list(path, f"{where}.elements", mapping.get("elements", []))
        ):
            el_where = f"{where}.elements[{j}]"
            el_map = _require_map(path, el_where, el)
            elements.append(
                ElementCheck(
                    selector=_get_str(path, el_where, el_map, "selector"),
                    description=_get_str(path, el_where, el_map, "description"),
                )
            )
        apps.append(
            WebApp(
                name=_get_str(path, where, mapping, "name"),
                url=_get_str(path, where, mapping, "url"),
                load_time_threshold_ms=int(
                    _get_num(path, where, mapping, "load_time_threshold_ms", 8000, minimum=1)
                ),
                bot_detection_expected=_get_bool(
                    path, where, mapping, "bot_detection_expected", False
                ),
                elements=tuple(elements),
            )
        )
    return tuple(apps)


def _parse_native_apps(path: Path, raw: Any) -> tuple[NativeApp, ...]:
    apps: list[NativeApp] = []
    for i, entry in enumerate(_require_list(path, "native_apps", raw if raw is not None else [])):
        where = f"native_apps[{i}]"
        mapping = _require_map(path, where, entry)
        desktop_file = mapping.get("desktop_file")
        if desktop_file is not None and (
            not isinstance(desktop_file, str) or not desktop_file.strip()
        ):
            raise _err(path, f"{where}.desktop_file", "expected a path string")
        apps.append(
            NativeApp(
                name=_get_str(path, where, mapping, "name"),
                process_name=_get_str(path, where, mapping, "process_name"),
                desktop_file=_expand(desktop_file) if desktop_file else None,
                launch_timeout_s=_get_num(
                    path, where, mapping, "launch_timeout_s", 10.0, minimum=1
                ),
            )
        )
    return tuple(apps)


def _parse_desktop_presence(path: Path, raw: Any) -> tuple[PresenceApp, ...]:
    apps: list[PresenceApp] = []
    for i, entry in enumerate(
        _require_list(path, "desktop_presence", raw if raw is not None else [])
    ):
        where = f"desktop_presence[{i}]"
        mapping = _require_map(path, where, entry)
        apps.append(
            PresenceApp(
                name=_get_str(path, where, mapping, "name"),
                desktop_id=_get_str(path, where, mapping, "desktop_id"),
                desktop_folder=_get_str(path, where, mapping, "desktop_folder"),
                start_menu_category=_get_str(path, where, mapping, "start_menu_category"),
            )
        )
    return tuple(apps)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_config(path: Path) -> AgentConfig:
    """Load and validate the YAML config at ``path``.

    Raises :class:`ConfigError` with file/key context on any problem,
    including a missing PyYAML installation.
    """
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise ConfigError(
            "PyYAML is not installed; run 'pip install pyyaml' "
            "(or 'apt install python3-yaml' on Ubuntu)"
        ) from exc

    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(f"{path}: config file not found")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    root = _require_map(path, "<top level>", raw)
    unknown = set(root) - {"agent", "web_apps", "native_apps", "desktop_presence"}
    if unknown:
        raise _err(path, "<top level>", f"unknown section(s): {sorted(unknown)}")

    cfg = AgentConfig(
        path=path,
        agent=_parse_agent(path, root.get("agent")),
        web_apps=_parse_web_apps(path, root.get("web_apps")),
        native_apps=_parse_native_apps(path, root.get("native_apps")),
        desktop_presence=_parse_desktop_presence(path, root.get("desktop_presence")),
    )
    if not (cfg.web_apps or cfg.native_apps or cfg.desktop_presence):
        raise _err(path, "<top level>", "config defines no apps to test")
    return cfg
