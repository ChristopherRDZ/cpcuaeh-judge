# Contest tools: registration, freeze, reveal, balloons, clarifications and the public scoreboard

Configure these tools on the contest's administration page. The contest tabs link
to the tools the signed-in user is allowed to use.

## Registration before the start

Every contest takes registrations before it starts. The upcoming contests list
shows **Register** or **Registered** and how many people registered. In an
individual contest without an access code, registering takes one click;
otherwise the button opens the registration page, where the team, its roster and
the access code are chosen. The access code is asked once, when registering, and
not again at the start.

Before the start, the contest page tells you whether you are registered and lets
you **Cancel registration**. Whoever registers a team sets its name and roster; to
change them, cancel and register again. The others on the roster can **Withdraw
from the roster**. Whoever registered the team cannot leave it, or be removed
from it, without cancelling first.

Nobody enters by themselves. When the contest starts, whoever registered gets a
short prompt on any page of the site asking whether to enter now, with **Enter**
and **Not now**. In a contest with a per-participant window it says how long
their time is and offers **Start** and **Later**; the time runs from then. Not
now is remembered in that browser, and they enter later from the contest page or
the contest list. Inside another contest, the prompt says they would leave it,
and only shows within an hour of the start. The prompt opens at second zero
without reloading and never takes the keyboard from someone who is typing. It
does not show on the duels pages.

Whoever did not register joins as always, also after the start.

A registration is a participation with `virtual = -2`. Until it enters, it stays
out of the rankings, the API, the reveal, balloons, rating, statistics and the
contest's user count. When people register themselves, the access code, the
private participants and the organizations allowed to join are checked then and
not again at the start, so set them up before people register.

In the navigation bar, a pill on **Contests** counts down the last hour before a
contest you registered for, and then shows the time left while you are in a
contest, in red in the last five minutes. It replaces the floating timer box.

## The Registered tab

The **Registered** tab (`/contest/<key>/registrations`) is open to whoever can
edit the contest, in every contest, also before the start. It shows how many
registered and how many already entered, with a search, a filter by state and
pages of 100.

The field above the table, **Register** (one per modality in an on site and
online contest, see below), registers accounts and teams ahead, also after the
start and until the contest ends. It searches usernames and teams like
the testers field of the admin, and also takes a pasted list of usernames and
team names; a team that is not registered is suggested only when its whole name
is typed. Individual contests offer no teams, and the search only shows
usernames. The notice says who was registered, why the others were not, who does
not exist and which name is ambiguous.

Whoever registers people from this tab answers for them: no access code is
asked, and the registration opens a private contest, or one limited to
organizations, to them while it lasts. A hidden contest stays hidden until it is
made visible; the notice says so. The rest is still checked: an active account,
no ban from the contest, not being one of its authors, curators or testers, and
one registration per person.

Teams go through a second page that lists their members, all ticked; whoever
cannot compete is left unticked, with the reason. Each team must stay within the
contest's minimum and maximum size, and a team with nobody ticked is not
registered. The registration stands under the team's owner if they compete, or
else under the member who joined the team first: they can **Cancel
registration**, and the others can **Withdraw from the roster**.

Nobody registered this way enters by themselves either: at the start they get
the same prompt as everybody. Their contest page and registration page say **The
organizers registered you.** No email is sent. In the table, their registration
is marked **by the organizers**.

Tick rows and **Remove registration** to take away registrations that have not
entered, after a confirmation. Those who entered stay, and the notice says how
many. Whoever was removed can register again, with the access code if there is
one. Every registration and removal is written to the site's log.

## On site and online

A contest is **Online only**, **On site only** or **On site and online**; every
contest created before this option is online only. Only **On site and online**
changes anything: it splits the standings and the balloons. In the other two,
everybody competes together.

In these contests the Registered tab has **Online** and **On site** tabs, each
with its count (it opens on Online). The field of each tab, **Add online** or
**Add on site**, registers in that modality whoever is not registered yet and
moves there whoever is; once the contest has ended, it only moves. A registered
team also goes by the username of anyone on its roster. Tick rows and move them
to the other tab, after a confirmation. The notice says who was moved.

The contest keeps an **on-site list** that follows every move: whoever is on it
starts on site when they register or join, also after the start. Those on it
without a registration, such as someone who cancelled theirs, show under the
field as **Not registered yet**; × takes them off the list. Every move is
written to the site's log.

The ranking shows **Global**, **On site** and **Online** above the table. Each
view numbers its rows from 1 and shows the global place in grey, frozen or not.
The reveal ceremony picks a view and computes first to solve and medals within
it; **Publish** unfreezes every view.

Balloons go only to those on site. If someone is moved on site in the middle of
the contest:

- the balloons for what they had already solved show as pending, and the
  Registered tab says how many;
- first to solve is whoever was really first among those on site; when other
  balloons of that problem were already delivered, the desk says so;
- a contest with seats shows **No seat assigned** until they get one.

Someone moved online by mistake stops getting balloons; the ones already
delivered stay on the desk marked **No longer on site**, to undo or keep. Once a
contest has started with people inside, it can no longer drop **On site and
online**; it can still be turned on.

**Rated participants**, in the Rating part of the contest form, chooses who of an
on site and online contest gets a rating: **All** (the default), **On site only**
or **Online only**. Other contests rate everybody, and the form refuses the other
two choices there. When it is not All, the contest page says so in its details,
which show only with **show short form settings display** ticked. If it changes
after the contest was rated, the form reminds you to rate it again. Without the
**Rate contests** permission the field is read-only.

## Frozen scoreboard

Set **freeze minutes** to hide the last part of the contest. During the freeze,
everyone except the contest's editors sees the standings as they were when the
freeze began, and the results after that point are also hidden from:

- participation pages and the ranking of each division;
- submission lists, submission details, best solutions and live updates;
- contest statistics, and the **AC rate** and **Users** of each problem, in the
  contest and in the site's problem list;
- user pages: points, performance points, solved problems and the activity map;
- the API, including the contest history in `/api/v2/user/<name>`.

Your own submissions are never hidden from you. On your own row, a **?** marks a
problem with a result the frozen board is not showing: a submission sent during
the freeze, one still being judged, or one sent just before the freeze whose
verdict arrived after it started. That last kind stays off the public board until
the reveal.

In contests with a per-participant time window, each participation freezes over
its own last minutes. The scoreboard stays frozen after the contest ends, until an
editor publishes it. Freezing does not change how a contest format calculates
points.

Once the freeze has started, its cutoff stays where it is until the reveal. If
you move the end of a contest without a per-participant window, the freeze
minutes change to keep the cutoff (rounded up to whole minutes) and the form says
so. Other changes that would move the cutoff (the start, the time limit, the
freeze minutes, removing the freeze, or an end before the cutoff) are refused
until the reveal. A contest that nobody has taken part in live hides nothing, so
its schedule can change freely.

A contest whose freeze has started cannot be rated until it is revealed: **Rate**
is hidden and **Rate all ratable contests** skips it. Freezing a rated contest
again removes its ratings until it is revealed and rated again.

Cloning a contest requires being able to edit it. The clone copies the freeze
minutes but starts unrevealed.

## Authors, curators and testers

A contest's authors, curators and testers are not contestants. If one of them
took part before getting the role, alone or on a team's roster, that
participation is left out of the scoreboard, the API, the balloon desk, the
ceremony awards and rating. It is kept, and counts again if they lose the role.
Virtual participations are not affected.

## Reveal ceremony

Editors can open **Reveal scoreboard** to present the final standings. The
ceremony uncovers hidden results from the bottom of the ranking up, animates
position changes, and supports manual control, reduced motion and a speed slider
from 0.25× to 4×.

It works for individual and team contests. In a mixed contest, choose the
individual or team division; in an on site and online contest, choose the global,
on-site or online view. Individual award cards show the contestant; team cards
show the registered members and their profile pictures.

Awards can use finishing places or medal ranges, respecting ties. First-to-solve
awards are independent of medals. Award cards pause the presentation. Settings are
stored per contest in the browser used for the ceremony.

**Publish** is a separate action that makes the real scoreboard public. Opening or
playing the ceremony does not publish anything. In a mixed contest, publishing
unfreezes both divisions. The admin also has **Reveal frozen scoreboards** and
**Freeze scoreboards again** actions.

A revealed scoreboard is frozen again only with **Freeze scoreboards again**; the
contest form refuses to do it. The form also refuses to reveal a scoreboard that
was frozen again after the form was opened. In both cases, open the page again
to save your other changes. Ticking **scoreboard revealed** on a freshly opened
form still reveals the scoreboard.

## Balloon desk

Set each problem's balloon color and color name, and assign **balloon staff** to
the contest. The color field has a picker, an editable hex value and **No color**.

The **Balloons** tab lists pending and delivered balloons, with the members of
each team, and an activity log. Only editors and balloon staff can open it.
Staff do not gain contest editing or submission access. If two volunteers
deliver the same balloon, the second gets a notice instead of a duplicate. Undo
is recorded in the log; nothing is erased.

A balloon is due for the first fully accepted, finally judged submission of an
official participation on a problem, within its contest window. Accepted
submissions made during the frozen part of a participation **never** earn a
balloon, not even after the reveal, so balloons cannot give away the frozen
results. A delivered balloon that later loses its AC stays on the list with a
warning. Select a problem to filter, or **All balloons** to see everything. In an
on site and online contest, only those on site get balloons (see
[On site and online](#on-site-and-online)).

### Participant locations

Editors see a row for each official entrant or team, each preassigned private
contestant or team, and anyone with a saved location. Fill in the location next
to the name; empty is allowed. Search by name; long lists use pages of 100. Save
before changing pages.

Only edited fields are saved, so rows on other pages are never overwritten. If
another editor changed the same rows meanwhile, the form reports a conflict and
keeps your input. Forms expire after 24 hours.

## Announcements and clarifications

**Announcements** are created in the admin, for the whole site (this needs the
*announce to the whole site* permission) or for one contest. Authors, curators
and whoever can edit every contest also write to a contest from its
**Clarifications** tab with **Post an announcement**, until it ends, without the
admin or its permission; announcements are withdrawn or corrected in the admin.
They are plain text, appear as a pop-up until their expiry time, and stay listed
on the contest's **Clarifications** tab. A contest announcement stops popping up
when the contest ends unless you set another time. To withdraw one without
deleting it, untick *is visible*.

The pop-up takes the name of the contest as its title. Inside a contest, a new
announcement or an answer pops up right away: saving it rings the contest's
channel in the event daemon, which carries no text, and the page asks the site
what is new. Without the event daemon, pages ask every minute.

**Clarifications:** participants and testers ask about the contest or a specific
problem from the **Clarifications** tab. Authors and curators do not ask: they
answer and post announcements. Questions are plain text, limited in length, and
limited per participant (three waiting for an answer, one every 30 seconds).
Team members share their team's questions and limits. While the contest runs,
the tab is only for those inside it; everybody else reads that clarifications
are published when the contest ends.

The jury answers from the same tab or from the admin:

- A **private** answer goes back only to whoever asked (their whole team, for a
  team).
- A **public** answer is sent to every participant of that contest as an
  announcement. The form shows the question as it will be published, to reword
  it or leave it out. Correcting the answer updates the same announcement;
  making it private withdraws it.
- To move a question to another contest, change the contest **on the
  clarification**; its announcement moves with it. Jury members can only use
  contests they can edit.
- Deleting a clarification also withdraws its announcement.

Editors of the contest see a counter of unanswered questions in the navigation
bar while they are in the contest.

## Public scoreboard

Tick **marcador público** (public scoreboard) in the Settings of a contest's
admin page, and anybody can follow the contest at `/marcador/<key>/`, without an
account: a podium, the table with a balloon per problem, and the latest
submissions. It works for private and hidden contests too, and it ignores
**scoreboard visibility**: the box alone decides. It shows each participant's
username, name and profile picture, so mention it in your privacy notice.
Problems appear only by their letter, and a letter opens the problem only for
whoever may open it on the site. Authors, curators, testers, disqualified
participants and virtual participations are left out.

The page updates by itself through the event daemon (on HTTPS it needs
`EVENT_DAEMON_GET_SSL`; without it, it asks every few seconds). During the
freeze it shows the frozen standings, and later submissions only as **?**. Once
the contest is over and nothing is frozen, **Comparar** (Compare) charts two
rows. With the box ticked, the contest list and the contest page link to it.
Clones start with the box off. The page is in Spanish only.

## Autocomplete

The editors of the submit page and the custom test suggest words of the code,
keywords and, in C and C++, whole `#include` lines; Tab or a click picks one.
Outside contests it is on. Inside a contest it is off unless **allow
autocompletion** is ticked in the Settings of its admin page. Each person can
still turn it off with the **Autocomplete** box next to the language.

## Applying the migrations

These tools need migrations `0150` to `0155`; registration and on site and
online contests, `0162`; registering others and **Rated participants**, `0164`;
autocomplete, `0167`; the public scoreboard, `0168`. Back up the database before
running them, compile the translation catalogs, and rebuild the offline
compression manifest if you use it. See the [setup guide](setup-guide.md).

Regression tests for the freeze, reveal and balloon code are in
[tests/contest_features](../tests/contest_features/README.md).
