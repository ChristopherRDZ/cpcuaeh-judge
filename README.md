<h1 align="center">
  <img src="logo.png" alt="DMOJ" width="120">
  <br>
  CPC UAEH Online Judge
</h1>
<p align="center">The online judge of the Club de Programación Competitiva UAEH (CPC UAEH):<br>a DMOJ fork for programming practice and ICPC-style contests.</p>
<p align="center">
  <a href="LICENSE"><img alt="License: AGPL v3" src="https://img.shields.io/badge/license-AGPL--3.0-blue"></a>
  <a href="docs/setup-guide.md">Installation</a> ·
  <a href="docs/fork-overview.md">Features</a> ·
  <a href="docs/contest-tools.md">Contest guide</a> ·
  <a href="docs/duels.md">Duels</a> ·
  <a href="docs/mass-email.md">Emails to users</a> ·
  <a href="#security">Security</a> ·
  <a href="#django-52">Django 5.2</a> ·
  <a href="MODIFICATIONS.md">Changelog</a>
</p>

<p align="center">
  <a href="docs/screenshots/public-scoreboard.png"><img src="docs/screenshots/public-scoreboard.png" alt="The public scoreboard of a contest in progress: a podium, a balloon per problem and the latest submissions"></a>
</p>

Run individual or team contests, on site and online at once, with registration
before the start, a public live scoreboard, frozen standings, a reveal ceremony
and balloon delivery. Hold 1v1 duels and knockout tournaments on a projector, and
write to your users from the admin. This fork is maintained by **CPC-UAEH**, the
Club de Programación Competitiva UAEH, and runs at [cpcjudge.com](https://cpcjudge.com).
It builds on [DMOJ](https://github.com/DMOJ/online-judge), which provides
statements in Markdown and LaTeX, live results, several contest formats, virtual
participation and many languages through the
[judge server](https://github.com/DMOJ/judge-server).

*The screenshots use fictional demo accounts, teams and contests, and show the
Spanish interface; English is included too. Click a picture to see it full size,
or browse the [gallery](docs/screenshots/README.md).*

## Contests

<table>
<tr>
<td width="50%" valign="top">
<b>Public scoreboard</b><br>
<a href="docs/screenshots/public-scoreboard-compare.png"><img src="docs/screenshots/public-scoreboard-compare.png" width="380" alt="The Compare tab of a finished contest, charting two contestants"></a><br>
A live page for anybody, without an account: a podium, a balloon per problem
and the latest submissions, following the freeze. After the contest, Compare
charts how two contestants went up. Also for private and hidden contests.
</td>
<td width="50%" valign="top">
<b>Registration before the start</b><br>
<a href="docs/screenshots/contest-list.png"><img src="docs/screenshots/contest-list.png" width="380" alt="The contest list with a registration and the countdown in the navigation bar"></a><br>
Register from the contest list or page; the access code is asked once. A pill in
the navigation bar counts down, then shows the time left, and at the start a
short prompt asks whether to enter.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>On site and online</b><br>
<a href="docs/screenshots/contest-registered.png"><img src="docs/screenshots/contest-registered.png" width="380" alt="The Registered tab of an on site and online contest, seen by an organizer"></a><br>
One contest with Global, On site and Online standings. Organizers register
people and teams from the Registered tab, mark who is on site and choose who
gets a rating.
</td>
<td width="50%" valign="top">
<b>Teams</b><br>
<a href="docs/screenshots/teams-my-teams.png"><img src="docs/screenshots/teams-my-teams.png" width="380" alt="My teams with members and a pending invitation"></a><br>
Teams with invitations, and individual, team or mixed contests with separate
rankings. Members submit from their own accounts and share the team's score and
clock.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>Frozen scoreboards</b><br>
<a href="docs/screenshots/scoreboard-freeze.png"><img src="docs/screenshots/scoreboard-freeze.png" width="380" alt="Team scoreboard during the freeze window"></a><br>
Results stay hidden everywhere during the freeze: standings, submissions,
statistics, the API and live updates. Organizers release them.
</td>
<td width="50%" valign="top">
<b>Announcements and clarifications</b><br>
<a href="docs/screenshots/announcements.png"><img src="docs/screenshots/announcements.png" width="380" alt="Contest announcements and an answered clarification"></a><br>
Announcements pop up inside the contest at once. Contestants ask about a
problem; the jury answers privately or for everyone.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>Reveal ceremony</b><br>
<a href="docs/screenshots/reveal-ceremony.png"><img src="docs/screenshots/reveal-ceremony.png" width="380" alt="The reveal ceremony halfway through, with pending cells to reveal"></a><br>
Reveal the final standings step by step, with medals, ties and first-to-solve
awards, for contestants or teams.
</td>
<td width="50%" valign="top">
<b>Balloon delivery</b><br>
<a href="docs/screenshots/balloon-desk.png"><img src="docs/screenshots/balloon-desk.png" width="380" alt="Balloon desk with problem colors, delivery status and locations"></a><br>
Pending and delivered balloons, problem colors, seat locations and undo, behind
their own staff permission.
</td>
</tr>
</table>

Also: every statement of a contest in one PDF ([pdfoid](docs/pdf.md)),
rankings that refresh themselves, and contest clones that keep their problems.
See the [contest guide](docs/contest-tools.md).

## Duels

First-to-solve battles between two players, alone or as best-of-N series, and
knockout tournaments of up to 64 places.

<table>
<tr>
<td width="50%" valign="top">
<b>Organizer panel</b><br>
<a href="docs/screenshots/duels-panel.png"><img src="docs/screenshots/duels-panel.png" width="380" alt="The duels panel with a battle and its next step"></a><br>
Every battle shows one button with its next step. Organizers call the players
and pick the problems by hand or from a problem bank.
</td>
<td width="50%" valign="top">
<b>Tournaments</b><br>
<a href="docs/screenshots/duels-tournament.png"><img src="docs/screenshots/duels-tournament.png" width="380" alt="The public page of a tournament with its bracket"></a><br>
A bracket that builds itself, public pages that follow the games live, and a
Glicko-2 ranking of the battles that count.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>Players</b><br>
<a href="docs/screenshots/duels-player.png"><img src="docs/screenshots/duels-player.png" width="380" alt="A player during a duel: statement, editor, custom input and submissions"></a><br>
Players stay on the duel page and submit from it until the battle is decided.
A round can start with a C++ template already written.
</td>
<td width="50%" valign="top">
<b>Projectors</b><br>
<a href="docs/screenshots/duels-projector.png"><img src="docs/screenshots/duels-projector.png" width="380" alt="A projector at the end of a game, with the winner"></a><br>
Paired with a code, a projector shows both players' code live, the scoreboard,
the statements and the bracket.
</td>
</tr>
</table>

See the [duels guide](docs/duels.md) for the setup, the rules of a game and who
organizes what.

## Problems and the editor

<table>
<tr>
<td width="50%" valign="top">
<b>Autocomplete</b><br>
<a href="docs/screenshots/editor-autocomplete.png"><img src="docs/screenshots/editor-autocomplete.png" width="380" alt="The submit editor suggesting while typing C++"></a><br>
Submit, custom tests and duels suggest as you type, including whole
<code>#include</code> lines in C and C++. Each contest or duel round decides
whether it is allowed.
</td>
<td width="50%" valign="top">
<b>Interactive problems</b><br>
<a href="docs/screenshots/interactive-problem.png"><img src="docs/screenshots/interactive-problem.png" width="380" alt="Problem data page with a testlib interactor"></a><br>
Upload an interactor next to the test data. Testlib is recognized and
<code>testlib.h</code> supplied, with a <a href="docs/interactive-problems.md">guide</a>
and a complete example.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>Sample tables and math</b><br>
<a href="docs/screenshots/sample-table.png"><img src="docs/screenshots/sample-table.png" width="380" alt="A statement with two sample tables"></a><br>
Samples written with <code>||input</code> and <code>||output</code> show as a
compact table with a copy icon, and inline formulas also accept
<code>$...$</code>. See <a href="docs/statements.md">the syntax</a>.
</td>
<td width="50%" valign="top">
<b>Test cases from ZIP archives</b><br>
<a href="docs/screenshots/testcase-autofill.png"><img src="docs/screenshots/testcase-autofill.png" width="380" alt="Problem data form populated from a sample ZIP archive"></a><br>
Input and output files pair themselves by extension or folder, in natural
order, with batches and points filled in.
</td>
</tr>
</table>

Also: a roomier [custom test](docs/screenshots/custom-test.png) editor with
output up to 64 KB, kept out of public histories and statistics.

## Administration and email

<table>
<tr>
<td width="50%" valign="top">
<b>Emails to users</b><br>
<a href="docs/screenshots/mass-email-preview.png"><img src="docs/screenshots/mass-email-preview.png" width="380" alt="An email to users previewed as a desktop and a phone would show it"></a><br>
Write one email in the admin and send it through Resend to everyone, to filtered
audiences or to people chosen by name. Whoever writes asks for approval; an
approver reviews and sends. See <a href="docs/mass-email.md">Emails to users</a>.
</td>
<td width="50%" valign="top">
<b>Light and dark themes</b><br>
<a href="docs/screenshots/dark-mode.png"><img src="docs/screenshots/dark-mode.png" width="380" alt="A frozen scoreboard, half in the light theme and half in the dark one"></a><br>
Every account chooses light, dark or the system's theme. Phones get a stable
480 px layout, and account emails share one design that reads well everywhere.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<b>Teams in the admin</b><br>
<a href="docs/screenshots/admin-teams.png"><img src="docs/screenshots/admin-teams.png" width="380" alt="Team administration with owners, members and participation counts"></a><br>
Teams, invitations and memberships, searchable by team or member, with their
contest participations.
</td>
<td width="50%" valign="top">
<b>Protected owner accounts</b><br>
<a href="docs/screenshots/admin-owner-lock.png"><img src="docs/screenshots/admin-owner-lock.png" width="380" alt="Owner-only deletion confirmation listing affected records"></a><br>
Accounts in <code>CPC_SERVER_OWNERS</code> control deletion, which lists what
goes before confirming; other superusers cannot edit them.
</td>
</tr>
</table>

## Security

Protections in the source include protected owner accounts, impersonation only
for superusers and logged, private custom tests with rate limits, jury tools
scoped to their contests, an upload check before large request bodies, email
limits and an event daemon that survives malformed messages. The
[deployment examples](deploy/examples/README.md) and
[security guides](docs/security/README.md) cover what each installation must
configure, and the known limitations.

<details>
<summary>All protections and deployment controls</summary>

### Application protections

- **Protected owner accounts:** owner-only deletion and sensitive account
  administration, including protection from changes by other superusers.
- **Restricted impersonation:** superuser-only access, protected superuser
  accounts and audit logging.
- **Private custom tests:** signed ownership checks, read-only result polling
  and exclusion from public histories, APIs and statistics.
- **Abuse controls:** configurable admission and rate checks for custom tests,
  plus rate and length limits for contestant clarification requests.
- **Controlled deletion:** explicit confirmations list the records affected by
  permanent team or contest deletion; submission records are preserved.
- **Scoped jury tools:** jury members can only file or move clarifications into
  contests they can edit, and answers change visibility together with their
  announcements.
- **Upload check:** Nginx asks the application whether a caller may upload
  problem data before accepting a large request body.
- **Live updates that stay up:** the event daemon answers malformed messages
  with an error instead of ending the process.
- **Email limits:** two password reset emails per address and one email change
  per account in 24 hours, a daily cap for the whole site with a reserve for
  existing accounts, and no account is created if its activation email cannot be
  sent.

### Deployment controls

The deployment examples and security guides provide configuration for:

- separate unprivileged service accounts, systemd isolation and read-only code;
- authenticated Redis and private service listeners;
- secure session and CSRF cookies, edge HSTS, a structural Content Security
  Policy and referrer policy;
- the visitor's real address behind a tunnel, so per-IP limits count each visitor;
- request-size limits, a permission check before large uploads, on-disk request
  buffering, disabled directory listings and a private event-publishing endpoint;
- protected configuration and backups, and scheduled cleanup of finished custom
  tests.

Application protections are included in the source; deployment controls must be
configured for each installation.

</details>

## Django 5.2

This fork runs on **Django 5.2.17**, pinned in [`requirements.txt`](requirements.txt),
with the tested django-mptt and django-compressor versions. It needs Python 3.10
or newer. The
[upgrade guide](docs/django52/README.md) covers the code changes, the settings
and how to upgrade an existing installation.

## Installation and documentation

Install it like DMOJ, cloning this repository instead of DMOJ's; do not install
DMOJ first and switch. [Setting up this fork](docs/setup-guide.md) lists the
differences, then covers settings, migrations, translations, static assets,
services, scheduled cleanup and your own branding.

| Guide | Contents |
| --- | --- |
| [Setup guide](docs/setup-guide.md) | Installation, configuration and verification |
| [Fork overview](docs/fork-overview.md) | Every change relative to upstream |
| [Django 5.2 upgrade](docs/django52/README.md) | Compatibility changes, tested dependencies and upgrade guidance |
| [Contest tools](docs/contest-tools.md) | Registration, on site and online, freeze, reveal, balloons, announcements, clarifications and the public scoreboard |
| [Interactive problems](docs/interactive-problems.md) | Interactors from the data page, judge requirements and verdicts |
| [Duels](docs/duels.md) | Battles, tournaments, projectors, who organizes what, settings and limitations |
| [Emails to users](docs/mass-email.md) | Announcements to many accounts through Resend: audiences, sending, unsubscribes, permissions |
| [Sample tables and math](docs/statements.md) | The `\|\|input` table and `$...$` in statements |
| [PDFs](docs/pdf.md) | Problem and contest PDFs with pdfoid |
| [Deployment examples](deploy/examples/README.md) | Example service units, Nginx settings and maintenance tasks |
| [Security guides](docs/security/README.md) | How to configure each control, and known limitations |
| [Screenshot gallery](docs/screenshots/README.md) | Every demonstration image, full size |

## Upstream and license

Based on DMOJ commit `97f3722ef3f7ca9731727c220622bdd1eab7d4b3`.
Distributed under the [GNU Affero General Public License v3](LICENSE).
See [MODIFICATIONS.md](MODIFICATIONS.md) for the fork's changes and attribution.

Production credentials, user data, problem packages, uploaded media, logs and
generated static files are excluded from this repository.

Upstream resources: [DMOJ](https://github.com/DMOJ/online-judge) ·
[Documentation](https://docs.dmoj.ca/) ·
[Judge server](https://github.com/DMOJ/judge-server) ·
[Community](https://dmoj.ca/about/discord/).
