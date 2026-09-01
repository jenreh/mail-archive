"""What the account form writes, and what must never come back out of it.

Two things are being pinned here, and only one of them is about parsing.

The **shape**: this is the first provider whose secret is built by
``mailarc_ui.accounts.state`` rather than by a consent runner, so the JSON that
reaches :meth:`ImapCredentials.from_secret` has every value as a string and
every optional field present-but-empty. A parser that only accepted its own
``to_secret`` output would work in every unit test and fail on the first
mailbox a human added.

The **leak**: pydantic quotes its input in every ``ValidationError``, and the
input here is somebody's app password. ``mail_accounts.last_error`` and
``mail_sync_jobs.error`` are not encrypted columns; the log is not an encrypted
file. ``mailarc_google.source.credentials`` documents the trap and its test
asserts the rendered message, so this one does the same.
"""

import json

import pytest

from mailarc_core.mail.errors import MailAuthError
from mailarc_imap.source import IMAPS_PORT, ImapCredentials

PASSWORD = "abcd-efgh-ijkl-mnop"  # noqa: S105 - a fixture, never a real one
"""Shaped like a real app-specific password, so a leak is unmistakable."""


def form(**overrides: str) -> str:
    """Exactly what ``mailarc_ui.accounts.state._credential_values`` produces.

    ``json.dumps`` over the descriptor's field names, every value a ``str``,
    including the ones a user left blank.
    """
    values = {
        "host": "imap.mail.me.com",
        "port": "993",
        "username": "jens@icloud.com",
        "password": PASSWORD,
    }
    values.update(overrides)
    return json.dumps(values)


class TestTheAccountFormsShape:
    """Every value a string, and an untouched optional field an empty one."""

    def test_a_port_arrives_as_a_string_and_becomes_an_int(self) -> None:
        assert ImapCredentials.from_secret(form()).port == 993

    def test_a_blank_port_falls_back_to_the_default(self) -> None:
        assert ImapCredentials.from_secret(form(port="")).port == IMAPS_PORT

    def test_a_row_written_before_the_walk_covered_the_account_still_opens(
        self,
    ) -> None:
        """The migration behaviour an opaque credential column exists to give.

        Accounts added while the form still asked for a folder have one sitting
        in their JSON. Rejecting it would make an upgrade re-type every app
        password; pydantic ignores unknown keys, so the row simply opens.
        """
        stored = ImapCredentials.from_secret(form(folder="[Gmail]/All Mail"))

        assert stored.host == "imap.mail.me.com"
        assert not hasattr(stored, "folder")

    def test_whitespace_around_a_host_is_not_part_of_it(self) -> None:
        assert ImapCredentials.from_secret(form(host="  imap.gmail.com ")).host == (
            "imap.gmail.com"
        )

    def test_whitespace_in_a_password_is_left_alone(self) -> None:
        """It may be part of it, and trimming would be an unexplainable refusal."""
        padded = f" {PASSWORD} "

        assert ImapCredentials.from_secret(form(password=padded)).password == padded

    def test_a_missing_host_is_still_a_bad_credential(self) -> None:
        """Dropping blanks applies to the optional fields alone."""
        with pytest.raises(MailAuthError):
            ImapCredentials.from_secret(form(host=""))

    def test_a_port_that_is_not_a_port_is_refused(self) -> None:
        with pytest.raises(MailAuthError):
            ImapCredentials.from_secret(form(port="70000"))

    def test_a_port_that_is_not_a_number_is_refused(self) -> None:
        with pytest.raises(MailAuthError):
            ImapCredentials.from_secret(form(port="nine-nine-three"))


class TestTheRoundTrip:
    """``to_secret`` and ``from_secret`` are the only two ways in and out."""

    def test_what_it_wrote_is_what_it_reads(self) -> None:
        stored = ImapCredentials.from_secret(form())

        assert ImapCredentials.from_secret(stored.to_secret()) == stored

    def test_nothing_rotates_so_the_secret_is_stable(self) -> None:
        """``app/worker.py`` compares this to what it opened the mailbox with."""
        stored = ImapCredentials.from_secret(form())

        assert stored.to_secret() == stored.to_secret()

    def test_it_is_frozen(self) -> None:
        stored = ImapCredentials.from_secret(form())

        with pytest.raises(ValueError, match="frozen"):
            stored.host = "elsewhere"  # ty: ignore[invalid-assignment]


class TestTheSecretNeverReachesTheMessage:
    """A password in ``last_error`` is a password in an unencrypted column."""

    def test_a_malformed_secret_does_not_quote_it(self) -> None:
        with pytest.raises(MailAuthError) as raised:
            ImapCredentials.from_secret(form(port="not-a-number"))

        assert PASSWORD not in str(raised.value)
        assert "input_value" not in str(raised.value)

    def test_a_missing_field_does_not_quote_it_either(self) -> None:
        secret = json.dumps({"password": PASSWORD})

        with pytest.raises(MailAuthError) as raised:
            ImapCredentials.from_secret(secret)

        assert PASSWORD not in str(raised.value)

    def test_json_that_is_not_an_object_at_all(self) -> None:
        """Valid JSON, wrong shape — a bare string where a mapping belongs."""
        with pytest.raises(MailAuthError) as raised:
            ImapCredentials.from_secret(json.dumps(PASSWORD))

        assert PASSWORD not in str(raised.value)

    def test_json_that_is_not_json_at_all(self) -> None:
        with pytest.raises(MailAuthError) as raised:
            ImapCredentials.from_secret(f"not json, but here is {PASSWORD}")

        assert PASSWORD not in str(raised.value)

    def test_the_detail_is_still_on_the_traceback(self) -> None:
        """Kept for whoever is holding a debugger, off the sentence a human reads."""
        with pytest.raises(MailAuthError) as raised:
            ImapCredentials.from_secret("{}")

        assert raised.value.__cause__ is not None

    def test_the_message_says_what_to_do_about_it(self) -> None:
        with pytest.raises(MailAuthError, match="check the server, username"):
            ImapCredentials.from_secret("{}")

    def test_the_repr_does_not_carry_the_password(self) -> None:
        """A frozen model ends up in tracebacks, log lines and debugger frames."""
        stored = ImapCredentials.from_secret(form())

        assert PASSWORD not in repr(stored)
        assert "imap.mail.me.com" in repr(stored)
