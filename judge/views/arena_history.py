"""The public history of the duel tournaments (2026-10-25): /duelos/torneos/ and the page of every tournament.

Open to anybody, even without an account; judge/arena/history.py says which tournaments and what of them. Everything
under /duelos/ is let through by the lock of a battle (judge/arena/lock.py), so a player held in a battle who opens
one of these pages is sent back to it, and a page left open takes them there by itself once they are called (it
asks `arena_history_called` every few seconds).

A tournament being played refreshes its page every few seconds. That answer is the same for everybody and is kept
for a few seconds, so many people watching cost about as much as one; it never moves a game forward.

Since 2026-11-03 the finished tournaments come 20 at a time (?page=), with the pages of the site's lists; what is
being played and what comes next, only on the first page. The ranking and the page to decide what counts use the
same pages (`paginate`, `page_links`).
"""
import hashlib
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import InvalidPage
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST, require_safe

from judge.arena import access, history, lock, serialize, tournament as arena_tournament
from judge.arena.engine import ArenaError
from judge.models.arena import ArenaTournament
from judge.utils.diggpaginator import DiggPaginator
from judge.views.arena import json_error, json_response, read_json

__all__ = ['history_list', 'history_tournament', 'history_tournament_state', 'history_called', 'history_visibility',
           'paginate', 'page_links']

STATE_SECONDS = 4
POLL_MS = 8000
CALLED_POLL_MS = 5000


def _held(request):
    profile = getattr(request, 'profile', None)
    return profile is not None and lock.is_held(profile.pk)


def paginate(items, per_page, number, clamp=False):
    """Page `number` of `items` (a list or an ordered queryset), with the paginator of the site's lists
    (DiggPaginatorMixin, list-pages.html). `number` as it came in the address; none is the first page. One that is
    not a whole number, below 1 or past the end is a 404, as on the lists of problems and users; with `clamp`, one
    past the end is the last page instead (the page to decide loads itself again after every decision, and its last
    page can empty)."""
    paginator = DiggPaginator(items, per_page, body=6, padding=2)
    try:
        number = int(number or 1)
    except (TypeError, ValueError):
        raise Http404()
    if clamp:
        number = min(number, paginator.num_pages)
    try:
        return paginator.page(number)
    except InvalidPage:
        raise Http404()


def page_links(path, name='page', keep=(), anchor=''):
    """page_prefix, page_suffix and first_page_href for list-pages.html: `path`, the pairs of `keep` that have a
    value (a filter, the page of another list) and `name` with the number last, then `anchor`."""
    query = urlencode([(key, value) for key, value in keep if value])
    first = '%s?%s' % (path, query) if query else path
    return {'page_prefix': '%s%s%s=' % (first, '&' if query else '?', name), 'page_suffix': anchor,
            'first_page_href': first + anchor}


def _base_context(request, tab):
    # Only somebody signed in can be called to a battle: the page asks for that only then.
    return {'history_tab': tab, 'can_manage': access.can_manage(request.user),
            'watch_called': request.user.is_authenticated, 'called_poll': CALLED_POLL_MS}


# GET and HEAD, as every page of the site (produccion-datos-docs-2, 2026-10-28).
@require_safe
def history_list(request):
    if _held(request):
        return HttpResponseRedirect(lock.HOME)
    finished = paginate(history.finished(), history.FINISHED_PER_PAGE, request.GET.get('page'))
    context = _base_context(request, 'tournaments')
    context.update({'title': _('Duel tournaments'), 'sections': history.listing(request.user, finished), 'full': True,
                    'finished_links': page_links(request.path)})
    return render(request, 'arena/history/list.html', context)


@require_safe
def history_tournament(request, slug):
    found = history.find(request.user, slug)
    if found is None:
        raise Http404()
    if _held(request):
        return HttpResponseRedirect(lock.HOME)
    tournament, preview = found
    data = history.page(tournament, request.user)
    context = _base_context(request, 'tournaments')
    context.update({
        'title': tournament.name,
        'data': data,
        'preview': preview,
        'can_change': access.can_change_history(request.user, tournament),
        'bracket': serialize.bracket_view(tournament) if data['started'] else None,
        'poll': POLL_MS,
    })
    return render(request, 'arena/history/tournament.html', context)


def _live_state(tournament, request):
    """What a page of a tournament being played asks for: the bracket and the results, the same for everybody."""
    key = 'arena:history:%d:%s' % (tournament.pk, request.LANGUAGE_CODE)
    state = cache.get(key)
    if state is None:
        data = history.page(tournament, None)
        results = render_to_string('arena/history/_results.html', {'data': data}, request)
        bracket = serialize.bracket_view(tournament)
        state = {'status': tournament.status, 'bracket': bracket, 'results': results,
                 'key': hashlib.sha256(results.encode('utf-8')).hexdigest()[:16]}
        cache.set(key, state, STATE_SECONDS)
    return state


@require_GET
def history_tournament_state(request, slug):
    request.no_profile_update = True
    found = history.find(request.user, slug)
    if found is None:
        raise Http404()
    tournament, _preview = found
    if tournament.status != ArenaTournament.RUNNING:
        # Over (or not started): the page loads itself again, with the problems named for whoever looks.
        return json_response({'status': tournament.status, 'held': _held(request)})
    state = dict(_live_state(tournament, request))
    state['held'] = _held(request)
    return json_response(state)


@require_GET
def history_called(request):
    """Whether the person looking at the history has just been called to a battle. Only the lock in the cache."""
    request.no_profile_update = True
    return json_response({'held': _held(request), 'home': lock.HOME})


@login_required
@require_POST
def history_visibility(request, slug):
    """Show or hide a tournament in the history, from its public page: its organizers and whoever moderates."""
    tournament = ArenaTournament.objects.filter(slug=slug).first()
    if tournament is None or not access.can_change_history(request.user, tournament):
        raise Http404()
    try:
        data = read_json(request)
        show = data.get('show')
        if not isinstance(show, bool):
            raise ArenaError(_('Invalid request.'))
        tournament = arena_tournament.set_show_in_history(request.profile, tournament.pk, show)
    except ArenaError as error:
        return json_error(error.message, error.status)
    # Nothing kept in the cache needs to go: a hidden tournament is not found (404) before the cache is read.
    return json_response({'show': tournament.show_in_history})
