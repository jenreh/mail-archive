"""Phase 0 leaves this package empty on purpose — it still has to import."""

import mailarc_sync


def test_package_imports_and_states_its_purpose() -> None:
    assert mailarc_sync.__doc__, "the docstring is what an empty package promises"
