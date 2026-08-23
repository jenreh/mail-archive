# Adding a mail provider

A provider is a component that implements one `Protocol` and declares what it
needs. Nothing above it learns its name.

The success criterion is stated in the plan and worth keeping: adding a provider
must produce **no diff** in `mailarc-sync`, `mailarc-analytics` or
`mailarc-core`. If it does, the port was cut wrong.

**This page has been through that test.** It was written before `mailarc-imap`
and `mailarc-m365` existed, speculating about both by name; both are now built
and registered, and the criterion held — neither cost a line in the three sealed
components. What it did cost is written down here, and where the guess was wrong
is called out as it comes up. [What the first two new providers actually
cost](#what-the-first-two-new-providers-actually-cost) collects the surprises in
one place.

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
| `watermark` | Where a delta would start **if it started now** — Gmail's `getProfile().historyId`, IMAP's `UIDVALIDITY`/`UIDNEXT` pair, Graph's first `deltaLink`. `None` from a source that has no delta at all |
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

**Find out what the mark costs before you design around it.** Gmail's is one
`getProfile` and IMAP's is the `EXAMINE` the run was going to issue anyway — so
the doc's "read it before the first listing" advice reads as free. It is not free
for Microsoft Graph: a `deltaLink` is only ever handed out at the *end* of a
delta chain, there is no `$deltatoken=latest` for Outlook resources, and
`M365Source.watermark()` therefore drains the chain metadata-only — roughly
`ceil(N/500)` requests per run. It is correct and it never loses mail; it is also
the most expensive line in that adapter. A provider whose watermark is a walk
should say so in its own docstring, because nothing above the port can see it.

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

Two things follow that the first draft of this page did not anticipate.

**A cursor that is a URL has to be fenced.** Graph's `nextLink` and `deltaLink`
are whole URLs and the adapter follows them *with a bearer token attached* — but
what comes back out of storage is whatever was written into an encrypted column,
and the engine hands it over untouched. So `mailarc_m365` refuses any cursor
whose origin is not `M365Config.api_base_url`'s, and refuses it as
`MailCursorExpired`, whose remedy — throw it away and walk the mailbox — is
exactly right. A provider whose cursor is an opaque token has no such problem; a
provider whose cursor is an address has this one.

**`MailCursorExpired` is only recoverable from the incremental listing.**
`ImportEngine.run` catches it, clears the checkpoint and restarts as a full walk
— unless the run *already is* a full walk, in which case it re-raises and
**nothing clears the checkpoint row**. An adapter that raises it from a full walk
therefore fails that account identically for ever. So recover in the adapter:
`ImapSource._resume_at` restarts at the first UID when a stored `UIDVALIDITY` no
longer matches and the walk is full, and `M365Source._resumable` logs a warning
and returns `None` so the walk starts from the top. Both still raise from the
delta, where the engine's fallback is what you want.

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
IMAP_DESCRIPTOR = ProviderDescriptor(
    provider=MailProvider.IMAP,
    label="IMAP (iCloud, Gmail app password, any mail host)",
    credential_fields=(
        CredentialField(name="host", label="IMAP server", placeholder=ICLOUD_IMAP_HOST),
        CredentialField(name="port", label="Port", required=False, placeholder="993"),
        CredentialField(name="username", label="Username"),
        CredentialField(name="password", label="App-specific password", secret=True),
        CredentialField(name="folder", label="Folder", required=False),
    ),
    supports_incremental=True,
)
```

Because the form is generated from `credential_fields` with `rx.foreach`, **a
new provider costs no UI change.** That was the promise, and both new providers
kept it: `mailarc-ui` has no diff either. One declaration means the form and the
registry cannot drift apart.

**Only put a field here if it belongs to the mailbox.** Gmail's descriptor
declares *nothing* — its OAuth client belongs to the installation and lives in
`app.google`, so a person adding a Gmail account types an address and presses
Connect. IMAP's declares five, because an IMAP host belongs to the account: an
archive holding an iCloud mailbox and a Gmail-app-password mailbox holds two
hosts at once, and §4.2's rule is that a second account must not need a second
config. Microsoft 365 splits the same way — the Entra application is
configuration, the tenant, the mailbox and the sign-in mode are fields.

Two constraints on the fields themselves, learned rather than designed:

- **A field may not be called `email_address`.** That name is taken: the account
  row's own address arrives in the same mapping under `CONSENT_ADDRESS_KEY`.
- **The form renders text boxes and nothing else** — no checkbox, no select. So
  Microsoft 365's two sign-in modes are a word somebody types, an empty box means
  the common one, and a word that is neither is refused rather than defaulted.

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

### Who writes that column decides what `from_secret` must accept

This is the single biggest thing the first draft of this page left out, and IMAP
is the provider that found it.

If your provider has a **consent runner**, the runner writes the secret, so
`from_secret` only ever reads back what `to_secret` wrote — Gmail's case, and
the reason the round trip above looks symmetrical.

If it has **none**, the account form writes it, and the form writes
`json.dumps({field.name: typed_value})` over your own `credential_fields`. Every
value in that mapping is a **string**: a port number arrives as `"993"`, a
boolean would arrive as `"true"`, and an omitted optional field arrives as `""`.
`ImapCredentials.from_secret` therefore has to accept two shapes — its own, and
the form's — and `tests/test_composition.py` hands the form's shape to the
registered factory precisely because that is the one the running application
produces.

Two smaller consequences of the same path:

- The form calls `.strip()` on every value before serialising, so a credential
  with a leading or trailing space cannot be entered through it at all. Gmail app
  passwords are displayed with *internal* spaces, which survive.
- After `verify()`, the account page compares the `AccountIdentity.address` you
  answered with against the account row's own address and **deletes every stored
  credential** when they differ. That check was written for OAuth, where a
  consent screen lets somebody sign in as a different account. A provider with no
  consent screen has no independent way to discover an address — IMAP can only
  report the authenticated username — so if your provider's usernames are not
  addresses, say so on the field's label, which is the only place a provider can
  say it.

### Rotation costs nothing if you expose it

`app/worker.py` reads `source.credentials.to_secret()` at the end of every run
and writes it back when it changed. Expose `credentials` as a property returning
a frozen model with `to_secret()` and refresh persistence works for free — and
answer it even when nothing rotates, the way `ImapSource` does, rather than
becoming a special case in that method.

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
  engine has no way to decide for it. `except (TimeoutException, RequestError)`
  is **not** that promise: `httpx.InvalidURL` descends from `Exception` and
  `httpx.StreamError` from `RuntimeError`, and building a URL out of a provider
  id that carries a lone surrogate raises a bare `UnicodeEncodeError` — all three
  were reaching out of `mailarc-m365` before its review, and all three are now
  `MailPermanentError`, because an address this client cannot form will not form
  on the second try.
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

### Not every provider has a status code

The section above is written for HTTP because Gmail was the first adapter. IMAP
has no status codes, and the same three rules land differently:

- **The library's own wrapping can lie about the taxonomy.** `imapclient.login`
  wraps *every* `IMAPClientError` into a `LoginError`, and `IMAPClientAbortError`
  is one — so a socket that died mid-`LOGIN` arrives looking exactly like a wrong
  password. Classifying that as `MailAuthError` is not cosmetic: it is terminal
  for the job, the account goes to `auth_error`, its schedule stops, and the UI
  asks a human to re-enter a password that was never wrong. `mailarc-imap` tells
  the two apart by `__context__` rather than by reading the English sentence.
- **A reply your client cannot parse is transient, not permanent.** `imapclient`
  decodes folder names from modified UTF-7, and a name that is not valid modified
  UTF-7 raises `UnicodeDecodeError` — a `ValueError`, so neither an
  `IMAPClientError` nor an `OSError` handler catches it. It is more often a proxy
  or a half-read socket than a broken mailbox, and the permanent branch would file
  it as a skipped message that never existed.
- **Two status codes can need two different meanings at once.** Graph's 404 means
  "skip this message" on a fetch and "this mailbox is not one this grant can open"
  in `verify()`, so `GraphClient.get_json` takes `not_found` and `gone` as
  parameters and lets the caller name the error. A single mapping table cannot
  express that.

### Concurrency is the adapter's problem too

The engine runs several `fetch_raw` streams at once behind a semaphore. Over HTTP
that is free — `httpx` pools connections. Over IMAP there is **one socket with
one command in flight**, so `mailarc-imap` serialises every command behind an
`asyncio.Lock` and its fake server records a marker when a second command arrives
before the first was answered. If your protocol has that shape, assert the
invariant directly; a test that only checks the *outcome* fails as a cascade of
read timeouts, which is a slow and easily misread signal.

## Registering it

One line, in the one file allowed to name an implementation:

```python
# app/composition.py
def provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(FAKE_DESCRIPTOR, FakeMailSource.create)
    registry.register(
        GmailSource.DESCRIPTOR,
        GmailSource.using(google_config()),
        consent=gmail_consent,
    )
    registry.register(ImapSource.DESCRIPTOR, ImapSource.using(imap_config()))
    registry.register(
        M365Source.DESCRIPTOR,
        M365Source.using(m365_config()),
        consent=consent_runner(m365_config()),
    )
    return registry
```

Registration order matters only for the UI: the account form lists providers in
the order they were registered, so the first one is what a new user is offered.
**Append.** Moving an existing line changes what the account page offers first
and fails nothing else.

Registering twice **replaces** rather than raises — a composition root that runs
again after a reload has to be able to say the same thing twice.

### The consent argument is a fact about the provider

Three registrations, three different answers, and none of them a convention:

- **Gmail** passes one. A browser round trip is not something a mailbox can be
  asked for through the port, and `mailarc-ui` may not import a provider to reach
  one — so the browser half is registered here, in the one module allowed to name
  Gmail, and the account page only learns that this provider has a second step.
- **IMAP** passes none. An app password is complete the moment it is typed. This
  is the path that proves a provider without a runner works end to end, and it is
  the path that makes the account form write the secret (see above).
- **Microsoft 365** passes one runner for **two** modes, and the asymmetry lives
  *inside* it: a delegated sign-in opens a browser, an app-only grant was
  consented once by an administrator in the tenant and only has its tenant,
  mailbox and client secret checked before a credential is handed back. That
  branch belongs in the component, which owns the credential model that tells the
  two apart. Registering no runner would tell the account page that Microsoft 365
  has no second step, which is false for the mode most people use; registering two
  descriptors would put two Microsoft entries in the provider list and make "which
  of these am I?" the first question a user has to answer.

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
configuration out of the adapter's own constructor call sites. **Every provider
in the running application is registered with `using`**; `create` exists for a
CLI, a script and the adapter's own tests.

### And add a field to `AppConfig`, or the config never arrives

`app/configuration.py` composes one settings object per component, and
`model_config['extra']` is `ignore`. A component whose config is *not* a field on
`AppConfig` therefore has its whole `app.<name>` block in
`configuration/config.yaml` dropped without a word, and `_registered(YourConfig)`
answers with whatever the environment alone produced. That has already happened
once in this repository — the `semantic` field's docstring records it — so both
new providers got their field in the same commit as their registration.

One more mechanic worth knowing before you write the YAML: a `secret:` reference
is resolved **eagerly**, while the configuration is validated, and a key the
secret provider does not hold fails the *whole* startup. That is why `app.m365`
ships commented out with its two references spelled in place and the matching
placeholders in `.env.default`: an installation with an Entra registration
uncomments two lines, and one without is not broken by a key it never had.

## The component skeleton

This is what `mailarc-imap` and `mailarc-m365` actually look like — the same
layout, layered so nothing points back up:

```text
components/mailarc-m365/
├── pyproject.toml
├── README.md
├── src/mailarc_m365/
│   ├── __init__.py           the three names app/composition.py needs
│   └── source/
│       ├── __init__.py       the public surface
│       ├── model.py          the provider's own shapes + the descriptor
│       ├── config.py         endpoints and timeouts — never an account
│       ├── credentials.py    what fills mail_credentials.secret
│       ├── client.py         the protocol conversation, and the taxonomy
│       ├── mapping.py        provider payloads → domain value objects
│       └── source.py         the six port methods, made of the rest
└── tests/
```

`mapping.py` and `source.py` were missing from the first draft of this skeleton
and both earn their place: `mapping.py` is where the cursor and the message id
are minted *and* read, and keeping that in one module is what makes "no
dictionary and no integer leaves this component" checkable. A provider with an
OAuth flow adds `oauth.py` and `loopback.py` beside them; `mailarc-imap` has
neither, which is the whole point of it.

Keep the top-level `__init__.py` exporting exactly what the composition root
needs — the descriptor, the source class, and the consent runner if there is one
— so `app/composition.py` never reaches into a submodule.

Then add it to the root `pyproject.toml` — `[tool.uv.sources]`,
`[tool.pytest.ini_options] testpaths`, `[tool.coverage.run] source`, and
`[tool.ruff.lint.isort] known-local-folder`.

`uv sync` installs the root's dependency closure and nothing else, so a member
nothing depends on yet is not importable. `mailarc-analytics` sat in the `dev`
group until the `derive` job gave the application a reason to import it — but a
**provider** is different: `app/composition.py` registers it the moment it
exists, because a mailbox kind nothing can build is not a feature an installation
may decline. Both new providers were born in the root's `dependencies`.

A component an installation *should* be able to decline goes in an extra
instead; `mailarc-mcp` is the only one.

### One thing you cannot do from inside your component

`components/mailarc-core/tests/test_isolation.py` enforces the import rules from
a subprocess, and it **does not discover components**: the package names come
from three hand-written tuples in that file. Adding a provider therefore needs
`"mailarc_yourprovider"` in `HEADLESS` and in `MUST_NOT_SEE_SYNC`, and a line in
`test_the_engine_does_not_import_a_provider` — and *not* in `OPTIONAL`, which
exists for the `mcp` extra alone. Neither `mailarc_imap` nor `mailarc_m365` is in
those tuples yet: the phase that built them sealed `mailarc-core`, so both
components carry an equivalent source-level probe of their own and say so in
their README rather than claiming enforcement they do not have.

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

IMAP had to work harder to keep that true, and the answer is worth copying. Its
host, port, username, password and folder are all *credential fields*, so
`ImapConfig` holds two timeouts, a page size and a certificate authority — and an
archive can hold an iCloud mailbox and a Gmail-app-password mailbox at once. The
well-known hosts a form prefills are module constants rather than settings for a
blunt reason: the thing that prefills the form is the descriptor, which is built
at import time with no configuration object in scope, and a setting nothing can
read is not a setting.

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
  two different things depending on what was asked for. And check the other half
  of it: a full-walk cursor your adapter cannot use must **recover**, not raise,
  because the engine re-raises that one and never clears the checkpoint.
- Paging: at least two pages, and the last one returning `next_cursor=None`.
- Resuming from a cursor.
- **Nothing outside the taxonomy escapes** — asserted by driving the adapter at a
  server that misbehaves, not by reading the `except` clauses. `mailarc-m365`'s
  promise that no `httpx` exception leaves the client module was false until a
  probe was written for it.
- **The descriptor and `watermark()` agree**, locally as well as in the
  composition test.

If your provider does not speak HTTP, `pytest-httpserver` cannot help and a mock
of the client library would be a test of the mock. `mailarc-imap` runs a real
IMAP4rev1 server on a loopback socket over TLS — with a throwaway certificate the
adapter genuinely verifies, because the adapter offers no way to switch
verification off and a suite that skipped it would never exercise that path. Its
failure paths are knobs on that server (refuse the login, drop the socket
mid-command, answer `EXAMINE` without a `UIDVALIDITY`, put a name on the wire
that is not valid modified UTF-7), which is how the taxonomy gets tested without
unplugging a network cable.

Finally, `tests/test_composition.py` holds the whole registry to its descriptors
— it builds every registered provider from a fixture secret and calls
`watermark()`. Adding a provider means adding a fixture credential and a local
stand-in there; the test asserts that the set of secrets matches the set of
registered providers, so a provider added without one fails loudly rather than
being skipped.

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

## What the first two new providers actually cost

The plan's criterion held: `mailarc-imap` and `mailarc-m365` produced **no diff**
in `mailarc-sync`, `mailarc-analytics` or `mailarc-core`, and none in
`mailarc-ui` either — the account form rendered IMAP's five fields and Microsoft
365's three without a line changing. What each provider cost outside its own
component was a field on `AppConfig`, an accessor and a registration in
`app/composition.py`, a block in `configuration/config.yaml`, and a fixture in
`tests/test_composition.py`.

Where this page had guessed, and what turned out to be true:

| The guess | What happened |
| --- | --- |
| "IMAP's `UIDNEXT`" | Right, but the cursor is the `UIDVALIDITY`/`UIDNEXT` **pair**, and the `UIDVALIDITY` half is the whole point: it is what makes a renumbered mailbox detectable rather than silently wrong |
| "MS Graph's `deltaLink`" | Right, and the first cursor that is a whole **URL** — which needed an origin fence, because following it sends a bearer token |
| A `components/mailarc-imap/` skeleton of five modules | Seven. `mapping.py` and `source.py` were missing and both earn their place |
| Nothing about consent | The three registrations need three different answers, and one runner covers two modes |
| Nothing about who writes the secret | A provider with no consent runner has its secret written by the **account form**, as JSON over its own fields with every value a string |
| Nothing about `AppConfig` | A component config that is not a field on it is silently dropped from `config.yaml` |
| "Use `pytest-httpserver`" | Useless for IMAP; that component runs a real IMAP4rev1 server over TLS instead |
| "no `httpx` exception leaves the client module" | True only after three more exception types were caught — the rule was right and the implementation of it was not |

Two things that were **not** anticipated at all and are worth knowing before the
third provider:

**An IMAP cursor is a mark per folder, not one pair.** This was got wrong first
and is worth stating as the corrected version. An IMAP UID identifies a message
inside one folder and one `UIDVALIDITY` and nothing else, so the first shape of
this adapter made the folder a credential field and synced one folder per
account. That is a defensible reading of the protocol and the wrong reading of
the product — a person asking to archive their mailbox means the mailbox, not
one drawer of it.

Walking the whole account instead costs two things and neither is in the engine.
The `provider_message_id` has to carry the folder as well as the generation, or
two folders sharing a `UIDVALIDITY` collide in the archived-messages ledger and
most of the mailbox is skipped as already archived — silent, permanent, reported
as success. And `SyncCursor.token` has to hold a mark per folder plus the folder
in progress, which no pair of decimals can carry; `mailarc-imap` puts JSON in
there and versions it. Both fit inside the port untouched, which is the point:
the token being opaque is what made a wrong guess about IMAP recoverable without
a core change.

The duplication the folder field was avoiding is real but is Gmail's alone:
`[Gmail]/All Mail` plus per-label folders means one download per label. The graph
is unharmed because `MessageArchiver` resolves by `canonical_id`. iCloud and
ordinary hosts keep a message in one folder.

**Graph has no mailbox-wide message delta.** Every documented `messages/delta`
URL carries a `mailFolders/{id}` segment, so `M365Config.delta_folder` exists and
defaults to the `allitems` search folder. Mail that never lands in that folder is
never in a delta and is only picked up by a full import.
