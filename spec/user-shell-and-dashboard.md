# Spec: Anwendungs-Shell und Willkommens-Dashboard

> **Status:** freigegeben, nicht begonnen.
> **Umsetzbar ohne Vorwissen:** Diese Datei enthält alle Repo-Fakten,
> Bibliotheks-APIs und Entscheidungen, die zur Umsetzung nötig sind. Alles
> hier wurde am Repo verifiziert, nicht angenommen. Wo etwas noch zu
> verifizieren ist, steht es ausdrücklich als Verifikationsschritt.

---

## 1. Ziel und Ausgangslage

`mail-archive` hat heute fünf Admin-Seiten und **keine Oberfläche für den
Benutzer**. `/` ist `app/pages/home.py` — eine „Hello World"-Seite mit dem
FalkorDB-Status. Die gesamte Navigation sind 20 Zeilen unangetastetes
Copier-Gerüst in `app/components/navbar.py`, das immer noch die Zeichenkette
`"Project Kit Navbar"` rendert: sechs nackte `rx.link`, keine Icons, kein
aktiver Zustand, keine Rollenprüfung.

Diese Arbeit baut:

1. eine **Shell** — Sidebar-Navigation aus Daten, ein `mn.app_shell`-Layout,
   durch das *jede* Seite geht;
2. ein **Willkommens-Dashboard** auf `/`, öffentlich erreichbar, mit denselben
   Kennzahlen, die die Admin-Seiten schon kennen;
3. den **Umzug der kompletten Oberfläche nach `mailarc-ui`** — Seiten
   eingeschlossen.

### 1.1 Fachliche Anforderungen

| # | Anforderung | Abschnitt |
| --- | --- | --- |
| A1 | Eine Navigation wie im Entwurf: Sidebar mit Icons, Gruppen, Benutzer-Fuß | §5.2 |
| A2 | Ein Dashboard wie im Entwurf: KPI-Banner, Statistik-Karten, zwei Verlaufs-Charts, Dienste-Liste | §7 |
| A3 | Eine **Seitenvorlage**, die Folgeseiten ohne Nachdenken benutzen | §5.3 |
| A4 | **Benutzerseiten brauchen keine Anmeldung** | §5.4 |

### 1.2 Getroffene Entscheidungen

| Frage | Entscheidung |
| --- | --- |
| Route des Dashboards | **`/`**. Die FalkorDB-Statusseite zieht nach `/admin/status`. |
| Umfang der Migration | **Alle** Seiten gehen auf die neue Shell; `app/components/navbar.py` wird gelöscht. |
| Wo die Oberfläche wohnt | **Komplett in `mailarc-ui`, Seiten eingeschlossen.** `app/` behält nur Composition Root, Konfiguration und Einstiegspunkte. |
| Speicherverlauf | **Echt.** Ein neues Graph-Statement summiert `Message.size_bytes` nach dem Datum von `ArchivedFrom.archived_at`. |
| Kopfleiste | **Keine.** Nur Sidebar und Seiteninhalt — kein Suchfeld, kein Filter-Knopf. |
| Karten-Primitive | Das `kit/`-Refactoring ist **im Umfang**, nicht vertagt (§9). |

### 1.3 Eine Annahme, die ausgesprochen gehört

Beide Charts **und** die Kachel „Zuletzt archiviert" kommen aus **einem**
neuen Graph-Statement (`Tag → Nachrichten, Bytes`) — nicht zusätzlich aus einem
SQLite-Read über `mail_archived_messages.archived_at`. Zwei Quellen für
dieselbe Zahl sind ein Fehler, der auf seinen Termin wartet.

Der Preis: steht FalkorDB, zeigen diese beiden Kacheln `—`. Die Dienste-Karte
direkt darunter sagt dann, warum.

---

## 2. Repo-Fakten

### 2.1 Was heute existiert

```
app/
├── app.py                 116 Z.  rx.App, Lifespans, publish_*, Seiten-Importe
├── styles.py               68 Z.  base_style / base_stylesheets (Copier-Default)
├── roles.py                13 Z.  ALL_ROLES — importiert NUR von app/pages/users.py
├── composition.py         784 Z.  Composition Root
├── components/navbar.py    20 Z.  das Gerüst mit "Project Kit Navbar"
├── pages/{home,mail_accounts,mail_review,mail_insights,mail_embedder,users}.py
└── states/graph_status_state.py 173 Z.

components/mailarc-ui/src/mailarc_ui/
├── accounts/  imports/  review/  insights/  embedder/
```

### 2.2 Der Haken, der schon vorbereitet ist

`appkit_user.authentication.templates` (installiert: appkit 1.13.6) enthält
**vier** Decorators. Drei sind in Benutzung, der vierte ist der richtige:

| Decorator | Layout | Auth | benutzt |
| --- | --- | --- | --- |
| `default_layout` | `rx.center` + Theme | keine | Login, Passwort-Reset |
| `navbar_layout` | `_render_layout` (Flex + Navbar) | **keine** | `app/pages/home.py` |
| `authenticated` | `_render_layout` + `session_monitor` | `check_auth` | die fünf Admin-Seiten |
| **`authenticated_page`** | **beliebiges `template`-Callable**, Default `mn.app_shell` | `check_auth` | **nirgends** |

`navbar_layout` und `authenticated` gehen beide durch `_render_layout(content,
navbar, with_header)` — ein `mn.flex` mit der Navbar als linker Spalte. Dieses
Layout ist **in der Bibliothek festverdrahtet**; es lässt sich nicht umstylen,
ohne appkit zu patchen. Genau dafür wurde `authenticated_page(template=…)`
eingeführt.

### 2.3 Das Schwesterprojekt als Vorlage

`/Users/jens/Workspace/projekte/voyager` (appkit 1.12.2, Reflex 0.9.5) benutzt
dieses Muster durchgehend:

- `voyager_commons/templates.py` — drei Layout-Callables (App, Vollbild-Karte,
  öffentlich), je ~14 Zeilen `mn.app_shell`.
- `voyager_commons/components/header.py` — Navigation **aus Daten**: eine Liste
  von Einträgen, Rollenprüfung deklarativ am Eintrag, aktiver Zustand aus
  `rx.State.router.page.path`.
- **Die Seiten liegen in den Komponenten** (`voyager_trips/pages/trips.py`,
  `voyager_homepage/pages/home.py`). `app/app.py` importiert sie nur, damit der
  Decorator die Route registriert.
- Öffentliche Seiten benutzen `@rx.page` und rufen das Layout-Callable selbst
  auf (`voyager_homepage/pages/home.py`).

Verifiziert: `appkit_user/authentication/templates.py` unterscheidet sich
zwischen beiden venvs um **eine** Zeile, in einer hier nicht benutzten
Funktion. Die API ist identisch.

### 2.4 Was in `mailarc-ui` schon liegt und wiederverwendet wird

| Fund | Ort | Rolle |
| --- | --- | --- |
| `_stat(label, value, color)` | `insights/components.py:44` | genau die Statistik-Kachel des Entwurfs — privat, nicht exportiert |
| `_card_heading(icon, title)` | `insights/components.py:735` | Karten-Überschrift |
| `mn.card(shadow="sm", padding="lg", radius="md", with_border=True, w="100%")` | **neunmal wortgleich** | die Karten-Rezeptur |
| Registry-Lookup in der Methode | `insights/state.py:109` | `analytics_reader()` — Vorlage für jeden neuen Lookup |
| Panelweise Fehlerisolation | `insights/state.py:117-150` | eigenes `loading_*`/`*_error` je Panel |
| `asyncio.to_thread` um blockierende Reads | `review/state.py:293` | jeder runic-Treiber blockiert |

### 2.5 Was `appkit_mantine` 1.13.6 mitbringt und nirgends benutzt wird

Verifiziert gegen `__init__.py`: `app_shell` (mit `.navbar/.header/.main/
.section/.aside/.footer`), `nav_link`, `burger`, `divider`, `avatar`,
`segmented_control`, `progress`, `ring_progress`, `area_chart`, `line_chart`,
`bar_chart`, `sparkline`, `indicator`, `timeline`.

`AppShellRoot` nimmt `navbar: Var[dict]`, `header`, `aside`, `footer`,
`padding`, `layout`, `mode`. `NavLink` nimmt `label`, `description`, `icon`,
`left_section`, `right_section`, `active`, `variant`, `color`, `opened`.
`CategoricalChartBase` (Area/Bar/Line) nimmt `data`, `data_key`, `series`,
`with_legend`, `with_tooltip`, `grid_axis`, `x_axis_props`, `unit`, `h`, `w`.

### 2.6 Was die Seiten heute aus `app` importieren

Per grep verifiziert — **nur `app_navbar`**, mit zwei Ausnahmen:

- `app/pages/users.py` → `app.roles.ALL_ROLES`. `app/roles.py` wird von sonst
  **nichts** importiert.
- `app/pages/home.py` → `app.states.graph_status_state`, das ohnehin umzieht.

Damit ist der Umzug der Seiten nach `mailarc-ui` mechanisch möglich.

### 2.7 Architekturregeln, die gelten (CLAUDE.md §6)

- `app/` darf eine Komponente importieren. Eine Komponente importiert
  **niemals** `app`.
- **`mailarc-ui` ist die einzige Komponente, die Reflex sehen darf.**
  `components/mailarc-core/tests/test_isolation.py` erzwingt das aus einem
  Subprozess.
- `app/composition.py` ist das **einzige** Modul, das eine Komponente aus
  Konfiguration baut.
- Ein `Protocol` verdient seinen Platz, wenn eine zweite Implementierung
  existiert.
- Wertobjekte sind pydantic-Modelle, unveränderliche mit
  `model_config = ConfigDict(frozen=True)` — nie `@dataclass`, nie `TypedDict`.
- Dateien ≤ 1000 Zeilen.

---

## 3. Zielstruktur

```
components/mailarc-ui/src/mailarc_ui/
├── styles.py            base_style / base_stylesheets        ← aus app/
├── kit/                 stat_tile · panel_card · card_heading · page_header
├── shell/               routes.py · model.py · navigation.py · templates.py
├── pages/               dashboard · status · accounts · review · insights
│                        · embedder · users · profile · auth
├── dashboard/           model · reads · state · components          [neu]
├── status/              state · components            ← aus app/states/
├── accounts/ imports/ review/ insights/ embedder/     (unverändert)

app/
├── app.py               rx.App, Lifespans, publish_*, Seiten-Importe
├── composition.py       + publish_graph_health() + publish_storage_reader()
├── configuration.py · worker.py · derive.py · mcp_server.py
└── gelöscht: pages/ · components/ · states/ · styles.py · roles.py
```

`assets/` bleibt im Wurzelverzeichnis — Reflex bedient es von dort. Genau so
trennt voyager es auch: `styles.py` zieht um, `assets/` nicht.

`app/roles.py` zieht nach **`mailarc_core/roles.py`** (re-exportiert aus
`mailarc_core`): Rollen sind archivweite Policy, Reflex-frei, und jede Schicht
darf `mailarc-core` importieren. Einen Auth-Rollenkatalog ins UI-Paket zu legen
wäre die überraschende Wahl.

---

## 4. `mailarc-core` — Speicher, Graph-Gesundheit, Rollen

### 4.1 Neues Paket `mailarc_core/storage/`

Eine Fähigkeit, feste Dateirollen (CLAUDE.md §6):

- **`model.py`** — `PathUsage` und `StorageUsage`, `BaseModel` mit
  `ConfigDict(frozen=True)`: Label, Pfad, `used_bytes`, `file_count`, dazu
  `total_bytes`/`free_bytes` des Datenträgers aus `shutil.disk_usage`.
- **`usage.py`** — `directory_bytes(path) -> tuple[int, int]` (Baum laufen,
  Anzahl + Bytes) und `StorageReader`, konstruiert mit den drei Pfaden, die er
  misst, mit `usage() -> StorageUsage`.

`StorageReader` blockiert. Aufrufer packen ihn in `asyncio.to_thread`, wie
`review/state.py:293` es für `ArchiveReader` tut.

Nur `app` kennt alle drei Pfade, also baut `app/composition.py` ihn aus
`archive_config().store_dir`, `graph_config().data_dir` und der SQLite-Datei
und veröffentlicht ihn.

### 4.2 Neues Modul `mailarc_core/graph/health.py`

`GraphHealth` hält eine `GraphConfig` und den `FalkorDBServer`-Handle:

```python
class GraphHealth:
    async def status(self) -> GraphServerStatus: ...  # read_status_async(config)
    def startup_error(self) -> str | None: ...  # server.startup_error
```

Das ist der Grund, warum `GraphStatusState` `app/` verlassen kann: heute
importiert er `app.composition.graph_status` direkt.

### 4.3 Neues Modul `mailarc_core/roles.py`

Der Inhalt von `app/roles.py`, re-exportiert aus `mailarc_core`.

### 4.4 Zwei Repository-Methoden

In `mailarc_core/database/repositories.py`:

- `SyncJobRepository.count_by_state(session) -> dict[str, int]` — ein
  `select(state, count()).group_by(state)`. Ersetzt das heutige
  „alle Zeilen laden und zählen" (`len(await find_queued(...))`).
- `FailedMessageRepository.find_recent(session, *, limit)` — nach
  `occurred_at desc`. Speist das Benachrichtigungs-Panel.

---

## 5. `mailarc-ui` — `kit/` und `shell/`

### 5.1 `kit/` — die Primitive, die das Repo privat schon hat

| Name | Herkunft |
| --- | --- |
| `stat_tile(label, value, color)` | das nicht exportierte `_stat`, `insights/components.py:44` |
| `panel_card(*children)` | die neunmal wortgleiche `mn.card`-Rezeptur |
| `card_heading(icon, title)` | `insights/components.py:735` |
| `page_header(title, subtitle, actions=None)` | der Titel/Untertitel-Block, den jede Seite wiederholt |

### 5.2 `shell/`

| Datei | Inhalt |
| --- | --- |
| `routes.py` | Jede Route als Konstante — **eine Quelle der Wahrheit**. Seiten aliasen sie in ihr `ROUTE`, damit Navigation, Seiten und `tests/test_documented_routes.py` nicht auseinanderlaufen können. |
| `model.py` | `NavItem` / `NavSection`, `BaseModel` mit `frozen=True`: `label`, `href`, `icon`, optional `requires_role` / `admin_only`. |
| `navigation.py` | `_NAV_SECTIONS` als Daten; `app_sidebar()` → `mn.app_shell.navbar` mit `mn.nav_link` je Eintrag, `mn.divider` zwischen Gruppen, Markenzeichen oben, Fußbereich (`mn.avatar` + Name + Adresse + Abmelden, oder ein „Anmelden"-Knopf, wenn `LoginState.is_authenticated` falsch ist). |
| `templates.py` | Die Layout-Callables und der Decorator für öffentliche Seiten. |

**Mechanik der Navigation** (aus voyagers `header.py`):

- Aktiver Zustand aus `rx.State.router.page.path` — kein Buchführungs-State.
  Das Ergebnis geht in `mn.nav_link(active=…)`; Styling über dessen
  `variant`/`color`, nicht über eigene Styles.
- Rollenprüfung ist **deklarative Angabe am Eintrag**, angewandt von einem
  `_gated(item, component)`-Helfer über `requires_admin` / `requires_role` aus
  `appkit_user.authentication.components.components`. Das ist hier wesentlich:
  `/` ist öffentlich, ein anonymer Besucher darf die `/admin/*`-Einträge nicht
  sehen.
- `mn.nav_link` rendert einen Router-Link, also funktioniert der bestehende
  `_link_targets`-Lauf über `to:"`-Props in den Seitentests weiter.

### 5.3 Die Vorlagen

```python
def mailarc_app(body: rx.Component) -> rx.Component:
    return mn.app_shell(
        app_sidebar(),
        mn.app_shell.main(body),
        navbar={"width": 260, "breakpoint": "sm"},
        padding="md",
    )


def mailarc_full_app(body: rx.Component) -> rx.Component:
    """Dieselbe Shell, `main` auf Viewport-Höhe — für `/admin/review`."""
```

`/admin/review` ist ein zweispaltiger Leser und verlässt sich heute auf die
`100vh`-Scrollspalte von `navbar_layout`. `mailarc_full_app` ist der Ersatz —
voyagers `voyager_map_app` existiert aus genau diesem Grund.

### 5.4 Öffentliche Seiten brauchen einen eigenen Decorator

`authenticated_page` setzt immer `LoginState.check_auth` an erste Stelle und
kann deshalb keine Seite ohne Anmeldung bedienen:

```python
def public_page(
    route, title, description=None, template=mailarc_app, on_load=None, meta=None
):
    """`rx.page` + Shell + `theme_wrapper` — ohne Auth-Prüfung."""
```

**Wichtig:** `rx.page` allein verliert `theme_wrapper` (das `rx.theme(...)`, das
an appkits `ThemeState` hängt) und `session_monitor()`. `public_page` muss
`theme_wrapper` ausdrücklich wieder anlegen, sonst rendert eine öffentliche
Seite ohne Theme. `session_monitor` bleibt bewusst weg — es gibt keine Sitzung
zu überwachen.

---

## 6. `mailarc-ui` — die Statusseite zieht ein

CLAUDE.md nennt `app/states/graph_status_state.py` als Ausreißer: Reflex-State
in `app/`, weil er `app.composition` direkt importiert. Mit `GraphHealth`
(§4.2) zieht er um:

- → `mailarc_ui/status/state.py`, liest `service_registry().get(GraphHealth)`
  über eine Lookup-Funktion in der Form von `analytics_reader()`
  (`insights/state.py:109`).
- Die drei Karten aus `app/pages/home.py` → `mailarc_ui/status/components.py`.
- `app/composition.py` bekommt `publish_graph_health()`, aufgerufen in
  `app/app.py` neben den fünf bestehenden `publish_*`.

---

## 7. `mailarc-ui` — das Dashboard

`mailarc_ui/dashboard/`: `model.py`, `reads.py`, `state.py`, `components.py`,
`__init__.py`.

### 7.1 Das neue Graph-Statement

Der Katalog ist geschlossen — *„No free Cypher from outside. A statement is a
module-level constant here or it does not exist"*
(`mailarc_analytics/queries/catalog.py`). Also:

**`queries/statements/reads.py`** — `ARCHIVED_PER_DAY`. `Message.archived_from`
ist eine deklarierte Relation mit `edge_model="ArchivedFrom"`
(`mailarc_core/archive/model.py:316`), damit ist der Kanten-Alias verfügbar:

```python
_m = alias(Message, "m")
_r = alias(ArchivedFrom, "r")

ARCHIVED_PER_DAY: QueryBuilder[Message] = (
    select(_m)
    .traverse(Message.archived_from, from_=_m, edge=_r)
    .where(_r.archived_at.is_not_null())
    .project(
        left(_r.archived_at, 10).as_("day"),
        count(_m).as_("messages"),
        sum_(_m.size_bytes).as_("bytes"),
    )
    .order_by("day")
)
```

`left`, `count` und `sum_` sind aus `runic.ogm` exportiert — verifiziert.
Gruppierung in Cypher ist implizit: die nicht aggregierte Projektion gruppiert.

Dazu: Eintrag in `CATALOG`; `ArchivedDay` (frozen `BaseModel`: `day`,
`messages`, `bytes`) in `queries/model.py`; und
`AnalyticsReader.archived_per_day(*, days: int)` in `queries/reports.py`, das
wie jede Schwester-Methode über `rows_of` liest, in Python auf das Fenster
schneidet und Lücken mit Nullen füllt, damit der Chart keine Löcher hat.

> **Verifikationsschritt — durch Ausführen, nicht durch Lesen von `build()`.**
> Dialekt-Wrapper erscheinen erst bei der Ausführung. `archived_at` ist eine
> datetime-konvertierte Spalte; `left()` darüber liefert auf FalkorDB
> möglicherweise nicht `YYYY-MM-DD`. Gegen den gepflanzten Graphen beweisen.
> **Rückfallebene:** rohes `_r.archived_at` + `_m.size_bytes` unter der
> bestehenden `MAX_ROWS`-Decke projizieren und in `archived_per_day` in Python
> nach Tagen bündeln. Die öffentliche API ist in beiden Fällen dieselbe — die
> Entscheidung bleibt im Reader.

### 7.2 `state.py` — `DashboardState`

Nach dem Vorbild von `AnalyticsInsightsState`, ohne Abweichung:

- Registry-Lookups **in der Methode**, nie beim Import.
- **Panelweise Fehlerisolation** — je Panel ein eigenes `loading_*`-Flag und ein
  eigener `*_error`-String, und Lesen *und* Projektion zusammen umschlossen, so
  dass ein totes Panel die Seite nicht leer macht (`insights/state.py:117-150`).
- Jeder blockierende Read über `asyncio.to_thread`.
- Ein `range: str` (`week` | `month` | `year`) speist **beide** Charts aus einem
  Read.

### 7.3 `components.py` — der Entwurf, abgebildet auf vorhandene Daten

| Im Entwurf | Gebaut aus |
| --- | --- |
| Gradient-KPI-Banner, 5 Kacheln | Archivierte Nachrichten (`AnalyticsReader.totals().messages`) · Zuletzt archiviert (größter Tag der neuen Reihe) · Konten (`MailAccountRepository.count`) · Jobs in Warteschlange (`SyncJobRepository.count_by_state`) · Benutzer (`appkit_user`, `user_repo.count`) |
| „System statistics", 3 Balken | Archivgesundheit als echte Verhältnisse: Embedding-Abdeckung (`SemanticSearch.coverage()`), identifizierte Absender (1 − `unidentified`/`messages`), abgeleitete Schicht vorhanden. `mn.progress` + Prozent. |
| „Disk statistics", 3 Balken | `StorageReader.usage()` — Mailstore, FalkorDB-Datenverzeichnis, SQLite-Datei, je `used / total` des Datenträgers. |
| Benachrichtigungsliste | `FailedMessageRepository.find_recent` + fehlgeschlagene Jobs + Konten mit `status` `auth_error`/`error` und `last_error`. `mn.empty_state`, wenn nichts anliegt — ein gesundes Archiv soll sich gesund lesen, nicht wie ein Fehler. |
| „Archived mails per day" + Woche/Monat/Jahr | `mn.area_chart` über `archived_per_day`, `mn.segmented_control` an `DashboardState.range`. |
| „Storage space used per day" | Dieselbe Reihe, `bytes` statt `messages`. |
| Dienste-Checkliste | FalkorDB erreichbar · KNN unterstützt · Sync-Worker läuft · Embedder konfiguriert · Dimension des Vektorindex passt — aus `GraphHealth` und `SemanticSearch`. |

Das Gradient-Banner ist das Einzige, wofür appkit keine Komponente hat: ein
`mn.simple_grid` aus Kacheln über **einem** Hintergrund-Gradienten, definiert in
einer neuen `assets/css/mail-archive.css` (neben `appkit.css` und
`react-zoom.css`) und in `base_stylesheets` eingetragen.

---

## 8. `mailarc_ui/pages/` und was von `app/` bleibt

Jedes Seitenmodul exportiert weiterhin `ROUTE` (jetzt ein Alias auf
`shell/routes.py`) und behält `route`, `title`, `description`, `admin_only` und
`on_load` wortgleich — alle fünf Argumente gibt es auf beiden Decorators.

| Modul | Route | Decorator |
| --- | --- | --- |
| `pages/dashboard.py` *(neu)* | `/` | `@public_page(...)`, `on_load=[DashboardState.load]` |
| `pages/status.py` *(aus `app/pages/home.py`)* | `/admin/status` | `@authenticated_page(template=mailarc_app, admin_only=True)` |
| `pages/accounts.py` | `/admin/accounts` | wie oben |
| `pages/review.py` | `/admin/review` | `@authenticated_page(template=mailarc_full_app, admin_only=True)` |
| `pages/insights.py` | `/admin/insights` | wie `accounts` |
| `pages/embedder.py` | `/admin/embedder` | wie `accounts` |
| `pages/users.py` | `/admin/users` | wie `accounts`; `ALL_ROLES` jetzt aus `mailarc_core` |
| `pages/profile.py` *(neu)* | `/profile` | eigene Seite unter der Shell, ersetzt `create_profile_page(app_navbar(), …)` — voyager macht es genauso, damit `/profile` dieselbe Navigation bekommt wie alles andere |
| `pages/auth.py` *(neu)* | — | `register_auth_pages()` um appkits `create_login_page()` / `create_password_reset_*()`, damit `app/app.py` keine eigene Seitenregistrierung mehr hält |

Jede Seite bringt ihre eigene Inhaltsbreite mit (`maw=MAX_CONTENT_WIDTH,
mx="auto", p="2rem"`), weil `authenticated_page` kein Padding liefert, wo
`navbar_layout` es tat.

**Gelöscht:** `app/pages/`, `app/components/`, `app/states/`, `app/styles.py`,
`app/roles.py`.

**`app/app.py`** schrumpft auf: Logging-Aufbau, die `publish_*`-Aufrufe (plus
`publish_graph_health()` und `publish_storage_reader()`),
`register_auth_pages()`, die Seiten-Importe für ihren Registrierungs-Seiteneffekt,
`rx.App(...)` mit Styles aus `mailarc_ui.styles`, und die drei Lifespans.

---

## 9. `kit/`-Refactoring — im Umfang

Sobald `kit/` existiert, gehen **alle** bestehenden Karten hindurch:
`insights/components.py` (sechs Stellen), `embedder/components.py`,
`status/components.py`, und das beförderte `_stat`.

Mechanisch, von bestehenden Tests abgedeckt, beseitigt eine neunfache
Doppelung — und drückt `insights/components.py` von 891 Zeilen nach unten, was
gegen die 1000-Zeilen-Grenze zählt.

---

## 10. Tests

Zuerst geschrieben, dann der Code (CLAUDE.md §4). Seitentests ziehen in die
Komponente; im Wurzelverzeichnis bleibt nur, was eine gebootete App braucht.

| Datei | Prüft |
| --- | --- |
| `components/mailarc-core/tests/` | `StorageReader.usage()` über einen Temp-Baum (**nie** `.state`); `count_by_state`; `find_recent`; der Rollen-Re-Export |
| `components/mailarc-analytics/tests/` | `ARCHIVED_PER_DAY` steht im `CATALOG`; **es führt gegen den gepflanzten Graphen aus** und bündelt ein bekanntes Fixture in die erwarteten Tage — das ist der Test, der die `left()`-Frage entscheidet; Lückenfüllung |
| `components/mailarc-ui/tests/test_ui_shell_navigation.py` | Die Sidebar baut; jedes `NavItem.href` ist eine Route, die eine Seite registriert; Admin-Einträge sind gesperrt |
| `components/mailarc-ui/tests/test_ui_dashboard_state.py` | `load` füllt jedes Panel; ein werfender Reader setzt *dessen* Fehler und lässt die anderen stehen; der Bereichswechsel formt beide Reihen um; Byte- und Zeitstempel-Formatierung |
| `components/mailarc-ui/tests/test_ui_pages.py` | Jede Seite baut, registriert ihre Route, grundiert sich und ist gesperrt. Löst die vier `tests/test_mail_*_page.py` ab |
| `components/mailarc-ui/tests/test_ui_status_state.py` | aus `tests/states/test_graph_status_state.py` |
| `tests/test_app_boot.py` | Behält die Subprozess-`BOOT_PROBE` (braucht `import app.app`): nach dem Boot sind `/`, `/admin/*` und `/profile` registriert und die Dienste veröffentlicht |
| `tests/test_dashboard_is_public.py` | Das `on_load` von `/` enthält **kein** `LoginState.check_auth`. Das ist die Zusicherung, die „Benutzerseiten brauchen keine Anmeldung" festnagelt |
| `tests/test_documented_routes.py` | Import aus `mailarc_ui.shell.routes`; `/` und `/admin/status` ergänzt |

Zwei Fallen, die beim Schreiben auffallen werden:

- `_gate_of` in den heutigen Seitentests liest `admin_only` als freie Variable
  aus dem Closure des Decorators. `authenticated_page` schließt über denselben
  Namen — es sollte also weiter aufgehen. **Verifizieren, nicht annehmen.**
- `components/mailarc-core/tests/test_isolation.py` benennt seine Pakete in drei
  handgeschriebenen Tupeln. Prüfen, dass `mailarc_core.storage` und
  `mailarc_core.roles` vom Reflex-Verbot erfasst sind und nicht durchrutschen.

---

## 11. Reihenfolge der Arbeit

0. Diese Datei — vor dem Code gelesen.
1. `mailarc-core`: Storage, `GraphHealth`, Rollen, zwei Repository-Methoden
   + Tests.
2. `mailarc-analytics`: `ARCHIVED_PER_DAY` + Reader-Methode + Tests — hier wird
   das `left()`-Risiko entschieden, deshalb früh.
3. `mailarc-ui`: `kit/`, `shell/`, `styles.py` + Navigationstests.
4. Alle Seiten nach `mailarc_ui/pages/`, Status-State umziehen, `app/`
   ausräumen, Tests nachziehen. **Am Ende dieses Schritts läuft die Anwendung
   vollständig** — noch ohne Dashboard, aber mit einer Navigation.
5. `mailarc-ui`: das Dashboard + State-Tests + `pages/dashboard.py`.
6. `kit/`-Refactoring (§9).
7. Vollständiges Gate, Browser-Verifikation, Screenshots.

---

## 12. End-to-End-Verifikation

**Niemals gegen das echte Archiv** — ausschließlich der `agent:`-Namensraum
(CLAUDE.md §6b; `PROFILES=agent_test` allein reicht nicht).

```bash
task agent:check && task agent:app
```

Dann im Browser auf `http://localhost:3031/`:

1. `/` rendert das Dashboard **abgemeldet**, die Sidebar zeigt keinen
   `/admin/*`-Eintrag, kein Panel zeigt einen Fehler.
2. Anmelden als `test@test.de` / `Test#2026` (nur Entwicklung) — die
   Admin-Einträge erscheinen, `/admin/status` zeigt die FalkorDB-Fakten, und
   `/admin/review` füllt weiterhin den Viewport mit seinen zwei Spalten.
3. Beide Charts zeichnen; die Umschaltung Woche/Monat/Jahr formt sie um.
4. Konsole ohne Fehler; Screenshot beider Zustände.

Dann das vollständige Gate:

```bash
task format && task lint && task typecheck
task test                       # Coverage ≥ 80 %
```

Keine Datei über 1000 Zeilen.

---

## 13. Nicht in diesem Umfang

- **Kopfleiste mit Suche und Filter** — bewusst ausgeschlossen (§1.2). Die
  Volltext- und Semantiksuche existiert bereits als `ArchiveSearchState` /
  `archive_search()`; eine öffentliche `/search`-Seite darauf ist die
  naheliegende Folgearbeit und die erste echte Nutzung von `public_page`.
- Ein Mantine-Theme-Modul mit eigener Palette (voyagers `app/theme.py`, ~500
  Zeilen). Die Shell benutzt die Standardpalette; ein eigenes Theme ist eine
  Gestaltungsentscheidung für sich.
- Responsives Einklappen der Sidebar über `mn.burger` — `mn.app_shell` bekommt
  `breakpoint`, aber der Umschalt-State bleibt ungebaut.
- Mehrsprachigkeit der Oberfläche.
- Benachrichtigungen als eigenes Konzept mit Zustand (gelesen/ungelesen). Das
  Panel liest Fehler, es verwaltet sie nicht.
