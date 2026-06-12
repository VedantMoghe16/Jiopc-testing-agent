"""config.py — YAML loading, defaults, validation errors with context."""

from __future__ import annotations

from pathlib import Path

import pytest

import _helpers
from conftest import FIXTURE_CONFIG
from jiopc_agent.config import ConfigError, load_config


def test_fixture_config_loads():
    cfg = load_config(FIXTURE_CONFIG)
    assert cfg.path == FIXTURE_CONFIG
    assert [a.name for a in cfg.web_apps] == [
        "FixtureOK",
        "FixtureSlow",
        "FixtureCaptcha",
        "FixtureMissingElements",
        "FixtureServerError",
    ]
    assert len(cfg.native_apps) == 1
    assert cfg.native_apps[0].process_name == "jiopc-fake-app"
    assert len(cfg.desktop_presence) == 5


def test_fixture_config_values():
    cfg = load_config(FIXTURE_CONFIG)
    ok = cfg.web_apps[0]
    assert ok.url.endswith("/ok.html")
    assert ok.load_time_threshold_ms == 8000
    assert ok.bot_detection_expected is False
    assert [e.selector for e in ok.elements] == ["nav", "input[type=search]"]

    captcha = cfg.web_apps[2]
    assert captcha.bot_detection_expected is True
    assert captcha.elements == ()  # default

    assert cfg.agent.part_order == ("A", "B", "C")
    assert cfg.agent.fail_on == frozenset({"FAIL", "MISSING", "MISPLACED", "ERROR"})
    assert cfg.agent.cooldown_s == 0.5
    assert cfg.agent.poll_interval_ms == 100
    assert cfg.agent.email.enabled is False
    # paths are Path objects (relative entries stay relative; ~ expands)
    assert all(isinstance(p, Path) for p in cfg.agent.paths.applications_dirs)
    assert "~" not in str(cfg.agent.log_dir)


def test_defaults_applied(tmp_path):
    cfg = _helpers.load_config_text(
        tmp_path,
        """
        web_apps:
          - name: OnlyApp
            url: http://127.0.0.1:1/x
        """,
    )
    agent = cfg.agent
    assert agent.part_order == ("A", "B", "C")
    assert agent.fail_on == frozenset({"FAIL", "MISSING", "MISPLACED", "ERROR"})
    assert agent.cooldown_s == 2.0
    assert agent.poll_interval_ms == 500
    assert agent.term_grace_s == 5.0
    assert agent.element_timeout_ms == 5000
    assert agent.parallel is False
    assert "just a moment" in agent.bot_detection_markers
    assert cfg.web_apps[0].load_time_threshold_ms == 8000
    assert cfg.web_apps[0].elements == ()


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("/nonexistent/jiopc-no-such-config.yaml"))


def test_invalid_yaml_raises(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        _helpers.load_config_text(tmp_path, "agent: [unclosed")


def test_unknown_top_level_section_raises(tmp_path):
    with pytest.raises(ConfigError, match="unknown section"):
        _helpers.load_config_text(
            tmp_path,
            """
            web_apps:
              - {name: A, url: http://x/}
            surprise_section: true
            """,
        )


def test_empty_config_raises(tmp_path):
    with pytest.raises(ConfigError, match="no apps"):
        _helpers.load_config_text(tmp_path, "agent: {log_dir: ~/x}")


def test_bad_fail_on_value_raises(tmp_path):
    with pytest.raises(ConfigError, match="fail_on"):
        _helpers.load_config_text(
            tmp_path,
            """
            agent:
              fail_on: [FAIL, BOGUS]
            web_apps:
              - {name: A, url: http://x/}
            """,
        )


def test_bad_part_order_raises(tmp_path):
    with pytest.raises(ConfigError, match="part_order"):
        _helpers.load_config_text(
            tmp_path,
            """
            agent:
              part_order: [A, A, Z]
            web_apps:
              - {name: A, url: http://x/}
            """,
        )


def test_missing_required_key_has_context(tmp_path):
    with pytest.raises(ConfigError, match=r"web_apps\[0\]\.url"):
        _helpers.load_config_text(
            tmp_path,
            """
            web_apps:
              - name: NoUrl
            """,
        )


def test_native_app_validation(tmp_path):
    with pytest.raises(ConfigError, match=r"native_apps\[0\]\.process_name"):
        _helpers.load_config_text(
            tmp_path,
            """
            native_apps:
              - name: NoProc
            """,
        )


def test_presence_validation(tmp_path):
    with pytest.raises(ConfigError, match=r"desktop_presence\[0\]"):
        _helpers.load_config_text(
            tmp_path,
            """
            desktop_presence:
              - {name: X, desktop_id: x.desktop, desktop_folder: Games}
            """,
        )


def test_cli_exits_2_on_config_error(capsys):
    from jiopc_agent import cli

    rc = _helpers.call_main(cli.main, ["--config", "/nonexistent/cfg.yaml"])
    assert rc == 2
    assert "config error" in capsys.readouterr().err
