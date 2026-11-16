# CPC-UAEH modifications to DMOJ

As of 2026-11-15, this source includes a public live scoreboard for each
contest, autocomplete in the code editor, a C++ template per round of a duel
tournament, cancelling a duel tournament, a duel panel organized around the next
step with tournaments by number of places and projectors paired with a code, a
public history and a ranking of duels, approval before emails to users are sent,
with senders of the site's domain, social links and pasted addresses,
organizers who register people and teams in a contest, rating by attendance,
registration before a contest starts,
contests on site and online with separate standings, contest announcements that
pop up right away, emails to many users sent through
Resend, permissions for one's own and for all duels and emails with duel
co-organizers, a footer line in every email about the club, a Back button in
read-only admin forms, 1v1 duels and knockout tournaments,
sample tables and `$...$` math in problem statements, reliable problem PDFs and
one PDF with every statement of a contest, an event daemon that survives
malformed messages, per-recipient and daily email limits, the visitor's real
address behind a tunnel, redesigned account emails and icons, removal of contest
problems by contest authors, a readable permission selector in the admin, a
stable phone layout, interactive problems configured from the problem data page,
team support and team contests, an owner account that restricts deletion in the
administration interface, a dark theme available to every account, test cases
filled in from a problem data archive, frozen scoreboards, a reveal ceremony,
balloon operations, contest announcements and clarifications, self-refreshing
rankings, resilient live updates, custom-test admission controls, a pre-upload
check for problem data and the Django 5.2.17 upgrade.
Timezone, storage/dependency and contest administration adaptations are
documented in the [upgrade guide](docs/django52/README.md). A grouped list of
every change is in the [fork overview](docs/fork-overview.md).

This repository contains a modified version of
[DMOJ](https://github.com/DMOJ/online-judge), originally distributed under the
GNU Affero General Public License version 3.

## Version information

- Upstream base commit: `97f3722ef3f7ca9731727c220622bdd1eab7d4b3`
- Initial publication date: 2026-09-07
- Modified version maintained by: CPC-UAEH
- Corresponding source branch: `cpc-production`

## Summary of modifications

- CPC-UAEH branding, icons, site footer, and registration email presentation.
- First-name and last-name fields in account registration.
- Authentication using either a username or an email address.
- A signed-in custom code testing workflow and its judge bridge support.
- Minor registration and presentation adjustments.

## Public scoreboard of a contest — 2026-11-10 and 2026-11-15

- `/marcador/<key>/` shows the scoreboard of a contest to anybody, without an
  account or a session: a podium, the whole table with a balloon per problem
  (partial points filled up to where they got) and the latest submissions. It
  updates by itself through the event daemon, and with the scoreboard frozen
  what was sent afterwards only shows as `?` until the reveal. Problems are
  named only by their letter; organizers, testers, disqualified participants and
  virtual participations are left out.
- Off by default: a public scoreboard box in the contest's admin page turns it
  on and alone decides, also for private and hidden contests. A clone starts
  with it off, and keeps the contest's problems, which the copy used to lose.
- Once the contest is over and nothing is frozen, a Compare tab charts how two
  rows went up. A letter links to its problem only when the problem is public,
  through `/marcador/<key>/problema/<letter>`, which sends there only whoever
  may open it. The list of submissions follows the tab.
- The page is fixed Spanish text. Migration `0168` adds the contest's column,
  false by default also in the database.

## Autocomplete in the code editor — 2026-11-09

- The editors of submit, the custom test and the duel player suggest as one
  types: words already in the code, the language's keywords and, in C and C++,
  whole `#include` lines. Tab or a click picks; Enter always starts a new line.
  An Autocomplete box next to the language turns it off in that browser.
- On in practice. A contest allows it with Allow autocompletion; a duel
  tournament in its Rules, each round as the tournament, yes or no, and a quick
  battle under More rules. Off by default in both.
- Ace goes from 1.1.3 to 1.43.3 (`ACE_URL`, now with a final slash). Migration
  `0167` adds the contest's column, false by default also in the database.

## A C++ template per round in duel tournaments — 2026-11-08

- Rounds and problems has a Template column: with it ticked, the games of that
  round open the editor in C++ with a minimal program and the cursor inside
  `main`, in the player's own C++ or the newest one the problem allows. Changing
  language before typing removes it. Quick battles have it under More rules.
- The settings of a battle save only what was changed in them, so a battle keeps
  following its tournament and round; chosen languages show ticked; a started
  round locks its games per battle; with 17 to 64 places the first rounds are
  Round of 32 and Round of 64.
- No migration: the template is a rule (`starter_code`) in the JSON rules of a
  round or a quick battle. Details in [docs/duels.md](docs/duels.md).

## Cancelling a duel tournament — 2026-11-06

- Something went wrong? → Cancel the tournament, in preparation or in play: the
  game in play is voided, prepared games are deleted and players are free at once;
  what was decided stays. A cancelled tournament leaves Duels → Tournaments, can
  be shown again without a podium, and never counts for the ranking.
- A projector following a tournament stays on a battle between its games and 30
  seconds after it is decided, then goes back to the bracket.
- Migration `0166` only adds the cancelled status (no change to the table).

## Approval for emails to users, senders and pasted addresses — 2026-11-05

- Writing is not sending: `judge.send_mass_email` writes one's own emails and asks
  for approval; the new `judge.approve_mass_email` reviews, corrects, sends back
  with a note, sends or schedules them. What is sent is what was reviewed.
  Approvers see an envelope with the count in the navigation bar.
- Senders chosen from a list of the site's domain, and links to social networks
  with their icons at the foot of every email, both kept in a new admin page.
- Addresses pasted from a form are added to the audience, sent without a name and
  never revealed to whoever only writes. `DMOJ_RESEND_CONTACT_LIMIT` caps the
  contacts kept in Resend.
- Migration `0165`. Guide in [docs/mass-email.md](docs/mass-email.md). The social
  network icons in `resources/icons/redes/` are from
  [Simple Icons](https://simpleicons.org/) (CC0 1.0), recolored grey.

## Organizers register people, rating by attendance — 2026-11-05

- The Registered tab of every contest registers accounts and teams ahead, also
  after the start and until the end, without the access code; the registration
  opens a private contest or one of organizations to them while it lasts. Teams go
  through a page with their members, within the contest's sizes. Organizers remove
  registrations that have not entered.
- On site and online contests choose who gets a rating: everybody, on site only or
  online only.
- Migration `0164` (two columns with database defaults). Details in the
  [contest guide](docs/contest-tools.md).

## Duel panel, tournaments by places, paired projectors — 2026-11-04

- The organizer panel has tabs (Now, Quick battles, Tournaments, Projectors,
  Problem banks), and each battle shows a single button with its next step.
- Tournaments are created with a number of places, from 2 to 64; problems go in
  before the names.
- A projector opens `/proyector` and shows a code that the organizer types in the
  panel; keys 1 to 6 switch its view. What ended stays on a projector for 30
  minutes before a new battle or tournament takes it.
- Without an account, `/duelos/` opens the tournaments; Approve results counts
  what is pending. Fixes found while using it are in the commit message.
- Migration `0163`. Details in [docs/duels.md](docs/duels.md).

## Duel history and ranking — 2026-10-26 and 2026-10-27

- Public pages for duel tournaments at `/duelos/torneos/`: started tournaments
  shown in the history, with their bracket, live while they are played, and
  upcoming ones announced with a date. Code, submissions and screens are never
  public. Migration `0160`, with the `hide_arena_history` permission.
- A Glicko-2 ranking at `/duelos/ranking/`. Everything starts as pending; only
  someone with the new `judge.decide_arena_ranking` permission decides what
  counts, also when creating it. Byes, walkovers, cancelled battles and battles
  with typed names never count. The ranking is recalculated on each change.
  Migration `0161` and the `rate_duels` command.

## Contest announcements and the Registered tab — 2026-11-01

- Inside a contest, an announcement or an answer pops up right away: saving it
  rings the contest's channel in the event daemon, with no text, and the page asks
  the site for what is new. The pop-up takes the contest's name as its title.
- Only participants and testers ask clarifications. Authors, curators and whoever
  can edit every contest post announcements from the Clarifications tab, without
  the admin or its permission.
- The Registered tab has On site and Online tabs, a token search to add people on
  site that also takes a pasted list, and a confirmation before moving anyone.
- The join page shows no choice when there is only one way to take part, and the
  access code is a plain text field.
- No migration. Details in the [contest guide](docs/contest-tools.md).

## Registration, on site and online contests — 2026-10-31

- Every contest takes registrations before it starts. At the start, a prompt on
  any page asks whoever registered whether to enter; nobody enters by themselves,
  and whoever did not register joins as before. A registration is a
  participation with `virtual = -2` and stays out of rankings, the API, the
  reveal, balloons and rating until it enters.
- Contests are online only, on site only, or on site and online. The last one
  has Global, On site and Online standings, a Registered tab to assign people,
  an on-site list for people who have not registered yet, balloons only on site
  and the reveal per view.
- A pill on Contests in the navigation bar replaces the floating timer box.
- Migration `0162` (three columns, no table). Details in the
  [contest guide](docs/contest-tools.md).

## Fixes from a review of the fork — 2026-10-28 to 2026-10-30

- Administration: reverting to an old version is for the owner accounts; rating,
  rejudging and test data take the right permissions, and the freeze covers the
  pages it missed.
- Content: previews need an account, MathJax runs with its safe extension, and
  pictures in statements go through the site.
- Duels: players do not manage their own games; safer holds and screens.
- Emails to users: scheduled sends, retries without duplicates, and each
  recipient checked again right before sending.
- Contests: freezes keep their cutoff, authors, curators and testers stay off the
  scoreboard, and teams are safer to edit.
- Clarifications only for those inside a running contest, bounded PDF rendering,
  stricter problem data forms, password reset limits that Unicode variants no
  longer dodge, and stale signups that give way to new ones.
- No migration. The commit messages describe each change.

## Email footer and the admin bottom bar — 2026-10-24

- Every email the site sends (activation, password reset, both email change
  messages and emails to users) ends with a line saying the club is independent
  and not an official service of its university. Emails to users also link the
  privacy notice at `/privacidad/`, in HTML and in plain text. The line names this
  club; see [Emails to users](docs/mass-email.md#the-footer) to change it.
- `templates/admin/submit_line.html`: on a change form that can only be viewed,
  Django asks for a Close button that wpadmin's bar does not draw, so the bar was
  black and empty. It now shows Back, to the list and keeping its filter.
- No migration. Tests in `tests/mass_email`.

## Own and all, co-organizers and audiences — 2026-10-23

- `judge.manage_arena` covers the battles a person created or co-organizes, with
  their own screens and problem banks; the new `judge.manage_all_arena` covers
  every battle. Tournaments and quick battles get an Organizers panel. What
  belongs to someone else answers 404 and stays out of the admin lists.
- `judge.send_mass_email` covers one's own emails to users; the new
  `judge.edit_all_mass_email` covers all of them and subscribing people again.
- Emails to users choose their audience explicitly: everyone with a confirmed
  email, those matching the filters, or people chosen by name. New rating filter.
- Fixed: "always include" without filters sent to everyone; the unsubscribed list
  showed full addresses to non-superusers; organizers could open other people's
  battles and move their projectors; problem banks showed private problems to
  organizers who could not see them, and saving them failed.
- The task progress page shows the site's logo, a Back button and wrapped errors.
- Migration `0159`. Tests in `tests/duels/test_duel_organizers.py` and `tests/mass_email`.

## Emails to users — 2026-10-21

- A section of the admin, Emails to users, to write one email for many accounts
  and send it through Resend Broadcasts, never through the site's SMTP: drafts in
  Markdown and HTML, a preview as a desktop and a phone show it, a test to oneself,
  recipients by filters, confirmation by typing their number, sending now or
  scheduled, delivery figures read from Resend and the list of unsubscribed people.
- A Celery task creates the Resend segment and contacts and asks Resend to send;
  retries never send twice. The client in `judge/utils/resend.py` respects
  Resend's request limit. 2026-10-22: each segment is sent as an object when a
  contact is created, which Resend requires.
- The common email template gains `title` and `footer` blocks; the account emails
  are unchanged.
- Settings `DMOJ_RESEND_*` and `DMOJ_MASS_EMAIL_*`, an `email` Markdown style and an
  admin menu entry. Migration `0158`. Guide in [docs/mass-email.md](docs/mass-email.md).

## Problem and contest PDFs — 2026-10-19

- Problem PDFs are rendered first and then moved into the cache at once. Upstream
  opened the cache file before rendering, so a failed render left an empty PDF that
  was served from then on, and a concurrent request could read a half-written one.
- `problem/raw.html` has a `<title>`, so the PDF is named without `exiftool`.
- `/contest/<key>/pdf`: every statement of a contest in one PDF, with a cover, one
  statement per page and translations in the reader's language. Offered at the
  bottom of the contest page and of its problem list to whoever is inside the
  contest once it started, and at any time to its authors, curators, testers and
  editors of every contest. Never for the hidden contests behind duels.
- Cached under a fingerprint of its HTML; only one request renders it, with a time
  limit below the uWSGI `harakiri`, and the others get a page that retries by itself.
- `render_pdf()` takes an optional `timeout`.
- A patch for [pdfoid](https://github.com/DMOJ/pdfoid) and an example unit in
  `deploy/examples`: upstream pdfoid leaves a ChromeDriver running after every PDF
  and waits up to 300 seconds for a page.
- No migration. Guide in [docs/pdf.md](docs/pdf.md); tests in `tests/statements`.

## Sample tables and inline math in statements — 2026-10-18

- Statements, their translations and editorials accept OmegaUp's sample table:
  `||input`, `||output`, an optional `||description` in Markdown, and `||end`.
  Without a closing `||end` the text stays as it was.
- The table brings its own stylesheet, appended once to the rendered statement, so
  it looks the same on every page that shows a statement and in PDFs, in both
  themes. Each column is as wide as its own samples (2026-10-19).
- `$...$` is inline math in the same styles, next to `~...~`, with Pandoc's rules
  so that prices stay text, and `\$` for a literal dollar sign.
- Other Markdown styles (comments, blog posts, contest descriptions, profiles) do
  not change. No migration. Guide in [docs/statements.md](docs/statements.md); tests
  in `tests/statements`.

## Duels and tournaments — 2026-10-17

- First-to-solve battles between two players, alone or as best-of-N series, and
  knockout tournaments of up to 64 players with an optional third-place match.
  At `/duelos/` since 2026-10-19; `/arena/` forwards there. In the code the feature
  is `judge/arena`, with `Arena*` models and `arena_*` URL names.
- An organizer panel behind the `judge.manage_arena` permission: battles, series,
  tournaments with a bracket built by itself, problems chosen by hand or dealt from
  problem banks, projector screens with secret links and an organizer guide in
  Spanish.
- Each game runs on a hidden, unrated contest, so judging uses the contest queue,
  the bridge and `update_contest()` unchanged. The engine decides start, end and
  winner on each state poll; organizer actions are versioned.
- Players are kept on the duel page while their battle lasts and can only submit
  from it (`ContestMiddleware` and `ProblemSubmit`). The lock expires after
  `CPC_ARENA_LOCK_IDLE_MINUTES` without activity.
- Request limits per player and per screen in `CPC_ARENA_*` settings.
- The admin gets a *Battles* section. The contests behind games are left out of
  `/contests/` and of the admin's contest and participation lists
  (`Contest.duel_contests()`), and are deleted with their game.
- Migration `0157` only creates the `judge_arena*` tables. Guide in
  [docs/duels.md](docs/duels.md); tests in `tests/duels`.

## Event daemon hardening — 2026-10-17

- `websocket/daemon.js` validates every request: a JSON object with a string
  command, looked up as an own property. A message such as `{}` or `null` used to
  throw out of the handler and end the process, and with it every live update.
- Both sockets have an `'error'` listener, sends to closing sockets are ignored,
  the long-polling server answers 500 instead of crashing, and incoming messages are
  capped (`max_receive_payload`, `max_post_payload`).

## Email limits and the visitor's address — 2026-10-14

- Password reset sends at most `DMOJ_PASSWORD_RESET_DAILY_LIMIT` emails (2) to one
  address in 24 hours, counted per address whether or not an account uses it, so the
  answer does not reveal who is registered. Email change accepts
  `DMOJ_EMAIL_CHANGE_DAILY_LIMIT` requests (1) per account in 24 hours.
- Every email the site sends is counted per UTC day against
  `DMOJ_EMAIL_DAILY_LIMIT` (90). Registrations and error reports leave
  `DMOJ_EMAIL_DAILY_RESERVE` (20) of it to existing accounts. Error reports to
  `ADMINS` are also capped at `DMOJ_EMAIL_ERROR_DAILY_LIMIT` (10) a day.
- Registration sends the activation email inside the transaction that creates the
  account. Upstream sent it after committing, so a failed send left an inactive
  account with no email whose address could not register again. Now nothing is
  created and the page says so.
- A failed password reset email stays hidden, as Django does, but no longer counts
  against the user; a failed email change is reported.
- The new limits answer with a translated page and HTTP 429, or 503 when sending
  failed.
- The example Nginx configuration takes the visitor's address from the edge's header,
  trusted only from loopback. Behind a tunnel, Django otherwise saw one address for
  every visitor, which made DMOJ's per-IP limits a single site-wide counter.
- No migration. Tests in `tests/email_limits`; operator documentation in
  [docs/security/email.md](docs/security/email.md) and
  [docs/security/nginx.md](docs/security/nginx.md#visitor-address).

## Account emails and icons — 2026-10-08

- Activation, password reset and the two email change messages share one template
  (`templates/registration/email_base.html` and `email_macros.html`): tables and
  inline styles that Gmail, Outlook and phone apps respect, the site logo at three
  times its displayed size, a button with the plain link below it, notes and
  warnings in boxes, a preview text and a contact line with `SITE_ADMIN_EMAIL`.
- The previous password reset and email change messages referenced an icon the
  site does not have, and the activation message offered to activate the account
  by replying to it, which this site does not support.
- The plain-text versions no longer show HTML entities: translated strings come out
  of `_()` already escaped, so the English source strings avoid apostrophes.
- A multi-size `favicon.ico` (16 to 256 px), the site's artwork instead of DMOJ's in
  the wide Windows tile and in `logo.png` (browser notifications), and the site's
  name in `manifest.json`.
- Spanish texts revised. No migration; the templates are outside the compressed
  blocks.

## Administration — 2026-10-07

- Contest authors can remove a problem from a contest they edit.
  `ContestProblemInline` declares `owner_lock_exempt = True`, which the owner-only
  deletion lock honours only on inlines and only when declared on the class itself,
  not inherited, and its delete permission is its change permission. Removing a
  problem deletes its submissions' link to the contest, its contest clarifications
  and its balloons; the problem and the submissions stay. Everything else stays
  locked, including deleting the contest itself.
- The two-list permission selector of the admin theme cut most permission names
  short. It now uses the full width and stacks both lists when they would be too
  narrow (`templates/admin/base_site.html`).
- No migration. Tests in `tests/contest_features/test_contest_problem_removal.py`.

## Phone layout — 2026-10-01

- Phones narrower than 480 px get the 480 px layout the stylesheets were written
  for, scaled to fit, from the first paint. Upstream `common.js` switched the
  viewport only on `resize`, so a phone showed the cramped device-width layout
  until scrolling moved the address bar. Because it measured the page it had just
  rescaled, at about one screen width in five between 320 and 480 px it then
  flipped between both layouts many times a second. An inline script in
  `templates/base.html` now decides from the screen size and orientation, so its
  own change cannot trigger it again.
- The viewport `<meta>` no longer has `id="viewport"`. The upstream handler is
  still in `common.js`, inside the compressed bundle, but finds nothing to change.
- MathJax's invisible MathML copy of each formula is kept to the width of the
  formula. Near the right edge it made pages pan sideways on phones and zoomed
  them out.
- The floating contest timer grows with its lines and keeps the same margin on
  both sides; on phones the clock fell out of its box.
- The problem data page fits on phones: below 760 px the form labels wrap and the
  test case table scrolls inside its own box. The markup and the row scripts are
  unchanged, and so is the desktop layout.
- No migration. The `{% compress %}` blocks of `base.html` are byte-identical, so
  an existing offline manifest stays valid; reload the web service.

## Interactive problems — 2026-09-25 to 2026-09-27

- The problem data page has an interactor upload field (C++, C or Python, up to
  1 MB) and a checkbox that shows the interactor's standard error to contestants.
  Migration `0156_problemdata_interactor` adds both columns; the boolean has a
  database default so code without the field can still insert rows.
- Saving the page writes the judge's `interactive:` block into `init.yml` every
  time, instead of dropping a hand-written one. Interactors that include
  `testlib.h` use the judge's testlib mode, and the page copies a bundled
  `testlib.h` next to them. Python interactors are pinned to `PY3`.
- The page reports whether the problem is interactive, whether it uses testlib and
  whether the interactor file is missing, and links to a Spanish guide for problem
  setters with a downloadable worked example
  (`/problem/<code>/test_data/interactive`, editors only).
- Fix: uploading a data archive over an existing one deleted the old file and
  never stored the new one, because `FieldFile.delete` also clears the field on
  the instance being saved. Replaced files are now removed by name after saving.
- Tests in `tests/interactive_problems`; operator documentation in
  [docs/interactive-problems.md](docs/interactive-problems.md).

## Fixes and hardening — 2026-09-20 to 2026-09-22

- Clarifications: the visibility of an answer and its announcement change
  together, from the contest tab and from the admin. Making a public answer
  private withdraws its announcement, answering again reuses it, moving the
  question to another contest moves its announcement, and deleting the question
  (directly or with the problem it refers to) withdraws it. Announcements with
  neither text nor a question are never shown.
- The clarification admin only offers, and only accepts, contests the account can
  edit, and rejects a problem from another contest. The contest of an
  announcement that carries a clarification is read-only in the announcement
  admin.
- Frozen scoreboards also freeze the contest history of `/api/v2/user/<name>` and
  the AC rate and Users counters of the contest's problem lists. On your own row,
  the frozen board marks results it is not showing, including a submission sent
  before the freeze whose verdict arrived after it started.
- Cloning a revealed contest no longer copies the revealed state, which silently
  disabled the new contest's freeze.
- Assigning a team owner in the admin adds that account to the members; the team
  page was otherwise unreachable for its own owner.
- Registering for a contest before it starts shows its message instead of an
  error page.
- Login by email no longer fails when several accounts share an address: the
  password decides which account signs in.
- The bottom action bar of admin change lists works with Django 5.2 and stays in
  sync with the top one (`templates/admin/change_list.html`).
- The custom test page has its title again.
- `problem_data_upload_gate`: an internal view that Nginx asks, through
  `auth_request`, before buffering a problem data upload. The example Nginx
  configuration now uses it, buffers request bodies on disk and closes an
  off-by-slash path traversal in its `/static` location.
- No migration and no schema or data change.

## Administration and deletion control — 2026-09-19

- `CPC_SERVER_OWNERS` in settings names the accounts allowed to delete from the
  administration interface. The restriction is applied in the admin rather than
  through a permission, because `PermissionsMixin.has_perm` returns True for any
  superuser without consulting permissions or backends. Two layers: the wrapped
  `has_delete_permission` of every registered ModelAdmin and inline, and the
  `delete_view`, `delete_model` and `delete_queryset` of `ModelAdmin`.
- The owner account is protected from other superusers: its user and profile
  cannot be edited, the Staff and Superuser fields are removed from the form for
  anybody else rather than shown read-only, and TOTP records are reserved.
- A permanent deletion path for teams and contests that dismantles the five
  `PROTECT` relations in order inside a transaction, behind a confirmation that
  enumerates what will be destroyed. Submissions are preserved; only their link
  to the contest is removed. No `PROTECT` relation was dropped.
- `IMPERSONATE_REQUIRE_SUPERUSER` was ignored by the installed
  django-impersonate, which reads an `IMPERSONATE` dictionary; impersonation was
  therefore open to every staff account. Corrected, with the audit log enabled.
- Teams and team invitations registered in the administration interface, with
  search by member, member and participation columns, inline membership editing
  and invitation cancellation.
- No migration and no schema or data change.

## Teams and team contests — 2026-09-18 and 2026-09-19

- Teams, memberships and invitations, with a My teams page. Migration `0155`.
- Individual, team and mixed participation modes per contest, a single official
  registration, shared virtual participations and separate rankings.
- Registration admits the whole roster on one access code: contest access is
  evaluated for whoever registers, while conditions that cannot be delegated —
  an inactive account, a ban from that contest, organising or testing it — are
  still evaluated per member, and the message names who is blocked and why.
- The members who compete are selected at registration time and frozen; minimum
  and maximum team sizes are measured on that selection.
- Member photographs in the award ceremony and balloons per team.

## Interface — 2026-09-18 and 2026-09-19

- The dark theme no longer depends on the `judge.test_site` permission and is
  available to every account; `auto` follows the operating system preference.
  The `{% compress %}` blocks of `base.html` were left byte-identical and the
  dark stylesheet is linked outside them, so the offline manifest stays valid.
- The custom test page was rebuilt: full width and height editor without line
  wrapping, output no longer truncated at 64 bytes, a configurable output prefix
  and translated messages inside the JavaScript.
- The navigation bar is translated through a `dmoj-user` catalog, which upstream
  does not provide.
- Test cases are filled in from an uploaded archive: pairing by extension or by
  folder, natural ordering, batch detection, an exact integer point split, and a
  report of files left without a pair. The view also reports how many form rows
  fit, since the form posts 14 fields per row.
- A scheduled cleanup for finished custom tests across all accounts, published
  as a deployment example rather than as application code.

## Freeze, reveal and balloon operations — 2026-09-16

- Configurable scoreboard freeze with snapshots and filtering across shared
  submission views, statistics, APIs and public events; explicit reveal.
- Reveal ceremony with animated standings, adjustable speed, result colors,
  per-participant award cards, medal ranges, tied ranks and first-to-solve awards.
- Balloon desk with dedicated staff permissions, live updates, delivery/undo
  history, problem colors and the rule that frozen-window ACs earn no balloon.
- Theme-aware contrast, explicit All filter and searchable, paginated location
  fields; updates preserve untouched rows and detect conflicting editor changes.
- Native color picker, editable hex and No color control for contest problems,
  including dynamically added administration rows.
- Migrations `0153` and `0154`, Spanish translations and isolated regression tests.

See the [organiser guide](docs/contest-tools.md) and
[test instructions](tests/contest_features/README.md).

## Contest features — 2026-09-15

- Site-wide and per-contest announcements, shown as a plain-text overlay and
  archived on a per-contest clarifications tab, with an administration form that
  picks the target and when the overlay stops appearing.
- Contest clarifications: contestants ask the jury about the contest or about a
  single problem, and answers are either private to whoever asked or broadcast
  to every contestant, in which case they are archived as an announcement of
  that contest. Question bodies are plain text, length capped and rate limited
  per user.
- Contest rankings that refresh themselves while the tab is visible, reusing the
  existing ranking fragment endpoint, which now also carries the editor controls
  and the table identifier it previously omitted.
- A heartbeat in the event daemon and reconnection with exponential backoff in
  the browser client, with long polling as a fallback.
- Spanish translations for the strings these features introduce.
- Django migrations `0150`, `0151` and `0152`.

## Security maintenance through 2026-09-14

- Signed ownership checks and read-only result polling for custom tests, with
  bounded cleanup of verified completed tests and transaction-safe file handling.
- Personal test exclusion from shared submission histories, API lists and
  statistics, with explicit owner/administrator access to source details.
- Documented deployment isolation, private configuration/backup boundaries,
  Redis authentication, Nginx request/origin restrictions and HTTPS controls.
- Updated public configuration examples and private-artifact ignore rules.
- Per-user custom-test fixed windows and an in-flight admission check, with
  HTTP 429 feedback; documented concurrency and retention limitations.
- A uWSGI request-timeout example, structural CSP/referrer headers, added judge
  isolation controls, Supervisor retirement, DNS/error-reporting repairs and
  offline-compression build requirements.

See the [security guides](docs/security/README.md) for how to configure each
control and its known limitations. Infrastructure changes are represented by
documentation and generic examples; no host settings, credentials or user data
are included.

## Django 5.2 upgrade — 2026-09-12

- Django 5.2.17, django-mptt 0.18.0 and django_compressor 4.6.0 requirements.
- Standard-library UTC and ZoneInfo support, updated static storage guidance,
  and one CompressorFinder in the base settings.
- Contest participant bans applied after a valid saved form, preserving
  participation when an administrative form is rejected.
- Optional compatibility tests and the versions of a working environment. See
  the [upgrade guide](docs/django52/README.md).

Git history and file-level diffs are the authoritative record of the exact
changes. The complete corresponding source is available without charge at
<https://github.com/ChristopherRDZ/cpcjudge-site/tree/cpc-production>.

## License

This modified version is distributed under the GNU Affero General Public
License version 3. The complete license text is provided in [LICENSE](LICENSE).
Existing copyright and license notices from DMOJ and bundled third-party
components are retained.

`judge/utils/interactive/testlib.h` is testlib 0.9.45 by Mike Mirzayanov
(<https://github.com/MikeMirzayanov/testlib>, commit
`2d20123984e9479b8a56ebe0d6a51e23ad7c35b3`), distributed under the MIT License,
whose text is included in `judge/utils/interactive/testlib-LICENSE.txt`.

Branding artwork is included as part of the deployed version by CPC-UAEH. Its
inclusion does not grant rights to third-party trademarks beyond those provided
by their respective owners.
