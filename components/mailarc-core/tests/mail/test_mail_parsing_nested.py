"""Attachments that sit below the top-level multipart.

``EmailMessage.iter_attachments`` looks one level down and yields nothing at
all for ``multipart/alternative``. Apple Mail wraps every rich-text mail in
exactly that: the PDF lives in a ``multipart/mixed`` that is itself one of the
alternatives, between two ``text/html`` fragments. The fixtures mirror the
originals in the blob store — the SwiftScan mails from an iPhone and from the
desktop client — and a newsletter, which nests the other way round:
``alternative`` → ``related`` → HTML plus the images it references by
Content-ID.
"""

from mailarc_core.mail.model import ParsedAttachment
from mailarc_core.mail.parsing import parse_message

APPLE_MAIL_INLINE_PDF = b"""\
From: Jens <jens@example.com>
To: Archive <archive@example.com>
Subject: Scan 2025-03-12
Message-ID: <apple.1@example.com>
Date: Wed, 12 Mar 2025 13:00:00 +0100
MIME-Version: 1.0
X-Mailer: Apple Mail (2.3864.600.51.1.1)
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: text/plain; charset="utf-8"

Anbei der Scan.

--ALT
Content-Type: multipart/mixed; boundary="MIX"

--MIX
Content-Type: text/html; charset="utf-8"

<html><body><p>Anbei der Scan.</p>
--MIX
Content-Disposition: inline; filename="Scan 2025-03-12.pdf"
Content-Type: application/pdf; x-unix-mode="0644"; name="Scan 2025-03-12.pdf"
Content-Transfer-Encoding: base64

SGVsbG8gUERG
--MIX
Content-Type: text/html; charset="us-ascii"

</body></html>
--MIX--

--ALT--
"""

IPHONE_MAIL_NO_PLAIN = b"""\
From: Jens <jens@example.com>
To: Archive <archive@example.com>
Subject: Scan
Message-ID: <iphone.1@example.com>
Date: Wed, 12 Mar 2025 13:05:00 +0100
MIME-Version: 1.0
X-Mailer: iPhone Mail (23G83)
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: multipart/mixed; boundary="MIX"

--MIX
Content-Type: text/html; charset="utf-8"

<html><body><p>Vom iPhone gesendet.</p>
--MIX
Content-Type: application/pdf; name="Scan.pdf"
Content-Disposition: attachment; filename="Scan.pdf"
Content-Transfer-Encoding: base64

SGVsbG8gUERG
--MIX
Content-Type: text/html; charset="us-ascii"

</body></html>
--MIX--

--ALT--
"""

NEWSLETTER_WITH_EMBEDDED_IMAGES = b"""\
From: News <news@example.com>
To: Jens <jens@example.com>
Subject: Neuigkeiten
Message-ID: <news.1@example.com>
Date: Wed, 12 Mar 2025 14:00:00 +0100
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: text/plain; charset="utf-8"

Neuigkeiten im Text.

--ALT
Content-Type: multipart/related; boundary="REL"

--REL
Content-Type: text/html; charset="utf-8"

<html><body><img src="cid:logo@MIME"><p>Neuigkeiten</p></body></html>
--REL
Content-Id: <logo@MIME>
Content-Type: image/png
Content-Transfer-Encoding: base64

iVBORw0KGgo=
--REL--

--ALT--
"""

PLAIN_TEXT_WITH_ATTACHMENT_IN_THE_MIDDLE = b"""\
From: Jens <jens@example.com>
To: Archive <archive@example.com>
Subject: Notiz
Message-ID: <plain.1@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="MIX"

--MIX
Content-Type: text/plain; charset="utf-8"

Davor.
--MIX
Content-Disposition: inline; filename="Scan.pdf"
Content-Type: application/pdf; name="Scan.pdf"
Content-Transfer-Encoding: base64

SGVsbG8gUERG
--MIX
Content-Type: text/plain; charset="us-ascii"

Danach.
--MIX--
"""

FORWARD_WITH_ITS_OWN_ATTACHMENT = b"""\
From: anna@example.com
To: jens@example.com
Subject: Fwd: Angebot
Message-ID: <fwd.2@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUT"

--OUT
Content-Type: text/plain

siehe unten
--OUT
Content-Type: message/rfc822
Content-Disposition: attachment; filename="original.eml"

From: carl@example.com
To: dora@example.com
Subject: Angebot
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="IN"

--IN
Content-Type: text/plain

der eigentliche Text
--IN
Content-Type: application/pdf; name="angebot.pdf"
Content-Disposition: attachment; filename="angebot.pdf"
Content-Transfer-Encoding: base64

SGVsbG8gUERG
--IN--

--OUT--
"""

MIXED_INSIDE_MIXED = b"""\
From: System <noreply@example.com>
To: Jens <jens@example.com>
Subject: Export
Message-ID: <nested.1@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUT"

--OUT
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: text/plain; charset="utf-8"

Der Export.
--ALT--
--OUT
Content-Type: multipart/mixed; boundary="IN"

--IN
Content-Type: application/octet-stream; name="export.bin"
Content-Disposition: attachment; filename="export.bin"
Content-Transfer-Encoding: base64

SGVsbG8gUERG
--IN--
--OUT--
"""

GMAIL_ATTACHMENT_WITH_CONTENT_ID = b"""\
From: Jens <jens@example.com>
To: Archive <archive@example.com>
Subject: Rechnung
Message-ID: <gmail.1@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="MIX"

--MIX
Content-Type: text/plain; charset="utf-8"

Anbei.
--MIX
Content-Type: application/pdf; name="rechnung.pdf"
Content-Disposition: attachment; filename="rechnung.pdf"
Content-Transfer-Encoding: base64
Content-ID: <f_abc123>

SGVsbG8gUERG
--MIX--
"""


class TestAppleMailShape:
    """alternative → mixed → pdf: the part the top-level iterator never sees."""

    def test_the_inline_pdf_inside_the_alternative_is_an_attachment(self) -> None:
        message = parse_message(APPLE_MAIL_INLINE_PDF)

        assert message.has_attachments is True
        assert len(message.attachments) == 1
        attachment = message.attachments[0]
        assert attachment.filename == "Scan 2025-03-12.pdf"
        assert attachment.content_type == "application/pdf"
        assert attachment.payload == b"Hello PDF"
        assert attachment.inline is True
        assert attachment.content_id is None

    def test_the_trailing_html_fragment_is_not_an_attachment(self) -> None:
        """Apple closes the HTML after the PDF; that tail is body, not a file."""
        types = [
            one.content_type for one in parse_message(APPLE_MAIL_INLINE_PDF).attachments
        ]

        assert "text/html" not in types

    def test_the_plain_alternative_is_still_the_body(self) -> None:
        message = parse_message(APPLE_MAIL_INLINE_PDF)

        assert message.body_text.strip() == "Anbei der Scan."

    def test_the_iphone_variant_without_a_plain_part(self) -> None:
        message = parse_message(IPHONE_MAIL_NO_PLAIN)

        assert message.has_attachments is True
        assert [one.filename for one in message.attachments] == ["Scan.pdf"]
        assert message.attachments[0].inline is False
        assert "Vom iPhone gesendet." in message.body_text

    def test_a_plain_text_mail_with_the_file_in_the_middle(self) -> None:
        """The same layout one level up: text, file, text — one attachment."""
        message = parse_message(PLAIN_TEXT_WITH_ATTACHMENT_IN_THE_MIDDLE)

        assert [one.filename for one in message.attachments] == ["Scan.pdf"]
        assert message.has_attachments is True


class TestNestedContainers:
    """A multipart child is walked, never stored as the envelope it is."""

    def test_the_file_inside_a_nested_mixed_is_the_attachment(self) -> None:
        attachments = parse_message(MIXED_INSIDE_MIXED).attachments

        assert [(one.content_type, one.filename) for one in attachments] == [
            ("application/octet-stream", "export.bin")
        ]
        assert attachments[0].payload == b"Hello PDF"


class TestEmbeddedImages:
    """A newsletter's logo is content the HTML references, not a file."""

    def test_the_image_is_kept_with_its_content_id(self) -> None:
        message = parse_message(NEWSLETTER_WITH_EMBEDDED_IMAGES)

        assert len(message.attachments) == 1
        image = message.attachments[0]
        assert image.content_type == "image/png"
        assert image.content_id == "logo@MIME"
        assert image.filename is None
        assert image.embedded is True

    def test_an_embedded_image_does_not_earn_a_paperclip(self) -> None:
        assert parse_message(NEWSLETTER_WITH_EMBEDDED_IMAGES).has_attachments is False

    def test_a_named_file_with_a_content_id_does(self) -> None:
        """Gmail stamps a Content-ID on every attachment; the filename decides."""
        message = parse_message(GMAIL_ATTACHMENT_WITH_CONTENT_ID)

        assert message.attachments[0].content_id == "f_abc123"
        assert message.attachments[0].embedded is False
        assert message.has_attachments is True


class TestForwardBoundary:
    """The walk stops at ``message/rfc822``: the forward is the attachment."""

    def test_the_forwarded_mails_own_attachment_is_not_lifted_out(self) -> None:
        attachments = parse_message(FORWARD_WITH_ITS_OWN_ATTACHMENT).attachments

        assert [one.content_type for one in attachments] == ["message/rfc822"]
        assert attachments[0].filename == "original.eml"
        assert b"angebot.pdf" in attachments[0].payload


class TestEmbeddedFlag:
    def test_embedded_needs_a_content_id_and_no_filename(self) -> None:
        assert ParsedAttachment(content_id="x").embedded is True
        assert ParsedAttachment(content_id="x", filename="a.png").embedded is False
        assert ParsedAttachment(filename=None).embedded is False
