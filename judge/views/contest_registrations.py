"""Whoever registered for a contest, and placing them on site or online (2026-10-31).

For whoever can edit the contest (is_editable_by), as the reveal and the seats of the balloons; everybody else is told
the page does not exist. It lists the official participations of the contest, the ones registered ahead and the ones
that entered (also without registering, once started), without whoever organizes or tests it today. In a contest that
is on site and online at once it moves people between the two, ticking their boxes or pasting a list of names;
anywhere else it is only the list.

The list of who competes on site (Contest.onsite_list) follows every move, and keeps whoever is pasted before they
register or join, so that they start on site when they do (the owner, 2026-10-31). Moving is idempotent and locks the
contest and then the rows, as join_attempt locks the contest before reading the list; recompute_results reads `onsite`
from the locked row, so a score saved at the same moment keeps the move. Only whoever organizes moves anybody: the
participant never chooses. Each move goes to the journal, with who made it, and the result says what it does to the
balloons: what was already solved is due at once, and what was delivered stays on the sheet, marked.
"""
import logging
import re

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext as _, ngettext
from django.views.decorators.http import require_GET, require_POST

from judge.models import Contest, ContestParticipation, Profile

__all__ = ['contest_registrations', 'contest_registrations_move', 'contest_registrations_search']

logger = logging.getLogger('judge.contest_registration')

PAGE_SIZE = 100
# What a pasted list may hold: plenty for a room, and a bound on the work of one request.
MAX_PASTE = 20000
MAX_NAMES = 2000
# How many names each line of the result lists before «…», and how much of each name it repeats.
SHOWN_NAMES = 30
NAME_CHARS = 60
# Lines separate names; tabs (columns copied from a spreadsheet), commas and semicolons too, unless the whole line is
# the name of a registration or, where teams compete, of an active team (a team called «Uno, dos»). A team name may
# carry spaces.
LINES = re.compile(r'[\r\n]+')
SEPARATORS = re.compile(r'[\t,;]+')
# Every place a pasted list may be split, to count its names before reading it.
TOO_MANY = re.compile(r'[\r\n\t,;]+')
# A chip picked from the suggestions: what it is (a registration, an account, a team), its key, and its name.
PICKED = re.compile(r'^([rpt])(\d{1,10})(?:\t(.*))?$')
STATES = ('registered', 'entered')
# What a move leaves for the page it goes back to, per contest: two tabs on two contests do not mix their notices.
NOTICES = 'registration_notices:%d'
# How the rows of the list of who competes on site without being registered are named in the form.
WAITING = re.compile(r'^([pt])(\d{1,10})$')
# The suggestions of the field that adds to the list of who competes on site (2026-11-01), of each kind.
SUGGESTIONS = 10
MAX_TERM = 100


def _contest(request, key):
    contest = get_object_or_404(Contest, key=key)
    if not contest.is_editable_by(request.user):
        raise Http404()
    return contest


def entrants(contest):
    """The official participations of the contest: registered ahead, or entered, without whoever organizes or tests
    it today. Without the registrations that wait in vain: individual ones of accounts no longer active, as from the
    number everybody sees (a team whose registrant left stays: the rest of its roster still enters), and those whose
    person or team already competes from another participation, which only the code before this change can leave
    (contest_registration.superseded)."""
    live = ContestParticipation.objects.filter(contest_id=contest.pk, virtual=ContestParticipation.LIVE)
    overtaken = (Q(Exists(live.filter(Q(team__isnull=True, user_id=OuterRef('user_id')) |
                                      Q(team_roster__profile_id=OuterRef('user_id')))), team__isnull=True) |
                 Q(Exists(live.filter(team_id=OuterRef('team_id'))), team__isnull=False))
    return (contest.users.official().without_contest_staff()
            .exclude(virtual=ContestParticipation.REGISTERED, team__isnull=True, user__user__is_active=False)
            .exclude(Q(virtual=ContestParticipation.REGISTERED) & overtaken))


def waiting_on_site(contest):
    """Who is on the list of who competes on site without any registration in the contest yet: [{'key': 'p<profile>'
    or 't<team>', 'name', 'team'}], by name. Whoever is on the roster of a registered team counts as registered."""
    from judge.models import Team
    from judge.models.team import ContestTeamMember
    profiles, teams = contest.onsite_designations()
    if not profiles and not teams:
        return []
    official = contest.users.official()
    registered = set(official.filter(team__isnull=True, user_id__in=profiles).values_list('user_id', flat=True))
    registered |= set(ContestTeamMember.objects.filter(participation__in=official, profile_id__in=profiles)
                      .values_list('profile_id', flat=True))
    registered_teams = set(official.filter(team_id__in=teams).values_list('team_id', flat=True))
    rows = [{'key': 'p%d' % pk, 'name': name, 'team': False} for pk, name in
            Profile.objects.filter(pk__in=profiles - registered).values_list('id', 'user__username')]
    rows += [{'key': 't%d' % pk, 'name': name, 'team': True} for pk, name in
             Team.objects.filter(pk__in=teams - registered_teams).values_list('id', 'name')]
    return sorted(rows, key=lambda row: (row['name'].casefold(), row['key']))


def _query(query='', modality='', state='', page=None):
    """The query string that keeps the search, the filters and the page, or ''."""
    kept = [(key, value) for key, value in (('q', query), ('modality', modality), ('state', state), ('page', page))
            if value]
    return '?' + urlencode(kept) if kept else ''


def _back(contest, params):
    url = reverse('contest_registrations', args=[contest.key])
    return HttpResponseRedirect(url + _query(params.get('q', '')[:150], params.get('modality', ''),
                                             params.get('state', ''), params.get('page', '')))


def _short(name):
    return name if len(name) <= NAME_CHARS else name[:NAME_CHARS - 1] + '…'


def _listed(names):
    # A team called «Uno, dos» goes between quotes, or «beto, Uno, dos» reads as three names (2026-11-01).
    names = sorted({'«%s»' % _short(name) if ',' in name else _short(name) for name in names},
                   key=lambda name: name.strip('«»').casefold())
    shown = ', '.join(names[:SHOWN_NAMES])
    return shown + ('…' if len(names) > SHOWN_NAMES else '')


def resolve_names(contest, text):
    """The participations named in a pasted list: usernames, team names, or the username of anybody on a team's
    roster, today's or the one it had when registering, and a team by the name it registered with or the one it has
    today. Of the names that match no registration, the active accounts they name, without whoever runs or tests the
    contest, and, where teams compete, the active teams, for the list of who competes on site. Returns (participation
    ids, {name: profile id} of accounts not registered, {name: team id} of teams not registered, names that match
    nothing, names that are not moved because they say more than one thing: more than one registration or team, a
    registered team and somebody's username, or a person and an active team they are not on)."""
    from judge.models import Team, TeamMembership
    from judge.team_services import team_name_key
    rows = list(entrants(contest).select_related('user__user', 'team')
                .prefetch_related('team_roster__profile__user'))
    # The username a roster kept from when it registered names the team only while no other account has it today
    # (2026-11-01): once somebody else is called so, the name is theirs.
    renamed = {member.username for row in rows if row.team_id for member in row.team_roster.all()
               if member.username.casefold() != member.profile.user.username.casefold()}
    held = {}
    if renamed:
        for pk, name in Profile.objects.filter(user__username__in=renamed).values_list('id', 'user__username'):
            held.setdefault(name.casefold(), set()).add(pk)
    # And who each username of a registration is, for the teams called as a person (below).
    by_username, by_team, person = {}, {}, {}
    for row in rows:
        if row.team_id:
            # By the name it registered with, and by the one it has today, if it was renamed since (2026-11-01).
            for name in {row.team_name, row.team.name}:
                by_team.setdefault(team_name_key(name), set()).add(row.pk)
            for member in row.team_roster.all():
                for name in {member.username, member.profile.user.username}:
                    if held.get(name.casefold(), set()) - {member.profile_id}:
                        continue
                    by_username.setdefault(name.casefold(), set()).add(row.pk)
                    person[name.casefold()] = member.profile_id
        else:
            by_username.setdefault(row.user.user.username.casefold(), set()).add(row.pk)
            person[row.user.user.username.casefold()] = row.user_id

    def matches(name):
        return by_username.get(name.casefold(), set()) | by_team.get(team_name_key(name), set())

    lines = [' '.join(line.split()) for line in LINES.split(text[:MAX_PASTE])]
    lines = [line for line in lines if line]
    # A whole line that names an active team stays whole too, registered or not (2026-11-01): a team called «Tres,
    # cuatro», picked from the suggestions, is one chip and one line. Asked only when some line would be split.
    # Where only individuals compete no team is looked up (2026-11-01): one put on the list could never register.
    with_teams = contest.participation_mode != Contest.INDIVIDUAL
    whole_teams = set()
    if with_teams and any(SEPARATORS.search(line) and not matches(line) for line in lines):
        whole_teams = {team_name_key(name) for name in
                       Team.objects.filter(is_active=True).values_list('name', flat=True)}

    tokens = []
    for line in lines:
        if matches(line) or (SEPARATORS.search(line) and team_name_key(line) in whole_teams):
            tokens.append(line)
        else:
            tokens.extend(token for token in (' '.join(part.split()) for part in SEPARATORS.split(line)) if token)
    tokens = tokens[:MAX_NAMES]

    # What names no registration: accounts and active teams, asked once for every token and every word of one.
    loose = set()
    for token in tokens:
        if not matches(token):
            loose.add(token)
            if ' ' in token:
                loose.update(token.split())
    # As the suggestions (2026-11-01): active accounts, without whoever runs or tests the contest.
    staff = set(contest.editor_ids) | set(contest.tester_ids)
    # A registered team named as somebody's username (2026-11-01): the name says the team or that person, and the
    # suggestions offer both, so it moves nobody and is told back as more than one.
    by_team_name_only = {word for token in tokens for word in {token, *token.split()}
                         if by_team.get(team_name_key(word)) and not by_username.get(word.casefold())}
    twins = set()
    if by_team_name_only:
        twins = {name.casefold() for name in
                 Profile.objects.filter(user__username__in=by_team_name_only, user__is_active=True)
                 .exclude(pk__in=staff).values_list('user__username', flat=True)}
    accounts = {}
    if loose:
        accounts = {name.casefold(): pk for pk, name in
                    Profile.objects.filter(user__username__in=loose, user__is_active=True).exclude(pk__in=staff)
                    .values_list('id', 'user__username')}
    # The active teams not registered here that a token or a word of one names, with their members.
    teams, members = {}, {}
    if tokens and with_teams:
        asked = {team_name_key(word) for token in tokens for word in {token, *token.split()}}
        registered = {row.team_id for row in rows if row.team_id}
        for pk, name in Team.objects.filter(is_active=True).exclude(pk__in=registered).values_list('id', 'name'):
            key = team_name_key(name)
            if key in asked:
                teams.setdefault(key, []).append(pk)
        if teams:
            for team, member in TeamMembership.objects.filter(
                    team_id__in=[pk for pks in teams.values() for pk in pks]).values_list('team_id', 'profile_id'):
                members.setdefault(team, set()).add(member)

    def doubled(name):
        # A registration or an account whose name is also that of an active team not registered here (2026-11-01):
        # the suggestions offer both and the name cannot tell them apart, unless it is the name of a person who is
        # on every team so called (designated_onsite puts on site the team they register in).
        others = teams.get(team_name_key(name), ())
        profile = person.get(name.casefold()) or accounts.get(name.casefold())
        return bool(others) and (profile is None or any(profile not in members.get(team, ()) for team in others))

    def known(name):
        return bool(matches(name)) or name.casefold() in accounts or team_name_key(name) in teams

    found, waiting_profiles, waiting_teams, unknown, ambiguous = set(), {}, {}, [], []
    for token in tokens:
        # Several usernames on one line, separated by spaces, each on its own; a line that names nobody at all is
        # told back whole (a team name with a typo).
        parts = token.split() if not known(token) and ' ' in token and any(known(part) for part in token.split()) \
            else [token]
        for part in parts:
            named = matches(part)
            if named and (len(named) > 1 or part.casefold() in twins or doubled(part)):
                ambiguous.append(part)
            elif named:
                found |= named
            elif part.casefold() in accounts and doubled(part):
                ambiguous.append(part)
            elif part.casefold() in accounts:
                waiting_profiles[part] = accounts[part.casefold()]
            elif len(teams.get(team_name_key(part), ())) == 1:
                waiting_teams[part] = teams[team_name_key(part)][0]
            elif team_name_key(part) in teams:
                ambiguous.append(part)
            else:
                unknown.append(part)
    return found, waiting_profiles, waiting_teams, unknown, ambiguous


def resolve_picked(contest, picked):
    """The chips picked from the suggestions (contest_registrations_search), by what each one is and not by its name
    (2026-11-01): a name both a person and a team says one of them. [(kind, pk, name)], where kind is 'r' (a
    registration), 'p' (an account) or 't' (a team), and the name only says it back. An account or a team registered
    since the suggestion moves its registration. Returns as resolve_names."""
    from judge.models import Team
    from judge.models.team import ContestTeamMember
    every = entrants(contest)
    wanted = {kind: {pk for chip, pk, _name in picked if chip == kind} for kind in 'rpt'}
    rows = set(every.filter(pk__in=wanted['r']).values_list('pk', flat=True)) if wanted['r'] else set()
    staff = set(contest.editor_ids) | set(contest.tester_ids)
    profiles, joined = {}, {}
    if wanted['p']:
        profiles = dict(Profile.objects.filter(pk__in=wanted['p'], user__is_active=True).exclude(pk__in=staff)
                        .values_list('id', 'user__username'))
        # Registered as resolve_names reads a name: on their own, or on the roster of a registered team.
        for user, pk in every.filter(team__isnull=True, user_id__in=profiles).values_list('user_id', 'pk'):
            joined.setdefault(user, set()).add(pk)
        for user, pk in (ContestTeamMember.objects.filter(participation__in=every, profile_id__in=profiles)
                         .values_list('profile_id', 'participation_id')):
            joined.setdefault(user, set()).add(pk)
    teams, team_rows = {}, {}
    if wanted['t'] and contest.participation_mode != Contest.INDIVIDUAL:
        teams = dict(Team.objects.filter(pk__in=wanted['t'], is_active=True).values_list('id', 'name'))
        team_rows = dict(every.filter(team_id__in=teams).values_list('team_id', 'pk'))

    found, waiting_profiles, waiting_teams, unknown, ambiguous = set(), {}, {}, [], []

    def wait(where, name, pk):
        # Two teams picked by the same name are two (team names repeat): the second one is told with its number.
        where['%s #%d' % (name, pk) if where.get(name, pk) != pk else name] = pk

    # Told back by their names today, not by the one the page sent, which is only said back for what is gone.
    for kind, pk, name in picked:
        if kind == 'r' and pk in rows:
            found.add(pk)
        elif kind == 'p' and len(joined.get(pk, ())) > 1:
            ambiguous.append(profiles[pk])
        elif kind == 'p' and pk in joined:
            found |= joined[pk]
        elif kind == 'p' and pk in profiles:
            wait(waiting_profiles, profiles[pk], pk)
        elif kind == 't' and pk in team_rows:
            found.add(team_rows[pk])
        elif kind == 't' and pk in teams:
            wait(waiting_teams, teams[pk], pk)
        else:
            # Gone since the suggestion: cancelled, made inactive, or now organizing the contest.
            unknown.append(name)
    return found, waiting_profiles, waiting_teams, unknown, ambiguous


@login_required
def contest_registrations(request, contest):
    contest = _contest(request, contest)
    profile = request.profile
    query = request.GET.get('q', '').strip()[:150]
    from judge.contest_teams import MODALITIES
    # Two tabs, on site and online, instead of a filter and a column (the owner, 2026-11-01); it opens online,
    # where everybody starts and from where they are moved.
    modality = request.GET.get('modality', '') if contest.is_hybrid else ''
    modality = modality if modality in MODALITIES else ('online' if contest.is_hybrid else '')
    state = request.GET.get('state', '')
    state = state if state in STATES else ''

    every = entrants(contest)
    # Named apart from the fields they count: an aggregate called like a field is what a later filter would read.
    found = every.aggregate(
        everyone=Count('id'),
        waiting=Count('id', filter=Q(virtual=ContestParticipation.REGISTERED)),
        inside=Count('id', filter=Q(virtual=ContestParticipation.LIVE)),
        in_person=Count('id', filter=Q(onsite=True)),
    )
    counts = {'total': found['everyone'], 'registered': found['waiting'], 'entered': found['inside'],
              'onsite': found['in_person'], 'online': found['everyone'] - found['in_person']}

    rows = every.select_related('user__user', 'team').prefetch_related('team_roster')
    # By what the field says (2026-11-01, «Buscar por usuario»): the username, or the name the team registered with,
    # and the usernames of its roster; not the full or display name the table shows under the username.
    if query:
        rows = rows.filter(Q(user__user__username__icontains=query) | Q(team_name__icontains=query) |
                           Q(team_roster__username__icontains=query)).distinct()
    if modality:
        rows = rows.filter(onsite=modality == 'onsite')
    if state:
        rows = rows.filter(virtual=ContestParticipation.REGISTERED if state == 'registered'
                           else ContestParticipation.LIVE)
    rows = sorted(rows, key=lambda row: ((row.team_name if row.team_id else row.user.user.username).casefold(),
                                         row.pk))
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get('page'))
    # Who is banned from the contest without being disqualified: registered, they will not get in.
    banned = set(contest.banned_users.filter(pk__in=[row.user_id for row in page.object_list if not row.team_id])
                 .values_list('pk', flat=True))

    def page_query(number):
        return _query(query, modality, state, number if number > 1 else None)

    kept = request.session.pop(NOTICES % contest.pk, None) or {}
    return render(request, 'contest/registrations.html', {
        'contest': contest,
        'title': _('Registrados'),
        'tab': 'registrations',
        # What contest-tabs.html expects from the contest views.
        'now': timezone.now(),
        'can_edit': True,
        'is_editor': profile.id in contest.editor_ids,
        'is_tester': profile.id in contest.tester_ids,
        'has_joined': contest.users.for_profile(profile).filter(virtual=ContestParticipation.LIVE).exists(),
        'counts': counts,
        'page': page,
        'banned': banned,
        'query': query,
        'modality': modality,
        'state': state,
        'filtered': bool(query or state),
        'tab_queries': {name: _query(query, name, state) for name in MODALITIES},
        # Without the search and the state, on the same tab.
        'clear_query': _query('', modality, ''),
        # The moves come back to the same search and page; the pasted list, to the same search.
        'here_query': page_query(page.number),
        'search_query': _query(query, modality, state),
        'previous_query': page_query(page.previous_page_number()) if page.has_previous() else '',
        'next_query': page_query(page.next_page_number()) if page.has_next() else '',
        'max_paste': MAX_PASTE,
        'waiting': waiting_on_site(contest) if contest.is_hybrid else [],
        'registration_errors': kept.get('errors', []),
        'registration_notices': kept.get('notices', []),
    })


@login_required
@require_GET
def contest_registrations_search(request, contest):
    """What the field that adds to the list of who competes on site suggests (2026-11-01, as the testers of the
    administration): first the registrations of the contest, then other accounts and, by their whole name, active
    teams. Each one carries what it is, not only its name (resolve_picked): 'r<participation>', 'p<profile>' or
    't<team>', so a name that is both a person and a team says the one picked (2026-11-01). What is typed without
    picking a suggestion goes by its name (resolve_names)."""
    from judge.models import Team
    from judge.models.team import ContestTeamMember
    contest = _contest(request, contest)
    term = ' '.join(request.GET.get('term', '').split())[:MAX_TERM]
    if not term:
        return JsonResponse({'results': []})
    results, taken = [], set()

    # Whether what is offered now is the whole name typed, as the database compares it (accents, capitals): the page
    # keeps such a suggestion on the list even once it is a chip (2026-11-01).
    whole = [True]

    def offer(key, name, team, registered):
        if key not in taken:
            taken.add(key)
            results.append({'id': key, 'text': name, 'team': team, 'registered': registered, 'exact': whole[0]})

    every = entrants(contest)
    # Not whoever runs or tests the contest: they never compete in it (entrants leaves them out too).
    staff = set(contest.editor_ids) | set(contest.tester_ids)

    def registrations(lookup):
        rows = (every.filter(Q(**{'user__user__username__' + lookup: term}) | Q(**{'team_name__' + lookup: term}) |
                             Q(**{'team__name__' + lookup: term}))
                .select_related('user__user', 'team').order_by('id')[:SUGGESTIONS])
        for row in rows:
            if row.team_id:
                # As it registered, and as it is called today if it was renamed since.
                name = row.team_name if row.team_name == row.team.name else '%s · %s' % (row.team_name,
                                                                                         row.team.name)
                offer('r%d' % row.pk, name, True, True)
            else:
                offer('r%d' % row.pk, row.user.user.username, False, True)

    def accounts(lookup):
        found = list(Profile.objects.filter(**{'user__username__' + lookup: term}, user__is_active=True)
                     .exclude(pk__in=staff).order_by('user__username')
                     .values_list('id', 'user__username')[:SUGGESTIONS])
        # Registered as resolve_picked reads them: on their own, or on the roster of a registered team, which is
        # the one they move then.
        ids = [pk for pk, _name in found]
        joined = {}
        if ids:
            for user, pk in every.filter(team__isnull=True, user_id__in=ids).values_list('user_id', 'pk'):
                joined.setdefault(user, []).append(pk)
            for user, pk in (ContestTeamMember.objects.filter(participation__in=every, profile_id__in=ids)
                             .values_list('profile_id', 'participation_id')):
                joined.setdefault(user, []).append(pk)
        for pk, name in found:
            if len(joined.get(pk, ())) == 1:
                offer('r%d' % joined[pk][0], name, False, True)
            else:
                offer('p%d' % pk, name, False, pk in joined)

    # The whole name first (2026-11-01): Enter takes the first suggestion, and «ana» is not «mariana».
    registrations('iexact')
    accounts('iexact')
    # No teams where only individuals compete (the owner, 2026-11-01). Where teams compete, accounts stay: naming one
    # puts on site the team it registers in (Contest.designated_onsite).
    # A team is seen only by its members (team_detail): one not registered here is offered only when its whole name
    # is typed, as a pasted name would confirm it, and never by a part of it.
    if contest.participation_mode != Contest.INDIVIDUAL:
        named = list(Team.objects.filter(name__iexact=term, is_active=True).order_by('name')
                     .values_list('id', 'name')[:SUGGESTIONS])
        registered_teams = dict(every.filter(team_id__in=[pk for pk, _name in named])
                                .values_list('team_id', 'pk')) if named else {}
        for pk, name in named:
            offer('r%d' % registered_teams[pk] if pk in registered_teams else 't%d' % pk, name, True,
                  pk in registered_teams)
    whole[0] = False
    registrations('icontains')
    accounts('icontains')
    return JsonResponse({'results': results})


def _leave(request, contest, notices=(), errors=()):
    request.session[NOTICES % contest.pk] = {'notices': list(notices), 'errors': list(errors)}


def _balloon_notices(contest, onsite, changed, names):
    """What a move does to the balloons and the seats of whoever moved, in words for whoever organizes."""
    from judge.models import ContestLocation, ContestTeamLocation
    from judge.views.contest_balloons import delivered_balloons, due_balloons
    notices = []
    if onsite:
        pending = due_balloons(contest, changed) - delivered_balloons(contest, changed)
        if pending:
            notices.append(ngettext(
                'Ya tenían problemas resueltos: %(count)d globo quedó pendiente en la hoja de globos.',
                'Ya tenían problemas resueltos: %(count)d globos quedaron pendientes en la hoja de globos.',
                len(pending)) % {'count': len(pending)})
        seats = ContestLocation.objects.filter(contest=contest)
        team_seats = ContestTeamLocation.objects.filter(contest=contest)
        if seats.exists() or team_seats.exists():
            seated = set(seats.values_list('user_id', flat=True))
            seated_teams = set(team_seats.values_list('team_id', flat=True))
            missing = [names[row.pk] for row in ContestParticipation.objects.filter(pk__in=changed)
                       if (row.team_id not in seated_teams if row.team_id else row.user_id not in seated)]
            if missing:
                notices.append(_('Sin lugar asignado en la hoja de globos: %(names)s.') % {'names': _listed(missing)})
    else:
        delivered = delivered_balloons(contest, changed)
        if delivered:
            notices.append(ngettext(
                'Ya no reciben globos; %(count)d que ya se les había entregado sigue en la hoja, marcado.',
                'Ya no reciben globos; %(count)d que ya se les habían entregado siguen en la hoja, marcados.',
                len(delivered)) % {'count': len(delivered)})
    return notices


@login_required
@require_POST
def contest_registrations_move(request, contest):
    contest = _contest(request, contest)
    back = _back(contest, request.GET)
    if not contest.is_hybrid:
        _leave(request, contest, errors=[
            _('Este concurso no es presencial y en línea a la vez: no hay modalidades que asignar.')])
        return back
    target = request.POST.get('target', '')
    if target not in ('onsite', 'online'):
        _leave(request, contest, errors=[_('Elige si pasan a presencial o a en línea.')])
        return back
    onsite = target == 'onsite'
    pasted = 'names' in request.POST or 'picked' in request.POST
    names, picked = '', []
    if pasted:
        # The page sends every chip in one value, a name per line, with scripts or without (2026-11-01); several
        # values are taken too. The chips picked from the suggestions travel apart, by what they are ('picked': a
        # key, a tab and the name, per line). A list longer than the bounds is refused whole rather than cut at some
        # name.
        # As the box counts it, a line break as one character: the browser sends it as two.
        values = request.POST.getlist('names')
        names = '\n'.join(values).replace('\r\n', '\n')
        chosen = [line for value in request.POST.getlist('picked') for line in LINES.split(value) if line.strip()]
        if len(values) > MAX_NAMES or len(names) > MAX_PASTE or len(chosen) > MAX_NAMES or \
                sum(len(line) for line in chosen) > MAX_PASTE or \
                sum(1 for part in TOO_MANY.split(names) if part.strip()) + len(chosen) > MAX_NAMES:
            _leave(request, contest, errors=[_('La lista es demasiado larga: pégala en partes.')])
            return back
        for line in chosen:
            match = PICKED.match(line)
            if match:
                name = ' '.join((match.group(3) or '').split())[:NAME_CHARS * 2]
                picked.append((match.group(1), int(match.group(2)), name or match.group(1) + match.group(2)))
    ids = set()
    if not pasted:
        for value in request.POST.getlist('selected')[:PAGE_SIZE]:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
    # The rows of the list of who competes on site without a registration, ticked to take them off it.
    ticked_profiles, ticked_teams = set(), set()
    for value in request.POST.getlist('waiting')[:MAX_NAMES]:
        match = WAITING.match(value)
        if match:
            (ticked_profiles if match.group(1) == 'p' else ticked_teams).add(int(match.group(2)))

    with transaction.atomic():
        # The contest first, as join_attempt locks it before reading the list of who competes on site. The names are
        # read after it (2026-11-01): whoever registers meanwhile is moved, not listed as not registered yet.
        locked = Contest.objects.select_for_update().get(pk=contest.pk)
        waiting_profiles, waiting_teams, unknown, ambiguous = {}, {}, [], []
        # Only what was sent: with the contest locked, whoever joins now waits for this.
        if names.strip():
            ids, waiting_profiles, waiting_teams, unknown, ambiguous = resolve_names(contest, names)
        if picked:
            more = resolve_picked(contest, picked)
            ids |= more[0]
            waiting_profiles.update(more[1])
            waiting_teams.update(more[2])
            unknown += more[3]
            ambiguous += more[4]
        unlisted_profiles, unlisted_teams = set(), set()
        if not onsite:
            # Pasted to go online (the page only offers it for the boxes): whoever is not registered leaves the list.
            unlisted_profiles = ticked_profiles | set(waiting_profiles.values())
            unlisted_teams = ticked_teams | set(waiting_teams.values())
            waiting_profiles, waiting_teams = {}, {}
        # Only rows of this contest, official and not of its staff, whatever ids were sent; then only those rows are
        # locked: MariaDB has no FOR UPDATE OF, and the joins of entrants() would lock their profiles too.
        valid = list(entrants(contest).filter(pk__in=ids).values_list('pk', flat=True)) if ids else []
        rows = list(ContestParticipation.objects.filter(pk__in=valid).select_for_update()
                    .values_list('pk', 'onsite', 'user_id', 'team_id'))
        changed = [pk for pk, now_onsite, _user, _team in rows if now_onsite != onsite]
        if changed:
            ContestParticipation.objects.filter(pk__in=changed).update(onsite=onsite)
        # The list follows every move: whoever cancels and registers again, or is pasted before registering, starts
        # where whoever organizes put them.
        people = {user for _pk, _onsite, user, team in rows if not team}
        teams = {team for _pk, _onsite, _user, team in rows if team}
        listed_profiles, listed_teams = locked.onsite_designations()
        if onsite:
            locked.change_onsite_list(add_profiles=people | set(waiting_profiles.values()),
                                      add_teams=teams | set(waiting_teams.values()))
        else:
            from judge.models.team import ContestTeamMember
            roster = set(ContestTeamMember.objects.filter(
                participation_id__in=[pk for pk, _onsite, _user, team in rows if team]).values_list('profile_id',
                                                                                                    flat=True))
            locked.change_onsite_list(remove_profiles=people | roster | unlisted_profiles,
                                      remove_teams=teams | unlisted_teams)
    if changed:
        logger.info('Contest %s: %s moved %d participations to %s: %s', contest.key, request.user.username,
                    len(changed), target, ' '.join(str(pk) for pk in sorted(changed)))
    newly_listed = [name for name, pk in waiting_profiles.items() if pk not in listed_profiles] + \
        [name for name, pk in waiting_teams.items() if pk not in listed_teams]
    unlisted = [name for pk, name in Profile.objects.filter(pk__in=unlisted_profiles & listed_profiles)
                .values_list('id', 'user__username')]
    if unlisted_teams & listed_teams:
        from judge.models import Team
        unlisted += list(Team.objects.filter(pk__in=unlisted_teams & listed_teams).values_list('name', flat=True))
    if newly_listed or unlisted:
        logger.info('Contest %s: %s %s the list of who competes on site: %s', contest.key, request.user.username,
                    'added to' if onsite else 'took off', ' '.join(sorted(newly_listed or unlisted)))

    notices, errors = [], []
    if not ids and not waiting_profiles and not waiting_teams and not unknown and not ambiguous and \
            not unlisted_profiles and not unlisted_teams:
        errors.append(_('No elegiste a nadie.'))
    elif changed:
        names = {row.pk: row.team_name if row.team_id else row.user.user.username for row in
                 ContestParticipation.objects.filter(pk__in=changed).select_related('user__user')}
        if onsite:
            text = ngettext('%(count)d pasó a presencial: %(names)s.', '%(count)d pasaron a presencial: %(names)s.',
                            len(changed))
        else:
            text = ngettext('%(count)d pasó a en línea: %(names)s.', '%(count)d pasaron a en línea: %(names)s.',
                            len(changed))
        notices.append(text % {'count': len(changed), 'names': _listed(names.values())})
        notices.extend(_balloon_notices(contest, onsite, changed, names))
    already = len(rows) - len(changed)
    if already:
        notices.append((ngettext('%(count)d ya estaba en presencial.', '%(count)d ya estaban en presencial.', already)
                        if onsite else
                        ngettext('%(count)d ya estaba en línea.', '%(count)d ya estaban en línea.', already))
                       % {'count': already})
    if waiting_profiles or waiting_teams:
        # Short (the owner, 2026-11-01): the chips under the field say the rest.
        notices.append(_('En la lista de la sede, sin registrarse todavía: %(names)s.')
                       % {'names': _listed(list(waiting_profiles) + list(waiting_teams))})
    if unlisted:
        notices.append(_('Ya no están en la lista de la sede: %(names)s.') % {'names': _listed(unlisted)})
    if unknown:
        errors.append(_('No hay ninguna cuenta ni equipo con estos nombres: %(names)s.') % {'names': _listed(unknown)})
    if ambiguous:
        # A picked suggestion says which one it is (2026-11-01); a typed name may say more than one.
        errors.append(_('Estos nombres coinciden con más de un registro, cuenta o equipo: elige el correcto en las '
                        'sugerencias, o márcalo en la tabla: %(names)s.') % {'names': _listed(ambiguous)})
    _leave(request, contest, notices, errors)
    return back
