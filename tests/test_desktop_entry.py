"""desktop_entry.py — minimal freedesktop .desktop parser (SPEC §Part C).

Interface under test (binding per SPEC): ``parse(path) -> dict``,
``exec_argv(entry) -> list[str]`` (strips %-field codes),
``categories(entry) -> set[str]``.
"""

from __future__ import annotations

import pytest

from conftest import DESKTOP_TREE

desktop_entry = pytest.importorskip(
    "jiopc_agent.desktop_entry", reason="spine module desktop_entry.py not implemented yet"
)

SAMPLE = """\
# top-of-file comment
[Desktop Entry]
Type=Application
Name=Sample App
Name[hi]=नमूना
# inline comment between keys
Comment=An app with everything the parser must handle
Exec=sample-app --flag value %U %f
Icon=sample-app
Terminal=false
Categories=Game;BoardGame;

[Desktop Action New]
Name=New Window
Exec=sample-app --new-window
"""


@pytest.fixture()
def sample_entry(tmp_path):
    path = tmp_path / "sample.desktop"
    path.write_text(SAMPLE, encoding="utf-8")
    return desktop_entry.parse(path)


def test_parse_basic_keys(sample_entry):
    assert sample_entry["Name"] == "Sample App"
    assert sample_entry["Exec"] == "sample-app --flag value %U %f"
    assert sample_entry["Type"] == "Application"


def test_parse_ignores_localised_keys(sample_entry):
    assert "Name[hi]" not in sample_entry
    assert sample_entry["Name"] == "Sample App"  # base key untouched


def test_parse_only_desktop_entry_section(sample_entry):
    # Exec from [Desktop Action New] must not clobber the main section's.
    assert "--new-window" not in sample_entry["Exec"]


def test_parse_ignores_comments(sample_entry):
    assert not any(k.startswith("#") for k in sample_entry)


def test_exec_argv_strips_field_codes(sample_entry):
    assert desktop_entry.exec_argv(sample_entry) == ["sample-app", "--flag", "value"]


@pytest.mark.parametrize(
    ("exec_line", "expected"),
    [
        ("app %U", ["app"]),
        ("app %u %f %F %i %c %k", ["app"]),
        ("/usr/bin/app --opt", ["/usr/bin/app", "--opt"]),
    ],
)
def test_exec_argv_variants(tmp_path, exec_line, expected):
    path = tmp_path / "x.desktop"
    path.write_text(f"[Desktop Entry]\nType=Application\nExec={exec_line}\n", encoding="utf-8")
    assert desktop_entry.exec_argv(desktop_entry.parse(path)) == expected


def test_categories_set(sample_entry):
    assert desktop_entry.categories(sample_entry) == {"Game", "BoardGame"}


def test_categories_empty_when_absent(tmp_path):
    path = tmp_path / "x.desktop"
    path.write_text("[Desktop Entry]\nType=Application\nExec=x\n", encoding="utf-8")
    assert desktop_entry.categories(desktop_entry.parse(path)) == set()


def test_parses_real_fixture_entry():
    entry = desktop_entry.parse(DESKTOP_TREE / "applications" / "org.fixture.Chess.desktop")
    assert entry["Name"] == "Fixture Chess"
    assert desktop_entry.exec_argv(entry) == ["fixture-chess", "--profile", "default"]
    assert desktop_entry.categories(entry) == {"Game", "BoardGame"}
