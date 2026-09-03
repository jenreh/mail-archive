"""The guard's own logic, without a store — what happens when there is no answer.

``test_derived_algorithms_local.py`` measures what FalkorDB does. This measures
what the guard does with it, and the two states worth asking about here are the
ones a real store will not produce on demand: a probe that fails outright, and a
backend that does not carry a procedure at all.

A recording stand-in rather than a server, for the reason
``test_derived_writes.py`` gives: these are claims about a wrapper, and a
process running redis-server would only make them slower to ask.
"""

from collections.abc import Mapping
from typing import Any, cast

from runic.ogm import QueryBuilder, Session

from mailarc_analytics.derived.algorithms import (
    address_betweenness,
    graph_algorithms,
    label_propagation,
    message_pagerank,
)
from mailarc_analytics.queries import catalog


class ProbeSession:
    """A session that answers ``dbms.procedures()`` from a script.

    ``execute`` and not ``all_rows``: the procedure statements are raw Cypher —
    runic 0.6 cannot start a statement with ``CALL`` — so
    :func:`~mailarc_analytics.queries.rows.rows_of` sends them down the other
    half of its dispatch.
    """

    def __init__(self, *, names: tuple[str, ...] = (), raises: bool = False) -> None:
        self._names = names
        self._raises = raises
        self.executed: list[str] = []

    def execute(
        self, statement: str, params: Mapping[str, Any] | None = None
    ) -> ProbeSession:
        self.executed.append(statement)
        if self._raises:
            raise RuntimeError("the store is not answering")
        return self

    def all_rows(
        self,
        statement: QueryBuilder[Any] | str,
        params: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        raise AssertionError("a procedure call is raw Cypher and never a builder")

    @property
    def columns(self) -> list[str]:
        return ["name"]

    @property
    def rows(self) -> list[list[str]]:
        return [[name] for name in self._names]


def _session(**fields: Any) -> tuple[ProbeSession, Session]:
    fake = ProbeSession(**fields)
    return fake, cast(Session, fake)


def test_a_store_that_will_not_list_its_procedures_is_a_store_with_none() -> None:
    """One unreachable capability must not cost the whole derived layer.

    A rebuild has nine other stages and every one of them is worth running, so
    the probe answers with nothing and each ``algo.*`` call is then skipped and
    counted — which is exactly the answer a user can act on.
    """
    _, session = _session(raises=True)

    assert graph_algorithms(session) == frozenset()


def test_a_procedure_the_store_does_not_have_is_skipped_before_a_round_trip() -> None:
    """The probe is what stops the call, so the store is never asked.

    ``all_rows`` on the stand-in raises if it is reached, and the three calls
    below would have to go through it — that is the assertion, and it is why
    the guard checks the name before it tries.
    """
    fake, session = _session(names=("dbms.procedures",))

    partition = label_propagation(session, max_iterations=20)
    ranked = message_pagerank(session)
    bridges = address_betweenness(session, sampling_size=4, seed=7)

    assert (partition.labels, partition.skipped) == ({}, 1)
    assert (ranked.scores, ranked.skipped) == ({}, 1)
    assert (bridges.scores, bridges.skipped) == ({}, 1)
    assert fake.executed == [catalog.PROCEDURES], "one probe, and no procedure call"


def test_the_probe_is_one_round_trip_however_often_it_is_asked() -> None:
    """Cached per session. Four stages asking would otherwise be four round
    trips for a set that cannot change while a session is open."""
    fake, session = _session(names=("algo.labelPropagation",))

    for _ in range(4):
        graph_algorithms(session)

    assert fake.executed == [catalog.PROCEDURES]


def test_the_probe_lower_cases_what_the_binary_spells_in_camel_case() -> None:
    """The binary writes ``algo.labelPropagation``; its errors lower-case it.

    One spelling has to win, and the guard compares against a name written in
    this repository — so the store's answer is the one that gets normalised.
    """
    _, session = _session(names=("algo.labelPropagation", "algo.WCC"))

    assert graph_algorithms(session) == {"algo.labelpropagation", "algo.wcc"}
