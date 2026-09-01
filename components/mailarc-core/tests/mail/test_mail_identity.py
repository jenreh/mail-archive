"""One mail, one id — the contract a second import depends on.

Everything the archive writer does is an upsert keyed on ``canonical_id``, so
if these tests go red a re-import stops being a no-op and starts duplicating
the graph. Synchronous, no I/O.
"""

from datetime import UTC, datetime

import pytest

from mailarc_core.mail.identity import SHA256_PREFIX, canonical_id, normalise_message_id
from mailarc_core.mail.model import EmailAddress
from mailarc_core.mail.parsing import parse_message

WITH_MESSAGE_ID = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Angebot
Message-ID: <ABC.123@Example.COM>
Date: Wed, 12 Mar 2025 09:14:00 +0100
Content-Type: text/plain; charset="utf-8"

Hallo Bob, anbei das Angebot.
"""

WITHOUT_MESSAGE_ID = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Angebot
Date: Wed, 12 Mar 2025 09:14:00 +0100
Content-Type: text/plain; charset="utf-8"

Hallo Bob, anbei das Angebot.
"""


class TestNormaliseMessageId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("<abc.123@example.com>", "abc.123@example.com"),
            ("  <abc.123@example.com>  ", "abc.123@example.com"),
            ("abc.123@example.com", "abc.123@example.com"),
            ("<ABC.123@Example.COM>", "ABC.123@example.com"),
            ("<no-domain>", "no-domain"),
        ],
    )
    def test_brackets_go_and_only_the_domain_is_lowercased(self, raw, expected) -> None:
        """RFC 5322 makes the local part significant; the domain never is."""
        assert normalise_message_id(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "<>", "< >"])
    def test_an_empty_header_is_the_same_as_no_header(self, raw) -> None:
        assert normalise_message_id(raw) is None


class TestCanonicalId:
    def test_the_message_id_wins_when_there_is_one(self) -> None:
        message = parse_message(WITH_MESSAGE_ID)

        assert message.canonical_id == "ABC.123@example.com"
        assert not message.canonical_id.startswith(SHA256_PREFIX)

    def test_the_same_eml_parsed_twice_yields_the_same_id(self) -> None:
        """The idempotence contract: two imports, zero new nodes."""
        first = parse_message(WITH_MESSAGE_ID)
        second = parse_message(WITH_MESSAGE_ID)

        assert first.canonical_id == second.canonical_id
        assert first == second

    def test_a_message_without_a_message_id_falls_back_deterministically(self) -> None:
        first = parse_message(WITHOUT_MESSAGE_ID)
        second = parse_message(WITHOUT_MESSAGE_ID)

        assert first.canonical_id.startswith(SHA256_PREFIX)
        assert first.canonical_id == second.canonical_id
        assert len(first.canonical_id) == len(SHA256_PREFIX) + 64

    def test_the_fallback_id_changes_when_the_body_changes(self) -> None:
        other = WITHOUT_MESSAGE_ID.replace(b"anbei das Angebot", b"anbei die Rechnung")

        assert (
            parse_message(WITHOUT_MESSAGE_ID).canonical_id
            != parse_message(other).canonical_id
        )

    def test_the_fallback_id_changes_when_the_subject_changes(self) -> None:
        other = WITHOUT_MESSAGE_ID.replace(b"Subject: Angebot", b"Subject: Rechnung")

        assert (
            parse_message(WITHOUT_MESSAGE_ID).canonical_id
            != parse_message(other).canonical_id
        )

    def test_two_transports_of_one_message_agree_on_the_fallback_id(self) -> None:
        """CRLF on the wire, LF on disk — still the same mail."""
        crlf = WITHOUT_MESSAGE_ID.replace(b"\n", b"\r\n")

        assert (
            parse_message(crlf).canonical_id
            == parse_message(WITHOUT_MESSAGE_ID).canonical_id
        )

    def test_the_fields_are_hashed_in_a_fixed_order(self) -> None:
        """Swapping two inputs must not produce the same digest."""
        sent_at = datetime(2025, 3, 12, 9, 14, tzinfo=UTC)
        sender = EmailAddress(address="alice@example.com")

        straight = canonical_id(
            rfc_message_id=None,
            sent_at=sent_at,
            sender=sender,
            subject="Angebot",
            body_bytes=b"Rechnung",
        )
        swapped = canonical_id(
            rfc_message_id=None,
            sent_at=sent_at,
            sender=sender,
            subject="Rechnung",
            body_bytes=b"Angebot",
        )

        assert straight != swapped

    def test_a_message_with_nothing_but_a_body_still_gets_an_id(self) -> None:
        assert canonical_id(
            rfc_message_id=None,
            sent_at=None,
            sender=None,
            subject="",
            body_bytes=b"",
        ).startswith(SHA256_PREFIX)
