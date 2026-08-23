"""Union-find, and nothing about a component that union order decides.

Its own file because :mod:`mailarc_analytics.derived.partition` is its own
module, and the reason both exist is the same: A2 and A3 are connected
components under different names, neither may import the other, and a component
that depended on the order the unions arrived in would rename a topic nobody
had changed.
"""

import pytest

from mailarc_analytics import DisjointSet


class TestDisjointSet:
    """Connected components, and nothing about them that union order decides."""

    def test_it_finds_the_components_it_was_joined_into(self) -> None:
        partition = DisjointSet(["a", "b", "c", "d", "e"])

        partition.union("a", "b")
        partition.union("d", "e")

        assert partition.components() == [["a", "b"], ["c"], ["d", "e"]]

    def test_the_answer_does_not_depend_on_the_order_of_the_unions(self) -> None:
        """A chain joined forwards and backwards is the same component.

        Which is what lets A2 union a bucket's members consecutively instead of
        pairwise: *k-1* unions connect exactly what *k(k-1)/2* would.
        """
        forwards = DisjointSet("abcde")
        backwards = DisjointSet("abcde")
        for left, right in zip("abcd", "bcde", strict=True):
            forwards.union(left, right)
        for left, right in zip("dcba", "edcb", strict=True):
            backwards.union(left, right)

        assert forwards.components() == backwards.components() == [list("abcde")]

    def test_a_chain_connects_what_a_clique_connects(self) -> None:
        """The equivalence the strong-signal shortcut is justified by."""
        chained = DisjointSet("abcd")
        clique = DisjointSet("abcd")
        for left, right in zip("abc", "bcd", strict=True):
            chained.union(left, right)
        for left in "abcd":
            for right in "abcd":
                clique.union(left, right)

        assert chained.components() == clique.components()

    def test_joining_the_same_pair_twice_changes_nothing(self) -> None:
        partition = DisjointSet("abc")

        partition.union("a", "b")
        partition.union("b", "a")

        assert partition.components() == [["a", "b"], ["c"]]

    def test_an_id_it_was_never_given_is_an_error_rather_than_a_singleton(
        self,
    ) -> None:
        """A caller that lost track of its own message set should hear about it.

        A lazily grown set would answer with a component of one, which reads
        exactly like a message nothing resembled.
        """
        partition = DisjointSet("ab")

        with pytest.raises(KeyError):
            partition.union("a", "z")
