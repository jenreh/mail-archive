"""Parsing real-shaped mail: the five analysis fields and the failure mode.

The fixtures are byte literals rather than files because that is what the
parser takes — an ``.eml`` on disk would only add a decode step that the
provider adapters do not have. Everything here runs synchronously and touches
nothing outside the process.

The load-bearing test is `test_a_company_footer_and_a_quoted_predecessor_are_both_gone`:
without it `body_clean` degrades to `body_text` unnoticed and the template
analysis silently returns every mail that shares a signature.
"""

import hashlib
from datetime import timedelta

import pytest

from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.errors import MailPermanentError
from mailarc_core.mail.model import ParsedAttachment
from mailarc_core.mail.parsing import (
    clean_body,
    extract_refs,
    normalise_subject,
    parse_message,
    participant_key,
)

GERMAN_REPLY = """\
From: Alice Muster <Alice@Example.COM>
To: Bob Beispiel <bob@example.com>, carol@partner.de
Cc: Team <team@example.com>
Subject: AW: Re: [PROJ-123] Angebot Q3
Message-ID: <abc.123@Example.COM>
In-Reply-To: <root.1@example.com>
References: <root.1@example.com> <second.2@example.com>
Date: Wed, 12 Mar 2025 09:14:00 +0100
Content-Type: text/plain; charset="utf-8"

Hallo Bob,

danke für die Unterlagen, ich prüfe das Angebot bis Freitag.
Ticket #4711 ist dazu noch offen.

Mit freundlichen Grüßen
Alice Muster

ACME GmbH · Musterstraße 1 · 12345 Berlin
Sitz der Gesellschaft: Berlin, Amtsgericht Charlottenburg, HRB 12345

Am 12.03.2025 um 08:02 schrieb Bob Beispiel <bob@example.com>:
> Hallo Alice,
> anbei die Unterlagen zum Angebot.
> Gruß Bob
""".encode()

ENGLISH_REPLY = b"""\
From: Bob <bob@example.com>
To: Alice <alice@example.com>
Subject: Re: Quarterly report
Message-ID: <en.1@example.com>
Date: Wed, 12 Mar 2025 10:00:00 +0000
Content-Type: text/plain; charset="utf-8"

Hi Alice,

the numbers are in, nothing surprising this quarter.

Best regards
Bob

CONFIDENTIALITY NOTICE: this e-mail is confidential and intended recipient
only.

On Wed, 12 Mar 2025 at 09:14, Alice <alice@example.com> wrote:
> Could you send the numbers?
"""

OUTLOOK_FORWARD = """\
From: Carol <carol@partner.de>
To: Alice <alice@example.com>
Subject: WG: Angebot
Message-ID: <ol.1@partner.de>
Date: Wed, 12 Mar 2025 11:00:00 +0100
Content-Type: text/plain; charset="utf-8"

Kurze Rückmeldung: passt so.

Von: Bob <bob@example.com>
Gesendet: Mittwoch, 12. März 2025 08:02
An: Carol <carol@partner.de>
Betreff: Angebot

Hallo Carol, anbei das Angebot.
""".encode()

HTML_ONLY = b"""\
From: news@shop.example
To: alice@example.com
Subject: Ihre Bestellung
Message-ID: <html.1@shop.example>
Date: Wed, 12 Mar 2025 12:00:00 +0100
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><head><style>p { color: red }</style></head><body>
<p>Hallo&nbsp;Alice,</p>
<p>Ihre Bestellung ist unterwegs.</p>
<script>track();</script>
</body></html>
"""

MULTIPART_WITH_ATTACHMENT = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Rechnung
Message-ID: <mp.1@example.com>
Date: Wed, 12 Mar 2025 13:00:00 +0100
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset="utf-8"

Anbei die Rechnung.

--BOUND
Content-Type: application/pdf; name="rechnung.pdf"
Content-Disposition: attachment; filename="rechnung.pdf"
Content-Transfer-Encoding: base64

SGVsbG8gUERG

--BOUND--
"""

BROKEN_MIME = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Kaputt
Message-ID: <broken.1@example.com>
Date: Wed, 12 Mar 2025 14:00:00 +0100
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="NEVER_APPEARS"

there is no boundary anywhere in this body
"""


@pytest.fixture
def config() -> MailConfig:
    return MailConfig()


class TestBodyClean:
    """`body_clean` is a precondition for A3, so it gets its own class."""

    def test_a_company_footer_and_a_quoted_predecessor_are_both_gone(
        self, config
    ) -> None:
        """The Phase 1 acceptance test: neither survives into `body_clean`."""
        message = parse_message(GERMAN_REPLY, config=config)

        assert "danke für die Unterlagen" in message.body_clean
        assert "Ticket #4711" in message.body_clean

        assert "ACME GmbH" not in message.body_clean
        assert "Amtsgericht" not in message.body_clean
        assert "Mit freundlichen Grüßen" not in message.body_clean
        assert "anbei die Unterlagen zum Angebot" not in message.body_clean
        assert ">" not in message.body_clean

    def test_body_text_keeps_everything_body_clean_removed(self, config) -> None:
        """Two fields, two jobs: full text for search, cleaned text for hashing."""
        message = parse_message(GERMAN_REPLY, config=config)

        assert "ACME GmbH" in message.body_text
        assert "anbei die Unterlagen zum Angebot" in message.body_text
        assert message.body_clean != message.body_text

    def test_an_english_sign_off_and_disclaimer_are_removed(self, config) -> None:
        message = parse_message(ENGLISH_REPLY, config=config)

        assert "nothing surprising this quarter" in message.body_clean
        assert "Best regards" not in message.body_clean
        assert "CONFIDENTIALITY" not in message.body_clean
        assert "Could you send the numbers" not in message.body_clean

    def test_an_outlook_header_block_starts_the_quoted_part(self, config) -> None:
        message = parse_message(OUTLOOK_FORWARD, config=config)

        assert message.body_clean == "Kurze Rückmeldung: passt so."

    def test_a_german_quote_intro_alone_removes_the_predecessor(self, config) -> None:
        """No sign-off in the way, so only the quote rule can do this."""
        text = (
            "Ja, passt.\n\n"
            "Am 12.03.2025 um 08:02 schrieb Bob <bob@example.com>:\n"
            "> Geht das bis Freitag?\n"
        )

        assert clean_body(text, config) == "Ja, passt."

    def test_an_english_quote_intro_alone_removes_the_predecessor(self, config) -> None:
        text = (
            "Yes, that works.\n\n"
            "On Wed, 12 Mar 2025 at 08:02, Bob <bob@example.com> wrote:\n"
            "> Can we make Friday?\n"
        )

        assert clean_body(text, config) == "Yes, that works."

    def test_the_original_message_separator_removes_the_predecessor(
        self, config
    ) -> None:
        text = (
            "Weitergeleitet zur Kenntnis.\n\n"
            "-----Ursprüngliche Nachricht-----\n"
            "Hallo Carol, anbei das Angebot.\n"
        )

        assert clean_body(text, config) == "Weitergeleitet zur Kenntnis."

    def test_quote_lines_without_an_intro_are_still_dropped(self, config) -> None:
        """Some clients quote without announcing it."""
        text = "Antwort unten.\n\n> Frage eins\n> Frage zwei\n"

        assert clean_body(text, config) == "Antwort unten."

    def test_the_two_switches_are_independent(self) -> None:
        text = (
            "Ja, passt.\n\n"
            "Mit freundlichen Grüßen\nAlice\n\n"
            "Am 12.03.2025 um 08:02 schrieb Bob <bob@example.com>:\n"
            "> Geht das?\n"
        )

        quotes_only = clean_body(text, MailConfig(strip_signatures=False))
        signatures_only = clean_body(text, MailConfig(strip_quotes=False))

        assert "Mit freundlichen Grüßen" in quotes_only
        assert "schrieb Bob" not in quotes_only
        assert "Mit freundlichen Grüßen" not in signatures_only

    def test_the_rfc_signature_separator_cuts_the_signature(self, config) -> None:
        text = "Text one.\n\n-- \nAlice Muster\nACME GmbH\n"

        assert clean_body(text, config) == "Text one."

    def test_a_disclaimer_without_a_sign_off_is_still_removed(self, config) -> None:
        text = (
            "Die Freigabe ist erteilt.\n\n"
            "Diese E-Mail ist vertraulich und ausschließlich für den "
            "genannten Empfänger bestimmt.\n"
        )

        assert clean_body(text, config) == "Die Freigabe ist erteilt."

    def test_switching_the_rules_off_keeps_the_text(self) -> None:
        """The knobs exist so a debugging session can see what a rule ate."""
        config = MailConfig(strip_quotes=False, strip_signatures=False)

        message = parse_message(GERMAN_REPLY, config=config)

        assert "ACME GmbH" in message.body_clean
        assert "anbei die Unterlagen zum Angebot" in message.body_clean

    def test_an_empty_body_cleans_to_empty(self, config) -> None:
        assert clean_body("", config) == ""


class TestSubjectNorm:
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            ("Re: Angebot Q3", "angebot q3"),
            ("AW: Angebot Q3", "angebot q3"),
            ("WG: Angebot Q3", "angebot q3"),
            ("Fwd: Angebot Q3", "angebot q3"),
            ("FW: Angebot Q3", "angebot q3"),
            ("AW: Re: WG: Angebot Q3", "angebot q3"),
            ("Re[2]: Angebot Q3", "angebot q3"),
            ("[PROJ-123] Angebot Q3", "angebot q3"),
            ("Re: Angebot Q3 (#4711)", "angebot q3"),
            ("  Angebot   Q3  ", "angebot q3"),
            ("Angebot UTF-8 Umstellung", "angebot utf-8 umstellung"),
        ],
    )
    def test_prefixes_and_ticket_tokens_are_stripped(self, subject, expected) -> None:
        assert normalise_subject(subject) == expected

    def test_a_reply_and_its_original_normalise_to_the_same_subject(self) -> None:
        """That equality is signal 3 of A2; without it the rank is dead."""
        assert normalise_subject("AW: [PROJ-123] Angebot Q3") == normalise_subject(
            "Angebot Q3"
        )


class TestParticipantKey:
    def test_the_key_ignores_header_order(self) -> None:
        """A reply-all reorders recipients; the group must not change."""
        first = parse_message(_with_recipients("bob@example.com, carol@x.de"))
        second = parse_message(_with_recipients("carol@x.de, bob@example.com"))

        assert first.participant_key == second.participant_key
        assert first.participant_key != ""

    def test_a_different_group_gets_a_different_key(self) -> None:
        first = parse_message(_with_recipients("bob@example.com, carol@x.de"))
        second = parse_message(_with_recipients("bob@example.com, dave@x.de"))

        assert first.participant_key != second.participant_key

    def test_the_same_address_twice_counts_once(self) -> None:
        first = parse_message(_with_recipients("bob@example.com"))
        second = parse_message(_with_recipients("bob@example.com, Bob@Example.com"))

        assert first.participant_key == second.participant_key

    def test_nobody_involved_yields_no_key(self) -> None:
        """An empty group must not collide with every other empty group."""
        assert participant_key(()) == ""


class TestRefs:
    def test_tokens_come_from_the_subject_and_the_body(self, config) -> None:
        message = parse_message(GERMAN_REPLY, config=config)

        assert message.refs == ("#4711", "PROJ-123")

    def test_tokens_from_a_quoted_predecessor_still_count(self, config) -> None:
        """A top-posted "passt so" carries its ticket only in the quote."""
        raw = _with_body("passt so\n\n> siehe [PROJ-9] und #1234\n")

        assert parse_message(raw, config=config).refs == ("#1234", "PROJ-9")

    @pytest.mark.parametrize(
        "text",
        ["UTF-8 kaputt", "siehe RFC-5322", "ISO-8859 Umstellung", "Frage #7"],
    )
    def test_standards_and_short_numbers_are_not_tickets(self, text) -> None:
        assert extract_refs(text) == ()


class TestStructure:
    def test_headers_become_normalised_addresses(self, config) -> None:
        message = parse_message(GERMAN_REPLY, config=config)

        assert message.sender is not None
        assert message.sender.address == "alice@example.com"
        assert message.sender.display_name == "Alice Muster"
        assert [address.address for address in message.to] == [
            "bob@example.com",
            "carol@partner.de",
        ]
        assert [address.address for address in message.cc] == ["team@example.com"]
        assert message.bcc == ()

    def test_the_reply_chain_is_normalised_and_the_root_is_the_hint(
        self, config
    ) -> None:
        message = parse_message(GERMAN_REPLY, config=config)

        assert message.in_reply_to == "root.1@example.com"
        assert message.references == ("root.1@example.com", "second.2@example.com")
        assert message.thread_hint == "root.1@example.com"

    def test_the_date_header_becomes_an_aware_datetime(self, config) -> None:
        message = parse_message(GERMAN_REPLY, config=config)

        assert message.sent_at is not None
        assert message.sent_at.tzinfo is not None
        assert message.sent_at.hour == 9

    def test_a_missing_date_is_not_an_error(self, config) -> None:
        raw = b"From: a@b.com\nTo: c@d.com\nSubject: no date\n\nHallo.\n"

        assert parse_message(raw, config=config).sent_at is None

    def test_an_attachment_is_hashed_and_kept(self, config) -> None:
        message = parse_message(MULTIPART_WITH_ATTACHMENT, config=config)

        assert message.has_attachments is True
        assert len(message.attachments) == 1
        attachment = message.attachments[0]
        assert attachment.filename == "rechnung.pdf"
        assert attachment.content_type == "application/pdf"
        assert attachment.payload == b"Hello PDF"
        assert attachment.size == 9
        assert len(attachment.sha256) == 64
        assert "Anbei die Rechnung." in message.body_text

    def test_html_is_the_fallback_when_there_is_no_plain_part(self, config) -> None:
        message = parse_message(HTML_ONLY, config=config)

        # `&nbsp;` is resolved and then collapsed like any other whitespace.
        assert "Hallo Alice," in message.body_text
        assert "Ihre Bestellung ist unterwegs." in message.body_text
        assert "<p>" not in message.body_text
        assert "track();" not in message.body_text
        assert "color: red" not in message.body_text

    def test_crlf_line_endings_parse_like_lf(self, config) -> None:
        """Real mail arrives with CRLF; the fixtures above do not."""
        crlf = GERMAN_REPLY.replace(b"\n", b"\r\n")

        assert (
            parse_message(crlf, config=config).body_clean
            == parse_message(GERMAN_REPLY, config=config).body_clean
        )

    def test_the_raw_bytes_are_measured_and_hashed(self, config) -> None:
        message = parse_message(GERMAN_REPLY, config=config)

        assert message.size_bytes == len(GERMAN_REPLY)
        assert len(message.eml_sha256) == 64

    def test_parsing_without_a_config_uses_the_defaults(self) -> None:
        message = parse_message(GERMAN_REPLY)

        assert "ACME GmbH" not in message.body_clean


class TestBrokenInput:
    def test_a_multipart_without_its_boundary_is_permanent(self, config) -> None:
        """Retrying cannot fix it, so the engine must skip and record it."""
        with pytest.raises(MailPermanentError, match="broken MIME"):
            parse_message(BROKEN_MIME, config=config)

    def test_empty_bytes_are_not_a_message(self, config) -> None:
        with pytest.raises(MailPermanentError, match="empty message"):
            parse_message(b"", config=config)

    def test_a_nonsense_date_header_costs_the_date_not_the_message(
        self, config
    ) -> None:
        raw = b"From: a@b.com\nTo: c@d.com\nSubject: x\nDate: gestern\n\nHallo.\n"

        message = parse_message(raw, config=config)

        assert message.sent_at is None
        assert message.body_text.strip() == "Hallo."

    def test_an_unknown_charset_falls_back_to_utf8(self, config) -> None:
        """Appliances invent charset names; the words are usually still there."""
        raw = (
            b"From: a@b.com\nTo: c@d.com\nSubject: x\n"
            b'Content-Type: text/plain; charset="x-erfunden"\n\n'
        ) + "Grüße aus Berlin.\n".encode()

        assert "Berlin" in parse_message(raw, config=config).body_text

    def test_a_multipart_with_no_readable_part_yields_an_empty_body(
        self, config
    ) -> None:
        raw = b"""\
From: a@b.com
To: c@d.com
Subject: nur Bilder
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="B"

--B
Content-Type: image/png
Content-Disposition: attachment; filename="bild.png"
Content-Transfer-Encoding: base64

iVBORw0KGgo=

--B--
"""

        message = parse_message(raw, config=config)

        assert message.body_text == ""
        assert message.has_attachments is True

    def test_a_message_that_is_one_binary_part_has_no_body_text(self, config) -> None:
        """Scanner appliances send the PDF as the message, not inside it."""
        raw = (
            b"From: scanner@office.local\nTo: alice@example.com\n"
            b"Subject: Scan\nContent-Type: application/pdf\n"
            b"Content-Transfer-Encoding: base64\n\nSGVsbG8gUERG\n"
        )

        message = parse_message(raw, config=config)

        assert message.body_text == ""
        assert message.body_clean == ""

    def test_a_message_with_only_headers_still_parses(self, config) -> None:
        """Odd is not broken: an empty body is a message we archive."""
        raw = b"From: a@b.com\nTo: c@d.com\nSubject: leer\n\n"

        message = parse_message(raw, config=config)

        assert message.body_text.strip() == ""
        assert message.simhash == 0

    def test_an_unparseable_address_costs_the_address_not_the_message(
        self, config
    ) -> None:
        raw = b"From: not-an-address\nTo: c@d.com\nSubject: x\n\nHallo.\n"

        message = parse_message(raw, config=config)

        assert message.sender is None
        assert [address.address for address in message.to] == ["c@d.com"]


def _with_recipients(recipients: str) -> bytes:
    return (
        f"From: Alice <alice@example.com>\n"
        f"To: {recipients}\n"
        f"Subject: Abstimmung\n"
        f"Message-ID: <group.1@example.com>\n"
        f"Date: Wed, 12 Mar 2025 09:00:00 +0100\n"
        f"\n"
        f"Kurz zur Abstimmung.\n"
    ).encode()


def _with_body(body: str) -> bytes:
    return (
        f"From: Alice <alice@example.com>\n"
        f"To: bob@example.com\n"
        f"Subject: Rückmeldung\n"
        f"Message-ID: <body.1@example.com>\n"
        f"Date: Wed, 12 Mar 2025 09:00:00 +0100\n"
        f'Content-Type: text/plain; charset="utf-8"\n'
        f"\n"
        f"{body}"
    ).encode()


class TestAttachmentBytes:
    """A part with no decodable payload of its own still has bytes.

    `get_payload(decode=True)` returns `None` for every container part, and a
    forwarded mail — `message/rfc822` around a multipart — is exactly that.
    Hashing the fallback would put every forward in the archive on one
    `Attachment` node and leave the bytes off disk entirely.
    """

    FORWARD = (
        b"From: anna@example.com\n"
        b"To: jens@example.com\n"
        b"Subject: Fwd: Angebot\n"
        b"Message-ID: <fwd@example.com>\n"
        b"MIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="OUT"\n'
        b"\n"
        b"--OUT\n"
        b"Content-Type: text/plain\n"
        b"\n"
        b"siehe unten\n"
        b"--OUT\n"
        b"Content-Type: message/rfc822\n"
        b'Content-Disposition: attachment; filename="original.eml"\n'
        b"\n"
        b"From: carl@example.com\n"
        b"To: dora@example.com\n"
        b"Subject: Angebot\n"
        b"MIME-Version: 1.0\n"
        b'Content-Type: multipart/alternative; boundary="IN"\n'
        b"\n"
        b"--IN\n"
        b"Content-Type: text/plain\n"
        b"\n"
        b"der eigentliche Text\n"
        b"--IN--\n"
        b"\n"
        b"--OUT--\n"
    )

    EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()

    def _forwarded(self) -> ParsedAttachment:
        attachments = parse_message(self.FORWARD).attachments
        assert len(attachments) == 1
        return attachments[0]

    def test_a_forwarded_message_carries_its_own_bytes(self) -> None:
        attachment = self._forwarded()

        assert attachment.content_type == "message/rfc822"
        assert attachment.size > 0
        assert attachment.sha256 != self.EMPTY_DIGEST
        assert attachment.sha256 == hashlib.sha256(attachment.payload).hexdigest()

    def test_the_nested_message_is_recoverable_from_those_bytes(self) -> None:
        """Whoever opens the attachment gets the mail back, not a stub."""
        inner = parse_message(self._forwarded().payload)

        assert inner.subject == "Angebot"
        assert inner.body_clean == "der eigentliche Text"

    def test_two_different_forwards_are_two_different_attachments(self) -> None:
        """The bug this guards: one shared node for every forward ever imported."""
        other = self.FORWARD.replace(b"der eigentliche Text", b"etwas ganz anderes")

        first = self._forwarded()
        second = parse_message(other).attachments[0]

        assert first.sha256 != second.sha256

    def test_a_genuinely_empty_attachment_still_hashes_as_empty(self) -> None:
        """Content-addressing an empty file is correct, not a collision."""
        raw = (
            b"From: anna@example.com\n"
            b"To: jens@example.com\n"
            b"Subject: leer\n"
            b"MIME-Version: 1.0\n"
            b'Content-Type: multipart/mixed; boundary="b"\n'
            b"\n"
            b"--b\n"
            b"Content-Type: text/plain\n"
            b"\n"
            b"Text\n"
            b"--b\n"
            b"Content-Type: application/octet-stream\n"
            b'Content-Disposition: attachment; filename="leer.bin"\n'
            b"\n"
            b"--b--\n"
        )

        attachment = parse_message(raw).attachments[0]

        assert attachment.size == 0
        assert attachment.sha256 == self.EMPTY_DIGEST


class TestSentAtIsAlwaysAware:
    """RFC 5322 §3.3 gives `-0000` its own meaning, and the stdlib renders it naive.

    Mailing lists and anonymising relays use it routinely. Two shapes of
    `sent_at` in one range-indexed graph property sort against each other
    wrongly, so the archive only ever sees one.
    """

    @staticmethod
    def _with_date(offset: bytes) -> bytes:
        return (
            b"From: anna@example.com\n"
            b"To: jens@example.com\n"
            b"Subject: Angebot\n"
            b"Message-ID: <" + offset + b"@example.com>\n"
            b"Date: Wed, 04 Mar 2026 09:15:00 " + offset + b"\n"
            b"\n"
            b"Hallo.\n"
        )

    def test_an_offset_is_kept_as_it_stands(self) -> None:
        sent_at = parse_message(self._with_date(b"+0100")).sent_at

        assert sent_at is not None
        assert sent_at.utcoffset() == timedelta(hours=1)

    def test_the_zone_unknown_form_becomes_utc(self) -> None:
        sent_at = parse_message(self._with_date(b"-0000")).sent_at

        assert sent_at is not None
        assert sent_at.tzinfo is not None, "a naive value would sort against the rest"
        assert sent_at.utcoffset() == timedelta(0)

    def test_the_two_forms_stay_comparable(self) -> None:
        """The whole point: `>` between them must not raise."""
        aware = parse_message(self._with_date(b"+0100")).sent_at
        unknown = parse_message(self._with_date(b"-0000")).sent_at

        assert aware is not None
        assert unknown is not None
        assert aware < unknown, "09:15+01:00 is earlier than 09:15Z"
