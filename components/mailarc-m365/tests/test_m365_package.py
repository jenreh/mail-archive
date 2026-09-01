"""What `app/composition.py` is allowed to reach for, and what it must not.

The composition root is the only module permitted to name a provider, so the
three names it needs have to be at the top of the package: reaching into
`mailarc_m365.source.source` for a class would make the submodule layout part
of the contract, which is exactly what `mailarc-google` avoids.

The import bans are enforced from a subprocess by
`components/mailarc-core/tests/test_isolation.py`; this file holds the part of
the same promise that can be checked from inside the process.
"""

import json
import subprocess
import sys

import mailarc_m365
from mailarc_core.mail.model import MailProvider

FORBIDDEN = (
    "mailarc_sync",
    "mailarc_google",
    "mailarc_imap",
    "mailarc_analytics",
    "mailarc_ui",
    "reflex",
    "runic.rag",
    "app",
)


def test_the_package_states_its_purpose() -> None:
    assert mailarc_m365.__doc__


def test_the_composition_root_finds_all_three_names_at_the_top() -> None:
    assert set(mailarc_m365.__all__) == {
        "M365_DESCRIPTOR",
        "M365Source",
        "consent_runner",
    }
    for name in mailarc_m365.__all__:
        assert hasattr(mailarc_m365, name)


def test_the_descriptor_and_the_source_agree_on_the_provider() -> None:
    assert mailarc_m365.M365_DESCRIPTOR.provider is MailProvider.M365
    assert mailarc_m365.M365Source.provider is MailProvider.M365
    assert mailarc_m365.M365Source.DESCRIPTOR is mailarc_m365.M365_DESCRIPTOR


def test_importing_it_pulls_in_no_sibling_component_and_no_framework() -> None:
    """A provider is a way of fetching mail, not of scheduling it.

    Reflex belongs to `mailarc-ui` alone, `mailarc_sync` drives this adapter
    through the port and must not be nameable from it, and `mailarc_google`'s
    loopback server is the obvious thing to reach for and is out of reach on
    purpose.

    In a **subprocess**, because by the time the whole suite has run, this
    process has imported half of them for other reasons and `sys.modules` would
    answer yes to everything.
    """
    probe = (
        "import json, sys, mailarc_m365, mailarc_m365.source;"
        f"print(json.dumps([n for n in {FORBIDDEN!r} if n in sys.modules]))"
    )
    finished = subprocess.run(  # noqa: S603 - this interpreter, a literal probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert json.loads(finished.stdout) == []
