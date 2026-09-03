"""The planted archive the three analyses are measured against.

Every block exists for exactly one finding, and the negatives exist so that a
broken analysis cannot pass by accident. The two recurring series are as long
as the mail they imitate — a six-word sample clusters under any hash function
and would prove nothing about either the SimHash or the cleaning that feeds it
— while the negatives are deliberately terse, because a short body under a long
footer is precisely the shape that makes ``body_text`` and ``body_clean``
disagree.

German with transliterated umlauts throughout. The parser's signature and
disclaimer rules are written for both spellings, and a corpus that only used
the pretty one would leave half of those rules unexercised — which is the half
a real German mailbox tends to hit, because clients lose umlauts.

A plain module rather than a ``conftest.py``. Most of what the phase has to
prove is a property of pure functions over these bytes, and those tests should
be able to say ``from corpus import planted_corpus`` without a fixture, a
server or a marker standing between them and the assertion.

Nothing here is a magic number. The expectations live in the test files as
measured values; this module only plants the mail they are measured from, and
every field the analyses read is produced by
:func:`~mailarc_core.mail.parsing.parse_message` from these bytes.
"""

import os
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

from pydantic import BaseModel, ConfigDict

from mailarc_analytics import AnalyticsConfig, MessageFacts
from mailarc_core.mail.model import ParsedMessage
from mailarc_core.mail.parsing import parse_message, simhash

OWN = "jens@nordlicht.example"
"""The address the archive belongs to; what "sent by me" resolves to."""

ANNA = "anna.meier@kunde.example"
THOMAS = "thomas.blau@kunde.example"
TEAM = "team@nordlicht.example"
NEWS = "newsletter@anbieter.example"
REVISION = "revision@nordlicht.example"
"""Only ever a Bcc. Any ``CO_ADDRESSED`` pair naming it is a bug, and its
absence from every pair is the assertion the whole B block exists for."""

HAUSMEISTER = "hausmeister@nordlicht.example"
BUCHHALTUNG = "buchhaltung@nordlicht.example"

PDF = b"%PDF-1.4 planted zeitplan bytes"
"""The one attachment, shared by two messages so A2 has a hash to join on."""

TICKET = "NORD-42"
"""The only ticket token in the corpus. Nothing outside block P may match
``[A-Z][A-Z0-9]{1,9}-\\d{1,8}`` or ``#\\d{2,8}``, or A2's strongest signal
would fire where nothing was planted."""

FOOTER = """\

Mit freundlichen Gruessen
Jens Rehpoehler
Nordlicht GmbH
Hafenstrasse 12, 20095 Hamburg
Telefon 040 123456-0, Telefax 040 123456-99
jens@nordlicht.example, www.nordlicht.example

Sitz der Gesellschaft: Hamburg, Amtsgericht Hamburg HRB 123456
Geschaeftsfuehrer: Jens Rehpoehler, Katrin Ohlsen
Umsatzsteuer-Identifikationsnummer: DE123456789
Glaeubiger-Identifikationsnummer: DE98ZZZ09999999999
Bankverbindung: Nordbank Hamburg, IBAN DE00 1234 5678 9012 3456 78

Diese E-Mail ist vertraulich und ausschliesslich fuer den Empfaenger
bestimmt. Sollten Sie nicht der richtige Adressat sein, informieren Sie
bitte den Absender und loeschen Sie diese Nachricht mitsamt allen
Anhaengen. Das unbefugte Kopieren, Weitergeben oder Verwerten dieser
Nachricht ist untersagt und kann rechtliche Folgen haben. Eine
rechtsverbindliche Erklaerung ist mit dieser Nachricht nicht verbunden,
soweit sie nicht ausdruecklich als solche gekennzeichnet ist. Wir weisen
darauf hin, dass die Kommunikation per E-Mail ueber das Internet
unsicher sein kann, da fuer unberechtigte Dritte grundsaetzlich die
Moeglichkeit der Kenntnisnahme und der Veraenderung besteht. Fuer
Schaeden, die durch die Nutzung dieses Kommunikationsweges entstehen,
uebernehmen wir keine Haftung, soweit uns nicht Vorsatz oder grobe
Fahrlaessigkeit zur Last faellt.

Informationen zur Verarbeitung Ihrer personenbezogenen Daten sowie zu
Ihren Rechten nach der Datenschutz-Grundverordnung finden Sie in unserer
Datenschutzerklaerung auf unserer Internetseite. Ihre Daten verarbeiten
wir ausschliesslich zur Abwicklung der Geschaeftsbeziehung und loeschen
sie nach Ablauf der gesetzlichen Aufbewahrungsfristen. Einer Verwendung
Ihrer Anschrift zu Werbezwecken koennen Sie jederzeit formlos
widersprechen.

Bitte denken Sie an die Umwelt, bevor Sie diese Nachricht ausdrucken.
"""
"""The company footer, and the reason ``body_clean`` is a separate field.

Long on purpose — a hundred-odd words against the twenty a short mail carries,
so that two messages with nothing whatever in common have a *``body_text``*
that hashes nearly alike. That is what makes the F and B blocks a real negative
control instead of a decorative one: an implementation that fingerprinted
``body_text`` would cluster them into one template, and this footer is what
makes it do so loudly enough to fail a test.

Every line of it is cut by :func:`~mailarc_core.mail.parsing.clean_body`: the
sign-off matches the signature rule and the confidentiality paragraph matches
the disclaimer rule, so ``body_clean`` keeps only what somebody actually wrote.
"""

STATUS_BODY = """\
Hallo zusammen,

hier der Statusbericht zum Projekt Nordlicht fuer {month}.

Der Zeitplan haelt. Die Migration der Bestandsdaten laeuft nach Plan, die
Abnahme durch den Fachbereich ist terminiert und die Testfaelle liegen vor.
Offene Punkte sind unveraendert die Anbindung des Rechnungssystems und die
Schulung der Sachbearbeitung. Fuer die Anbindung warten wir weiterhin auf die
Freigabe der Schnittstellenbeschreibung durch den Hersteller. Die Schulung
planen wir in zwei Bloecken, damit der laufende Betrieb nicht stillsteht.

Budget und Risiken unveraendert. Rueckfragen gerne jederzeit an mich.
"""
"""Block S: one month name apart, twelve times. The whole of A3's positive
case for the sent direction."""

NEWS_BODY = """\
Guten Tag,

die Wochenpost bringt Ihnen wie immer die Neuigkeiten aus unserem Haus.

Dies ist die Ausgabe {issue} unserer Wochenpost fuer das laufende Jahr.

Unser Sortiment umfasst weiterhin die gesamte Bandbreite der Buerotechnik, von
der Beschriftung ueber die Ablage bis zur Vernichtung. Alle Preise verstehen
sich zuzueglich der gesetzlichen Mehrwertsteuer und gelten solange der Vorrat
reicht. Die Lieferung erfolgt frei Haus ab einem Bestellwert von einhundert
Euro, darunter berechnen wir eine Versandkostenpauschale von sechs Euro
neunzig. Rueckgaben nehmen wir innerhalb von vierzehn Tagen entgegen, sofern
die Ware ungeoeffnet ist und die Originalverpackung vorliegt.

Unsere Fachberatung erreichen Sie werktags von acht bis achtzehn Uhr unter der
bekannten Rufnummer. Fuer groessere Bestellungen erstellen wir Ihnen gerne ein
individuelles Angebot; sprechen Sie uns einfach an. Rahmenvertraege fuer
Behoerden und Bildungseinrichtungen bearbeitet unser Aussendienst.

Der Katalog liegt in der aktuellen Fassung zum Herunterladen bereit. Er
enthaelt saemtliche Artikel mit Bestellnummer, Staffelpreisen und den Angaben
zur Verfuegbarkeit. Aenderungen und Irrtuemer bleiben vorbehalten, massgeblich
ist stets die Bestaetigung Ihrer Bestellung.

Wir bedanken uns fuer Ihr Vertrauen und wuenschen eine gute Woche.
"""
"""Block N: one issue number apart, ten times, and carrying no footer — a
newsletter signs off with its own text, not with the archive owner's."""

MONTHS = (
    "Januar", "Februar", "Maerz", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)  # fmt: skip

STATUS_DAYS = (5, 2, 2, 6, 4, 1, 6, 3, 7, 5, 2, 7)
"""Roughly monthly and not exactly monthly, so ``regularity`` has a coefficient
of variation to measure rather than a perfect zero."""

NEWS_DATES = (
    (1, 7), (1, 14), (1, 28), (2, 4), (2, 25),
    (3, 4), (3, 11), (4, 15), (4, 22), (6, 3),
)  # fmt: skip
"""Weekly, then not — a newsletter that skips. The point of the block is that
its regularity is measurably worse than the status report's."""


class PlantedMessage(BaseModel):
    """One planted mail, plus the two things the fixture knows and RFC 5322
    does not: what to call it, and which thread the provider put it in."""

    model_config = ConfigDict(frozen=True)

    key: str
    """Short name of the message — ``p1``, ``s07``, ``n03`` — and the local part
    of its ``Message-ID``, so a failure names the block it came from."""

    raw: bytes
    thread: str | None = None
    """The provider's thread id, which no header carries. Only the two messages
    that really are one thread have it."""


def _message(
    *,
    key: str,
    sender: str,
    to: tuple[str, ...],
    subject: str,
    body: str,
    sent: datetime,
    cc: tuple[str, ...] = (),
    bcc: tuple[str, ...] = (),
    thread: str | None = None,
    reply_to: str | None = None,
    attachment: bytes | None = None,
) -> PlantedMessage:
    """One planted mail as the bytes a provider would have handed over.

    Real RFC 5322 and not a hand-built :class:`ParsedMessage`, because every
    field the analyses read — ``body_clean``, ``simhash``, ``refs``,
    ``subject_norm``, ``participant_key`` — is computed by the parser, and a
    fixture that skipped it would test the tests.

    A ``Bcc`` header on a message the archive's owner sent is what a Sent
    folder actually holds: the recipients never saw it, the sender's own copy
    did.
    """
    message = EmailMessage()
    message["Message-ID"] = f"<{key}@nordlicht.example>"
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = subject
    message["Date"] = format_datetime(sent)
    if reply_to:
        message["In-Reply-To"] = f"<{reply_to}@nordlicht.example>"
        message["References"] = f"<{reply_to}@nordlicht.example>"
    message.set_content(body)
    if attachment is not None:
        message.add_attachment(
            attachment,
            maintype="application",
            subtype="pdf",
            filename="Zeitplan.pdf",
        )
    return PlantedMessage(key=key, raw=message.as_bytes(), thread=thread)


def _project() -> list[PlantedMessage]:
    """Block P — one project, ticket NORD-42, five messages, three people.

    ``p3`` and ``p5`` carry the ticket **in the body only**, and their subjects
    normalise to something else entirely. They belong to the topic solely
    because :func:`~mailarc_core.mail.parsing.extract_refs` reads the full body;
    a reader that switched to ``body_clean`` or to the subject alone would drop
    the topic from five members to three, and the test would say so.

    All five carry all three addresses, so they share one ``participant_key``
    and form the corpus's only three-person group.
    """
    return [
        _message(
            key="p1", sender=OWN, to=(ANNA,), cc=(THOMAS,),
            subject=f"[{TICKET}] Angebot Datenmigration",
            body="Hallo Frau Meier,\n\nanbei unser Angebot fuer die"
                 " Datenmigration. Der\nZeitplan liegt als PDF bei."
                 " Rueckmeldung bitte bis Freitag." + FOOTER,
            sent=datetime(2026, 1, 12, 9, 0, tzinfo=UTC),
            thread="t-nord", attachment=PDF,
        ),
        _message(
            key="p2", sender=ANNA, to=(OWN,), cc=(THOMAS,),
            subject=f"AW: [{TICKET}] Angebot Datenmigration",
            body="Guten Tag Herr Rehpoehler,\n\nvielen Dank, das Angebot passt"
                 " inhaltlich.\nZur Preisstellung haben wir noch eine Nachfrage"
                 " aus dem Einkauf.",
            sent=datetime(2026, 1, 13, 11, 30, tzinfo=UTC),
            thread="t-nord", reply_to="p1",
        ),
        _message(
            key="p3", sender=THOMAS, to=(OWN, ANNA),
            subject="Rueckfrage Zeitplan Einkauf",
            body=f"Hallo,\n\nkoennen wir zu {TICKET} den Meilenstein im Maerz"
                 " noch einmal\ngemeinsam durchgehen? Der Einkauf braucht eine"
                 " belastbare Zahl.",
            sent=datetime(2026, 1, 20, 8, 15, tzinfo=UTC),
        ),
        _message(
            key="p4", sender=OWN, to=(ANNA, THOMAS),
            subject=f"{TICKET} Abnahmetermin",
            body="Hallo zusammen,\n\nals Abnahmetermin schlage ich den dritten"
                 " Februar vor.\nDen aktualisierten Zeitplan haenge ich noch"
                 " einmal an." + FOOTER,
            sent=datetime(2026, 2, 3, 7, 45, tzinfo=UTC), attachment=PDF,
        ),
        _message(
            key="p5", sender=ANNA, to=(OWN, THOMAS),
            subject="Protokoll Steuerungsgruppe",
            body=f"Guten Morgen,\n\ndas Protokoll ist verteilt. Wie in {TICKET}"
                 " besprochen\nhaelt der Termin, die Freigabe kommt aus dem"
                 " Einkauf bis Monatsende.",
            sent=datetime(2026, 2, 10, 16, 0, tzinfo=UTC),
        ),
    ]  # fmt: skip


def _status() -> list[PlantedMessage]:
    """Block S — the monthly status report, written by the archive's owner.

    Twelve messages to one recipient, so the pair is too small for a ``Group``
    and the block cannot smuggle a second one into A1's answer. Twelve distinct
    subjects, because ``Statusbericht Nordlicht Januar 2026`` normalises
    differently every month — which leaves the shared ``participant_key``, at
    weight 0.2, as the only thing A2 could join them by. It must not, and if a
    bug promotes that signal the corpus grows a topic of twelve where one was
    planted.
    """
    return [
        _message(
            key=f"s{index:02d}",
            sender=OWN,
            to=(TEAM,),
            subject=f"Statusbericht Nordlicht {month} 2026",
            body=STATUS_BODY.format(month=month) + FOOTER,
            sent=datetime(2026, index, day, 8, 0, tzinfo=UTC),
        )
        for index, (month, day) in enumerate(
            zip(MONTHS, STATUS_DAYS, strict=True), start=1
        )
    ]


def _newsletter() -> list[PlantedMessage]:
    """Block N — a received series, irregular, longer and wordier than block S.

    Planted to be found *and* to rank below the status report: it recurs more
    erratically and it is more than twice as long, so both the regularity and
    the brevity factor pull it down. Received, so it also proves that the two
    directions are clustered apart rather than scored apart.
    """
    return [
        _message(
            key=f"n{index:02d}",
            sender=NEWS,
            to=(OWN,),
            subject=f"Anbieter Wochenpost Ausgabe {index:02d}",
            body=NEWS_BODY.format(issue=f"{index:02d}"),
            sent=datetime(2026, month, day, 6, 30, tzinfo=UTC),
        )
        for index, (month, day) in enumerate(NEWS_DATES, start=1)
    ]


def _footer_only() -> list[PlantedMessage]:
    """Block F — two messages whose only common text is the company footer.

    The negative control ``body_clean`` exists for. Measured on these bytes,
    the two are within the template threshold of each other on ``body_text``
    and nowhere near it on ``body_clean``: an implementation that fingerprinted
    the full text would put them in one template together with block B and,
    through it, with the twelve status reports.
    """
    return [
        _message(
            key="f1", sender=OWN, to=(HAUSMEISTER,),
            subject="Schluessel fuer den Serverraum",
            body="Guten Morgen,\n\nkoennen wir den Termin vorziehen?" + FOOTER,
            sent=datetime(2026, 3, 17, 7, 0, tzinfo=UTC),
        ),
        _message(
            key="f2", sender=OWN, to=(BUCHHALTUNG,),
            subject="Zaehlerstaende Maerz",
            body="Hallo,\n\nanbei die Zaehlerstaende." + FOOTER,
            sent=datetime(2026, 3, 18, 7, 0, tzinfo=UTC),
        ),
    ]  # fmt: skip


def _blind_copied() -> list[PlantedMessage]:
    """Block B — two messages Bcc'd to an address that appears nowhere else.

    Two questions in one block, and they have to be answered differently.

    ``CO_ADDRESSED`` must never name :data:`REVISION`: a Bcc recipient was
    written to *without* the other recipients knowing, and an edge between them
    would materialise into a finding exactly the confidentiality the header
    exists to protect. Each message has one visible recipient, so a correct A1
    draws no pair from this block at all.

    ``Group`` must count it all the same. ``participant_key`` is hashed over
    the sender, To, Cc **and** Bcc, so this block is a three-address group with
    two messages — and it clears ``min_group_size`` only if the Bcc was
    counted. An implementation that dropped Bcc from the participant set sees a
    group of two here and filters it away, which is why the corpus expects
    exactly two groups rather than one.

    Both carry the footer, so they join block F in the negative control above.
    """
    return [
        _message(
            key="b1", sender=OWN, to=(ANNA,), bcc=(REVISION,),
            subject="Rechnung fuer die Aprillieferung",
            body="Sehr geehrte Frau Meier,\n\ndie Rechnung fuer die"
                 " Aprillieferung geht Ihnen heute zu." + FOOTER,
            sent=datetime(2026, 4, 8, 9, 30, tzinfo=UTC),
        ),
        _message(
            key="b2", sender=OWN, to=(ANNA,), bcc=(REVISION,),
            subject="Gutschrift zur Aprillieferung",
            body="Sehr geehrte Frau Meier,\n\nfuer die Palette erhalten Sie"
                 " eine Gutschrift." + FOOTER,
            sent=datetime(2026, 4, 21, 14, 15, tzinfo=UTC),
        ),
    ]  # fmt: skip


def _weak() -> list[PlantedMessage]:
    """Block W — two messages sharing a ``participant_key`` and nothing else.

    A2's negative control for its weakest signal. Different subjects, no
    ticket, no thread, no attachment, two months apart and about entirely
    different things: at weight 0.2 the shared participant group must not join
    them, and a topic containing ``w1`` means the threshold has slipped.
    """
    return [
        _message(
            key="w1", sender=OWN, to=(ANNA,),
            subject="Urlaubsvertretung im August",
            body="Hallo Frau Meier,\n\nim August vertritt mich Frau Koch. Sie"
                 " erreichen\nsie unter der bekannten Durchwahl.",
            sent=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        ),
        _message(
            key="w2", sender=ANNA, to=(OWN,),
            subject="Einladung Sommerfest",
            body="Guten Tag,\n\nwir laden Sie herzlich zum Sommerfest am"
                 " zwanzigsten\nJuni auf unserem Betriebsgelaende ein."
                 " Anmeldung bis Ende Mai.",
            sent=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        ),
    ]  # fmt: skip


def planted_corpus() -> tuple[PlantedMessage, ...]:
    """Thirty-three mails: five findings and four ways of failing to find them.

    ============ == ========= ==================================================
    block         n direction planted for
    ============ == ========= ==================================================
    P p1-p5       5 mixed     A2's ticket topic, A1's three-address group
    S s01-s12    12 sent      A3's sent template, high automation score
    N n01-n10    10 received  A3's received template, low score, direction split
    F f1-f2       2 sent      body_clean: a shared footer is not a template
    B b1-b2       2 sent      Bcc: in the group, never in a pair
    W w1-w2       2 mixed     the weakest signal alone joins nothing
    ============ == ========= ==================================================
    """
    return (
        *_project(),
        *_status(),
        *_newsletter(),
        *_footer_only(),
        *_blind_copied(),
        *_weak(),
    )


ENV_PREFIX = "app_analytics_"
"""What :class:`~mailarc_analytics.derived.config.AnalyticsConfig` answers to."""


def calibrated_config() -> AnalyticsConfig:
    """The thresholds every expectation in this phase was measured against.

    ``AnalyticsConfig`` reads ``.env``, the way every component's config does,
    and here that is a hazard rather than a convenience: the assertions around
    this corpus are exact template memberships, exact topic ids and
    six-decimal automation scores, every one of them calibrated against the
    *defaults*. A developer trying one threshold against their own mailbox
    would turn four test files red with failures that name a cluster count and
    never the setting that moved.

    ``_env_file=None`` alone does not do it, and finding out why is the point
    of this function existing at all: ``appkit_commons`` calls
    ``load_dotenv(override=True)`` when it is imported, so ``.env`` is already
    in ``os.environ`` before any settings source runs and it is the
    *environment* source that wins. Measured — the assertion is in
    ``test_derived_config.py``. So the prefix comes out of the environment for
    the length of the call and goes straight back, and the dotenv source is
    turned off as well in case appkit ever stops doing that.

    Deliberately not a fixture: every one of these test modules builds its
    ``CONFIG`` at import time, and a fixture runs after collection.
    """
    stashed = {
        name: value
        for name, value in os.environ.items()
        if name.lower().startswith(ENV_PREFIX)
    }
    for name in stashed:
        del os.environ[name]
    try:
        return AnalyticsConfig(_env_file=None)
    finally:
        os.environ.update(stashed)


def by_key() -> dict[str, PlantedMessage]:
    """The corpus keyed by its short names, for a test that wants one block."""
    return {planted.key: planted for planted in planted_corpus()}


def canonical(key: str) -> str:
    """The canonical id a planted key ends up with in the graph.

    Every message carries a ``Message-ID``, so
    :func:`~mailarc_core.mail.identity.canonical_id` keeps it rather than
    hashing a substitute — which is what lets a test name ``p1`` and assert
    against what the analyses returned.
    """
    return f"{key}@nordlicht.example"


TOP_BIT_BODY = """\
Sehr geehrte Damen und Herren,

hiermit bestaetigen wir den Eingang Ihrer Bestellung mit der Kennung
{marker} und melden uns nach der Pruefung des Bestandes erneut bei Ihnen.

Die Bearbeitung erfolgt in der Reihenfolge des Eingangs. Sollten einzelne
Positionen nicht sofort verfuegbar sein, teilen wir Ihnen einen Liefertermin
mit, sobald er uns vom Hersteller bestaetigt wurde. Eine Teillieferung nehmen
wir nur nach Ruecksprache vor, damit Ihnen keine zusaetzlichen Versandkosten
entstehen. Aenderungen an der Bestellung beruecksichtigen wir bis zum Versand
der Ware, danach gilt der Vorgang als abgeschlossen.

Fuer Rueckfragen zu dieser Bestellung nennen Sie uns bitte die oben genannte
Kennung, dann finden wir den Vorgang sofort.
"""
"""A body chosen so its SimHash has bit 63 set, whatever the marker says.

The graph stores a fingerprint through
:func:`~mailarc_core.archive.model.to_signed_64`, because every Cypher
backend's integer is signed 64-bit — so this text is the one the corpus plants
to make a stored value *negative*. The markers below were searched for rather
than invented; the test asserts the sign rather than trusting this docstring.
"""

TOP_BIT_MARKERS = ("AB 100", "AB 101", "AB 102")
"""Three order references that leave the fingerprint's top bit set.

The text above carries the sign bit on its own — every marker tried put it
there — so these three were picked for being within the template threshold of
one another, which is what makes them a template rather than three singletons.
Nothing about the numbers matters; what matters is the sign, and the test
measures it rather than trusting this docstring.
"""


def top_bit_messages() -> tuple[PlantedMessage, ...]:
    """Three near-identical mails whose stored fingerprint is negative.

    The regression fixture for the one bug this phase was most likely to ship.
    A stored SimHash with bit 63 set comes back as a negative integer, and a
    band, a Hamming distance or a hex rendering taken from it without
    converting is wrong in a way that produces *no* clusters rather than wrong
    ones. Three copies, because a template needs three, so the trap is caught
    end to end and not only in the band function.
    """
    return tuple(
        _message(
            key=f"t{index:02d}",
            sender=OWN,
            to=(ANNA,),
            subject=f"Auftragsbestaetigung {marker}",
            body=TOP_BIT_BODY.format(marker=marker),
            sent=datetime(2026, 9, index * 7, 11, 0, tzinfo=UTC),
        )
        for index, marker in enumerate(TOP_BIT_MARKERS, start=1)
    )


ACCOUNT_ID = "1"
"""The SQLite row id the whole corpus is archived under.

Also half of every ``Thread`` key, which is why the fact projection below has
to know it: a thread node is ``{account}:{provider_thread_id}``, and a test
comparing a projected fact with one the reader fetched has to agree on that.
"""


def _thread_key(planted: PlantedMessage, parsed: ParsedMessage) -> str | None:
    """The ``Thread`` node key the writer would give this message.

    A second implementation of ``mailarc_core.archive.writer._thread_node``,
    for the same reason the rest of this projection is one: two paths to the
    same value is what makes either being wrong visible.

    Three keys in order — the provider's own thread id, the root of
    ``References``, and failing both the message's own ``Message-ID``. The
    third is what makes a conversation whole where the provider hands out no
    thread ids: its first message carries neither header, and would otherwise
    sit outside the thread its own replies form. A message with no
    ``Message-ID`` gets no thread, because its canonical id is a digest no
    reply can reference.
    """
    named = planted.thread or parsed.thread_hint or parsed.rfc_message_id
    return f"{ACCOUNT_ID}:{named}" if named else None


def facts_of(
    planted: PlantedMessage,
    *,
    own: frozenset[str] = frozenset({OWN}),
    fingerprint_body_text: bool = False,
) -> MessageFacts:
    """One planted mail as the analyses will see it, without a graph.

    A second implementation of what :func:`mailarc_analytics.read_facts` does
    against a real archive, and deliberately so: the pure tests get their facts
    from the parser directly, and one ``graph_local`` test then asserts that
    the reader produces exactly these values out of a written graph. Two paths
    to the same tuple is what makes either one wrong visible — a mirror that
    quietly agreed with a broken reader would be worse than no mirror.

    *fingerprint_body_text* is the naive implementation the negative controls
    are aimed at: it fingerprints the full text instead of the cleaned one,
    which is precisely the mistake ``body_clean`` exists to prevent. Nothing in
    production takes this path; the tests use it to show what it would cost.
    """
    parsed = parse_message(planted.raw)
    sender = parsed.sender.address if parsed.sender else ""
    addressed = tuple(
        sorted({one.address for one in parsed.to} | {one.address for one in parsed.cc})
    )
    everyone = set(addressed) | {one.address for one in parsed.bcc}
    if sender:
        everyone.add(sender)
    return MessageFacts(
        id=parsed.canonical_id,
        sent_at=parsed.sent_at,
        subject_norm=parsed.subject_norm,
        participant_key=parsed.participant_key,
        simhash=simhash(parsed.body_text) if fingerprint_body_text else parsed.simhash,
        refs=parsed.refs,
        thread_id=_thread_key(planted, parsed),
        sender=sender,
        addressed=addressed,
        participants=tuple(sorted(everyone)),
        attachments=tuple(sorted(one.sha256 for one in parsed.attachments)),
        outbound=bool(sender) and sender in own,
        body_clean=parsed.body_clean,
    )


def planted_facts(*, fingerprint_body_text: bool = False) -> tuple[MessageFacts, ...]:
    """The whole corpus projected, in the order the reader returns it.

    Sorted by canonical id, because that is what
    :data:`~mailarc_analytics.queries.catalog.MESSAGE_PROPERTIES` orders by and
    an analysis that only agreed with the fixture in fixture order would not be
    the analysis that runs.
    """
    found = [
        facts_of(planted, fingerprint_body_text=fingerprint_body_text)
        for planted in planted_corpus()
    ]
    return tuple(sorted(found, key=lambda one: one.id))


def circle_of(key: str) -> str:
    """The ``participant_key`` a planted message ends up with.

    A sha256 of the sorted address set, so it has no readable form to write
    into a test. Asked for by the name of a message instead, which is what lets
    an assertion say "this group is the project block's circle" rather than
    quoting thirty-two hex characters.
    """
    return facts_of(by_key()[key]).participant_key
