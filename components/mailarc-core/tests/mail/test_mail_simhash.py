"""The lexical fingerprint A3 buckets on.

A3 asks which mails are written *with the same words*, not which are *about
the same thing*, so the claim under test is numeric: a re-sent template stays
in a tight neighbourhood while an unrelated mail is an order of magnitude
away. The bodies below are the length real status mails have — a five-word
sample would pass with any hash function and prove nothing.

Note what the numbers say about the threshold. SimHash sensitivity scales with
body length: one word changed in a 140-word mail moves about four bits, in a
500-word mail about one. A3's ``<= 3`` is therefore a bucketing decision to
calibrate against real archives, not a property this hash guarantees, and the
assertions below deliberately bound the *ratio* rather than pin that constant.
"""

import pytest

from mailarc_core.mail.parsing import SIMHASH_BITS, hamming_distance, simhash

STATUS_MAIL = """\
Hallo zusammen,

hier der wöchentliche Statusbericht für das Projekt Nordlicht.

Die Migration der Bestandsdaten aus dem Altsystem ist abgeschlossen. Von den
gut zweihunderttausend Datensätzen mussten wir dreihundert manuell nacharbeiten,
weil die Adressfelder dort nie gepflegt waren. Die Abnahme durch den Fachbereich
steht für Donnerstag an, die Testfälle liegen seit gestern vor.

Offene Punkte sind weiterhin die Anbindung des Rechnungssystems und die Schulung
der Sachbearbeitung. Für die Anbindung warten wir auf die Freigabe der
Schnittstellenbeschreibung durch den Hersteller, angekündigt ist sie für Anfang
kommender Woche. Die Schulung planen wir in zwei Blöcken, damit der laufende
Betrieb nicht stillsteht.

Risiken sehen wir derzeit keine, die Kolleginnen aus dem Rechnungswesen sind
eingebunden und der Termin im April bleibt realistisch. Sollte die Freigabe
später kommen als angekündigt, verschiebt sich die Anbindung um eine Woche, der
Rest des Zeitplans bleibt davon unberührt.

Rückfragen gerne jederzeit an mich.
"""

STATUS_MAIL_NEXT_WEEK = STATUS_MAIL.replace("Donnerstag", "Freitag")

UNRELATED_MAIL = """\
Guten Tag,

anbei erhalten Sie die Rechnung für die im Februar bezogenen Lizenzen. Der
Betrag ist innerhalb von vierzehn Tagen ohne Abzug fällig. Bei Rückfragen zur
Position drei wenden Sie sich bitte an unsere Buchhaltung, die Durchwahl finden
Sie im Briefkopf. Bitte geben Sie bei der Überweisung die Rechnungsnummer an,
sonst können wir den Eingang nicht zuordnen. Wir bedanken uns für die
Zusammenarbeit im vergangenen Quartal und freuen uns auf die weitere.
"""

#: What one edited sentence in a mail of this length is allowed to cost.
NEAR_BITS = 8

#: What two mails with nothing in common have to be at least.
FAR_BITS = 20


class TestSimhash:
    def test_two_runs_of_the_same_template_stay_close(self) -> None:
        distance = hamming_distance(
            simhash(STATUS_MAIL), simhash(STATUS_MAIL_NEXT_WEEK)
        )

        assert distance <= NEAR_BITS

    def test_an_unrelated_mail_is_far_away(self) -> None:
        distance = hamming_distance(simhash(STATUS_MAIL), simhash(UNRELATED_MAIL))

        assert distance >= FAR_BITS

    def test_the_gap_between_near_and_far_is_what_makes_bucketing_possible(
        self,
    ) -> None:
        """The single claim A3 rests on, stated as one comparison."""
        near = hamming_distance(simhash(STATUS_MAIL), simhash(STATUS_MAIL_NEXT_WEEK))
        far = hamming_distance(simhash(STATUS_MAIL), simhash(UNRELATED_MAIL))

        assert far > 4 * near

    def test_identical_text_hashes_identically(self) -> None:
        assert simhash(STATUS_MAIL) == simhash(STATUS_MAIL)

    def test_the_hash_fits_in_64_bits(self) -> None:
        assert 0 <= simhash(STATUS_MAIL) < (1 << SIMHASH_BITS)

    def test_case_does_not_move_the_hash(self) -> None:
        """Tokenisation is lowercased words; a shouting client is the same mail."""
        assert simhash(STATUS_MAIL) == simhash(STATUS_MAIL.upper())

    def test_punctuation_and_reflowing_do_not_move_the_hash(self) -> None:
        reflowed = STATUS_MAIL.replace("\n", " ").replace(",", "").replace(".", "")

        assert simhash(STATUS_MAIL) == simhash(reflowed)

    def test_reordering_paragraphs_still_reads_as_the_same_template(self) -> None:
        """Shingles overlap, so only the seams change when blocks move."""
        head, _, tail = STATUS_MAIL.partition("Offene Punkte")

        distance = hamming_distance(
            simhash(STATUS_MAIL), simhash(f"Offene Punkte{tail}\n{head}")
        )

        assert distance <= NEAR_BITS

    def test_empty_text_hashes_to_zero(self) -> None:
        assert simhash("") == 0
        assert simhash("   \n\n  ") == 0

    def test_a_body_shorter_than_one_shingle_still_hashes(self) -> None:
        """Falls back to single words rather than to zero, which is a bucket."""
        assert simhash("kurz", shingle_size=3) != 0

    def test_the_shingle_size_is_a_knob_not_a_constant(self) -> None:
        assert simhash(STATUS_MAIL, shingle_size=3) != simhash(
            STATUS_MAIL, shingle_size=5
        )


class TestHammingDistance:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [(0, 0, 0), (0b1011, 0b1011, 0), (0b0000, 0b1111, 4), (1, 1 << 63, 2)],
    )
    def test_counts_the_differing_bits(self, left, right, expected) -> None:
        assert hamming_distance(left, right) == expected
