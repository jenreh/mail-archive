"""LSH banding, and the sign that would quietly turn it into noise.

:func:`~mailarc_analytics.derived.templates.band_keys` is four lines long and
carries the whole of A3's candidate generation, so the things that can go wrong
with it are few and each of them is fatal in its own way. Leave the band index
out of the key and band 0 of one message collides with band 3 of another, which
produces candidates that are not similar at all. Take the bands off the value
the graph handed back without converting it and every fingerprint with bit 63
set — roughly half of all real messages — is compared as a negative number,
where :func:`~mailarc_core.mail.parsing.hamming_distance` answers 62 for a pair
that differs in two bits. The second failure does not produce wrong clusters;
it produces none, which is much harder to notice.

The pigeonhole guarantee is asserted too, because it is the reason four
sixteen-bit bands are the right shape: *d* differing bits touch at most *d*
bands, so any pair within three bits shares at least one band exactly. Past
that recall is probabilistic, and A3 buys the rest back with single linkage
rather than with narrower bands.
"""

import corpus
import pytest

from mailarc_analytics import band_keys
from mailarc_core.archive.model import to_signed_64, to_unsigned_64
from mailarc_core.mail.parsing import SIMHASH_BITS, hamming_distance

TOP_BIT = 0xA0B86145044638A0
"""One of the planted corpus's own fingerprints, and negative once stored."""

FOUR_BANDS = 0x123456789ABCDEF0
"""Four distinct sixteen-bit bands, so a rotation of them collides on values
without colliding on keys."""

ROTATED = 0x56789ABCDEF01234
"""The same four band values, each moved one band along."""


def test_sixteen_bit_bands_cut_the_fingerprint_into_four() -> None:
    assert len(band_keys(FOUR_BANDS, 16)) == 4
    assert len(band_keys(FOUR_BANDS, 8)) == 8
    assert len(band_keys(FOUR_BANDS, SIMHASH_BITS)) == 1


def test_the_bands_are_cut_from_the_least_significant_end() -> None:
    """Which is what makes the reconstruction below total."""
    assert band_keys(FOUR_BANDS, 16) == (
        (0, 0xDEF0),
        (1, 0x9ABC),
        (2, 0x5678),
        (3, 0x1234),
    )


def test_the_bands_put_back_together_are_the_fingerprint_again() -> None:
    """No bit is dropped and none is counted twice — the property that makes
    a band a partition rather than a sample."""
    rebuilt = sum(value << (index * 16) for index, value in band_keys(TOP_BIT, 16))

    assert rebuilt == TOP_BIT


def test_the_band_index_is_part_of_the_key() -> None:
    """Without it, two messages sharing no band would still be candidates.

    These two carry the same four band *values* in a different order. An
    implementation keyed on the value alone would call them a candidate pair
    and hand a Hamming distance of twenty-four to the verification step; keyed
    on the pair, they share nothing.
    """
    left = band_keys(FOUR_BANDS, 16)
    right = band_keys(ROTATED, 16)

    assert {value for _index, value in left} == {value for _index, value in right}
    assert set(left).isdisjoint(right)
    assert hamming_distance(FOUR_BANDS, ROTATED) == 24


class TestTheSignTrap:
    """A stored fingerprint is signed; everything downstream assumes it is not."""

    def test_a_stored_fingerprint_with_the_top_bit_set_is_negative(self) -> None:
        assert to_signed_64(TOP_BIT) < 0

    def test_banding_the_stored_value_gives_the_same_keys(self) -> None:
        """The conversion happens inside :func:`band_keys`, once, deliberately.

        Masking a negative value happens to give the right bits — Python
        shifts arithmetically over an infinite two's complement and the mask
        cuts the sign extension off again — so this assertion looks like it
        cannot fail. It exists because the *value* goes on to be hashed,
        rendered and compared, and a version of the function that skipped the
        conversion would pass a bands-only test and break everything after it.
        """
        assert band_keys(to_signed_64(TOP_BIT), 16) == band_keys(TOP_BIT, 16)

    def test_the_conversion_is_idempotent(self) -> None:
        """A caller that already converted must not be converted back."""
        assert band_keys(TOP_BIT, 16) == band_keys(to_unsigned_64(TOP_BIT), 16)

    def test_a_hamming_distance_taken_from_stored_values_is_nonsense(self) -> None:
        """Why the conversion is not decoration, stated as the numbers.

        ``int.bit_count()`` counts the ones of the *absolute* value, so a
        mixed-sign exclusive or is silently meaningless. Two bits apart becomes
        sixty-two, which is not a near miss — it is the opposite answer, and
        clusters built on it never form at all.
        """
        left, right = (1 << 63) | 0b11, 0b1

        assert hamming_distance(left, right) == 2
        assert hamming_distance(to_signed_64(left), to_signed_64(right)) == 62


class TestTheRecallGuarantee:
    """Why four bands, and what four bands do and do not promise."""

    @pytest.mark.parametrize("flips", [(), (0,), (0, 63), (5, 31, 62)])
    def test_any_pair_within_three_bits_shares_a_band(
        self, flips: tuple[int, ...]
    ) -> None:
        """Pigeonhole: *d* differing bits touch at most *d* of the four bands.

        So at three bits or fewer one band is untouched and the pair is
        certainly a candidate. This is the zero-loss part of the design and the
        reason the distance threshold, not the band width, is the knob to turn.
        """
        other = TOP_BIT
        for bit in flips:
            other ^= 1 << bit

        assert hamming_distance(TOP_BIT, other) <= 3
        assert set(band_keys(TOP_BIT, 16)) & set(band_keys(other, 16))

    def test_a_pair_that_differs_in_every_band_is_not_a_candidate(self) -> None:
        """Four bits, one per band, is where the guarantee stops.

        Single linkage is what buys the reach back: a group only needs *k-1* of
        its *k(k-1)/2* pairs, and the planted twelve-message series comes back
        whole despite intra-group distances of eight.
        """
        other = TOP_BIT
        for bit in (1, 17, 33, 49):
            other ^= 1 << bit

        assert hamming_distance(TOP_BIT, other) == 4
        assert not set(band_keys(TOP_BIT, 16)) & set(band_keys(other, 16))


@pytest.mark.parametrize("band_bits", [0, -1, 65])
def test_a_band_width_outside_the_fingerprint_is_refused(band_bits: int) -> None:
    """Silently banding nothing would produce one bucket holding the archive."""
    with pytest.raises(ValueError, match="band_bits"):
        band_keys(TOP_BIT, band_bits)


def test_the_default_band_width_divides_the_fingerprint() -> None:
    """A width that does not divide 64 leaves the top bits out of every bucket,
    which costs recall for nothing."""
    assert SIMHASH_BITS % corpus.calibrated_config().lsh_band_bits == 0
