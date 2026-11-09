"""The organizer side of the arena: the panel, quick battles, tournaments, problem banks and projector screens.

Everything here requires `judge.manage_arena` (own battles) or `judge.manage_all_arena` (all of them); anybody else
is told the page does not exist, and so is an organizer who opens a battle, tournament, screen or bank that is not
theirs (judge/arena/access.py). Every change is a JSON POST carrying the version of the object it was made on, so
two organizers working at once get a conflict instead of overwriting each other, and the panel refreshes itself
every couple of seconds.
"""
import logging
from datetime import datetime
from functools import wraps

from django.core.paginator import InvalidPage
from django.db.models import Count, OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from judge.arena import access, engine, rules as arena_rules, serialize, tournament as arena_tournament
from judge.arena.engine import ArenaError
from judge.jinja2.gravatar import gravatar
from judge.models import Language, Profile, Submission
from judge.models.arena import ArenaBankProblem, ArenaGame, ArenaMatch, ArenaProblemBank, ArenaScreen, \
    ArenaTournament, RANKING_PENDING, SLOT_A, SLOT_B
from judge.utils.diggpaginator import DiggPaginator
from judge.views.arena import _ace_context, can_manage, json_error, json_response, read_json

logger = logging.getLogger('judge.arena')

SEARCH_LIMIT = 20
MAX_ORGANIZERS = 20

__all__ = ['manage_guide', 'manage_dashboard', 'manage_overview', 'manage_duel_create', 'manage_match', 'manage_match_state',
           'manage_match_action', 'manage_game_code', 'manage_submission_source', 'manage_problem_search',
           'manage_user_search', 'manage_tournament_create', 'manage_tournament', 'manage_tournament_state',
           'manage_tournament_action', 'manage_banks', 'manage_bank_action', 'manage_screen_action', 'manage_draw',
           'manage_quick', 'manage_quick_new', 'manage_tournaments', 'manage_tournament_new', 'manage_screens',
           'manage_screens_state']


def _languages():
    return list(Language.objects.order_by('name', 'key').values('key', 'name'))


def manager_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not can_manage(request.user):
            raise Http404()
        return view(request, *args, **kwargs)
    return wrapped


def arena_errors(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ArenaError as error:
            return json_error(error.message, error.status)
    return wrapped


def _profile(value):
    if isinstance(value, bool):
        return None
    # isdecimal, not isdigit (2026-10-28), here and below: a superscript digit passed isdigit and int() failed with a
    # 500.
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if not isinstance(value, int):
        return None
    return Profile.objects.filter(pk=value, user__is_active=True).select_related('user').first()


def _planned(value):
    """The planned date of a tournament from a datetime-local field, read on the clock of whoever organizes (the
    time zone of their profile, judge/timezone.py). Empty takes it away."""
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ArenaError(_('Invalid date.'))
    try:
        moment = datetime.strptime(value[:16], '%Y-%m-%dT%H:%M')
    except ValueError:
        raise ArenaError(_('Invalid date.'))
    return timezone.make_aware(moment)


def _history_box(value, default=None):
    """The box «show in the history»: only a true or false counts."""
    return value if isinstance(value, bool) else default


def _ranking(request, value):
    """Whether a new battle or tournament counts for the ranking of the duels (judge/arena/rating.py). Only
    whoever may decide the ranking chooses; for anybody else it is not decided yet, as it always was."""
    if value in (None, '', RANKING_PENDING):
        return RANKING_PENDING
    if not access.decides_ranking(request.user):
        raise ArenaError(_('Only whoever decides the ranking of the duels can choose whether it counts.'), status=403)
    return engine.clean_ranking(value)


def _id(value, required=False):
    """An object id from a JSON body: a positive int, or None when absent. Anything else is refused."""
    if value in (None, '') and not required:
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ArenaError(_('Invalid request.'))
    return value


def _query_id(request, name):
    """The id in ?<name>= of a page, or None without one. Anything but plain digits is a page that does not exist
    (2026-10-28): a superscript digit passed isdigit() and made int() fail with a 500."""
    value = request.GET.get(name, '')
    if not value:
        return None
    if not (value.isascii() and value.isdecimal()):
        raise Http404()
    return int(value)


def _slot(value):
    if value not in (SLOT_A, SLOT_B):
        raise ArenaError(_('Choose a player.'))
    return value


# Somebody else's battle, tournament, screen or bank is answered as if it did not exist, like any other page here.

def _own_match(request, match_id, page=False):
    match = ArenaMatch.objects.select_related('tournament').filter(pk=match_id).first()
    if match is None or not access.can_manage_match(request.user, match):
        if page:
            raise Http404()
        raise ArenaError(_('That battle does not exist.'), status=404)
    return match


def _own_tournament(request, slug, page=False):
    tournament = ArenaTournament.objects.filter(slug=slug).first()
    if tournament is None or not access.can_manage_tournament(request.user, tournament):
        if page:
            raise Http404()
        raise ArenaError(_('That tournament does not exist.'), status=404)
    return tournament


def _own_game(request, game_id):
    game = ArenaGame.objects.select_related('match', 'match__tournament').filter(pk=game_id).first()
    if game is None or not access.can_manage_match(request.user, game.match):
        raise Http404()
    return game


def _bank_choices(user, current=None):
    """The banks a tournament of `user` can take: theirs, plus the one it already has even if it is somebody else's,
    so that saving the page does not drop it."""
    banks = access.banks_for(user)
    if current is not None:
        banks = banks | ArenaProblemBank.objects.filter(pk=current)
    return list(banks.order_by('name').values('id', 'name'))


def _organizers(values, exclude=None):
    """The co-organizers sent by a page: only accounts that can organize battles."""
    if not isinstance(values, list) or len(values) > MAX_ORGANIZERS * 2:
        raise ArenaError(_('Invalid list of organizers.'))
    profiles, seen = [], set()
    for value in values:
        profile = _profile(value)
        if profile is None:
            raise ArenaError(_('One of the organizers does not exist.'))
        if profile.pk in seen or profile.pk == exclude:
            continue
        if not access.is_eligible_organizer(profile):
            raise ArenaError(_('%(name)s cannot organize battles: give them a permission to organize battles first.')
                             % {'name': profile.username})
        seen.add(profile.pk)
        profiles.append(profile)
    if len(profiles) > MAX_ORGANIZERS:
        raise ArenaError(_('A battle or tournament can have at most %(count)d co-organizers.') %
                         {'count': MAX_ORGANIZERS})
    return profiles


def _set_organizers(actor, obj, profiles, **where):
    obj.organizers.set(profiles)
    engine._log('organizers', actor, organizers=[profile.username for profile in profiles], **where)


# ---------------------------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------------------------

@manager_required
@require_GET
def manage_dashboard(request):
    # «Now», the first tab (2026-11-03): what waits for the organizer and what is being played. A plain read: the
    # first screen is created with the first battle or tournament, which are POSTs.
    return render(request, 'arena/manage/dashboard.html', _panel(request, 'now', {
        'title': _('Battle panel'),
        'state': serialize.overview(user=request.user),
    }))


@manager_required
@require_GET
def manage_overview(request):
    request.no_profile_update = True
    return json_response(serialize.overview(user=request.user))


@manager_required
@require_GET
def manage_match(request, match):
    match = _own_match(request, match, page=True)
    context = {
        'title': _('Battle: %(name)s') % {'name': str(match)},
        'state': serialize.organizer_match_state(match, request.LANGUAGE_CODE, user=request.user),
        'defaults': arena_rules.DEFAULTS,
        'languages': _languages(),
        'starter_code': arena_rules.STARTER_CODE['CPP'],
    }
    context.update(_ace_context())
    return render(request, 'arena/manage/match.html',
                  _panel(request, 'tournaments' if match.tournament_id else 'quick', context))


@manager_required
@require_GET
def manage_tournament(request, slug):
    tournament = _own_tournament(request, slug, page=True)
    return render(request, 'arena/manage/tournament.html', _panel(request, 'tournaments', {
        'title': _('Tournament: %(name)s') % {'name': tournament.name},
        'state': serialize.tournament_state(tournament, request.LANGUAGE_CODE, user=request.user),
        'defaults': arena_rules.DEFAULTS,
        'languages': _languages(),
        'banks': _bank_choices(request.user, tournament.bank_id),
        'starter_code': arena_rules.STARTER_CODE['CPP'],
        'min_places': arena_tournament.MIN_PARTICIPANTS,
        'max_places': arena_tournament.MAX_PARTICIPANTS,
    }))


@manager_required
@require_GET
def manage_guide(request):
    return render(request, 'arena/manage/guide.html', _panel(request, 'guide', {'title': _('Guide for organizers')}))


@manager_required
@require_GET
def manage_banks(request):
    banks = access.banks_for(request.user).order_by('name')
    selected = None
    wanted = _query_id(request, 'bank')
    if wanted is not None:
        # Somebody else's bank does not exist here, as the module says (2026-10-28; until then the page opened with
        # no bank chosen), and neither does the bank of a tournament they still play, which banks_for leaves out
        # (round 2 of the adversarial review): it lists the problems their next games may draw, with their levels.
        selected = banks.filter(pk=wanted).first()
        if selected is None:
            raise Http404()
    entries, hidden = [], 0
    if selected is not None:
        rows = list(ArenaBankProblem.objects.filter(bank=selected).select_related('problem'))
        visible = set(engine.visible_problems(request.user).filter(id__in=[e.problem_id for e in rows])
                      .values_list('id', flat=True))
        # A problem this organizer cannot see (somebody else added it, or it became private) is not shown: saving
        # the bank keeps it as it is (tournament.set_bank_problems).
        entries = [{'id': e.problem_id, 'code': e.problem.code, 'name': e.problem.name, 'level': e.level,
                    'note': e.note, 'public': e.problem.is_public} for e in rows if e.problem_id in visible]
        hidden = len(rows) - len(entries)
    return render(request, 'arena/manage/bank.html', _panel(request, 'banks', {
        'title': _('Problem banks'),
        'banks': banks,
        'selected': selected,
        'entries': entries,
        'hidden': hidden,
    }))


# The panel in tabs (2026-11-03): Now, Quick battles, Tournaments, Projectors and Problem banks, one page each.

PAGE_SIZE = 20
OPEN_MATCH = (ArenaMatch.READY, ArenaMatch.CALLED, ArenaMatch.PLAYING)
# A cancelled tournament (2026-11-06) goes with the archived ones: out of the way, still there to look at.
TOURNAMENT_GROUPS = {'active': (ArenaTournament.DRAFT, ArenaTournament.RUNNING),
                     'finished': (ArenaTournament.FINISHED,),
                     'archived': (ArenaTournament.ARCHIVED, ArenaTournament.CANCELLED)}


def _panel(request, tab, context):
    context.update({'panel_tab': tab, 'sees_all': access.manages_all(request.user)})
    return context


def _paginate(request, items, size=PAGE_SIZE):
    """A page of `items` with the pagination of the site (list-pages.html). A page past the last one shows the last;
    anything but a number is a page that does not exist."""
    value = request.GET.get('page', '1')
    if not (value.isascii() and value.isdecimal()):
        raise Http404()
    paginator = DiggPaginator(items, size, body=6, padding=2)
    try:
        page = paginator.page(int(value), softlimit=True)
    except InvalidPage:
        raise Http404()
    query = request.GET.copy()
    query.pop('page', None)
    return {'page_obj': page, 'page_prefix': '?' + (query.urlencode() + '&' if query else '') + 'page=',
            'page_suffix': '', 'first_page_href': None}


def _listed_matches(user):
    """The battles the panel lists to `user`: what they organize, but not what they are playing (serialize.overview)."""
    profile = user.profile
    return access.matches_for(user).exclude(Q(player_a=profile) | Q(player_b=profile), status__in=OPEN_MATCH)


def _listed_tournaments(user):
    return access.tournaments_for(user).exclude(id__in=access.contended_tournaments(user).values('id'))


@manager_required
@require_GET
def manage_quick(request):
    show = 'done' if request.GET.get('show') == 'done' else 'open'
    quick = _listed_matches(request.user).filter(tournament__isnull=True)
    groups = {'open': quick.filter(status__in=OPEN_MATCH),
              'done': quick.filter(status__in=(ArenaMatch.DECIDED, ArenaMatch.CANCELLED))}
    live = ArenaGame.objects.filter(match=OuterRef('pk'), status__in=ArenaGame.ACTIVE_STATUSES).values('number')[:1]
    rows = groups[show].select_related('player_a__user', 'player_b__user').annotate(live_game=Subquery(live))
    rows = rows.order_by('-created_at', '-id') if show == 'open' else rows.order_by('-decided_at', '-id')
    context = _paginate(request, rows)
    context.update({'title': _('Quick battles'), 'show': show,
                    'counts': {key: queryset.count() for key, queryset in groups.items()}})
    return render(request, 'arena/manage/quick.html', _panel(request, 'quick', context))


@manager_required
@require_GET
def manage_quick_new(request):
    return render(request, 'arena/manage/quick_new.html', _panel(request, 'quick', {
        'title': _('New quick battle'),
        'defaults': arena_rules.DEFAULTS,
        'languages': _languages(),
        'starter_code': arena_rules.STARTER_CODE['CPP'],
        'decides_ranking': access.decides_ranking(request.user),
    }))


@manager_required
@require_GET
def manage_tournaments(request):
    show = request.GET.get('show') if request.GET.get('show') in TOURNAMENT_GROUPS else 'active'
    tournaments = _listed_tournaments(request.user)
    counts = {key: tournaments.filter(status__in=statuses).count() for key, statuses in TOURNAMENT_GROUPS.items()}
    rows = tournaments.filter(status__in=TOURNAMENT_GROUPS[show]).annotate(
        n_participants=Count('participants', distinct=True),
        n_live=Count('matches', filter=Q(matches__status=ArenaMatch.PLAYING), distinct=True))
    rows = rows.order_by('-finished_at', '-id') if show == 'finished' else rows.order_by('-created_at', '-id')
    context = _paginate(request, rows)
    context.update({'title': _('Tournaments'), 'show': show, 'counts': counts})
    return render(request, 'arena/manage/tournaments.html', _panel(request, 'tournaments', context))


@manager_required
@require_GET
def manage_tournament_new(request):
    return render(request, 'arena/manage/tournament_new.html', _panel(request, 'tournaments', {
        'title': _('New tournament'),
        'min_places': arena_tournament.MIN_PARTICIPANTS,
        'max_places': arena_tournament.MAX_PARTICIPANTS,
    }))


def _screen_choices(user, screens):
    """What a projector can be given to show: the battles open now and the tournaments not archived, and whatever a
    projector of theirs already shows, so its list never loses it."""
    shown_matches = [screen['match'] for screen in screens if screen['match']]
    shown_tournaments = [screen['tournament'] for screen in screens if screen['tournament']]
    # The page asks for this every 3 seconds: the rounds of each tournament in the same query (n_rounds).
    matches = serialize.with_round_count(_listed_matches(user).filter(
        Q(status__in=OPEN_MATCH, tournament__isnull=True) |
        Q(status__in=OPEN_MATCH, tournament__status=ArenaTournament.RUNNING) | Q(id__in=shown_matches))
        .select_related('player_a__user', 'player_b__user', 'tournament', 'round')).order_by('-id')[:60]
    tournaments = _listed_tournaments(user).filter(
        Q(status__in=(ArenaTournament.DRAFT, ArenaTournament.RUNNING, ArenaTournament.FINISHED)) |
        Q(id__in=shown_tournaments)).order_by('-created_at', '-id')[:40]
    return {
        'matches': [{'id': match.pk, 'name': '%s vs %s' % (match.shown_name(SLOT_A), match.shown_name(SLOT_B)),
                     'tournament': match.tournament.name if match.tournament_id else '',
                     'round': serialize.round_label(match) if match.tournament_id else '',
                     'open': match.status in OPEN_MATCH} for match in matches],
        'tournaments': [{'id': tournament.pk, 'name': tournament.name, 'status': tournament.status}
                        for tournament in tournaments],
    }


def _screens_state(user):
    screens = serialize.screens_overview(user)
    return dict(_screen_choices(user, screens), screens=screens)


@manager_required
@require_GET
def manage_screens(request):
    return render(request, 'arena/manage/screens.html', _panel(request, 'screens', {
        'title': _('Projectors'),
        'state': _screens_state(request.user),
        'projector_url': request.build_absolute_uri(reverse('arena_projector')),
    }))


@manager_required
@require_GET
def manage_screens_state(request):
    request.no_profile_update = True
    return json_response(_screens_state(request.user))


# ---------------------------------------------------------------------------------------------------------------
# Quick battles and matches
# ---------------------------------------------------------------------------------------------------------------

@manager_required
@require_POST
@arena_errors
def manage_duel_create(request):
    data = read_json(request)
    games = data.get('games')
    match = engine.create_duel(
        request.profile, _profile(data.get('player_a')), _profile(data.get('player_b')), games,
        best_of=data.get('best_of', 1), rules=data.get('rules') or {}, display_a=data.get('display_a', ''),
        display_b=data.get('display_b', ''), title=data.get('title', ''), call=data.get('call', True) is not False,
        ranking=_ranking(request, data.get('ranking')),
    )
    return json_response({'id': match.pk, 'url': reverse('arena_manage_match', args=[match.pk])})


@manager_required
@require_GET
def manage_match_state(request, match):
    request.no_profile_update = True
    match = _own_match(request, match, page=True)
    return json_response(serialize.organizer_match_state(match, request.LANGUAGE_CODE, user=request.user))


@manager_required
@require_POST
@arena_errors
def manage_match_action(request, match):
    match_obj = _own_match(request, match)
    data = read_json(request)
    action = data.get('action')
    version = data.get('version')
    actor = request.profile
    game = _id(data.get('game'))
    result = {}
    if action == 'call':
        engine.call_match(actor, match, version)
    elif action == 'start':
        outcome = engine.start_game(actor, match, game, force=bool(data.get('force')), version=version)
        if not outcome['started']:
            result['warnings'] = outcome['warnings']
    elif action == 'extend':
        engine.extend_game(actor, match, game, data.get('minutes'), version)
    elif action == 'cancel_game':
        engine.cancel_game(actor, match, game, version)
    elif action == 'declare':
        engine.declare_winner(actor, match, game, _slot(data.get('slot')), version)
    elif action == 'finalize':
        engine.finalize_verification(actor, match, game, version)
    elif action == 'walkover':
        engine.walkover(actor, match, _slot(data.get('slot')), version)
    elif action == 'set_winner':
        arena_tournament.set_winner(actor, match, _slot(data.get('slot')), version)
    elif action == 'cancel':
        engine.cancel_match(actor, match, version)
    elif action == 'problems':
        # A game by its id, or by its number in the series (it is created if it does not exist yet).
        engine.set_game_problems(actor, match, game, data.get('problems'), version, number=_id(data.get('number')))
    elif action == 'add_game':
        engine.add_game(actor, match, version)
    elif action == 'update':
        engine.update_match(actor, match, version, best_of=data.get('best_of'), rules=data.get('rules'),
                            title=data.get('title'), display_a=data.get('display_a'),
                            display_b=data.get('display_b'))
    elif action == 'replace':
        engine.replace_player(actor, match, _slot(data.get('slot')), _profile(data.get('profile')),
                              data.get('display', ''), version)
    elif action == 'organizers':
        if match_obj.tournament_id:
            raise ArenaError(_('The battles of a tournament are run by the organizers of the tournament.'))
        _set_organizers(actor, match_obj, _organizers(data.get('organizers'), exclude=match_obj.created_by_id),
                        match=match_obj)
    else:
        raise ArenaError(_('Unknown action.'))
    match_obj = ArenaMatch.objects.get(pk=match)
    result['state'] = serialize.organizer_match_state(match_obj, request.LANGUAGE_CODE, user=request.user)
    return json_response(result)


@manager_required
@require_GET
def manage_game_code(request, game):
    request.no_profile_update = True
    game = _own_game(request, game)
    since = {}
    for slot in (SLOT_A, SLOT_B):
        try:
            since[slot] = max(0, int(request.GET.get(slot, 0)))
        except (TypeError, ValueError):
            since[slot] = 0
    return json_response(serialize.code_payload(game, since, enforce_screen_rule=False))


@manager_required
@require_GET
def manage_submission_source(request, submission):
    submission = get_object_or_404(Submission.objects.select_related('language', 'source', 'contest_object',
                                                                     'problem'), pk=submission)
    game = ArenaGame.objects.select_related('match', 'match__tournament') \
        .filter(contest_id=submission.contest_object_id).first() if submission.contest_object_id else None
    if game is None or not access.can_manage_match(request.user, game.match):
        # Only arena submissions, and only of one's own battles: this is not a way around the normal submission
        # permissions.
        raise Http404()
    # What the judge said: a compile error, or the warnings of the compiler on a graded submission, are the
    # contestant's own. An internal error, which may carry the compile log of an interactor (problemas-datos-3), only
    # for those who may edit the problem.
    error = submission.error or ''
    if 'IE' in (submission.status, submission.result) and not submission.problem.is_editable_by(request.user):
        error = ''
    return json_response({'id': submission.pk, 'language': submission.language.name,
                          'source': submission.source.source, 'status': submission.status,
                          'result': submission.result, 'error': error})


# ---------------------------------------------------------------------------------------------------------------
# Searches
# ---------------------------------------------------------------------------------------------------------------

@manager_required
@require_GET
def manage_problem_search(request):
    request.no_profile_update = True
    term = request.GET.get('term', '').strip()[:100]
    problems = engine.visible_problems(request.user)
    if term:
        problems = problems.filter(Q(code__icontains=term) | Q(name__icontains=term))
    problems = problems.order_by('code')[:SEARCH_LIMIT]
    players = []
    for slot in (SLOT_A, SLOT_B):
        players.append((slot, _profile(request.GET.get(slot))))
    used = set()
    match = None
    wanted = _query_id(request, 'match')
    if wanted is not None:
        # Not for whoever plays in that tournament (2026-10-28): which problems are taken told them the problems of
        # their next game.
        match = ArenaMatch.objects.select_related('tournament').filter(pk=wanted).first()
        if match is not None and not access.can_manage_match(request.user, match):
            match = None
        if match is not None and match.tournament_id:
            used = engine.problems_used_in_tournament(match.tournament)
    results = []
    for problem in problems:
        warnings = engine.problem_warnings(problem, players)
        if problem.id in used:
            warnings.append('used')
        results.append({'id': problem.id, 'code': problem.code, 'name': problem.name, 'points': problem.points,
                        'warnings': warnings})
    return json_response({'results': results})


@manager_required
@require_GET
def manage_user_search(request):
    request.no_profile_update = True
    term = request.GET.get('term', '').strip()[:100]
    if not term:
        return json_response({'results': []})
    # ?organizers=1: only who could be a co-organizer, that is, who has a permission to organize battles.
    profiles = access.eligible_organizers() if request.GET.get('organizers') else \
        Profile.objects.filter(user__is_active=True)
    profiles = list(profiles.filter(Q(user__username__icontains=term) | Q(username_display_override__icontains=term))
                    .select_related('user').order_by('user__username')[:SEARCH_LIMIT])
    # Who is busy and who organizes, for all the results at once (duelos-gestion-js-3, 2026-10-28). An account that
    # organizes battles is marked like staff (serialize.privileged).
    ids = [profile.pk for profile in profiles]
    busy = engine.busy_profile_ids(ids)
    organizers = set(access.eligible_organizers().filter(pk__in=ids).values_list('pk', flat=True))
    duel = serialize.duel_rating_map(ids)
    return json_response({'results': [{
        'id': profile.pk, 'username': profile.username, 'name': profile.display_name,
        'avatar': gravatar(profile, 48), 'rating': profile.rating, 'duel_rating': duel.get(profile.pk),
        'privileged': bool(profile.user.is_staff or profile.user.is_superuser or profile.pk in organizers),
        'busy': profile.pk in busy,
    } for profile in profiles]})


# ---------------------------------------------------------------------------------------------------------------
# Tournaments
# ---------------------------------------------------------------------------------------------------------------

def _bank(value, user, current=None):
    """A bank `user` may give to a tournament or change: one of theirs, or the one the tournament already has, which
    stays as it is. Never another one of a tournament they still play (access.contended_banks, 2026-10-28): it is
    closed to them, answered as if it did not exist. Keeping the one the tournament already has gives them nothing
    (round 3 of the adversarial review): refusing it stopped them from saving the settings of a tournament they run
    that shares its bank with one they play, because the page always sends the bank."""
    if value in (None, '', 0):
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArenaError(_('Invalid problem bank.'))
    bank = ArenaProblemBank.objects.filter(pk=value).first()
    if bank is not None and bank.pk == current:
        return bank
    if bank is not None and access.contends_bank(user, bank.pk):
        raise ArenaError(_('That bank does not exist.'), status=404)
    if bank is None or not access.can_manage_bank(user, bank):
        raise ArenaError(_('That bank does not exist.'))
    return bank


def _refuse_closed_bank(user, bank_id):
    """Drawing from the bank of a tournament `user` still plays reads that bank (access.contended_banks): another
    tournament of theirs that takes the same bank does not draw from it through them."""
    if access.contends_bank(user, bank_id):
        raise ArenaError(_('That bank does not exist.'), status=404)


def _profiles(values, empty=False):
    """The participants sent by a page. With `empty`, a null is a place without a name yet (2026-11-03)."""
    if not isinstance(values, list) or len(values) > arena_tournament.MAX_PARTICIPANTS * 2:
        raise ArenaError(_('Invalid list of participants.'))
    profiles = []
    for value in values:
        if empty and value is None:
            profiles.append(None)
            continue
        profile = _profile(value)
        if profile is None:
            raise ArenaError(_('One of the participants does not exist.'))
        profiles.append(profile)
    return profiles


@manager_required
@require_POST
@arena_errors
def manage_tournament_create(request):
    data = read_json(request)
    tournament = arena_tournament.create_tournament(
        request.profile, data.get('name'), _profiles(data.get('participants') or []),
        seeding=data.get('seeding', ArenaTournament.SEED_MANUAL), third_place=bool(data.get('third_place')),
        rules=data.get('rules') or {}, description=data.get('description', ''),
        bank=_bank(data.get('bank'), request.user),
        show_in_history=_history_box(data.get('show_in_history'), True), planned_at=_planned(data.get('planned_at')),
        ranking=_ranking(request, data.get('ranking')), places=data.get('places'),
    )
    return json_response({'slug': tournament.slug,
                          'url': reverse('arena_manage_tournament', args=[tournament.slug])})


@manager_required
@require_GET
def manage_tournament_state(request, slug):
    request.no_profile_update = True
    tournament = _own_tournament(request, slug, page=True)
    return json_response(serialize.tournament_state(tournament, request.LANGUAGE_CODE, user=request.user))


@manager_required
@require_POST
@arena_errors
def manage_tournament_action(request, slug):
    tournament = _own_tournament(request, slug)
    data = read_json(request)
    action = data.get('action')
    version = data.get('version')
    actor = request.profile
    result = {}
    if action == 'update':
        arena_tournament.update_tournament(
            actor, tournament.pk, version, name=data.get('name'), description=data.get('description'),
            rules=data.get('rules'),
            bank=_bank(data.get('bank'), request.user, tournament.bank_id) if 'bank' in data else False,
            third_place=data.get('third_place'), draw=data.get('draw'),
            show_in_history=_history_box(data.get('show_in_history')),
            planned_at=_planned(data['planned_at']) if 'planned_at' in data else False)
    elif action == 'participants':
        changed = arena_tournament.set_participants(
            actor, tournament.pk, _profiles(data.get('participants') or [], empty=True), seeding=data.get('seeding'),
            version=version, places=data.get('places'))
        result['lost_problems'] = changed.lost_problems
    elif action == 'rounds':
        arena_tournament.set_rounds(actor, tournament.pk, data.get('rounds'), version=version)
    elif action == 'round':
        number = data.get('number')
        if isinstance(number, bool) or not isinstance(number, int):
            raise ArenaError(_('That round does not exist.'), status=404)
        arena_tournament.set_round(actor, tournament.pk, number, best_of=data.get('best_of'),
                                   rules=data.get('rules'), name=data.get('name'), version=version,
                                   draw=data.get('draw'), starter_code=data.get('starter_code'),
                                   autocomplete=data.get('autocomplete'))
    elif action == 'generate':
        arena_tournament.generate_bracket(actor, tournament.pk, version)
    elif action == 'start':
        arena_tournament.start_tournament(actor, tournament.pk, version, shrink=data.get('shrink') is True)
    elif action == 'archive':
        arena_tournament.archive_tournament(actor, tournament.pk, version)
    elif action == 'cancel':
        arena_tournament.cancel_tournament(actor, tournament.pk, version)
    elif action == 'fill':
        _refuse_closed_bank(request.user, tournament.bank_id)
        result['filled'] = arena_tournament.fill_ready_matches(actor, tournament.pk)
    elif action == 'organizers':
        _set_organizers(actor, tournament, _organizers(data.get('organizers'), exclude=tournament.created_by_id),
                        tournament=tournament)
    else:
        raise ArenaError(_('Unknown action.'))
    tournament.refresh_from_db()
    result['state'] = serialize.tournament_state(tournament, request.LANGUAGE_CODE, user=request.user)
    return json_response(result)


@manager_required
@require_POST
@arena_errors
def manage_draw(request, match):
    """Suggest problems for one game from the tournament bank, with the count and difficulties of its round.
    Nothing is saved: the suggestion goes into the list, and the organizer saves it or changes it."""
    data = read_json(request)
    match_obj = _own_match(request, match)
    _refuse_closed_bank(request.user, match_obj.tournament.bank_id if match_obj.tournament_id else None)
    game_id, number = _id(data.get('game')), _id(data.get('number'))
    games = ArenaGame.objects.filter(match=match_obj)
    game = games.filter(pk=game_id).first() if game_id else (games.filter(number=number).first() if number else None)
    rules = arena_rules.match_rules(match_obj)
    settings = arena_tournament.draw_settings(match_obj)
    problems, warnings = arena_tournament.draw_problems(
        request.profile, match_obj, game, settings['count'], level_min=settings['level_min'],
        level_max=settings['level_max'])
    players = [(slot, match_obj.player(slot)) for slot in (SLOT_A, SLOT_B)]
    return json_response({'problems': [{'id': p.id, 'code': p.code, 'name': p.name,
                                        'points': arena_rules.DEFAULT_POINTS,
                                        'warnings': engine.problem_warnings(p, players)} for p in problems],
                          'warnings': warnings, 'partial': rules['partial']})


# ---------------------------------------------------------------------------------------------------------------
# Banks and screens
# ---------------------------------------------------------------------------------------------------------------

@manager_required
@require_POST
@arena_errors
def manage_bank_action(request):
    data = read_json(request)
    action = data.get('action')
    if action == 'create':
        bank = arena_tournament.create_bank(request.profile, data.get('name'), data.get('description', ''))
    elif action == 'problems':
        bank = _bank(data.get('bank'), request.user)
        if bank is None:
            raise ArenaError(_('That bank does not exist.'))
        bank = arena_tournament.set_bank_problems(request.profile, bank.pk, data.get('problems'))
    else:
        raise ArenaError(_('Unknown action.'))
    return json_response({'id': bank.pk, 'url': reverse('arena_manage_banks') + '?bank=%d' % bank.pk})


def _match_arg(value, user):
    # What a screen shows: only what the panel would show this organizer (2026-10-28), never a battle they play.
    value = _id(value)
    if value is None:
        return None
    match = ArenaMatch.objects.select_related('tournament').filter(pk=value).first()
    if match is None or not access.can_manage_match(user, match):
        raise ArenaError(_('That battle does not exist.'), status=404)
    return match


def _tournament_arg(value, user):
    value = _id(value)
    if value is None:
        return None
    tournament = ArenaTournament.objects.filter(pk=value).first()
    if tournament is None or not access.can_manage_tournament(user, tournament):
        raise ArenaError(_('That tournament does not exist.'), status=404)
    return tournament


@manager_required
@require_POST
@arena_errors
def manage_screen_action(request):
    data = read_json(request)
    action = data.get('action')
    actor = request.profile
    result = {}
    # Connecting a projector with its code as a new one (2026-11-03): the code is checked before creating it.
    if action == 'pair' and data.get('screen') == 'new':
        if not engine.pairing_open(data.get('code')):
            raise ArenaError(_('That code does not exist or has expired. Check the code on the projector.'))
        mine = access.screens_for(request.user).count()
        screen = engine.create_screen(actor, _('Projector %(number)s') % {'number': mine + 1})
        engine.pair_screen(actor, data.get('code'), screen)
        result.update({'screens': serialize.screens_overview(request.user), 'paired': screen.name})
        return json_response(result)
    if action != 'create':
        screen_id = _id(data.get('screen'), True)
        if not access.screens_for(request.user).filter(pk=screen_id).exists():
            raise ArenaError(_('That screen does not exist.'), status=404)
    if action == 'create':
        engine.create_screen(actor, data.get('name'))
    elif action == 'show':
        view = data.get('view')
        engine.show_on_screen(actor, screen_id, data.get('mode'), match=_match_arg(data.get('match'), request.user),
                              tournament=_tournament_arg(data.get('tournament'), request.user),
                              message=data.get('message', ''), follow_live=data.get('follow_live'),
                              version=data.get('version'), view=view if isinstance(view, str) else None)
    elif action == 'pair':
        screen = engine.pair_screen(actor, data.get('code'), ArenaScreen.objects.get(pk=screen_id))
        result['paired'] = screen.name
    elif action == 'rename':
        engine.rename_screen(actor, screen_id, data.get('name'))
    elif action == 'regenerate':
        engine.regenerate_screen_token(actor, screen_id)
    else:
        raise ArenaError(_('Unknown action.'))
    result['screens'] = serialize.screens_overview(request.user)
    return json_response(result)
