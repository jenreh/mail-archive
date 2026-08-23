"""Union-find over message ids — connected components and nothing else.

Its own module because both remaining analyses are connected components under
different names: A2 clusters a similarity graph, A3 chains near-identical
bodies into one template through the copies that sit between them. Neither may
import the other, so the shared piece needs somewhere neutral to live — and
that somewhere is not ``model.py``, which §6 reserves for value objects that
know no I/O. This is an algorithm with mutable state, so it gets the one file
that is one responsibility.

Thirty lines instead of a graph library. The spec writes "ggf." beside
networkx, and a dependency whose entire job is to find the components of a
graph this package builds itself is one more thing to resolve, pin, audit and
vendor into a Tauri bundle for the sake of one loop.
"""

from collections.abc import Iterable


class DisjointSet:
    """The components a sequence of unions leaves behind.

    Every item has to be known at construction. An id arriving at :meth:`union`
    that was never handed in is a caller that lost track of its own message
    set, and a lazily grown set would answer that with a component of one
    instead of with the error it is.
    """

    def __init__(self, items: Iterable[str]) -> None:
        self._parent: dict[str, str] = {item: item for item in items}
        self._rank: dict[str, int] = dict.fromkeys(self._parent, 0)

    def find(self, item: str) -> str:
        """The representative of *item*'s component, flattening the path.

        Iterative rather than recursive: a chain of a hundred thousand
        near-identical newsletters is a legitimate archive and a plausible
        stack overflow.
        """
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        """Put the two components into one. Idempotent, order-independent."""
        first, second = self.find(left), self.find(right)
        if first == second:
            return
        if self._rank[first] < self._rank[second]:
            first, second = second, first
        self._parent[second] = first
        if self._rank[first] == self._rank[second]:
            self._rank[first] += 1

    def components(self) -> list[list[str]]:
        """Every component, members sorted, the components sorted among them.

        Sorted on the way out rather than trusted from the way in. Which item
        ends up as a root depends on the order the unions happened to arrive
        in, and a topic keyed on its root would be renamed by a rebuild that
        found exactly the same messages.
        """
        found: dict[str, list[str]] = {}
        for item in sorted(self._parent):
            found.setdefault(self.find(item), []).append(item)
        return sorted(found.values())
