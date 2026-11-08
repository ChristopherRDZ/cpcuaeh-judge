"""Single-elimination tournaments: participants, seeding, bracket generation and moving winners forward.

The bracket is filled up to the next power of two with the standard seeding order (1 against 8, 4 against 5, 2
against 7, 3 against 6 for eight places), so the empty places, the byes, always fall against the best seeds and
never against each other. A match with a bye is decided on the spot.
"""
import logging
import secrets
from datetime import timedelta
from random import SystemRandom

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _, ngettext

from judge.arena import rules as arena_rules
from judge.arena.engine import ArenaError, _bump, _check_version, _close_contest, _log, clean_ranking, \
    ensure_default_screen, lock_match, problems_used_in_tournament, refresh_lock_on_commit, take_free_screen, \
    visible_problems
from judge.models import Submission
from judge.models.arena import ArenaBankProblem, ArenaGame, ArenaGameProblem, ArenaMatch, ArenaParticipant, \
    ArenaProblemBank, ArenaRating, ArenaRound, ArenaTournament, RANKING_COUNTS, RANKING_EXCLUDED, RANKING_PENDING, \
    SLOT_A, SLOT_B

logger = logging.getLogger('judge.arena')

MIN_PARTICIPANTS = 2
MAX_PARTICIPANTS = 64
# The planned date of a tournament (an announcement in the history, judge/arena/history.py) may be at most this far.
MAX_PLANNED_DAYS = 730


def seeding_order(size):
    """Seeds in bracket order for a power-of-two `size`: consecutive pairs play each other in the first round."""
    order = [1]
    while len(order) < size:
        total = len(order) * 2 + 1
        order = [seed for current in order for seed in (current, total - current)]
    return order


def bracket_size(count):
    size = 1
    while size < count:
        size *= 2
    return size


def round_count(count):
    size, rounds = bracket_size(count), 0
    while size > 1:
        size //= 2
        rounds += 1
    return rounds


def default_round_name(number, rounds):
    from_end = rounds - number
    if from_end == 0:
        return _('Final')
    if from_end == 1:
        return _('Semifinal')
    if from_end == 2:
        return _('Quarterfinal')
    if from_end == 3:
        return _('Round of 16')
    # Up to 64 places (2026-11-08): until then the first rounds of a bracket of 17 or more were «Round 1», «Round 2».
    if from_end == 4:
        return _('Round of 32')
    if from_end == 5:
        return _('Round of 64')
    return _('Round %(number)s') % {'number': number}


def round_name(round_obj, rounds):
    return round_obj.name or default_round_name(round_obj.number, rounds)


# Rules each round chooses for itself and the tournament never holds (2026-11-08): stored for the whole tournament,
# a round without its own value would take it, as the rounds that a bigger bracket adds at the start.
ROUND_RULES = ('starter_code',)


def _without_round_rules(rules):
    return {name: value for name, value in rules.items() if name not in ROUND_RULES}


def _unique_slug(name):
    base = slugify(name)[:50] or 'torneo'
    slug = base
    while ArenaTournament.objects.filter(slug=slug).exists():
        slug = '%s-%s' % (base, secrets.token_hex(2))
    return slug


def _clean_participants(profiles):
    seen, result = set(), []
    for profile in profiles:
        if profile is None or profile.pk in seen:
            continue
        seen.add(profile.pk)
        result.append(profile)
    if len(result) < MIN_PARTICIPANTS:
        raise ArenaError(_('A tournament needs at least %(count)s participants.') % {'count': MIN_PARTICIPANTS})
    if len(result) > MAX_PARTICIPANTS:
        raise ArenaError(_('A tournament can have at most %(count)s participants.') % {'count': MAX_PARTICIPANTS})
    return result


def clean_places(value):
    """How many places a bracket has (2026-11-03): from MIN_PARTICIPANTS to MAX_PARTICIPANTS, or None."""
    if value in (None, ''):
        return None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or \
            not MIN_PARTICIPANTS <= value <= MAX_PARTICIPANTS:
        raise ArenaError(_('A tournament has from %(min)s to %(max)s places.') %
                         {'min': MIN_PARTICIPANTS, 'max': MAX_PARTICIPANTS})
    return value


def _clean_slots(slots, places):
    """The places of a tournament as the page sends them: one entry per place, in order, a profile or None for a
    place without a name yet. Nobody twice, and nobody beyond the last place."""
    if not isinstance(slots, list):
        raise ArenaError(_('Invalid list of participants.'))
    slots = list(slots)
    while slots and slots[-1] is None:
        slots.pop()
    if len(slots) > places:
        raise ArenaError(_('There are more names than places: add places or remove somebody.'))
    seen = set()
    for profile in slots:
        if profile is None:
            continue
        if profile.pk in seen:
            raise ArenaError(_('%(name)s is in two places.') % {'name': profile.username})
        seen.add(profile.pk)
    return slots + [None] * (places - len(slots))


def _refuse_self(actor, profiles):
    """Whoever creates a tournament or changes its participants does not put themselves among them (2026-10-28,
    round 2 of the adversarial review), as nobody puts themselves in a battle (engine._refuse_self): a participant
    does not run the tournament while they play it (access.contended_tournaments), so the draft vanished from their
    panel, and with no co-organizer nobody was left to run it. Another organizer of the tournament adds them."""
    if actor is not None and any(profile.pk == actor.pk for profile in profiles):
        raise ArenaError(_('You cannot add yourself to a tournament you organize: another organizer has to add you.'))


def _ordered_by_seeding(profiles, seeding):
    if seeding == ArenaTournament.SEED_RANDOM:
        profiles = list(profiles)
        SystemRandom().shuffle(profiles)
        return profiles
    if seeding == ArenaTournament.SEED_RATING:
        return sorted(profiles, key=lambda p: (p.rating is None, -(p.rating or 0), p.pk))
    if seeding == ArenaTournament.SEED_DUEL:
        # The ranking of the duels (judge/arena/rating.py); who has played no game that counts goes last, in the order
        # of their contest rating.
        duel = dict(ArenaRating.objects.filter(profile_id__in=[p.pk for p in profiles])
                    .values_list('profile_id', 'rating'))
        return sorted(profiles, key=lambda p: (p.pk not in duel, -duel.get(p.pk, 0), p.rating is None,
                                               -(p.rating or 0), p.pk))
    return list(profiles)


def _lock_tournament(tournament_id):
    try:
        return ArenaTournament.objects.select_for_update().get(pk=tournament_id)
    except ArenaTournament.DoesNotExist:
        raise ArenaError(_('That tournament does not exist.'), status=404)


def clean_planned_at(value, now=None):
    """The planned date of a tournament: None, or a moment with its time zone that has not passed (a day of grace,
    for a tournament played late on its day) and is at most MAX_PLANNED_DAYS ahead."""
    if value is None:
        return None
    if timezone.is_naive(value):
        raise ArenaError(_('Invalid date.'))
    now = now or timezone.now()
    if value < now - timedelta(days=1):
        raise ArenaError(_('That planned date has already passed.'))
    if value > now + timedelta(days=MAX_PLANNED_DAYS):
        raise ArenaError(_('The planned date can be at most two years ahead.'))
    return value


@transaction.atomic
def create_tournament(actor, name, profiles, seeding=ArenaTournament.SEED_MANUAL, third_place=False, rules=None,
                      description='', bank=None, show_in_history=True, planned_at=None, ranking=RANKING_PENDING,
                      places=None):
    """`ranking`: whether it counts for the ranking of the duels, if whoever creates it may decide that.

    `places` (2026-11-03): how many places the bracket has. With it, `profiles` may be fewer, or none: the places
    without a name are filled later, and the problems can be chosen meanwhile. Without it, one place per profile, as
    before; either way a new tournament keeps its number of places, so removing somebody later leaves a place empty.

    One transaction (duelos-nucleo-1, 2026-10-28): from 2026-10-26 to 2026-10-28 the decorator sat on
    clean_planned_at by mistake, and a tournament with byes (3, 5, 6, 7... players) failed with a 500 when its byes
    locked their matches, half built."""
    name = (name or '').strip()[:100]
    if not name:
        raise ArenaError(_('Give the tournament a name.'))
    ranking = clean_ranking(ranking)
    if seeding not in dict(ArenaTournament.SEEDING):
        raise ArenaError(_('Invalid seeding.'))
    try:
        rules = _without_round_rules(arena_rules.clean_rules(rules))
    except ValidationError as error:
        raise ArenaError(error.messages[0])
    planned_at = clean_planned_at(planned_at)
    places = clean_places(places)
    if places is None:
        profiles = _clean_participants(profiles)
        places = len(profiles)
    else:
        profiles = [profile for profile in _clean_slots(list(profiles), places) if profile is not None]
    _refuse_self(actor, profiles)
    profiles = _ordered_by_seeding(profiles, seeding)
    tournament = ArenaTournament.objects.create(
        name=name, slug=_unique_slug(name), description=(description or '').strip()[:2000],
        # The order is applied once, here: from now on the list is the order (2026-11-03).
        seeding=ArenaTournament.SEED_MANUAL,
        third_place_match=bool(third_place), rules=rules, bank=bank, created_by=actor,
        show_in_history=bool(show_in_history), planned_at=planned_at, ranking=ranking, places=places,
    )
    ArenaParticipant.objects.bulk_create([ArenaParticipant(tournament=tournament, profile=profile, seed=index)
                                          for index, profile in enumerate(profiles, 1)])
    _sync_rounds(tournament, places)
    # The bracket is there from the start, so its matches can get their problems before anything else.
    _build_bracket(actor, tournament)
    _log('tournament_create', actor, tournament=tournament, participants=len(profiles), places=places,
         ranking=ranking)
    ensure_default_screen(actor)
    return tournament


def _sync_rounds(tournament, count):
    """One round per level of the bracket, keeping whatever was configured on each round by its place counted from
    the final (2026-11-03): the final stays the final, with its name and its games, when the bracket grows or shrinks.
    Until then the rounds were kept by their number from the first one, so a bracket of 8 that grew to 9 or more
    took its quarterfinal for the round of 16 and its final for a semifinal.

    The matches of the tournament must be gone already (_build_bracket): a round that goes takes its matches."""
    needed = round_count(count)
    rounds = list(ArenaRound.objects.filter(tournament=tournament).order_by('number'))
    shift = needed - len(rounds)
    if shift < 0:
        # The first rounds go; the others move down, in order, so no two share a number at any moment.
        ArenaRound.objects.filter(pk__in=[r.pk for r in rounds[:-shift]]).delete()
        for round_obj in rounds[-shift:]:
            round_obj.number += shift
            round_obj.save(update_fields=['number'])
    elif shift > 0:
        for round_obj in reversed(rounds):
            round_obj.number += shift
            round_obj.save(update_fields=['number'])
        ArenaRound.objects.bulk_create([ArenaRound(tournament=tournament, number=number)
                                        for number in range(1, shift + 1)])
    if ArenaRound.objects.filter(tournament=tournament).count() != needed or \
            set(ArenaRound.objects.filter(tournament=tournament).values_list('number', flat=True)) != \
            set(range(1, needed + 1)):
        raise ArenaError(_('The rounds of this tournament are out of order. Ask an administrator.'))


@transaction.atomic
def update_tournament(actor, tournament_id, version=None, name=None, description=None, rules=None, bank=False,
                      third_place=None, draw=None, show_in_history=None, planned_at=False):
    """`draw` holds how the matches get their problems: by hand, or by themselves from the bank (see rules.py).
    `planned_at`: False leaves it as it is, None takes it away."""
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    fields = []
    if show_in_history is not None and bool(show_in_history) != tournament.show_in_history:
        tournament.show_in_history = bool(show_in_history)
        fields.append('show_in_history')
    if planned_at is not False and planned_at != tournament.planned_at:
        tournament.planned_at = clean_planned_at(planned_at)
        fields.append('planned_at')
    if name is not None:
        name = name.strip()[:100]
        if not name:
            raise ArenaError(_('Give the tournament a name.'))
        tournament.name = name
        fields.append('name')
    if description is not None:
        tournament.description = description.strip()[:2000]
        fields.append('description')
    if rules is not None or draw is not None:
        try:
            cleaned = _without_round_rules(arena_rules.clean_rules(rules)) if rules is not None else None
            tournament.rules = arena_rules.with_draw(tournament.rules, cleaned, arena_rules.clean_draw(draw))
        except ValidationError as error:
            raise ArenaError(error.messages[0])
        fields.append('rules')
    if bank is not False:
        tournament.bank = bank
        fields.append('bank')
    rebuild = False
    if third_place is not None and bool(third_place) != tournament.third_place_match:
        if tournament.status != ArenaTournament.DRAFT:
            raise ArenaError(_('The bracket is already set; this can only change in a draft.'))
        tournament.third_place_match = bool(third_place)
        fields.append('third_place_match')
        rebuild = True
    tournament.version += 1
    tournament.save(update_fields=fields + ['version'])
    if rebuild:
        _build_bracket(actor, tournament)
    _log('tournament_update', actor, tournament=tournament, fields=fields)
    return tournament


@transaction.atomic
def set_show_in_history(actor, tournament_id, show):
    """From the public page of the tournament: whoever may (access.can_change_history) shows or hides it in the
    history. The version moves, so an organizer page open at that moment refreshes instead of undoing it."""
    tournament = _lock_tournament(tournament_id)
    if bool(show) != tournament.show_in_history:
        tournament.show_in_history = bool(show)
        tournament.version += 1
        tournament.save(update_fields=['show_in_history', 'version'])
        _log('history', actor, tournament=tournament, show=bool(show))
    return tournament


ORDERS = (ArenaTournament.SEED_RATING, ArenaTournament.SEED_DUEL, ArenaTournament.SEED_RANDOM)


@transaction.atomic
def set_participants(actor, tournament_id, profiles, seeding=None, version=None, places=None, order=None):
    """The places of a draft and who is in each one. Saving rebuilds the bracket at once: there is no separate step
    to forget, and the problems already chosen stay with their battles (_build_bracket).

    `profiles` has one entry per place, a profile or None for a place without a name yet (2026-11-03); `places`, how
    many there are (None leaves it as it is). For a tournament from before (no `places`) with no `places` given, the
    list is one place per profile, as it always was.

    `order` (or `seeding`, the old name): put the names in order once, by contest rating, by duel rating or at
    random; the empty places go after them. Until 2026-11-03 the order of a tournament was applied again at every
    save, so a random one was drawn anew each time; now the list is the order."""
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    if tournament.status != ArenaTournament.DRAFT:
        raise ArenaError(_('Participants can only change while the tournament is a draft.'))
    if order is None and seeding not in (None, ArenaTournament.SEED_MANUAL):
        order = seeding
    if order is not None and order not in ORDERS:
        raise ArenaError(_('Invalid seeding.'))
    places = clean_places(places)
    if places is None and tournament.places is None:
        slots = _clean_participants(profiles)
        places_value = None
    else:
        places_value = places or tournament.places
        slots = _clean_slots(profiles, places_value)
    names = [profile for profile in slots if profile is not None]
    _refuse_self(actor, names)
    if order is not None:
        names = _ordered_by_seeding(names, order)
        slots = names + [None] * (len(slots) - len(names))
    # Read before anything changes: the problems stay with their place in the bracket.
    chosen = _chosen_problems(tournament)
    ArenaParticipant.objects.filter(tournament=tournament).delete()
    ArenaParticipant.objects.bulk_create([ArenaParticipant(tournament=tournament, profile=profile, seed=index)
                                          for index, profile in enumerate(slots, 1) if profile is not None])
    tournament.places = places_value
    tournament.seeding = ArenaTournament.SEED_MANUAL
    tournament.version += 1
    tournament.save(update_fields=['seeding', 'places', 'version'])
    lost = _build_bracket(actor, tournament, chosen=chosen)
    _log('participants', actor, tournament=tournament, participants=len(names), places=places_value,
         order=order or '', lost=lost)
    # How many battles lost the problems chosen for them (a place that became a bye, a round that went): the page
    # says so. An attribute, so whoever called this before still gets the tournament.
    tournament.lost_problems = lost
    return tournament


@transaction.atomic
def set_round(actor, tournament_id, number, best_of=None, rules=None, name=None, version=None, draw=None,
              starter_code=None):
    """`starter_code` (2026-11-08): whether the editor starts with the C++ template in the games of this round that
    have not started yet; those started keep the rules they started with."""
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    round_obj = ArenaRound.objects.filter(tournament=tournament, number=number).first()
    if round_obj is None:
        raise ArenaError(_('That round does not exist.'), status=404)
    fields = []
    if best_of is not None:
        try:
            best_of = arena_rules.clean_best_of(best_of)
        except ValidationError as error:
            raise ArenaError(error.messages[0])
        if best_of != round_obj.best_of:
            started = ArenaGame.objects.filter(match__round=round_obj, contest__isnull=False).exists()
            if started:
                raise ArenaError(_('This round has already started; change the length of each match instead.'))
            round_obj.best_of = best_of
            fields.append('best_of')
            # Only when the round changes (2026-11-08): «Save» of «Rounds and problems» sends every round, and a battle
            # given other games on its own page went back to those of its round, mid-series too.
            ArenaMatch.objects.filter(round=round_obj).exclude(status=ArenaMatch.DECIDED).update(best_of=best_of)
    if rules is not None or draw is not None:
        kept = (round_obj.rules or {}).get('starter_code')
        try:
            cleaned = arena_rules.clean_rules(rules) if rules is not None else None
            draw = {key: value for key, value in arena_rules.clean_draw(draw).items() if key != 'auto_draw'}
            round_obj.rules = arena_rules.with_draw(round_obj.rules, cleaned, draw)
        except ValidationError as error:
            raise ArenaError(error.messages[0])
        if cleaned is not None and 'starter_code' not in cleaned and kept is not None:
            # Game rules sent without the template keep the one of the round.
            round_obj.rules['starter_code'] = kept
        fields.append('rules')
    if starter_code is not None:
        try:
            value = arena_rules.clean_rules({'starter_code': starter_code})['starter_code']
        except ValidationError as error:
            raise ArenaError(error.messages[0])
        if (round_obj.rules or {}).get('starter_code') != value:
            round_obj.rules = dict(round_obj.rules or {}, starter_code=value)
            if 'rules' not in fields:
                fields.append('rules')
    if name is not None:
        round_obj.name = name.strip()[:60]
        fields.append('name')
    if fields:
        round_obj.save(update_fields=fields)
    tournament.version += 1
    tournament.save(update_fields=['version'])
    _log('round', actor, tournament=tournament, number=number, fields=fields)
    return round_obj


@transaction.atomic
def generate_bracket(actor, tournament_id, version=None):
    """Build every match of the bracket from the seeds again. Only in a draft; problems already chosen stay."""
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    if tournament.status != ArenaTournament.DRAFT:
        raise ArenaError(_('The bracket can only be built while the tournament is a draft.'))
    _build_bracket(actor, tournament)
    return tournament


def _chosen_problems(tournament):
    """Problems already chosen for the games of a draft bracket, by place: (round counted from the final, position,
    third place, game). From the final (2026-11-03), so they stay with their battle when the bracket grows or
    shrinks: the battle of the places 1 and 8 of a bracket of 8 is the quarterfinal 1 in a bracket of 16 too."""
    chosen = {}
    total = ArenaRound.objects.filter(tournament=tournament).count()
    games = ArenaGame.objects.filter(match__tournament=tournament, contest__isnull=True).select_related('match__round')
    for game in games:
        rows = list(ArenaGameProblem.objects.filter(game=game).order_by('order').values_list('problem_id', 'points'))
        if rows and game.match.round_id:
            chosen[(total - game.match.round.number, game.match.position, game.match.is_third_place,
                    game.number)] = rows
    return chosen


def _build_bracket(actor, tournament, chosen=None):
    """Every match of the bracket from the seeds, for a draft tournament already locked by the caller. Returns how
    many battles lost problems that had been chosen for them, because they are gone or became a bye.

    Building again starts the matches over, but the problems an organizer already chose stay with their place in
    the bracket (round from the final, position, game), so changing the participants does not undo that work.
    `chosen`: read before the caller changed anything (set_participants); by default, read here.

    The bracket has `places` places (2026-11-03). A place without a name yet is kept, with its seed and no player,
    so its battle can get problems; only the places beyond the last one are byes. A bye of a place without a name
    sends that place, still without a name, to the next round.
    """
    participants = list(ArenaParticipant.objects.filter(tournament=tournament).select_related('profile')
                        .order_by('seed'))
    count = tournament.place_count(len(participants))
    if count < MIN_PARTICIPANTS:
        raise ArenaError(_('A tournament needs at least %(count)s participants.') % {'count': MIN_PARTICIPANTS})
    if chosen is None:
        chosen = _chosen_problems(tournament)
    # Games point to their match with PROTECT: the prepared ones go first (in a draft none has been played). The
    # matches go before the rounds change, which takes the matches of a round that goes (a 500 until 2026-11-03).
    ArenaGame.objects.filter(match__tournament=tournament, contest__isnull=True).delete()
    ArenaMatch.objects.filter(tournament=tournament).delete()
    _sync_rounds(tournament, count)
    rounds = {r.number: r for r in ArenaRound.objects.filter(tournament=tournament)}
    total_rounds = len(rounds)
    size = bracket_size(count)

    # Build from the final backwards so every match already knows where its winner goes.
    by_round = {}
    for number in range(total_rounds, 0, -1):
        matches = []
        for position in range(size >> number):
            following = by_round.get(number + 1)
            next_match = following[position // 2] if following else None
            matches.append(ArenaMatch.objects.create(
                tournament=tournament, round=rounds[number], position=position, best_of=rounds[number].best_of,
                next_match=next_match, next_slot=(SLOT_A if position % 2 == 0 else SLOT_B) if next_match else '',
                status=ArenaMatch.PENDING, created_by=actor,
            ))
        by_round[number] = matches
    # With three players one semifinal is a bye: it has no loser, so a third place match would wait forever for its
    # second player and the tournament would never finish. From four on, both semifinals are played.
    if tournament.third_place_match and total_rounds >= 2 and count >= 4:
        third = ArenaMatch.objects.create(
            tournament=tournament, round=rounds[total_rounds], position=1, is_third_place=True,
            best_of=rounds[total_rounds].best_of, status=ArenaMatch.PENDING, created_by=actor,
        )
        for position, semifinal in enumerate(by_round[total_rounds - 1]):
            semifinal.loser_next_match = third
            semifinal.loser_next_slot = SLOT_A if position == 0 else SLOT_B
            semifinal.save(update_fields=['loser_next_match', 'loser_next_slot'])

    seeds = {participant.seed: participant.profile for participant in participants}
    order = seeding_order(size)
    now = timezone.now()
    for position, match in enumerate(by_round[1]):
        seed_a, seed_b = order[2 * position], order[2 * position + 1]
        match.player_a, match.seed_a = seeds.get(seed_a), seed_a if seed_a <= count else None
        match.player_b, match.seed_b = seeds.get(seed_b), seed_b if seed_b <= count else None
        if match.player_a and match.player_b:
            match.status = ArenaMatch.READY
            match.save(update_fields=['player_a', 'seed_a', 'player_b', 'seed_b', 'status'])
        else:
            match.save(update_fields=['player_a', 'seed_a', 'player_b', 'seed_b'])
    # Byes: a place against nobody goes straight through, with its name or still without one.
    for match in by_round[1]:
        if match.seed_a is None or match.seed_b is None:
            match = lock_match(match.pk)
            slot = SLOT_A if match.seed_a is not None else SLOT_B
            match.status = ArenaMatch.DECIDED
            match.winner_slot = slot
            match.decided_reason = 'bye'
            match.decided_at = now
            _bump(match, 'status', 'winner_slot', 'decided_reason', 'decided_at')
            advance(match, now)
    restored = set()
    if chosen:
        for match in ArenaMatch.objects.filter(tournament=tournament).exclude(status=ArenaMatch.DECIDED) \
                .select_related('round'):
            where = (total_rounds - match.round.number, match.position, match.is_third_place)
            numbers = sorted(number for (from_final, position, third, number) in chosen
                             if (from_final, position, third) == where)
            for number in range(1, max(numbers, default=0) + 1):
                game = ArenaGame.objects.create(match=match, number=number)
                rows = chosen.get(where + (number,), [])
                ArenaGameProblem.objects.bulk_create([ArenaGameProblem(game=game, problem_id=problem_id, order=order,
                                                                       points=points)
                                                      for order, (problem_id, points) in enumerate(rows)])
            if numbers:
                restored.add(where)
    lost = len({key[:3] for key in chosen} - restored)
    tournament.version += 1
    tournament.save(update_fields=['version'])
    _log('bracket', actor, tournament=tournament, participants=len(participants), places=count, size=size, lost=lost)
    return lost


@transaction.atomic
def start_tournament(actor, tournament_id, version=None, shrink=False):
    """Start a draft. Every place needs a name (2026-11-03); with `shrink`, the places without one are dropped first:
    the names keep their order, and the bracket is rebuilt for that many places, as when the places are lowered."""
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    if tournament.status != ArenaTournament.DRAFT:
        raise ArenaError(_('This tournament has already started.'))
    participants = list(ArenaParticipant.objects.filter(tournament=tournament).order_by('seed'))
    missing = tournament.place_count(len(participants)) - len(participants)
    if missing > 0:
        if not shrink:
            raise ArenaError(ngettext('%(count)d place has no name yet.', '%(count)d places have no name yet.',
                                      missing) % {'count': missing})
        if len(participants) < MIN_PARTICIPANTS:
            raise ArenaError(_('A tournament needs at least %(count)s participants.') % {'count': MIN_PARTICIPANTS})
        chosen = _chosen_problems(tournament)
        for seed, participant in enumerate(participants, 1):
            if participant.seed != seed:
                participant.seed = seed
                participant.save(update_fields=['seed'])
        tournament.places = len(participants)
        tournament.save(update_fields=['places'])
        _build_bracket(actor, tournament, chosen=chosen)
        tournament.refresh_from_db()
    elif _has_empty_places(tournament):
        # Every name is there, but the bracket kept a place without anybody (a tournament edited with the code from
        # before this change and installed again): it is built again for its names, in their order, before it starts.
        chosen = _chosen_problems(tournament)
        for seed, participant in enumerate(participants, 1):
            if participant.seed != seed:
                participant.seed = seed
                participant.save(update_fields=['seed'])
        _build_bracket(actor, tournament, chosen=chosen)
        tournament.refresh_from_db()
    if not ArenaMatch.objects.filter(tournament=tournament).exists():
        raise ArenaError(_('Build the bracket before starting.'))
    tournament.status = ArenaTournament.RUNNING
    tournament.started_at = timezone.now()
    tournament.version += 1
    tournament.save(update_fields=['status', 'started_at', 'version'])
    _log('tournament_start', actor, tournament=tournament)
    ensure_default_screen(actor)
    take_free_screen(actor, tournament=tournament)
    return tournament


def _has_empty_places(tournament):
    """Whether a battle of the first round still has a place with its number and nobody in it."""
    empty = Q(seed_a__isnull=False, player_a__isnull=True) | Q(seed_b__isnull=False, player_b__isnull=True)
    return ArenaMatch.objects.filter(tournament=tournament, round__number=1).exclude(status=ArenaMatch.DECIDED) \
        .filter(empty).exists()


@transaction.atomic
def archive_tournament(actor, tournament_id, version=None):
    """Hide a finished tournament from the panel. Only a finished one (duelos-gestion-js-2, 2026-10-28): a running
    tournament archived could no longer call its matches nor finish, and the history showed it as over."""
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    if tournament.status != ArenaTournament.FINISHED:
        raise ArenaError(_('Only a finished tournament can be archived.'))
    if ArenaMatch.objects.filter(tournament=tournament, status__in=ArenaMatch.LOCKING_STATUSES).exists():
        raise ArenaError(_('Finish or decide the matches being played first.'))
    tournament.status = ArenaTournament.ARCHIVED
    tournament.version += 1
    tournament.save(update_fields=['status', 'version'])
    _log('tournament_archive', actor, tournament=tournament)
    return tournament


@transaction.atomic
def cancel_tournament(actor, tournament_id, version=None):
    """Drop a draft or a tournament in progress (2026-11-06): a practice left half played kept its players in a
    battle that never ended, so nobody could call them to another one, and a running tournament could neither be
    archived nor finish.

    Every battle not decided is cancelled as engine.cancel_match cancels a quick one: the game being played (or a
    tie waiting for the organizer) is voided, the games prepared and never played go, and its players are let out at
    once. What was decided stays as it was. The tournament leaves the history (it can be shown again) and never counts
    for the ranking of the duels: its decision becomes «does not count» and rating.set_tournament_ranking refuses
    to change it."""
    from judge.arena import access, rating
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    if tournament.status not in (ArenaTournament.DRAFT, ArenaTournament.RUNNING):
        raise ArenaError(_('Only a tournament being prepared or played can be cancelled.'))
    # Organizing does not decide the ranking (2026-10-25), and cancelling takes out for good what already counted
    # (adversarial review, 2026-11-06).
    if tournament.ranking == RANKING_COUNTS and actor is not None and not access.decides_ranking(actor.user):
        raise ArenaError(_('This tournament counts for the ranking: only whoever decides the ranking can cancel it.'))
    now = timezone.now()
    open_ids = list(ArenaMatch.objects.filter(tournament=tournament)
                    .exclude(status__in=(ArenaMatch.DECIDED, ArenaMatch.CANCELLED))
                    .order_by('round__number', 'is_third_place', 'position').values_list('pk', flat=True))
    for match_id in open_ids:
        # The tournament first, then each match (engine.lock_match), as every other change of a bracket.
        match = lock_match(match_id)
        for game in ArenaGame.objects.filter(match=match, status__in=ArenaGame.ACTIVE_STATUSES + (ArenaGame.TIE,)):
            game.status = ArenaGame.CANCELLED
            game.finished_at = now
            game.closed_at = game.closed_at or now
            game.decided_by = actor
            game.save(update_fields=['status', 'finished_at', 'closed_at', 'decided_by'])
            _close_contest(game, max(game.started_at, min(now, game.ends_at)))
        ArenaGame.objects.filter(match=match, status=ArenaGame.PREPARED, contest__isnull=True).delete()
        match.status = ArenaMatch.CANCELLED
        match.decided_at = now
        _bump(match, 'status', 'decided_at')
        _log('cancel', actor, match)
    before = {'ranking': tournament.ranking, 'show_in_history': tournament.show_in_history,
              'status': tournament.status}
    tournament.status = ArenaTournament.CANCELLED
    tournament.finished_at = now
    tournament.show_in_history = False
    tournament.ranking = RANKING_EXCLUDED
    tournament.version += 1
    tournament.save(update_fields=['status', 'finished_at', 'show_in_history', 'ranking', 'version'])
    _log('tournament_cancel', actor, tournament=tournament, battles=len(open_ids), **before)
    if before['ranking'] == RANKING_COUNTS:
        # Its battles decided so far counted: they stop counting.
        rating.recompute_on_commit()
    refresh_lock_on_commit()
    return tournament


def _place(match_id, slot, profile, seed):
    target = lock_match(match_id)
    if ArenaGame.objects.filter(match=target, contest__isnull=False).exists():
        raise ArenaError(_('The next match has already been played; it cannot receive another player.'))
    setattr(target, 'player_' + slot, profile)
    setattr(target, 'seed_' + slot, seed)
    fields = ['player_' + slot, 'seed_' + slot]
    if target.player_a_id and target.player_b_id and target.status == ArenaMatch.PENDING:
        target.status = ArenaMatch.READY
        fields.append('status')
    elif not (target.player_a_id and target.player_b_id) and target.status == ArenaMatch.READY:
        target.status = ArenaMatch.PENDING
        fields.append('status')
    _bump(target, *fields)
    return target


def advance(match, now=None):
    """Send the winner (and, for a semifinal with a third place match, the loser) to their next match."""
    if match.tournament_id is None or not match.winner_slot:
        return
    now = now or timezone.now()
    winner_slot = match.winner_slot
    loser_slot = SLOT_B if winner_slot == SLOT_A else SLOT_A
    if match.next_match_id:
        _place(match.next_match_id, match.next_slot, match.player(winner_slot),
               getattr(match, 'seed_' + winner_slot))
    if match.loser_next_match_id and match.player_id(loser_slot):
        _place(match.loser_next_match_id, match.loser_next_slot, match.player(loser_slot),
               getattr(match, 'seed_' + loser_slot))
    _check_finished(match.tournament_id, now)


def _check_finished(tournament_id, now):
    tournament = ArenaTournament.objects.select_for_update().get(pk=tournament_id)
    if tournament.status != ArenaTournament.RUNNING:
        return
    final = ArenaMatch.objects.filter(tournament=tournament, next_match__isnull=True, is_third_place=False) \
        .order_by('-round__number').first()
    third = ArenaMatch.objects.filter(tournament=tournament, is_third_place=True).first()
    if final is not None and final.status == ArenaMatch.DECIDED and \
            (third is None or third.status in (ArenaMatch.DECIDED, ArenaMatch.CANCELLED)):
        tournament.status = ArenaTournament.FINISHED
        tournament.finished_at = now
        tournament.version += 1
        tournament.save(update_fields=['status', 'finished_at', 'version'])
        _log('tournament_finish', None, tournament=tournament)


@transaction.atomic
def set_winner(actor, match_id, slot, version=None):
    """Correct the winner of a decided tournament match, as long as the next match has not been played."""
    if slot not in (SLOT_A, SLOT_B):
        raise ArenaError(_('Choose a winner.'))
    match = lock_match(match_id)
    _check_version(match, version)
    # As the panel and the guide say: a quick battle is not corrected (2026-10-28).
    if match.tournament_id is None:
        raise ArenaError(_('Only the winner of a tournament match can be corrected.'))
    # A cancelled tournament stays as it was left (2026-11-06): correcting a winner would move the bracket.
    if match.tournament.status == ArenaTournament.CANCELLED:
        raise ArenaError(_('The tournament was cancelled: nothing in it changes.'))
    if match.status != ArenaMatch.DECIDED or match.winner_slot == slot:
        raise ArenaError(_('Only the winner of a decided match can be corrected.'))
    if not match.player_id(slot):
        raise ArenaError(_('That player is not set yet.'))
    # Nobody corrects a battle they played, nor one that decides whom they meet next (2026-10-28, adversarial review
    # of duelos-web-1): the panel of a tournament is theirs again once they are out of it or it finished.
    players = {match.player_a_id, match.player_b_id}
    for target_id in (match.next_match_id, match.loser_next_match_id):
        if target_id:
            players.update(*ArenaMatch.objects.filter(pk=target_id).exclude(
                status__in=(ArenaMatch.DECIDED, ArenaMatch.CANCELLED)).values_list('player_a_id', 'player_b_id'))
    if actor is not None and actor.pk in players:
        raise ArenaError(_('You played this battle or play the next one: another organizer has to correct it.'))
    for target_id in (match.next_match_id, match.loser_next_match_id):
        if target_id and ArenaGame.objects.filter(match_id=target_id, contest__isnull=False).exists():
            raise ArenaError(_('The next match has already been played; it cannot receive another player.'))
    match.winner_slot = slot
    # A battle won by walkover stays one, with the other winner (duelos-nucleo-7, 2026-10-28): a walkover never counts
    # for the ranking of the duels, and calling it «organizer» made its games played before the walkover count.
    if match.decided_reason != 'walkover':
        match.decided_reason = 'organizer'
    match.decided_at = timezone.now()
    _bump(match, 'winner_slot', 'decided_reason', 'decided_at')
    tournament = ArenaTournament.objects.select_for_update().get(pk=match.tournament_id) \
        if match.tournament_id else None
    if tournament is not None and tournament.status == ArenaTournament.FINISHED:
        tournament.status = ArenaTournament.RUNNING
        tournament.finished_at = None
        tournament.save(update_fields=['status', 'finished_at'])
    advance(match)
    _log('set_winner', actor, match, winner=slot)
    # Only games count for the ranking of the duels, so the ratings stay.
    from judge.arena import rating
    rating.after_decided(match)
    refresh_lock_on_commit()
    return match


# ---------------------------------------------------------------------------------------------------------------
# Problem banks
# ---------------------------------------------------------------------------------------------------------------

@transaction.atomic
def create_bank(actor, name, description=''):
    name = (name or '').strip()[:100]
    if not name:
        raise ArenaError(_('Give the bank a name.'))
    return ArenaProblemBank.objects.create(name=name, description=(description or '').strip()[:2000],
                                           created_by=actor)


@transaction.atomic
def set_bank_problems(actor, bank_id, entries):
    """Replace the problems of a bank. `entries` is a list of {id, level, note}.

    The problems already in the bank that `actor` cannot see are not shown to them (views/arena_manage.py,
    manage_banks), so they never come back in `entries`: they are kept as they are, after the others."""
    bank = ArenaProblemBank.objects.select_for_update().filter(pk=bank_id).first()
    if bank is None:
        raise ArenaError(_('That bank does not exist.'), status=404)
    if not isinstance(entries, list) or len(entries) > 500:
        raise ArenaError(_('Invalid list of problems.'))
    rows, ids = [], []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ArenaError(_('Invalid list of problems.'))
        problem_id = entry.get('id')
        # isdecimal, not isdigit (2026-10-28): a superscript digit passed isdigit and int() failed with a 500.
        if isinstance(problem_id, str) and problem_id.isdecimal():
            problem_id = int(problem_id)
        if isinstance(problem_id, bool) or not isinstance(problem_id, int):
            raise ArenaError(_('Invalid list of problems.'))
        level = entry.get('level')
        if level in ('', None):
            level = None
        else:
            if isinstance(level, str) and level.isdecimal():
                level = int(level)
            if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
                raise ArenaError(_('The level must be between 1 and 5.'))
        note = entry.get('note') or ''
        if not isinstance(note, str):
            raise ArenaError(_('Invalid list of problems.'))
        ids.append(problem_id)
        rows.append((problem_id, level, note.strip()[:200], index))
    if len(set(ids)) != len(ids):
        raise ArenaError(_('The same problem cannot appear twice in a bank.'))
    existing = list(ArenaBankProblem.objects.filter(bank=bank))
    visible = set(visible_problems(actor.user).filter(id__in=ids + [entry.problem_id for entry in existing])
                  .values_list('id', flat=True))
    if not visible.issuperset(ids):
        raise ArenaError(_('One of the problems does not exist or you cannot see it.'))
    kept = [entry for entry in existing if entry.problem_id not in visible]
    ArenaBankProblem.objects.filter(bank=bank).delete()
    ArenaBankProblem.objects.bulk_create([ArenaBankProblem(bank=bank, problem_id=problem_id, level=level, note=note,
                                                           order=order)
                                          for problem_id, level, note, order in rows] +
                                         [ArenaBankProblem(bank=bank, problem_id=entry.problem_id, level=entry.level,
                                                           note=entry.note, order=len(rows) + index)
                                          for index, entry in enumerate(kept)])
    return bank


def draw_problems(actor, match, game, count=1, level_min=None, level_max=None):
    """Propose problems for a game from the tournament bank. Nothing is saved: the organizer confirms or edits.

    Never repeats a problem already used in the tournament, prefers problems neither player has solved or tried,
    and orders the proposal by level. Returns (problems, warnings).
    """
    bank = match.tournament.bank if match.tournament_id else None
    if bank is None:
        raise ArenaError(_('This battle has no problem bank to draw from.'))
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 5))
    entries = list(ArenaBankProblem.objects.filter(bank=bank).select_related('problem'))
    if level_min:
        entries = [entry for entry in entries if entry.level and entry.level >= level_min]
    if level_max:
        entries = [entry for entry in entries if entry.level and entry.level <= level_max]
    # Only what whoever draws can see (duelos-nucleo-3, 2026-10-28): the organizer who asks, or the creator of the
    # tournament when it draws by itself (actor None). Until then that case took every problem of the bank, also one
    # that became private (reserved for a coming contest) or that somebody else added, which the page of the banks
    # hides from that organizer; the game then showed it to the players and to the room.
    ids = [entry.problem_id for entry in entries]
    viewer = actor if actor is not None else match.tournament.created_by
    visible = set(visible_problems(viewer.user if viewer is not None else AnonymousUser()).filter(id__in=ids)
                  .values_list('id', flat=True))
    used = problems_used_in_tournament(match.tournament, exclude_game=game) | \
        set(ArenaGameProblem.objects.filter(game__match=match).exclude(game=game).values_list('problem_id', flat=True))
    candidates = [entry for entry in entries if entry.problem_id in visible and entry.problem_id not in used]
    players = [pk for pk in (match.player_a_id, match.player_b_id) if pk]
    touched = set(Submission.objects.filter(user_id__in=players, problem_id__in=[e.problem_id for e in candidates])
                  .values_list('problem_id', flat=True))
    fresh = [entry for entry in candidates if entry.problem_id not in touched]
    warnings = []
    pool = fresh
    if len(fresh) < count:
        pool = candidates
        if candidates:
            warnings.append(_('Not enough problems neither player has tried; some of the proposal may be known.'))
    if len(pool) < count:
        raise ArenaError(_('The bank does not have enough unused problems for this game.'))
    chosen = SystemRandom().sample(pool, count)
    chosen.sort(key=lambda entry: (entry.level or 0, entry.order))
    return [entry.problem for entry in chosen], warnings


def draw_settings(match):
    """How a tournament match gets its problems: {'auto', 'bank', 'count', 'level_min', 'level_max'}.

    'auto' is on when the tournament gives problems from its bank by itself. The round can change how many
    problems each game has and which difficulties to draw from; the tournament gives the defaults.
    """
    tournament = match.tournament if match.tournament_id else None
    if tournament is None:
        return {'auto': False, 'bank': None, 'count': 1, 'level_min': None, 'level_max': None}
    stored = tournament.rules or {}
    round_rules = (match.round.rules if match.round_id else None) or {}
    return {
        'auto': bool(stored.get('auto_draw')) and tournament.bank_id is not None,
        'bank': tournament.bank_id,
        'count': round_rules.get('draw_count') or stored.get('draw_count') or 1,
        'level_min': round_rules.get('level_min'),
        'level_max': round_rules.get('level_max'),
    }


def fill_from_bank(match, game, actor=None, force=False):
    """Give `game` its problems from the tournament bank, if the tournament does that by itself (or `force`) and
    the game has none yet. Problems an organizer chose by hand are never replaced.

    Called with the match already locked. Returns True when problems were given. Never raises: a bank without
    enough unused problems leaves the game empty, the log says why, and the panel asks for them by hand.
    """
    if match.tournament_id is None or game is None or game.status != ArenaGame.PREPARED:
        return False
    settings = draw_settings(match)
    if not settings['bank'] or not (settings['auto'] or force):
        return False
    if ArenaGameProblem.objects.filter(game=game).exists():
        return False
    try:
        with transaction.atomic():
            problems, warnings = draw_problems(actor, match, game, settings['count'], settings['level_min'],
                                               settings['level_max'])
            ArenaGameProblem.objects.bulk_create([
                ArenaGameProblem(game=game, problem=problem, order=index, points=arena_rules.DEFAULT_POINTS)
                for index, problem in enumerate(problems)])
            _log('bank_problems', actor, match, game, problems=[problem.code for problem in problems],
                 warnings=[str(warning) for warning in warnings])
        return True
    except ArenaError as error:
        _log('bank_empty', actor, match, game, reason=error.message)
        return False
    except Exception:
        # Drawing is a convenience: whatever goes wrong, the game simply waits for problems chosen by hand.
        logger.warning('Arena: drawing problems from the bank failed for match %s.', match.pk, exc_info=True)
        return False


def fill_ready_matches(actor, tournament_id):
    """Give the next game of every match whose players are known, and that has no problems yet, its problems from
    the bank. Returns how many games got problems. Each match in its own transaction, under its own lock."""
    tournament = ArenaTournament.objects.filter(pk=tournament_id).first()
    if tournament is None:
        raise ArenaError(_('That tournament does not exist.'), status=404)
    if tournament.bank_id is None:
        raise ArenaError(_('Choose a problem bank for the tournament first.'))
    filled = 0
    ids = ArenaMatch.objects.filter(tournament=tournament, status__in=(ArenaMatch.READY, ArenaMatch.CALLED,
                                                                         ArenaMatch.PLAYING)) \
        .order_by('round__number', 'position').values_list('id', flat=True)
    from judge.arena.engine import _ensure_next_game
    for match_id in ids:
        with transaction.atomic():
            match = lock_match(match_id)
            # Decided or cancelled (the tournament too) since the list was read (2026-11-06).
            if match.status not in (ArenaMatch.READY, ArenaMatch.CALLED, ArenaMatch.PLAYING):
                continue
            if ArenaGame.objects.filter(match=match, status__in=ArenaGame.ACTIVE_STATUSES).exists():
                continue
            if fill_from_bank(match, _ensure_next_game(match), actor, force=True):
                _bump(match)
                filled += 1
    return filled


@transaction.atomic
def set_rounds(actor, tournament_id, rounds, version=None):
    """All the rounds of a tournament at once, from the tab «Rounds and problems» (2026-11-03), so one Save keeps
    them all or none: `rounds` is a list of {number, name, best_of, draw_count, level_min, level_max, starter_code}."""
    if not isinstance(rounds, list) or len(rounds) > round_count(MAX_PARTICIPANTS) + 1:
        raise ArenaError(_('Invalid request.'))
    tournament = _lock_tournament(tournament_id)
    _check_version(tournament, version)
    for row in rounds:
        if not isinstance(row, dict) or isinstance(row.get('number'), bool) or not isinstance(row.get('number'), int):
            raise ArenaError(_('That round does not exist.'), status=404)
        draw = {key: row[key] for key in ('draw_count', 'level_min', 'level_max') if key in row}
        set_round(actor, tournament.pk, row['number'], best_of=row.get('best_of'), name=row.get('name'),
                  version=ArenaTournament.objects.get(pk=tournament.pk).version, draw=draw or None,
                  starter_code=row.get('starter_code'))
    return ArenaTournament.objects.get(pk=tournament.pk)


def problem_conflicts(tournament):
    """{match id: [{slot, profile, name, code, kind}]}: in the battles not played yet whose players are known, who
    already solved ('solved') or tried ('tried') a problem chosen for them. The panel says so when a problem is chosen
    (engine.problem_warnings); a place named later, after its problems, is checked here (2026-11-03). Two queries
    for the whole bracket."""
    matches = {m.pk: m for m in ArenaMatch.objects.filter(
        tournament=tournament, status__in=(ArenaMatch.PENDING, ArenaMatch.READY, ArenaMatch.CALLED))
        .select_related('player_a__user', 'player_b__user')}
    rows = list(ArenaGameProblem.objects.filter(game__match_id__in=list(matches), game__status=ArenaGame.PREPARED)
                .values_list('game__match_id', 'problem_id', 'problem__code'))
    players = {profile_id for match in matches.values() for profile_id in (match.player_a_id, match.player_b_id)
               if profile_id}
    if not rows or not players:
        return {}
    problems = {problem_id for _match, problem_id, _code in rows}
    attempts = Submission.objects.filter(user_id__in=players, problem_id__in=problems)
    tried = set(attempts.values_list('user_id', 'problem_id').distinct())
    solved = set(attempts.filter(result='AC', case_points__gte=F('case_total'))
                 .values_list('user_id', 'problem_id').distinct())
    result = {}
    for match_id, problem_id, code in rows:
        match = matches[match_id]
        for slot in (SLOT_A, SLOT_B):
            profile_id = match.player_id(slot)
            if not profile_id or (profile_id, problem_id) not in tried:
                continue
            entry = {'slot': slot, 'profile': profile_id, 'name': match.shown_name(slot), 'code': code,
                     'kind': 'solved' if (profile_id, problem_id) in solved else 'tried'}
            if entry not in result.setdefault(match_id, []):
                result[match_id].append(entry)
    return result


__all__ = ['seeding_order', 'bracket_size', 'round_count', 'default_round_name', 'round_name', 'create_tournament',
           'update_tournament', 'set_participants', 'set_round', 'set_rounds', 'generate_bracket', 'start_tournament',
           'archive_tournament', 'cancel_tournament', 'advance', 'set_winner', 'create_bank', 'set_bank_problems', 'draw_problems',
           'clean_places', 'problem_conflicts']
