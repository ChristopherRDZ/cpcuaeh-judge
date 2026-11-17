# What this fork adds

Every change this fork makes to upstream DMOJ, grouped by area, with the date
each one was added.

## Contests

| Change | Notes |
| --- | --- |
| Scoreboard freeze | Freezes the board, participations, submission lists and details, live updates, statistics, per-problem counters and the API. Your own row marks results the board is not showing. Migration `0153`. 2026-09-16 |
| Reveal ceremony | ICPC-style reveal, adjustable speed, medal ranges, ties, first-to-solve, award cards for contestants and teams. 2026-09-16 |
| Balloon desk | Pending and delivered lists, undo, per-problem colors, per-participant locations, staff permission. Migration `0154`. 2026-09-16 |
| Announcements and clarifications | Site-wide or per-contest, plain text, rate limited; public answers are sent as announcements and move, update or withdraw with their question. Migrations `0150`–`0152`. 2026-09-15 |
| Self-refreshing rankings | Polls only while the tab is visible, backs off, stops after the contest. 2026-09-15 |
| Resilient live updates | Heartbeat in the event daemon, exponential backoff in the browser, long-polling fallback. 2026-09-15 |
| Contest cloning | A clone keeps the freeze settings but starts unrevealed. 2026-09-21 |
| Clones with their problems | A cloned contest keeps its problems, which the copy used to lose, and starts without a public scoreboard. 2026-11-10 |
| Removing contest problems | Contest authors can remove a problem from a contest they edit; the problem and its submissions stay. The only exemption from the owner-only deletion lock. 2026-10-07 |
| Contest PDF | Every statement of a contest in one PDF, for participants once it started and for organizers at any time; cached by content, rendered once at a time. 2026-10-19 |
| Registration before the start | Register from the contest list or page; the access code is asked once. At the start a prompt on any page asks whoever registered whether to enter; nobody enters alone, and joining without registering still works. Migration `0162`. 2026-10-31 |
| On site and online | Global, on-site and online standings; a Registered tab to assign people, with an on-site list for those not registered yet; balloons only on site and the reveal per view. Migration `0162`. 2026-10-31 |
| Contest clock in the navigation bar | A pill on Contests counts down to a contest you registered for and shows the time left inside one; the floating timer box is gone. 2026-10-31 |
| Announcements right away | Inside a contest, announcements and answers pop up at once through a text-free ring of the event daemon, titled with the contest's name. Authors and curators post them from the Clarifications tab and no longer ask questions. 2026-11-01 |
| Registering others | The Registered tab of every contest registers accounts and teams ahead, also after the start, without the access code; teams through a page of their members. Organizers remove registrations that have not entered. Migration `0164`. 2026-11-05 |
| Rating by attendance | On site and online contests choose who gets a rating: everybody, on site only or online only. Migration `0164`. 2026-11-05 |
| Public scoreboard | `/marcador/<key>/` for anybody, without an account, in contests that tick its box, also private or hidden ones: a podium, a balloon per problem and the latest submissions, live and following the freeze; problems only by their letter. It shows usernames, names and profile pictures. Fixed Spanish text. The contest list and the contest's page link to it. Migration `0168`. 2026-11-10 |
| Comparing on the public scoreboard | Once a contest is over and unfrozen, a Compare tab charts how two rows went up. Letters link to problems that are public; the list of submissions follows the tab; a new time bar. 2026-11-15 |

## Duels

| Change | Notes |
| --- | --- |
| 1v1 duels | First-to-solve battles, alone or as best-of-N series, with partial or all-or-nothing points and optional penalties. At `/duelos/`; `/arena/` forwards there. Migration `0157`. 2026-10-17 |
| Tournaments | Knockout brackets of up to 64 players, seeded by hand, at random or by rating, with an optional third-place match; problems by hand or from problem banks. 2026-10-17 |
| Organizer panel | Behind `judge.manage_arena` (see Own and all); versioned actions so several organizers can work at once; a guide in Spanish. 2026-10-17 |
| Player lock | Players stay on the duel page and submit only from it while their battle lasts; expires after a period without activity. 2026-10-17 |
| Projector screens | Secret, revocable links with combined, scoreboard, live code, statements and bracket views. 2026-10-17 |
| Duel contests out of the lists | The hidden contest behind each game stays out of `/contests/` and the admin's contest lists, and is deleted with its game. 2026-10-19 |
| Own and all | `manage_arena` now covers one's own battles and the ones where one is a co-organizer; `manage_all_arena` covers every battle. Co-organizers per tournament and quick battle, screens and problem banks per organizer. Migration `0159`. 2026-10-23 |
| Public history | Pages for started and upcoming tournaments at `/duelos/torneos/`, live while they are played; code, submissions and screens stay private. Migration `0160`. 2026-10-26 |
| Ranking | Glicko-2 at `/duelos/ranking/`; only `decide_arena_ranking` decides what counts. Migration `0161`. 2026-10-27 |
| Panel by next step | Tabs Now, Quick battles, Tournaments, Projectors and Problem banks; each battle shows one button with its next step. 2026-11-04 |
| Tournaments by places | Created with a number of places, from 2 to 64; problems before names. Migration `0163`. 2026-11-04 |
| Paired projectors | A projector opens `/proyector` and the organizer types its code; keys 1 to 6 switch the view; what ended stays 30 minutes. Migration `0163`. 2026-11-04 |
| Cancel a tournament | Voids what was not decided, frees the players, leaves the history and never counts. Projectors stay on a battle between its games. Migration `0166`. 2026-11-06 |
| C++ template per round | A Template column in Rounds and problems, and a box in quick battles; the editor opens in C++ with a minimal program. Battle settings save only what changed. 2026-11-08 |

## Teams

| Change | Notes |
| --- | --- |
| Teams and invitations | Team model, membership, invitations, My teams page. Migration `0155`. 2026-09-18 |
| Individual, team and mixed contests | Participation mode per contest, separate rankings. 2026-09-18 |
| One access code per roster | Whoever registers carries contest access for the whole roster; personal blockers are still checked one by one. 2026-09-19 |
| Member selection | Pick who competes when registering, with the minimum and maximum measured on that selection. 2026-09-19 |
| Teams in the admin | `Team` and `TeamInvitation` registered, search by member, inline membership editing; a new owner is added to the members automatically. 2026-09-19 |

## Interface

| Change | Notes |
| --- | --- |
| Dark theme for everyone | No longer gated behind the `test_site` permission; `auto` follows the system. 2026-09-18 |
| Rebuilt custom test | Full-width editor, no line wrapping, output up to 64 KB, translated messages. 2026-09-18 |
| Translated navigation bar | Through a `dmoj-user` catalog that did not exist upstream; the `navbar` fixture uses its labels and includes Duels. 2026-09-19 |
| Test case autofill | Pairs files from a zip, natural ordering, batch detection, point distribution. 2026-09-19 |
| Interactive problems | Interactor upload on the data page, `interactive:` written on every save, testlib detection with a bundled `testlib.h`, optional contestant feedback, a guide with a downloadable example. Migration `0156`. 2026-09-25 |
| Data archive replacement | Uploading an archive over an existing one lost both upstream; the new one is now stored and the old one removed. 2026-09-25 |
| Admin bulk selection | The bottom action bar of admin lists works again with Django 5.2 and stays in sync with the top one. 2026-09-20 |
| Sample tables | OmegaUp's `\|\|input` / `\|\|output` / `\|\|description` / `\|\|end` in statements, translations and editorials, as a compact table with its own style. 2026-10-18 |
| Inline math with dollars | `$...$` next to `~...~` in the same styles, with Pandoc's rules so prices stay text. 2026-10-18 |
| Phone layout | Phones get the 480 px layout from the first paint. Upstream switched only on `resize` and, at about one width in five, kept flipping between layouts. 2026-10-01 |
| Phone overflow fixes | MathJax formulas no longer make pages pan sideways, the contest timer keeps its clock inside the box, and the problem data page fits with its case table scrolling on its own. 2026-10-01 |
| Readable permission selector | The admin's two-list selector shows full permission names at any width. 2026-10-07 |
| Account emails | One design for activation, password reset and email change, readable in Gmail, Outlook and phone apps. 2026-10-08 |
| Email footer | Every email says the club is independent and not an official service of its university; emails to users link the privacy notice. 2026-10-24 |
| Back in read-only admin forms | The bottom bar of a form that can only be viewed was black and empty; it now offers Back, keeping the list filter. 2026-10-24 |
| Site icons | A multi-size favicon, and the site's own artwork in the Windows tile and browser notifications. 2026-10-08 |
| Autocomplete in the editor | Submit, custom tests and duels suggest words of the code, keywords and, in C and C++, `#include` lines; Tab picks and Enter always starts a new line. On in practice; each contest, duel tournament, round or quick battle allows it. Ace 1.43.3. Migration `0167`. 2026-11-09 |
| Spanish interface | Spanish translations for every screen the fork adds. |

## Emails to users

| Change | Notes |
| --- | --- |
| Emails to users | A section of the admin to write one email for many accounts and send it through Resend Broadcasts, never through the site's SMTP: drafts, desktop and phone preview, a test to oneself, confirmation by typing the number of recipients, sending now or scheduled, delivery figures and unsubscribes. Migration `0158`. 2026-10-21 |
| Audiences | Everyone with a confirmed email, those matching filters (contests, organizations, groups, activity, sign-up dates, solved problems, rating), or people chosen by name; "never include" always wins. 2026-10-23 |
| Own and all | `send_mass_email` covers one's own emails, `edit_all_mass_email` all of them. Unsubscribed addresses are masked for non-superusers. 2026-10-23 |
| Approval | Whoever writes asks for approval; `approve_mass_email` reviews, corrects, sends back or sends. Migration `0165`. 2026-11-05 |
| Senders and networks | Senders of the site's domain chosen per email, and social links with icons at the foot of every email. 2026-11-05 |
| Pasted addresses | Addresses from a form added to the audience, without a name and never revealed to whoever only writes; a cap on the contacts kept in Resend. Migration `0165`. 2026-11-05 |

## Accounts

| Change | Notes |
| --- | --- |
| Registration fields | First and last name are required when registering. |
| Login by email | Sign in with the username or the email address; if several accounts share an address, the password decides which one. |
| Email limits | Two password reset emails per address and one email change per account in 24 hours, and a daily cap per UTC day with a reserve for existing accounts. 2026-10-14 |
| No stuck sign-ups | The activation email is sent inside the registration transaction; if it fails, no account is created and the address can register again. 2026-10-14 |

## Security

| Change | Notes |
| --- | --- |
| Owner account | `CPC_SERVER_OWNERS` decides who may delete from the admin and protects that account from other superusers. 2026-09-19 |
| Permanent deletion path | Dismantles the five `PROTECT` keys in order inside a transaction, after a confirmation that counts what disappears. Submissions are kept. 2026-09-19 |
| Impersonation fix | Upstream's `IMPERSONATE_REQUIRE_SUPERUSER` is ignored by the installed library, which left impersonation open to every staff account. 2026-09-19 |
| Clarification scope in the admin | Jury members can only file or move clarifications into contests they can edit, and a problem must belong to the same contest. 2026-09-21 |
| Custom test ownership | Signed ownership checks, read-only result polling, exclusion from shared histories and statistics. 2026-09-08 |
| Custom test ceilings | Two in flight, twelve per minute, two hundred per hour, per user, answered with HTTP 429. 2026-09-14 |
| Request body ceilings | 16M globally, 2M for custom tests, 500M only on the problem data upload route. 2026-09-19 |
| Upload check before buffering | Nginx asks the application whether the caller may upload problem data before reading the body. 2026-09-21 |
| Service isolation | One unprivileged account per service with systemd namespace restrictions. 2026-09-10 |
| Transport and origin | Redis authentication, loopback-only listeners, HSTS, structural CSP, referrer policy, secure cookies. 2026-09-11 |
| Visitor address behind a tunnel | Nginx takes the edge's header, from loopback only, so per-IP limits count each visitor. 2026-10-14 |
| Error report cap | Error emails to `ADMINS` are capped per day and never use the reserve that existing accounts need. 2026-10-14 |
| Event daemon input checks | Malformed messages, frames or long-polling URLs get an error instead of ending the daemon; message size caps. 2026-10-17 |

## Operations

| Change | Notes |
| --- | --- |
| Scheduled custom-test cleanup | Removes finished tests from every account, not only from whoever submits next. 2026-09-19 |
| Offline compression | The guides explain what invalidates the manifest, for installations that use it. 2026-09-14 |
| Django 5.2.17 | Upgrade with timezone, storage and dependency adaptations. 2026-09-12 |
| Reliable problem PDFs | A failed render no longer leaves an empty PDF in the cache; the PDF takes its title from the page; a patch and a unit for pdfoid. 2026-10-19 |

Git history and file-level diffs are the authoritative record.
See [MODIFICATIONS.md](../MODIFICATIONS.md) for the licensing statement.
