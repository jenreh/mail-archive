# Adding a mail provider

A provider is a component that implements one `Protocol` and declares what it
needs. Nothing above it learns its name.

The success criterion is stated in the plan and worth keeping: adding a provider
must produce **no diff** in `mailarc-sync`, `mailarc-analytics` or
`mailarc-core`. If it does, the port was cut wrong.

## The port

```python
class MailSourcePort(Protocol):
    provider: MailProvider

    async def verify(self) -> AccountIdentity: ...
    async def list_labels(self) -> Sequence[LabelInfo]: ...
    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage: ...
    async def fetch_raw(
        self, refs: Sequence[MessageRef]
    ) -> AsyncIterator[RawMessage]: ...
    async def watermark(self) -> SyncCursor | None: ...
    async def aclose(self) -> None: ...
```

Six methods, and one hard rule: **every one may raise from
`mailarc_core.mail.errors` and nothing else.** An adapter that lets an `httpx`
error through has not decided whether the engine should retry.

| Method | Contract |
| --- | --- |
| `verify` | Prove the credentials work, and report *whose* mailbox this is. For an OAuth provider this includes the consent round trip and writing the refreshed token back |
| `list_labels` | Every label or folder the account has. Read once per run |
| `list_messages` | One page of references. `None` starts from the beginning. Paging is yours; the engine only asks whether a next cursor came back |
| `fetch_raw` | A **coroutine returning an async iterator**, not an async generator — see below |
| `watermark` | Where a delta would start **if it started now** — Gmail's `getProfile().historyId`, IMAP's `UIDNEXT`, Graph's first `deltaLink`. `None` from a source that has no delta at all |
| `aclose` | Release the connection. Safe to call twice |

`watermark` is the one that is easy to get subtly wrong. The engine reads it
**before** the first listing and stores it only when the run finishes: a mark
read before the walk sits behind everything the walk fetched, so the next run's
overlap is filtered by the archived-messages ledger and nothing can fall in a
gap. An end-point would lose every message that arrived while the run was
going. Do not cache it across runs either — a stale mark is stale in the losing
direction.

Two more things follow from that, and both are the engine's job rather than
yours. A full walk that is resumed inherits the mark of the attempt that *began*
it, out of the `full-pending` checkpoint scope, because a mailbox lists newest
first and a resumed attempt only ever goes further back. And a `watermark()`
that returns a mark **new arrivals can sneak under** is worse than none: if your
ordering is not one arrivals respect — file names, subjects, anything a user
chose — return a mark that accounts for nothing and let the ledger do the
filtering, the way `FakeMailSource` does over a directory.

### `fetch_raw` returns a stream

```python
async def fetch_raw(self, refs) -> AsyncIterator[RawMessage]:
    return self._stream(refs)  # not: yield inside this function
```

A stream rather than a list, because a batch of full messages is tens of
megabytes and the writer can start before the last one lands.

And a coroutine *returning* an iterator rather than an async generator, because
that is what the signature spells — an adapter that gets it wrong fails at the
call site rather than silently never being iterated.

### The cursor is yours and opaque

```python
class SyncCursor(BaseModel):
    provider: MailProvider
    token: str
    kind: SyncCursorKind
```

Gmail puts a `historyId` in `token`, IMAP a `UIDVALIDITY/UIDNEXT` pair, MS Graph
a `deltaLink`. **The engine never looks inside.** It stores the token and hands
it back — which is what keeps the port from growing a provider-shaped hole.

Worth stealing from `FakeMailSource`: it resumes by *bisection* over sorted
names, so a cursor naming an item that has since vanished lands on the next one
that still exists rather than silently restarting the mailbox from the top.
Nothing would be duplicated either way — the engine filters what it already has
— but a restart that reports itself as a resume is the kind of thing only ever
noticed as an unexplained hour of listing.

## The descriptor

One declaration that faces both ways: the registry keys on it, and the account
form renders from it.

```python
GMAIL_DESCRIPTOR = ProviderDescriptor(
    provider=MailProvider.GMAIL,
    label="Gmail",
    credential_fields=(
        CredentialField(name="client_id", label="OAuth client ID"),
        CredentialField(name="client_secret", label="OAuth client secret", secret=True),
    ),
    supports_incremental=True,
)
```

Because the form is generated from `credential_fields` with `rx.foreach`, **a
new provider costs no UI change.** One declaration means the form and the
registry cannot drift apart.

`supports_incremental` must be honest, and it now has a consumer that acts on
it: the interval scheduler queues an `incremental` job for every enabled account
whose provider claims a delta. Saying `True` before `list_messages` can walk one
promises the engine something the adapter cannot do — and a descriptor that
claims a delta while `watermark()` answers `None` is a mailbox nothing will ever
sync. Assert the pair against a fixture, the way
`test_google_source.py::TestTheDelta::test_the_descriptor_and_the_watermark_agree`
does — and note that
`tests/test_composition.py::test_every_provider_agrees_with_its_own_descriptor`
walks the whole registry and will hold your provider to it too.

## The credential

`mail_credentials.secret` is one encrypted, structureless column. Serialise your
own pydantic model into it:

```python
class GmailCredentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_id: str
    client_secret: str
    refresh_token: str
    access_token: str | None = None  # cache; losing it costs one round trip
    expires_at: datetime | None = None

    def to_secret(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_secret(cls, secret: str) -> GmailCredentials:
        try:
            return cls.model_validate_json(secret)
        except ValueError as error:
            raise MailAuthError(
                f"stored credentials are unreadable: {error}"
            ) from error
```

**No migration.** That is the whole reason the column is opaque.

Three things worth copying from the Gmail implementation:

- **Frozen.** A refresh produces a *new* object, so the caller cannot forget
  that a rotated refresh token has to go back into `mail_credentials` — it is
  holding the only copy.
- **A parse failure becomes `MailAuthError`**, not a `ValidationError` nobody
  upstream knows what to do with.
- **Never overwrite a refresh token with nothing.** Providers reissue them only
  occasionally, and blanking one locks the account out:
  `refresh_token=token.refresh_token or self.refresh_token`.

## Mapping errors

The status code decides, not the error string — a rate limit and a 5xx are the
same instruction, and everything else the endpoint says no to is a credential
the user has to grant again:

```python
def _refusal(response: httpx.Response) -> MailError:
    if response.status_code == 429 or response.status_code >= 500:
        return MailTransientError(..., retry_after=_retry_after(response))
    return MailAuthError(...)
```

`retry_after` is a floor the engine may exceed and never undercut. An HTTP-date
in that header is legal and ignored on purpose — the engine has its own backoff.

Three rules the Gmail client learned the hard way, and every adapter needs:

- **No `httpx` exception leaves the client module.** Not one. An adapter that
  lets one through has not decided whether the engine should retry, and the
  engine has no way to decide for it.
- **Do not retry inside the adapter.** The engine already backs off with jitter
  and honours `Retry-After`; a second loop underneath it multiplies every wait
  by a number invisible from the outside. The one exception is a **401**, which
  usually means the access token aged out mid-run — refresh **once** and repeat
  the call. A second 401 is a credential the user has to grant again, not a
  clock.
- **Read the body when the status code lies.** Gmail spends its 250 units/user/s
  quota as a **403** at least as often as a 429, so a 403 has to be inspected
  (`ratelimitexceeded`, `userratelimitexceeded`, `quotaexceeded`) to tell a
  quota refusal from a real permission failure. Getting that wrong turns a
  wait-and-retry into a re-consent prompt.

## Registering it

One line, in the one file allowed to name an implementation:

```python
# app/composition.py
def provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(FAKE_DESCRIPTOR, FakeMailSource.create)
    registry.register(GMAIL_DESCRIPTOR, GmailSource.create)  # ← this
    return registry
```

Registration order matters only for the UI: the account form lists providers in
the order they were registered, so the first one is what a new user is offered.

Registering twice **replaces** rather than raises — a composition root that runs
again after a reload has to be able to say the same thing twice.

The factory is a plain callable:

```python
type MailSourceFactory = Callable[[Any, str], MailSourcePort]
```

The first argument is the `MailAccountEntity`, kept untyped so the domain does
not import the persistence layer for a signature. The second is the decrypted
secret.

### Giving the factory its configuration

The factory signature has no room for a `Config`, and only the composition root
may build one. `GmailSource` solves that by closing over it:

```python
@classmethod
def using(cls, config: GmailConfig) -> MailSourceFactory:
    def build(account: Any, secret: str) -> MailSourcePort:
        return cls(GmailCredentials.from_secret(secret), config)

    return build
```

So the composition root registers `GmailSource.using(google_config())` when it
has built a config, and `GmailSource.create` — which reads one from the
environment — when it has not. Copy the pair; it is the shape that keeps
configuration out of the adapter's own constructor call sites.

## The component skeleton

```text
components/mailarc-imap/
├── pyproject.toml
├── README.md
├── src/mailarc_imap/
│   ├── __init__.py
│   └── source/
│       ├── __init__.py       the public surface
│       ├── model.py          the provider's own shapes + the descriptor
│       ├── config.py         endpoints and timeouts — never an account
│       ├── credentials.py    what fills mail_credentials.secret
│       └── client.py         the protocol conversation
└── tests/
```

Then add it to the root `pyproject.toml` — `[tool.uv.sources]`,
`[tool.pytest.ini_options] testpaths`, `[tool.coverage.run] source`, and
`[tool.ruff.lint.isort] known-local-folder`.

`uv sync` installs the root's dependency closure and nothing else, so a member
nothing depends on yet is not importable. Put it in the `dev` group until the
application actually wires it in — that is where `mailarc-analytics` sat until
the `derive` job gave the application a reason to import it.

A component an installation should be able to *decline* goes in an extra
instead: `mailarc-mcp` sits behind `[project.optional-dependencies] mcp` so the
desktop bundle does not carry `fastmcp`. The price is one rule — nothing under
`app/` may import it at module level except its own entry point — and one test
that reads every module in `app/` and holds it.

### Config holds no account

Five settings for Gmail, and not one names a mailbox. Which account, whose
credentials and how far the last run got are **state in SQLite**. A second Gmail
account must not need a second config.

The two URLs are settings rather than constants for exactly one reason: a test
has to be able to point them at a local HTTP server.

## Testing it

**No test may talk to the real provider.** Use `pytest-httpserver`, and cover at
least:

- The happy path for each of the six methods.
- A **429 with `Retry-After`** → `MailTransientError` carrying the value.
- A **5xx** → `MailTransientError`.
- An **expired or revoked token** → `MailAuthError`.
- A **404 on a message** → `MailPermanentError`.
- A **cursor the provider refuses as too old** → `MailCursorExpired`, and *only*
  from the delta call. Gmail answers 404 to both, so the same status has to mean
  two different things depending on what was asked for.
- Paging: at least two pages, and the last one returning `next_cursor=None`.
- Resuming from a cursor.

`FakeMailSource` is the reference implementation — 150 lines, all six methods,
the full error taxonomy — and it is worth reading before writing a new one.

## Fetch the raw bytes, always

Gmail is fetched with `format=raw`. Pull the RFC 5322 bytes and let
`mailarc_core.mail.parsing` do the rest.

Two reasons: one parser serves every provider, and the bytes are what get hashed
for `eml_sha256` and what go to the blob store — so a parser fix can be replayed
over the whole archive without asking the provider again.

Never map a provider's parsed JSON onto `ParsedMessage` yourself. Labels, thread
ids and delta tokens come alongside as metadata; the message itself is bytes.
