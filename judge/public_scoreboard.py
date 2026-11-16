"""The public scoreboard of a contest (2026-11-10): what /marcador/<key>/ shows to anybody, without an account.

Only a visible contest whose `public_scoreboard` is on has one: a contest that the site hides (one being prepared, a
clone) has none, whatever its box says. The page asks for one small document, the snapshot, and draws
everything from it: the problems, the rows in the order of the scoreboard, and the last submissions. It is told that
something happened by the event daemon (a submission arrives: `submitted`, here; a verdict: the `contest_<id>` that the
bridge already posts) and then it asks for the snapshot again. So nothing that the page shows depends on having heard
every event.

What it costs. A snapshot is built at most once per change: every request within FRESH seconds of the last check
gets the stored one without a query, and a check is three small aggregate queries (`_fingerprint`). The stored one
lives in the cache, so the six processes share it.

The freeze. With the scoreboard frozen, a row is its frozen copy, in the frozen order, and what was sent since is only
a count: the «?» of the cell and a line with no verdict in the list. Nothing else of a hidden submission leaves here.

This module is imported by judge.judgeapi, which the models import: it must not import them at module level.
"""
import hashlib
import json
import logging
import re
import time
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from judge import event_poster as event

logger = logging.getLogger('judge.public_scoreboard')

KEY_RE = re.compile(r'^[a-z0-9]{1,20}$')
COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
CHANNEL = 'marcador_%d'
VERSION = 1

FRESH = 0.1         # seconds a stored snapshot is served without looking at the database
MAX_AGE = 20.0      # seconds after which it is built again even if nothing that is watched changed (a name, a colour)
OFF_FOR = 2         # seconds a «there is no such scoreboard» is remembered
KEEP_FOR = 300      # seconds the cache keeps a snapshot nobody asks for
LOCK_FOR = 5        # seconds a process may take to build before another one tries
QUEUE = 40          # submissions in the list
# With the scoreboard frozen, a verdict that landed after the freeze for something sent just before it is not in the
# frozen copy. A cell whose copy differs from the live one hides what began to be judged this close to the freeze.
RACE = timedelta(seconds=90)

# For problems without a balloon colour of their own: twenty that can be told apart, none of them close to white.
PALETTE = ('#e6194b', '#3cb44b', '#ffd21f', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#bfef45', '#ff8fb4',
           '#469990', '#b78cff', '#9a6324', '#800000', '#808000', '#000075', '#7f8c8d', '#00c781', '#ff5e3a', '#1e9fd3')

_memo = {}          # per process: contest key -> the entry last read, so that a burst costs no trip to the cache


def _cache_key(key):
    return 'marcador:%d:%s' % (VERSION, key)


def letter(index):
    index += 1
    out = ''
    while index > 0:
        out = chr((index - 1) % 26 + 65) + out
        index = (index - 1) // 26
    return out


def _ms(moment):
    return int(moment.timestamp() * 1000)


def _number(value, digits=3):
    """A score as JSON: an int when it is whole, so that 100.0 does not travel as «100.0»."""
    try:
        value = round(float(value or 0), digits)
    except (TypeError, ValueError):
        return 0
    return int(value) if value == int(value) else value


def submitted(submission):
    """Called when a submission has been handed to the judges: tells the public scoreboard of its contest, if it has
    one, so that the «?» shows at once. It never raises: a scoreboard must not be able to break a submission."""
    try:
        contest_id = getattr(submission, 'contest_object_id', None)
        if not contest_id:
            return
        from judge.models import Contest
        if Contest.objects.filter(id=contest_id, public_scoreboard=True, is_visible=True).exists():
            event.post(CHANNEL % contest_id, {'type': 'envio'})
    except Exception:
        logger.exception('public scoreboard: could not announce submission %s', getattr(submission, 'id', None))


def _load(key):
    from judge.models import Contest
    return Contest.objects.filter(key=key).only(
        'id', 'key', 'name', 'start_time', 'end_time', 'time_limit', 'freeze_minutes', 'scoreboard_revealed',
        'format_name', 'format_config', 'participation_mode', 'attendance_mode', 'public_scoreboard', 'is_visible',
        'points_precision').first()


def _frozen(contest, now):
    cutoff = contest.submission_freeze_cutoff
    return bool(contest.freeze_active and cutoff is not None and now >= cutoff)


def _fingerprint(contest, now):
    """Everything that, when it changes, changes what the page shows. Never sent to anybody: under a freeze it
    moves with submissions that the snapshot only counts."""
    from django.db.models import Count, Max, Q, Sum
    from judge.models import ContestParticipation, ContestSubmission, Submission

    started = now >= contest.start_time
    parts = [VERSION, contest.id, contest.name, contest.start_time, contest.end_time, contest.time_limit,
             contest.freeze_minutes, contest.scoreboard_revealed, contest.format_name, contest.participation_mode,
             contest.attendance_mode, contest.points_precision, started, now > contest.end_time, _frozen(contest, now)]
    if started:
        parts.append(sorted(ContestSubmission.objects.filter(
            participation__contest_id=contest.id, participation__virtual=ContestParticipation.LIVE,
        ).aggregate(
            n=Count('id'), last=Max('id'), points=Sum('points'), began=Max('submission__judged_date'),
            again=Max('submission__rejudged_date'),
            busy=Count('id', filter=Q(submission__status__in=Submission.IN_PROGRESS_GRADING_STATUS)),
            odd=Count('id', filter=Q(submission__result__in=('CE', 'IE', 'AB'))),
        ).items()))
        parts.append(sorted(ContestParticipation.objects.filter(
            contest_id=contest.id, virtual=ContestParticipation.LIVE,
        ).aggregate(
            n=Count('id'), last=Max('id'), score=Sum('score'), cumtime=Sum('cumtime'), copies=Count('frozen_at'),
            out=Count('id', filter=Q(is_disqualified=True)), onsite=Count('id', filter=Q(onsite=True)),
        ).items()))
    else:
        parts.append(ContestParticipation.objects.filter(
            contest_id=contest.id, virtual=ContestParticipation.REGISTERED).count())
    return hashlib.sha1(repr(parts).encode('utf-8')).hexdigest()


def build(contest, now=None):
    """The snapshot of a contest as a dict. See the module docstring for what may and may not be in it."""
    from django.db.models import Prefetch
    from judge.models import Contest, ContestParticipation, ContestSubmission, Organization, Submission

    now = now or timezone.now()
    cutoff = contest.submission_freeze_cutoff
    frozen = _frozen(contest, now)
    icpc = contest.format_name == 'icpc'
    mode = contest.participation_mode
    data = {
        'v': VERSION, 'id': contest.id, 'nombre': contest.name,
        'ini': _ms(contest.start_time), 'fin': _ms(contest.end_time),
        'corte': _ms(cutoff) if cutoff is not None and contest.freeze_active else None,
        'cong': frozen, 'icpc': icpc, 'hib': contest.attendance_mode == Contest.HYBRID,
        'mixto': mode == Contest.MIXED, 'pr': [], 'f': [], 'e': [], 'reg': None,
    }
    if now < contest.start_time:
        # Before the start there is nobody on the scoreboard, and neither how many problems there are.
        data['reg'] = contest.users.waiting_registrations().count()
        return data

    problems = list(contest.contest_problems.order_by('order', 'id').values_list('id', 'points', 'balloon_color'))
    index = {}
    for position, (problem_id, points, color) in enumerate(problems):
        index[problem_id] = position
        data['pr'].append({
            'l': letter(position), 'm': _number(points),
            'c': color.lower() if color and COLOR_RE.match(color) else PALETTE[position % len(PALETTE)],
        })
    count = len(problems)

    queryset = contest.users.filter(virtual=ContestParticipation.LIVE, is_disqualified=False).without_contest_staff()
    if mode == Contest.TEAM:
        queryset = queryset.filter(team__isnull=False)
    elif mode != Contest.MIXED:
        queryset = queryset.filter(team__isnull=True)
    participations = list(queryset.select_related('user__user').prefetch_related(
        'team_roster', Prefetch('user__organizations', queryset=Organization.objects.only('id', 'short_name')),
    ).only(
        'id', 'contest_id', 'user_id', 'team_id', 'team_name', 'virtual', 'real_start', 'score', 'cumtime',
        'tiebreaker', 'format_data', 'frozen_score', 'frozen_cumtime', 'frozen_tiebreaker', 'frozen_format_data',
        'frozen_at', 'onsite', 'user__id', 'user__user_id', 'user__username_display_override',
        'user__user__id', 'user__user__username', 'user__user__first_name', 'user__user__last_name',
    ))

    by_id, starts = {}, {}
    for participation in participations:
        # The contest already in hand: `participation.contest` would be one query per row.
        participation.contest = contest
        by_id[participation.id] = participation
        starts[participation.id] = participation.freeze_starts_at if frozen else None

    # One cell per row and problem: [counted attempts, in review, hidden by the freeze, first to solve it].
    marks = {participation.id: [[0, 0, 0, 0] for _ in range(count)] for participation in participations}
    # Whose copy differs from the live cell: what the frozen scoreboard is holding back (frozen_pending_map).
    differs = set()
    if frozen:
        for participation in participations:
            if participation.frozen_at is None:
                continue
            kept, live = participation.frozen_format_data or {}, participation.format_data or {}
            for cell_key in set(kept) | set(live):
                if kept.get(cell_key) == live.get(cell_key):
                    continue
                try:
                    position = index.get(int(cell_key))
                except (TypeError, ValueError):
                    continue
                if position is not None:
                    differs.add((participation.id, position))

    busy_states = Submission.IN_PROGRESS_GRADING_STATUS
    first = [None] * count
    queue = []
    rows = ContestSubmission.objects.filter(
        participation__contest_id=contest.id, participation__virtual=ContestParticipation.LIVE,
    ).order_by('submission__date', 'id').values_list(
        'id', 'participation_id', 'problem_id', 'points', 'submission__date', 'submission__status',
        'submission__result', 'submission__judged_date', 'submission__rejudged_date')
    for sub_id, participation_id, problem_id, points, date, status, result, began, again in rows:
        position = index.get(problem_id)
        if participation_id not in by_id or position is None:
            continue
        mark = marks[participation_id][position]
        busy = status in busy_states
        start = starts[participation_id]
        hidden = start is not None and (
            date >= start or busy or (began is not None and began >= start) or
            (again is not None and again >= start) or
            ((participation_id, position) in differs and (began or date) >= start - RACE))
        shown = 0
        if hidden:
            mark[2] += 1
            state = 'c'
        elif busy:
            mark[1] += 1
            state = 'p'
        elif result in (None, 'IE', 'AB'):
            state = 'x'
        else:
            if not (icpc and result == 'CE'):
                mark[0] += 1
            best = problems[position][1] or 0
            points = points or 0
            shown = _number(points)
            if best > 0 and points >= best - 1e-9:
                state = 'o'
                if first[position] is None:
                    first[position] = sub_id
                    mark[3] = 1
            else:
                state = 'a' if points > 0 else 'm'
        seconds = int((date - contest.start_time).total_seconds())
        queue.append([sub_id, participation_id, position, max(seconds, 0), state, shown,
                      1 if first[position] == sub_id else 0])

    ranked = []
    for participation in participations:
        copied = frozen and participation.frozen_at is not None
        cells_data = (participation.frozen_format_data if copied else participation.format_data) or {}
        score = (participation.frozen_score if copied else participation.score) or 0
        cumtime = (participation.frozen_cumtime if copied else participation.cumtime) or 0
        tiebreaker = (participation.frozen_tiebreaker if copied else participation.tiebreaker) or 0
        cells = []
        for position, (problem_id, _points, _color) in enumerate(problems):
            mark = marks[participation.id][position]
            if (participation.id, position) in differs and not mark[2]:
                mark[2] = 1
            cell = cells_data.get(str(problem_id))
            if not isinstance(cell, dict):
                cell = None
            if cell is None and not any(mark[:3]):
                cells.append(0)
                continue
            try:
                points, seconds = _number((cell or {}).get('points')), int((cell or {}).get('time') or 0)
            except (TypeError, ValueError):
                points, seconds = 0, 0
            cells.append([points, max(seconds, 0)] + mark)

        if participation.team_id:
            roster = sorted(participation.team_roster.all(), key=lambda member: member.id)
            name, real, organization = participation.team_name, ' · '.join(member.username for member in roster), ''
            pictures = [member.profile_id for member in roster]
        else:
            profile = participation.user
            name = profile.display_name
            real = ' '.join(profile.user.get_full_name().split())
            organization = profile.organization.short_name if profile.organization else ''
            pictures = [profile.id]
        ranked.append(((-score, cumtime, tiebreaker, participation.id), {
            'i': participation.id, 'n': name, 'r': real, 'o': organization, 'a': pictures,
            'e': 1 if participation.team_id else 0, 's': 1 if participation.onsite else 0,
            'p': _number(score, contest.points_precision), 't': int(cumtime), 'b': _number(tiebreaker), 'c': cells,
        }))
    ranked.sort(key=lambda pair: pair[0])
    data['f'] = [row for _key, row in ranked]
    data['e'] = queue[:-QUEUE - 1:-1]
    return data


def _dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def _make_entry(contest, now, fingerprint):
    body = _dump(build(contest, now))
    stamp = time.time()
    return {'ts': stamp, 'built': stamp, 'fp': fingerprint, 'body': body, 'id': contest.id, 'name': contest.name,
            'tag': hashlib.sha1(body.encode('utf-8')).hexdigest()[:20]}


def entry_for(key):
    """The stored snapshot of the contest with this key: a dict with `body` (JSON text) and `tag` (what a reader
    sends back to be told «nothing new»), or None when there is no such public scoreboard."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None
    stamp = time.time()
    entry = _memo.get(key)
    if entry is not None and 0 <= stamp - entry['ts'] < FRESH:
        return entry
    cache_key = _cache_key(key)
    try:
        entry = cache.get(cache_key)
    except Exception:
        logger.exception('public scoreboard: the cache does not answer')
        entry = None
    if entry is not None and 0 <= stamp - entry['ts'] < (OFF_FOR if entry.get('off') else FRESH):
        return _keep(key, entry)

    # Stale or missing. Only one process looks at the database; the rest serve what there is meanwhile.
    lock_key = cache_key + ':lock'
    try:
        mine = cache.add(lock_key, 1, LOCK_FOR)
    except Exception:
        mine = True
    if not mine and entry is not None and not entry.get('off'):
        return _keep(key, entry, local=False)
    try:
        contest = _load(key)
        if contest is None or not contest.public_scoreboard or not contest.is_visible:
            fresh = {'ts': stamp, 'off': True}
        else:
            now = timezone.now()
            fingerprint = _fingerprint(contest, now)
            if (entry is not None and not entry.get('off') and entry.get('fp') == fingerprint and
                    0 <= stamp - entry.get('built', 0) < MAX_AGE):
                fresh = dict(entry, ts=time.time())
            else:
                fresh = _make_entry(contest, now, fingerprint)
        try:
            cache.set(cache_key, fresh, OFF_FOR if fresh.get('off') else KEEP_FOR)
        except Exception:
            logger.exception('public scoreboard: the cache does not keep')
    finally:
        if mine:
            try:
                cache.delete(lock_key)
            except Exception:
                pass
    return _keep(key, fresh)


def _keep(key, entry, local=True):
    if entry.get('off'):
        _memo.pop(key, None)
        return None
    if local:
        if len(_memo) > 64:
            _memo.clear()
        _memo[key] = entry
    return entry
