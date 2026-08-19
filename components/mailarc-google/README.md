# mailarc-google

Gmail, behind the one seam this design allows to be abstract.

The package implements `mailarc-core`'s mail source port and nothing else:
OAuth, paging, and the mapping from Google's JSON to the domain's value
objects. Provider shapes stop at that boundary — no Gmail field reaches the
graph unmapped.

The package is `source/`, named after the capability rather than the vendor, so
that `mailarc_imap.source` and `mailarc_m365.source` can look identical.

Empty until phase 3 fills it.

## Rules

- Depends on `mailarc-core` alone (plus httpx and google-auth from phase 3).
- **No `mailarc-sync`.** The engine drives this adapter through the port; it
  never learns its name. `app/composition.py` does the registering.
- **No Reflex, no `appkit` UI package.**
- **No `runic.rag`.**

`components/mailarc-core/tests/test_isolation.py` enforces the last two.
