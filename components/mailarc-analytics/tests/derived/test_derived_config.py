"""Fifteen thresholds, and the prefix that decides whether any of them is read.

A typo in ``env_prefix`` costs nothing at import time and everything at run
time: the setting is simply never seen and the default quietly wins, which
looks exactly like an analysis that ignored the user. So the prefix is checked
against the environment rather than against the string in the source.

The defaults are asserted one by one because three of them are decisions rather
than round numbers — ``simhash_max_distance`` is five where the spec says
three, ``template_max_comparisons`` is a budget where its neighbour
``topic_bucket_cap`` is a cap, and there is no ``topic_min_signals`` where §6.2
invites one. All three would be re-introduced by the next reader if nothing
wrote them down as expectations.

Built through :func:`corpus.calibrated_config`, which takes the
``app_analytics_`` prefix out of the environment for the length of the call.
That indirection is load-bearing rather than tidy: ``appkit_commons`` calls
``load_dotenv(override=True)`` when it is imported, so a repository ``.env``
is in ``os.environ`` before any settings source runs and ``_env_file=None``
cannot keep it out. This phase's expectations are exact counts, exact ids and
six-decimal scores calibrated against the defaults, so one ``.env`` line would
turn this file and four others red with failures naming a cluster count and
never the setting that moved. The mechanism is measured below rather than
assumed, and an *exported* variable still overrides a run, which the last
three tests prove.
"""

import corpus
import pytest

from mailarc_analytics import AnalyticsConfig

DEFAULTS = {
    "min_group_size": 3,
    "min_group_messages": 2,
    "co_addressed_max_recipients": 25,
    "simhash_max_distance": 5,
    "lsh_band_bits": 16,
    "template_min_occurrences": 3,
    "template_max_comparisons": 10_000_000,
    "template_sample_length": 500,
    "template_ideal_words": 200,
    "template_min_words": 25,
    "frequency_saturation": 12,
    "topic_min_score": 0.5,
    "topic_bucket_cap": 200,
    "topic_max_weak_pairs": 2_000_000,
    "max_messages": 0,
    "community_min_size": 3,
    "community_max_iterations": 20,
    "circle_min_share": 0.5,
    "centrality_max_edges": 2_000_000,
    "betweenness_sampling": 0,
    "topic_keyword_count": 8,
    "topic_keyword_members": 20,
    "topic_keyword_chars": 2000,
    "tag_suggest_min_tagged": 2,
    "tag_suggest_min_share": 0.3,
    "tag_auto_accept": False,
    "tag_auto_accept_min_score": 0.6,
}


def _calibrated() -> AnalyticsConfig:
    """The config the whole phase was measured against, ``.env`` kept out."""
    return corpus.calibrated_config()


def test_the_defaults_are_the_calibrated_ones() -> None:
    """Measured against the planted corpus, not copied from the spec."""
    config = _calibrated()

    assert {name: getattr(config, name) for name in DEFAULTS} == DEFAULTS


def test_there_are_no_settings_beyond_the_twenty_seven() -> None:
    """A new knob is a new way for two rebuilds to disagree; it should be a
    visible change here rather than a quiet one in ``config.py``."""
    assert set(AnalyticsConfig.model_fields) == set(DEFAULTS)


def test_betweenness_is_off_until_somebody_asks_for_it() -> None:
    """Zero means skip, and it is the default because the number it produces
    is not one anything renders yet.

    ``algo.betweenness`` refuses a sampling size of zero outright, so the guard
    is "do not call it" rather than "call it with nothing" — which is why the
    setting is the sampling size and not a boolean.
    """
    assert _calibrated().betweenness_sampling == 0


def test_auto_accept_is_off_and_its_threshold_is_above_a_weak_group() -> None:
    """Two settings, and the pair is the decision.

    A tag is a human's word for a set of messages, so nothing may join one
    without somebody saying yes — the flag is off. Turned on, the threshold
    still has to sit above what a weak group produces: a community group at
    ``0.4`` weight cannot reach ``0.6`` however many of its members are
    tagged, so auto-accept is a thread or a topic saying so.
    """
    config = _calibrated()

    assert config.tag_auto_accept is False
    assert config.tag_auto_accept_min_score > config.tag_suggest_min_share


def test_the_keyword_read_is_capped_in_both_directions() -> None:
    """Members times characters is the whole cost of the keyword stage, and
    both halves are settings so that neither can grow without the other being
    looked at."""
    config = _calibrated()

    assert config.topic_keyword_members > 0
    assert config.topic_keyword_chars > 0
    assert config.topic_keyword_count < config.topic_keyword_members


def test_the_distance_threshold_is_five_and_not_the_spec_s_three() -> None:
    """§6.3 says three. Measured on real German business mail, three finds
    almost nothing — a twelve-message monthly series breaks into pieces of
    seven, three, one and one — and the corpus tests are what re-measure that
    claim, at that threshold, rather than recalling it."""
    assert _calibrated().simhash_max_distance == 5


def test_a3_gets_a_comparison_budget_and_not_a2_s_bucket_cap() -> None:
    """The two guards look alike and mean opposite things.

    An over-sized *topic* bucket is boilerplate by definition, so A2 drops it.
    An over-sized *template* bucket is the largest finding A3 could make, and
    it is also the cheap one — a genuine family joins up in its first unions
    and every later pair skips its distance. A cap there would throw away the
    finding to bound a cost it does not incur.
    """
    assert "template_bucket_cap" not in AnalyticsConfig.model_fields
    assert _calibrated().template_max_comparisons > 0


def test_there_is_no_signal_count_threshold() -> None:
    """With weights, "how many signals" is already "how much score".

    Two would kill the ticket topic §6.2 calls a fact; one would do nothing.
    Asserted so that adding it back is a failing test rather than a plausible
    afternoon.
    """
    assert "topic_min_signals" not in AnalyticsConfig.model_fields


def test_a_dotenv_entry_arrives_through_the_environment_and_not_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which is why the suite scrubs the prefix rather than passing
    ``_env_file=None``.

    ``appkit_commons`` calls ``load_dotenv(override=True)`` at import, so a
    repository ``.env`` is already in ``os.environ`` when the first settings
    source runs and it is the *environment* source that answers. Turning the
    dotenv source off therefore changes nothing at all — measured here, so
    that the next reader does not repeat the fix that does not work.
    """
    monkeypatch.setenv("app_analytics_simhash_max_distance", "2")

    assert AnalyticsConfig(_env_file=None).simhash_max_distance == 2
    assert _calibrated().simhash_max_distance == 5


def test_scrubbing_the_prefix_puts_the_environment_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper that quietly ate a developer's override would be worse than
    the contamination it exists to prevent."""
    monkeypatch.setenv("app_analytics_max_messages", "500")

    _calibrated()

    assert AnalyticsConfig().max_messages == 500


def test_the_environment_prefix_is_app_analytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("app_analytics_min_group_size", "7")
    monkeypatch.setenv("app_analytics_simhash_max_distance", "2")
    monkeypatch.setenv("app_analytics_topic_min_score", "0.9")

    config = AnalyticsConfig()

    assert (config.min_group_size, config.simhash_max_distance) == (7, 2)
    assert config.topic_min_score == 0.9


def test_an_explicit_value_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition root passes a built config; nothing may shadow it."""
    monkeypatch.setenv("app_analytics_max_messages", "500")

    assert AnalyticsConfig(_env_file=None, max_messages=10).max_messages == 10


def test_a_setting_the_prefix_does_not_match_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another component's variable must not reach this one."""
    monkeypatch.setenv("app_archive_min_group_size", "99")

    assert _calibrated().min_group_size == 3
