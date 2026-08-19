"""`ArchiveConfig` is two limits; the prefix is the part that can silently rot.

A typo in `env_prefix` costs nothing at import time and everything at run time —
the setting is simply never read and the default quietly wins.
"""

from pathlib import Path

from mailarc_core.archive.config import ArchiveConfig


def test_the_defaults_are_the_ones_the_spec_names() -> None:
    config = ArchiveConfig()

    assert config.store_dir == Path(".state/mailstore")
    assert config.body_text_limit == 64 * 1024


def test_the_environment_prefix_is_app_archive(monkeypatch, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("app_archive_store_dir", str(elsewhere))
    monkeypatch.setenv("app_archive_body_text_limit", "1024")

    config = ArchiveConfig()

    assert config.store_dir == Path(elsewhere)
    assert config.body_text_limit == 1024


def test_an_explicit_value_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("app_archive_body_text_limit", "1024")

    assert ArchiveConfig(body_text_limit=99).body_text_limit == 99
