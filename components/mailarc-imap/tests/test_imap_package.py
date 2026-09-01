"""What ``app/composition.py`` is allowed to reach for, and what it is not.

The composition root registers a provider with two names — a descriptor and a
class — and §4.2 puts both at the top level of the component, so that nothing
above ever has to reach into a submodule. That is one import line in
``app/composition.py`` and it is checked here rather than discovered when
somebody renames a module.

The rest is the component's half of the isolation rules. The subprocess probes
in ``components/mailarc-core/tests/test_isolation.py`` are the enforcement, and
they cannot see this package until the tuples in that sealed file name it — so
until they do, this file is what holds the line.
"""

import ast
import inspect
import pkgutil
from types import ModuleType

import pytest

import mailarc_imap
from mailarc_core.mail.model import ProviderDescriptor

FORBIDDEN = (
    "mailarc_sync",
    "mailarc_analytics",
    "mailarc_google",
    "mailarc_mcp",
    "mailarc_ui",
    "reflex",
    "appkit_mantine",
    "appkit_user",
    "app",
)
"""What a provider may not see.

``mailarc_sync`` because the engine drives this adapter through the port and
must not be nameable from underneath it. ``mailarc_google`` because a component
may not import a sibling — its loopback server and OAuth module are the obvious
thing to reach for when this provider grows a second authentication method, and
they are out of reach on purpose. Reflex and the ``appkit`` UI packages because
``mailarc-ui`` is the only component allowed to see a browser. ``app`` because
the arrow points the other way.
"""


def modules() -> list[ModuleType]:
    """Every module in this component, imported."""
    import importlib

    found = [mailarc_imap]
    for info in pkgutil.walk_packages(mailarc_imap.__path__, prefix="mailarc_imap."):
        found.append(importlib.import_module(info.name))
    return found


class TestThePublicSurface:
    """Two names at the top, which is all the composition root ever needs."""

    def test_the_descriptor_and_the_source_are_re_exported(self) -> None:
        assert isinstance(mailarc_imap.IMAP_DESCRIPTOR, ProviderDescriptor)
        assert mailarc_imap.ImapSource.DESCRIPTOR is mailarc_imap.IMAP_DESCRIPTOR

    def test_all_says_the_same_thing(self) -> None:
        """Out of step with the imports is how a composition root ends up reaching in."""
        assert set(mailarc_imap.__all__) == {"IMAP_DESCRIPTOR", "ImapSource"}
        assert all(hasattr(mailarc_imap, name) for name in mailarc_imap.__all__)

    def test_the_package_states_its_purpose(self) -> None:
        assert mailarc_imap.__doc__


class TestTheImportRules:
    """The bans, read off the parsed source of every module in the package."""

    @pytest.mark.parametrize("module", modules(), ids=lambda module: module.__name__)
    def test_it_imports_nothing_it_may_not(self, module: ModuleType) -> None:
        tree = ast.parse(inspect.getsource(module))
        imported = {
            name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in _packages(node)
        }

        assert not imported & set(FORBIDDEN)

    def test_runic_rag_is_not_in_the_room(self) -> None:
        """No module in this project imports it, ``app`` included."""
        import sys

        assert "runic.rag" not in sys.modules


def _packages(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Every top-level package one import statement pulls in.

    A relative import names nothing outside the package, so it contributes
    nothing to the ban list.
    """
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.module and not node.level else []
    return [alias.name for alias in node.names]
