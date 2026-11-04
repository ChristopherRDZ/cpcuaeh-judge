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
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext as _, ngettext
from django.views.decorators.http import require_POST

from judge.models import Contest, ContestParticipation, Profile

__all__ = ['contest_registrations', 'contest_registrations_move']

logger = logging.getLogger('judge.contest_registration')

PAGE_SIZE = 100
# What a pasted list may hold: plenty for a room, and a bound on the work of one request.
MAX_PASTE = 20000
MAX_NAMES = 2000
# How many names each line of the result lists before «…», and how much of each name it repeats.
SHOWN_NAMES = 30
NAME_CHARS = 60
# Lines separate names; tabs (columns copied from a spreadsheet), commas and semicolons too, unless the whole line is
# the name of a registration (a team called «Uno, dos»). A team name may carry spaces.
LINES = re.compile(r'[\r\n]+')
SEPARATORS = re.compile(r'[\t,;]+')
STATES = ('registered', 'entered')
# What a move leaves for the page it goes back to, per contest: two tabs on two contests do not mix their notices.
NOTICES = 'registration_notices:%d'
# How the rows of the list of who competes on site without being registered are named in the form.
WAITING = re.compile(r'^([pt])(\d{1,10})$')


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
    names = sorted({_short(name) for name in names}, key=str.casefold)
    shown = ', '.join(names[:SHOWN_NAMES])
    return shown + ('…' if len(names) > SHOWN_NAMES else '')


def resolve_names(contest, text):
    """The participations named in a pasted list: usernames, team names, or the username of anybody on a team's
    roster, today's or the one it had when registering. Of the names that match no registration, the accounts and the
    active teams they name, for the list of who competes on site. Returns (participation ids, {name: profile id} of
    accounts not registered, {name: team id} of teams not registered, names that match nothing, names that match more
    than one registration or team, which are not moved)."""
    from judge.models import Team
    from judge.team_services import team_name_key
    rows = list(entrants(contest).select_related('user__user').prefetch_related('team_roster__profile__user'))
    by_username, by_team = {}, {}
    for row in rows:
        if row.team_id:
            by_team.setdefault(team_name_key(row.team_name), set()).add(row.pk)
            for member in row.team_roster.all():
                for name in {member.username, member.profile.user.username}:
                    by_username.setdefault(name.casefold(), set()).add(row.pk)
        else:
            by_username.setdefault(row.user.user.username.casefold(), set()).add(row.pk)

    def matches(name):
        return by_username.get(name.casefold(), set()) | by_team.get(team_name_key(name), set())

    tokens = []
    for line in LINES.split(text[:MAX_PASTE]):
        line = ' '.join(line.split())
        if not line:
            continue
        if matches(line):
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
    accounts = {}
    if loose:
        accounts = {name.casefold(): pk for pk, name in
                    Profile.objects.filter(user__username__in=loose).values_list('id', 'user__username')}
    teams = {}
    wanted = {team_name_key(name) for name in loose if name.casefold() not in accounts}
    if wanted:
        for pk, name in Team.objects.filter(is_active=True).values_list('id', 'name'):
            key = team_name_key(name)
            if key in wanted:
                teams.setdefault(key, []).append(pk)

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
            if len(named) == 1:
                found |= named
            elif named:
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


@login_required
def contest_registrations(request, contest):
    contest = _contest(request, contest)
    profile = request.profile
    query = request.GET.get('q', '').strip()[:150]
    from judge.contest_teams import MODALITIES
    modality = request.GET.get('modality', '') if contest.is_hybrid else ''
    modality = modality if modality in MODALITIES else ''
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
    if query:
        rows = rows.filter(Q(user__user__username__icontains=query) |
                           Q(user__username_display_override__icontains=query) |
                           Q(team_name__icontains=query) | Q(team_roster__username__icontains=query)).distinct()
    if modality:
        rows = rows.filter(onsite=modality == 'onsite')
    if state:
        rows = rows.filter(virtual=ContestParticipation.REGISTERED if state == 'registered'
                           else ContestParticipation.LIVE)
    rows = sorted(rows, key=lambda row: (row.display_name.casefold(), row.pk))
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
        'filtered': bool(query or modality or state),
        # The moves come back to the same search and page; the pasted list, to the same search.
        'here_query': page_query(page.number),
        'search_query': _query(query, modality, state),
        'previous_query': page_query(page.previous_page_number()) if page.has_previous() else '',
        'next_query': page_query(page.next_page_number()) if page.has_next() else '',
        'max_paste': MAX_PASTE,
        'waiting': waiting_on_site(contest) if contest.is_hybrid else [],
        'registration_errors': kept.get('errors', []),
        'registration_notices': kept.get('notices', []),
        # The pasted list stays open when what came back from it needs a look.
        'paste_open': bool(kept.get('pasted')),
    })


def _leave(request, contest, notices=(), errors=(), pasted=False):
    request.session[NOTICES % contest.pk] = {'notices': list(notices), 'errors': list(errors),
                                             'pasted': pasted and bool(errors)}


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
    pasted = 'names' in request.POST
    waiting_profiles, waiting_teams, unknown, ambiguous = {}, {}, (), ()
    if pasted:
        ids, waiting_profiles, waiting_teams, unknown, ambiguous = resolve_names(contest, request.POST.get('names', ''))
    else:
        ids = set()
        for value in request.POST.getlist('selected')[:PAGE_SIZE]:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
    # The rows of the list of who competes on site without a registration, ticked to take them off it.
    unlisted_profiles, unlisted_teams = set(), set()
    for value in request.POST.getlist('waiting')[:MAX_NAMES]:
        match = WAITING.match(value)
        if match:
            (unlisted_profiles if match.group(1) == 'p' else unlisted_teams).add(int(match.group(2)))
    if onsite:
        unlisted_profiles, unlisted_teams = set(), set()
    else:
        # Pasted to go online (the page only offers it for the boxes): whoever is not registered leaves the list.
        unlisted_profiles |= set(waiting_profiles.values())
        unlisted_teams |= set(waiting_teams.values())
        waiting_profiles, waiting_teams = {}, {}

    # Only rows of this contest, official and not of its staff, whatever ids were sent; then only those rows are
    # locked: MariaDB has no FOR UPDATE OF, and the joins of entrants() would lock their profiles too.
    valid = list(entrants(contest).filter(pk__in=ids).values_list('pk', flat=True)) if ids else []
    with transaction.atomic():
        # The contest first, as join_attempt locks it before reading the list of who competes on site.
        locked = Contest.objects.select_for_update().get(pk=contest.pk)
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
        notices.append(_('Todavía no se registran: quedan en la lista de la sede y entrarán como presenciales en '
                         'cuanto se registren o se unan: %(names)s.')
                       % {'names': _listed(list(waiting_profiles) + list(waiting_teams))})
    if unlisted:
        notices.append(_('Ya no están en la lista de la sede: %(names)s.') % {'names': _listed(unlisted)})
    if unknown:
        errors.append(_('No hay ninguna cuenta ni equipo con estos nombres: %(names)s.') % {'names': _listed(unknown)})
    if ambiguous:
        errors.append(_('Hay más de un registro o equipo con estos nombres: márcalos en la tabla, o pega el usuario de '
                        'un integrante: %(names)s.') % {'names': _listed(ambiguous)})
    _leave(request, contest, notices, errors, pasted)
    return back
