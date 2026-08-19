"""`MailConfig` is three fields; the prefix is the part that can silently rot.

A typo in `env_prefix` costs nothing at import time and everything at run
time — the setting is simply never read and the default quietly wins.
"""

from mailarc_core.mail.config import MailConfig


def test_the_defaults_are_what_the_analyses_need() -> None:
    config = MailConfig()

    assert config.shingle_size == 3
    assert config.strip_quotes is True
    assert config.strip_signatures is True


def test_the_environment_prefix_is_app_mail(monkeypatch) -> None:
    monkeypatch.setenv("app_mail_shingle_size", "5")
    monkeypatch.setenv("app_mail_strip_quotes", "false")

    config = MailConfig()

    assert config.shingle_size == 5
    assert config.strip_quotes is False


def test_an_explicit_value_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("app_mail_shingle_size", "5")

    assert MailConfig(shingle_size=4).shingle_size == 4
