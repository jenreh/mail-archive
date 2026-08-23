# mailarc-m365

Microsoft 365 mail over the Graph API, behind the one seam this design allows
to be abstract.

The package implements `mailarc-core`'s mail source port and nothing else: the
consent step, `messages/delta` paging, and the `$value` fetch that hands the
domain the same RFC 5322 bytes every other provider hands it. Graph shapes stop
at that boundary — no `@odata.nextLink`, no `deltaLink` and no mail-folder id
reaches the archive unmapped.

The package is `source/`, named after the capability rather than the vendor, so
that `mailarc_google.source` and `mailarc_imap.source` look identical.

## Two ways in, one provider

The plan left the choice between a delegated (per-user) and an app-only
(per-tenant) grant open, and observed that the opaque credential blob carries
both. It does, so both are here.

| | Delegated | App-only |
| --- | --- | --- |
| Who signs in | the mailbox's owner, in a browser | nobody |
| MSAL | `PublicClientApplication`, auth code + PKCE | `ConfidentialClientApplication`, client credentials |
| Scope | `Mail.Read` + `User.Read` | `.default` |
| Entra needs | a public-client `http://localhost` redirect | a client secret, the `Mail.Read` **application** permission, admin consent |
| Tenant | `common` is fine | must be the tenant's own id or domain |
| Graph paths | `/me/…` | `/users/{address}/…` |
| Consent step | a browser | none — it happened once, in the tenant |

A literal `mode` field discriminates the stored blob, so `from_secret` cannot
read one as the other. The account form asks for it as a word, because a
`CredentialField` renders a text box and nothing else; **an empty box means
delegated**, which is what a desktop archive and a personal mailbox both want.

The Entra application belongs to the installation, not to a mailbox: `client_id`
and, for app-only, `client_secret` are configuration (`app_m365_*`), so nobody
adding a mailbox is asked to create an app registration.

## What Graph is the first to prove

Graph is the only provider whose cursor is a **whole URL**. `SyncCursor.token`
is opaque to the engine, which is what makes that legal: Gmail puts a
`historyId` in it, IMAP a `UIDVALIDITY/UIDNEXT` pair, and Graph the entire
`nextLink` or `deltaLink` it was handed. Nothing above the port looks inside,
and the day Microsoft changes the shape of that link no other component
notices.

Being a URL is also the one thing that has to be policed: the cursor comes back
out of an encrypted column and the next thing that happens to it is a request
carrying a mailbox's access token. `mapping.read_cursor_url` refuses any link
that does not share the configured Graph origin, as `MailCursorExpired` — the
error whose remedy, walk the mailbox instead, is exactly right for a cursor
that cannot be used. `GraphClient._url` refuses the same thing again as a
backstop.

What happens next depends on which walk asked. From the **delta** the error is
let out, because that is precisely what makes `ImportEngine.run` fall back to a
full walk. From a **full walk** it is caught in `M365Source._resumable` and the
walk restarts from the top instead, because the engine re-raises an expired
cursor for a run that already is a full walk *and never clears the checkpoint
row* — an unusable page token would otherwise be read, refused and re-raised on
every run for ever. Restarting is what `MailCursorExpired` asks for anyway, and
the first page overwrites the bad row.

## Three things the API does not do, and what this adapter does instead

**There is no mailbox-wide message delta.** Every documented URL for
`messages/delta` carries a `mailFolders/{id}` segment, so a delta is always a
delta of *one folder*. `M365Config.delta_folder` names it and defaults to
`allitems`, the search folder that spans the mailbox. Some mailboxes — older
hybrid and single-tenant configurations — answer `Default folder AllItems not
found`; that is a 404 and reaches the operator as one, naming the path, rather
than being swallowed into a delta that silently covers nothing. Such a
deployment sets `delta_folder` to `inbox` or to a folder id.

**There is no cheap watermark.** A `deltaLink` is only ever handed out at the
*end* of a delta chain, and `$deltatoken=latest` exists for SharePoint and
OneDrive, not for Outlook. So `watermark()` drains the chain — `changeType=created`,
a large page size, and the same `$select` the delta itself will use, because
Graph bakes the query into the link it returns. The drain is bounded by
`watermark_max_pages`; reaching that bound is not a failure, because the
`nextLink` it stopped at is itself a legal incremental cursor and the next run
carries the enumeration further.

**There is no mailbox-wide folder list either.** `GET /me/mailFolders` returns
the folders directly under the root and stops — Microsoft's own words: *"This
API does not return all mail folders in a mailbox; to get all folders, each
child folder must be traversed separately."* An Archive filed inside the Inbox
is the ordinary shape of a tidy mailbox, so `list_labels` follows every folder
that reports a `childFolderCount` into its `childFolders`. `childFolderCount`
rides in the default projection this adapter already asks for, so a flat
mailbox still costs exactly one request; `MAX_FOLDER_PAGES` bounds the whole
traversal — pages and descents alike — at fifty. A tenant that omits the
property reports no children and degrades to the flat listing rather than to an
error.

## Layout

| Module | What it owns |
| --- | --- |
| `model.py` | Graph's endpoints, the two scope sets, the `@odata` names, MSAL's token dict — and `M365_DESCRIPTOR`, the one declaration that faces the domain |
| `config.py` | `M365Config`: where Graph is, which Entra application speaks for us, which folder the delta runs over, the timeouts and the page sizes. Never an account |
| `credentials.py` | The two shapes `mail_credentials.secret` can hold, the discriminated union that tells them apart, and the MSAL refresh in its blocking and threaded forms |
| `loopback.py` | The throwaway HTTP server that catches the redirect — tolerant of browser preconnects, bounded by a deadline, gone afterwards |
| `oauth.py` | The delegated consent on top of it, the deliberate absence of one for app-only, and `consent_runner(config)` — the one runner the composition root registers |
| `client.py` | Graph over `httpx`: the only reader of a status code, the place the taxonomy is decided, and the only thing that will not send a bearer token to a URL that is not Graph |
| `mapping.py` | Graph JSON in, domain value objects out. No dictionary crosses out of it |
| `source.py` | `M365Source` — the six port methods, made of the rest |

## Registering it

`app/composition.py` is the only module allowed to name a provider:

```python
registry.register(
    M365Source.DESCRIPTOR,
    M365Source.using(m365_config()),
    consent=consent_runner(m365_config()),
)
```

All three names are re-exported at the top of `mailarc_m365`, so the
composition root never reaches into a submodule.

## Dependency cost

`msal` 1.37.0 (MIT) is the only distribution this component adds. Its
requirements — `requests`, `pyjwt[crypto]`, `cryptography`, `urllib3` — were
already in `uv.lock` before it, except `pyjwt`, which the web deployment
already carried through `fastmcp`. Measured with the command `task tauri:deps`
uses: the desktop resolution went from 82 to 85 distributions (adding
`imapclient`, `msal` and `pyjwt`) and the web one from 125 to 127. Graph itself
is spoken to with `httpx`, never through MSAL.

## Rules

- Depends on `mailarc-core` alone, plus `msal` and `httpx`.
- **No `mailarc-sync`.** The engine drives this adapter through the port; it
  never learns its name. `app/composition.py` does the registering.
- **No `mailarc-google`.** The loopback server and the OAuth module over there
  are not shared code; a component may not import a sibling. This component's
  consent flow is written here, and `loopback.py` plus `retry_after_seconds`
  are duplicated as the price of the rule.
- **No Reflex, no `appkit` UI package.** The account form is generated from
  `credential_fields`, so a new provider costs no UI change.
- **No `runic.rag`.**
- **Only the four errors.** Every method raises from `mailarc_core.mail.errors`
  and nothing else — an adapter that lets an `httpx` or an `msal` error through
  has not decided whether the engine should retry, and a `deltaLink` Graph
  refuses as too old is `MailCursorExpired`, never `MailPermanentError`.
- **No test talks to Microsoft.** `M365Config` carries the API root and the
  authority host as settings so `pytest-httpserver` can serve them, and MSAL is
  replaced at `credentials._application` and `oauth._public_application` — the
  only two doors to `login.microsoftonline.com`.

`components/mailarc-core/tests/test_isolation.py` enforces the framework and
`runic.rag` bans once its package tuples name this component;
`tests/test_m365_package.py` holds the same promise from a subprocess in the
meantime.
