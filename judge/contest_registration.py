"""Registering for a contest before it starts, and entering it once it does (2026-10-31).

A registration is the participation itself, created ahead with `virtual = ContestParticipation.REGISTERED`: the access
code is asked then, once, and the team roster is fixed then, as when joining. Nothing that reads the scoreboard takes
that value, so whoever registers and never enters appears nowhere. Entering turns the same row LIVE
(`enter_registration`), with its start at that moment, and makes it the current contest of whoever enters; for a team,
also of the rest of its roster who are in no other contest. Never before the start: nothing after `current_contest`
asks whether the contest started, so it would open the problems ahead of time.

Nobody enters without saying so (the owner, 2026-10-31): when the contest starts, wherever they are on the site, a box
asks whether to enter now and go to the problems (base.html, `registration_prompts`), and in a contest with a time
window per participation whether to start their time now. Saying no, they enter later from the page of the contest or
from the list. Locks, always in this order: the contest row (only join_attempt and the cancelling take it), the
participation, the profiles.
"""
import logging
import math
import time
from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import Contest, ContestParticipation, Profile

logger = logging.getLogger('judge.contest_registration')

HINT_KEY = 'contest_registration_hint:%d'
# How long a profile's next registration is remembered between requests. Registering, cancelling and entering forget
# it at once; a start moved by whoever organizes shows here at most this late, and entering always asks the database.
HINT_SECONDS = 60
# The tab «Concursos» of the bar counts down over the last hour before the start (the owner, 2026-10-31), and says
# it started over the first hour after it.
PILL_SECONDS = 3600


def _members(entry):
    """Who an official participation is for: its roster, or the person alone."""
    if entry.team_id:
        return list(entry.team_roster.values_list('profile_id', flat=True))
    return [entry.user_id]


def superseded(entry):
    """Whether somebody on this registration already competes in its contest from another participation. Only the
    code before this change can leave that (deploy.py --rollback while registrations waited: it lets them join as
    always, without the registration): such a registration can never enter, and is shown nowhere."""
    from django.db.models import Q
    members = _members(entry)
    others = Q(team__isnull=True, user_id__in=members) | Q(team_roster__profile_id__in=members)
    if entry.team_id:
        others |= Q(team_id=entry.team_id)
    return (ContestParticipation.objects.filter(others, contest_id=entry.contest_id, virtual=ContestParticipation.LIVE)
            .exclude(pk=entry.pk).exists())


def forget_hints(profile_ids):
    """Drop what is remembered about the next registration of these profiles; never raises."""
    keys = [HINT_KEY % profile_id for profile_id in profile_ids]
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception:
        logger.exception('Could not forget the registration hints')


def forget_hints_on_commit(profile_ids):
    profile_ids = list(profile_ids)
    transaction.on_commit(lambda: forget_hints(profile_ids))


def _can_enter_later(entry, profile):
    """Whether the registration could still be entered by this profile, as far as asking costs little: not when they
    organize or test the contest since (they watch it as a spectator) or were banned from it."""
    contest = entry.contest
    if profile.id in contest.editor_ids or profile.id in contest.tester_ids:
        return False
    return not contest.banned_users.filter(pk=profile.pk).exists()


def pending_registration(profile):
    """What is remembered about the registrations of this profile that have not entered yet, or None without any:
    {'entries': [one dict per registration, by start]}. Remembered for a minute, because the bar of every page asks;
    a failure of the cache falls back to the database."""
    if profile is None:
        return None
    key = HINT_KEY % profile.id
    try:
        state = cache.get(key)
    except Exception:
        state = None
    if state is None:
        from judge.models.team import ContestTeamMember
        waiting = (ContestParticipation.objects
                   .filter(virtual=ContestParticipation.REGISTERED, is_disqualified=False,
                           contest__end_time__gt=timezone.now())
                   .select_related('contest').order_by('contest__start_time', 'pk'))
        # Two queries, each on its own index, rather than for_profile: an OR with a subquery, which MariaDB answers
        # reading the whole table, on the first request of every person each minute.
        found = sorted(
            list(waiting.filter(user=profile, team__isnull=True)[:3]) +
            list(waiting.filter(pk__in=ContestTeamMember.objects.filter(profile=profile)
                                .values('participation_id'))[:3]),
            key=lambda item: (item.contest.start_time, item.pk))
        state = {'entries': [{'participation': entry.pk, 'contest': entry.contest.pk, 'key': entry.contest.key,
                              'name': entry.contest.name, 'team': entry.team_name if entry.team_id else '',
                              'start': entry.contest.start_time.timestamp(),
                              'end': entry.contest.end_time.timestamp(),
                              'limit': entry.contest.time_limit.total_seconds() if entry.contest.time_limit else None,
                              'windowed': entry.contest.time_limit is not None}
                             for entry in found if not superseded(entry) and _can_enter_later(entry, profile)]}
        try:
            cache.set(key, state, HINT_SECONDS)
        except Exception:
            pass
    return state if state['entries'] else None


def _entry_blocker(contest, entry, actor):
    """Why `actor` cannot enter with this registration now, or None. What is personal (a ban, a disabled account,
    organizing or testing the contest, access); for whoever made the registration, also what they carried for the
    rest of the roster when registering (the organizations allowed), which may have changed since; and whether a
    private contest still authorizes the team. A registration that whoever organizes made answers for the privacy and
    the organisations (2026-11-04): only what is personal, and that the contest opens, which it does unless hidden."""
    from judge.contest_teams import _access_blocker, _roster_blocker
    if entry.by_organizer:
        blocker = _roster_blocker(contest, actor)
        if blocker is None and not contest.is_accessible_by(actor.user):
            blocker = _('No tienes acceso a este concurso.')
        return blocker
    if actor.pk == entry.user_id:
        blocker = _access_blocker(contest, actor)
    else:
        blocker = _roster_blocker(contest, actor)
        if blocker is None and not contest.is_accessible_by(actor.user):
            blocker = _('No tienes acceso a este concurso.')
    if blocker is None and entry.team_id and contest.is_private and contest.private_teams.exists() and \
            not contest.private_teams.filter(pk=entry.team_id).exists():
        blocker = _('Este equipo no está autorizado para participar en el concurso privado.')
    return blocker


def promote(entry, actor):
    """Turn the registration `entry`, already locked by the caller, into the live participation. Returns True if this
    call did it, False if a teammate had already entered; raises ValidationError with the reason it cannot. What is
    personal is checked for whoever enters either way: a teammate entering first lets nobody banned since in."""
    from judge.models.arena import ArenaGame
    contest = entry.contest
    if not entry.contains_profile(actor.pk):
        raise ValidationError(_('No formas parte de la lista de este registro.'))
    if entry.virtual not in ContestParticipation.OFFICIAL or (
            entry.virtual == ContestParticipation.REGISTERED and superseded(entry)):
        raise ValidationError(_('Ese registro ya no existe.'))
    if not contest.started:
        raise ValidationError(_('El concurso todavía no ha comenzado.'))
    if contest.ended or entry.is_disqualified or (entry.virtual == ContestParticipation.LIVE and entry.ended):
        raise ValidationError(_('Esta participación ya terminó o está descalificada.'))
    blocker = _entry_blocker(contest, entry, actor)
    if blocker:
        raise ValidationError(blocker)
    if entry.virtual == ContestParticipation.LIVE:
        return False
    if ArenaGame.objects.filter(contest_id=contest.pk).exists():
        raise ValidationError(_('This contest belongs to an arena battle.'))
    if entry.team_id and not entry.team.is_active:
        raise ValidationError(_('El equipo de este registro ya no está activo.'))
    entry.virtual = ContestParticipation.LIVE
    entry.real_start = timezone.now()
    entry.frozen_score = entry.frozen_cumtime = entry.frozen_tiebreaker = entry.frozen_format_data = None
    entry.frozen_at = None
    entry.save(update_fields=['virtual', 'real_start', 'frozen_score', 'frozen_cumtime', 'frozen_tiebreaker',
                              'frozen_format_data', 'frozen_at'])
    for name in ('start', 'end_time', 'freeze_starts_at'):
        entry.__dict__.pop(name, None)
    return True


def attach(entry, actor, first, switch):
    """Make `entry` the current contest of `actor` (leaving any other one only when `switch`) and, the first time the
    team enters, of the rest of its roster who are in no contest and could enter themselves."""
    from judge.contest_teams import _roster_blocker
    members = _members(entry)
    if switch:
        Profile.objects.filter(pk=actor.pk).update(current_contest=entry)
    else:
        Profile.objects.filter(pk=actor.pk, current_contest__isnull=True).update(current_contest=entry)
    actor.current_contest_id = Profile.objects.filter(pk=actor.pk).values_list('current_contest_id', flat=True).first()
    if first and entry.team_id:
        contest = entry.contest
        others = [profile for profile in Profile.objects.filter(pk__in=[pk for pk in members if pk != actor.pk],
                                                                current_contest__isnull=True)
                  .select_related('user').order_by('pk')]
        ready = [profile.pk for profile in others if _roster_blocker(contest, profile) is None]
        if ready:
            Profile.objects.filter(pk__in=ready, current_contest__isnull=True).update(current_contest=entry)
    return members


def enter_after_commit(contest, members):
    def done():
        forget_hints(members)
        try:
            contest.recount_users()
        except Exception:
            # Whoever entered is in already: a lost connection here must not turn the entry into an error page.
            logger.exception('Could not recount the users of contest %s', contest.key)
    transaction.on_commit(done)


def enter_registration(contest_id, actor, participation_id):
    """Enter the registration `participation_id` of this contest: the box that asks at the start, or the button on the
    page of the contest. Leaves any other contest `actor` was in. Returns the live participation; raises
    ValidationError if it cannot."""
    try:
        participation_id = int(participation_id)
    except (TypeError, ValueError):
        raise ValidationError(_('Ese registro ya no existe.'))
    with transaction.atomic():
        try:
            # The participation alone: MariaDB has no FOR UPDATE OF, and with select_related it would also lock the
            # row of the contest (and of the team), so everybody entering at the start would queue on it, and in the
            # opposite order to join_attempt. The contest and the team are read after, without locks.
            entry = ContestParticipation.objects.select_for_update().get(pk=participation_id, contest_id=contest_id)
        except ContestParticipation.DoesNotExist:
            raise ValidationError(_('Ese registro ya no existe.'))
        first = promote(entry, actor)
        members = attach(entry, actor, first, switch=True)
        enter_after_commit(entry.contest, members)
    return entry


def _pill(request, entries, now):
    """What the tab «Concursos» counts down to (base.html), from the last hour before a start to the first hour after
    it: the next registration to start, or else the last one that started, which waits to be entered."""
    near = [hint for hint in entries if -PILL_SECONDS <= hint['start'] - now <= PILL_SECONDS]
    coming = [hint for hint in near if hint['start'] > now]
    hint = min(coming, key=lambda item: item['start']) if coming else max(near, key=lambda item: item['start'],
                                                                        default=None)
    if hint is not None:
        request.registration_pill = dict(hint, seconds=max(0, math.ceil(hint['start'] - now)))


def registration_hint(request, profile):
    """For the middleware, on every request of a signed-in profile: what the tab «Concursos» counts down to
    (`registration_pill`, only outside any contest), and the registrations to ask about (`registration_prompts`): the
    one whose contest started last and has not ended, and the next one to start, with how long until then, so that
    its box opens at the start on whatever page is open, also when the box of the other one was closed long ago (a
    window per participation, open for weeks). Never raises: a failure only leaves both empty."""
    request.registration_pill, request.registration_prompts = None, []
    try:
        # Not for an administrator acting as somebody (the answer would be that person's), nor for the API.
        if getattr(request, 'no_profile_update', False) or request.headers.get('authorization'):
            return
        state = pending_registration(profile)
        if not state:
            return
        now = time.time()
        entries = [hint for hint in state['entries'] if hint['end'] > now]
        if request.participation is None:
            _pill(request, entries, now)
        else:
            # Inside another contest, only around a start: one with a time window that opened days ago waits until
            # they are out of it, instead of interrupting whoever competes.
            entries = [hint for hint in entries if abs(hint['start'] - now) <= PILL_SECONDS]
        started = [hint for hint in entries if hint['start'] <= now]
        coming = [hint for hint in entries if hint['start'] > now]
        chosen = []
        if started:
            chosen.append(max(started, key=lambda item: (item['start'], item['participation'])))
        if coming:
            chosen.append(min(coming, key=lambda item: (item['start'], item['participation'])))
        inside = request.participation.contest_id if request.participation is not None else None
        request.registration_prompts = [
            dict(hint, seconds=max(0, math.ceil(hint['start'] - now)),
                 window=timedelta(seconds=hint['limit']) if hint['limit'] else None)
            for hint in chosen if hint['contest'] != inside]
    except Exception:
        logger.exception('Could not read the registrations of %s', getattr(profile, 'username', profile))
        request.registration_pill, request.registration_prompts = None, []


def _registrant(team, members):
    """Whom a team registered by whoever organizes stands under (2026-11-04): its owner, if they compete, or else
    whoever on the roster joined the team first. They can cancel it, as whoever registers a team themselves; the rest
    can take themselves off the roster."""
    from judge.models import TeamMembership
    for member in members:
        if member.pk == team.owner_id:
            return member
    joined = dict(TeamMembership.objects.filter(team=team, profile__in=members).values_list('profile_id', 'joined'))
    return min(members, key=lambda member: (joined.get(member.pk) is None, joined.get(member.pk), member.pk))


@transaction.atomic
def register_for(contest_id, organizer, profile_ids=(), teams=(), onsite=False):
    """Register people and teams ahead, by whoever organizes the contest (2026-11-04), from the tab «Registrados»:
    also once it started, until it ends. They answer for them (by_organizer): neither the access code, nor the
    privacy, nor the organisations of the contest are asked. What is personal still is: an active account, not banned
    from the contest, not organizing or testing it, and one official registration per person. A team goes with the
    members picked, `teams` being [(team id, [profile ids])], within the sizes of the contest. In a contest on site
    and online they start where `onsite` says, and the list of who competes on site follows. Nobody enters: at the
    start they are asked, as everybody (the owner, 2026-11-04).

    Returns (registered, refused, created): the names registered, why each of the others was not, in a sentence that
    names it, and the participations created. Raises ValidationError when the contest takes nobody. The contest row
    first, as join_attempt locks it; then the teams and the profiles, each by id."""
    from judge.contest_teams import _require_presentable_name, _roster_blocker
    from judge.models import Team
    from judge.models.arena import ArenaGame
    from judge.models.team import ContestTeamMember
    from judge.team_services import require_teams_enabled
    contest = Contest.objects.select_for_update().get(pk=contest_id)
    if contest.ended:
        raise ValidationError(_('El concurso ya terminó.'))
    if ArenaGame.objects.filter(contest_id=contest.pk).exists():
        raise ValidationError(_('This contest belongs to an arena battle.'))
    onsite = bool(onsite) and contest.is_hybrid
    people = list(dict.fromkeys(profile_ids))
    teams = [(team_id, list(dict.fromkeys(members))) for team_id, members in dict(teams).items()]

    official = contest.users.official()
    taken = set(official.filter(team__isnull=True).values_list('user_id', flat=True))
    taken |= set(ContestTeamMember.objects.filter(participation__in=official).values_list('profile_id', flat=True))
    taken_teams = set(official.filter(team__isnull=False).values_list('team_id', flat=True))

    locked = {team.pk: team for team in
              Team.objects.select_for_update().filter(pk__in=[team_id for team_id, _members in teams]).order_by('pk')}
    everybody = set(people) | {pk for _team, members in teams for pk in members}
    # Locked alone and read after: with select_related MariaDB would lock the accounts too.
    list(Profile.objects.select_for_update().filter(pk__in=everybody).order_by('pk').values_list('pk', flat=True))
    profiles = Profile.objects.filter(pk__in=everybody).select_related('user').in_bulk()

    registered, refused, created = [], [], []
    now = timezone.now()
    if people and contest.participation_mode == Contest.TEAM:
        refused += [_('%(user)s: este concurso sólo admite equipos.') % {'user': profiles[pk].username}
                    for pk in people if pk in profiles]
        people = []
    for pk in people:
        profile = profiles.get(pk)
        if profile is None:
            continue
        blocker = _roster_blocker(contest, profile)
        if blocker is None and pk in taken:
            blocker = _('%(user)s ya está inscrito en otra participación de este concurso.') % {
                'user': profile.username}
        if blocker:
            refused.append(blocker)
            continue
        created.append(ContestParticipation.objects.create(
            contest=contest, user=profile, virtual=ContestParticipation.REGISTERED, real_start=now, onsite=onsite,
            by_organizer=True))
        taken.add(pk)
        registered.append(profile.username)

    for team_id, member_ids in teams:
        team = locked.get(team_id)
        if team is None or not team.is_active:
            refused.append(_('Uno de los equipos ya no está activo.') if team is None else
                           _('El equipo %(team)s ya no está activo.') % {'team': team.name})
            continue
        if contest.participation_mode == Contest.INDIVIDUAL:
            refused.append(_('%(team)s: este concurso sólo admite participaciones individuales.') % {'team': team.name})
            continue
        if team.pk in taken_teams:
            refused.append(_('El equipo %(team)s ya está registrado en este concurso.') % {'team': team.name})
            continue
        current = set(team.members.values_list('pk', flat=True))
        gone = [profiles[pk].username for pk in member_ids if pk in profiles and pk not in current]
        if gone or any(pk not in profiles for pk in member_ids):
            refused.append(_('El equipo %(team)s cambió de integrantes: vuelve a elegirlos.') % {'team': team.name})
            continue
        members = sorted((profiles[pk] for pk in member_ids), key=lambda member: member.pk)
        # The sizes of the contest, checked here whatever the page let through (the owner, 2026-11-04).
        if not contest.team_min_size <= len(members) <= contest.team_max_size:
            refused.append(_('El equipo %(team)s debe tener entre %(minimum)s y %(maximum)s integrantes y elegiste '
                             '%(count)s.') % {'team': team.name, 'minimum': contest.team_min_size,
                                              'maximum': contest.team_max_size, 'count': len(members)})
            continue
        try:
            require_teams_enabled()
            _require_presentable_name(contest, team)
        except ValidationError as exc:
            refused.append(' '.join(exc.messages))
            continue
        blockers = [_roster_blocker(contest, member) for member in members]
        blockers += [_('%(user)s ya está inscrito en otra participación de este concurso.') % {'user': member.username}
                     for member in members if member.pk in taken]
        blockers = [blocker for blocker in blockers if blocker]
        if blockers:
            refused.append(_('El equipo %(team)s no se registró: %(why)s') % {'team': team.name,
                                                                              'why': ' '.join(blockers)})
            continue
        entry = ContestParticipation.objects.create(
            contest=contest, user=_registrant(team, members), team=team, team_name=team.name,
            virtual=ContestParticipation.REGISTERED, real_start=now, onsite=onsite, by_organizer=True)
        ContestTeamMember.objects.bulk_create([
            ContestTeamMember(participation=entry, profile=member, username=member.username) for member in members])
        created.append(entry)
        taken |= {member.pk for member in members}
        taken_teams.add(team.pk)
        registered.append(team.name)

    if created:
        alone = {entry.user_id for entry in created if not entry.team_id}
        rosters = {pk for entry in created if entry.team_id for pk in _members(entry)}
        on_teams = {entry.team_id for entry in created if entry.team_id}
        if contest.is_hybrid and onsite:
            contest.change_onsite_list(add_profiles=alone, add_teams=on_teams)
        elif contest.is_hybrid:
            contest.change_onsite_list(remove_profiles=alone | rosters, remove_teams=on_teams)
        forget_hints_on_commit(alone | rosters)
        logger.info('Contest %s: %s registered %d %s: %s', contest.key, organizer.username, len(created),
                    'on site' if onsite else 'online' if contest.is_hybrid else 'participations',
                    ' '.join(str(entry.pk) for entry in created))
    return registered, refused, created


@transaction.atomic
def remove_registrations(contest_id, organizer, participation_ids):
    """Take away registrations that have not entered, by whoever organizes the contest (2026-11-04, the owner: «sí
    podría quitar el registro»): the ones they made and the ones made by whoever registered. Gone as if cancelled:
    whoever was on them can register again, with the code if there is one, and they leave the list of who competes on
    site. Returns (the names taken away, how many of the ones asked had entered already). The contest row first, as
    cancel_registration locks it, then the participations."""
    from judge.models.team import ContestTeamMember
    contest = Contest.objects.select_for_update().get(pk=contest_id)
    rows = list(contest.users.select_for_update().filter(pk__in=list(participation_ids),
                                                         virtual__in=ContestParticipation.OFFICIAL)
                .order_by('pk').values_list('pk', 'virtual', 'user_id', 'team_id', 'team_name'))
    waiting = [row for row in rows if row[1] == ContestParticipation.REGISTERED]
    entered = len(rows) - len(waiting)
    if not waiting:
        return [], entered
    ids = [pk for pk, _virtual, _user, _team, _name in waiting]
    roster = set(ContestTeamMember.objects.filter(participation_id__in=ids).values_list('profile_id', flat=True))
    alone = {user for _pk, _virtual, user, team, _name in waiting if not team}
    on_teams = {team for _pk, _virtual, _user, team, _name in waiting if team}
    names = dict(Profile.objects.filter(pk__in=alone).values_list('id', 'user__username'))
    removed = [name if team else names.get(user, '') for _pk, _virtual, user, team, name in waiting]
    ContestTeamMember.objects.filter(participation_id__in=ids).delete()
    ContestParticipation.objects.filter(pk__in=ids).delete()
    if contest.is_hybrid:
        contest.change_onsite_list(remove_profiles=alone | roster, remove_teams=on_teams)
    forget_hints_on_commit(alone | roster)
    logger.info('Contest %s: %s took away %d registrations: %s', contest.key, organizer.username, len(ids),
                ' '.join(str(pk) for pk in ids))
    return removed, entered


@transaction.atomic
def cancel_registration(contest_id, actor, participation_id):
    """Undo a registration before entering. Only whoever made it; a teammate on its roster can take themselves off it
    instead (contest_teams.withdraw_from_roster). The contest row first, as join_attempt locks it."""
    contest = Contest.objects.select_for_update().get(pk=contest_id)
    try:
        entry = contest.users.select_for_update().get(pk=int(participation_id),
                                                      virtual=ContestParticipation.REGISTERED)
    except (ValueError, TypeError, ContestParticipation.DoesNotExist):
        raise ValidationError(_('Ese registro ya no existe.'))
    if entry.user_id != actor.pk:
        raise ValidationError(_('Sólo quien hizo el registro puede cancelarlo; tú puedes retirarte de la lista.'))
    members = _members(entry)
    entry.team_roster.all().delete()
    entry.delete()
    forget_hints_on_commit(members)
    return members
