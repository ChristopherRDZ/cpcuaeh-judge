"""Team mutations. Lock order: team, then profiles in primary-key order.

Invitation responses and changes to the roster serialize on the team row. No
notification delivery or external side effect occurs inside these transactions.
"""
import unicodedata
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import Profile
from judge.models.team import Team, TeamInvitation, TeamMembership

# Characters a team name cannot carry (equipos-globos-4, 2026-10-29): controls, format characters (zero width spaces,
# direction marks and overrides, the byte order mark), private use, surrogates, and line and paragraph separators. Two
# names that look the same, or a name that turns the ones after it around, could pass for another team on the
# scoreboard, at the ceremony and on the balloon sheet.
FORBIDDEN_IN_NAMES = frozenset(('Cc', 'Cf', 'Co', 'Cs', 'Zl', 'Zp'))
# Format characters that names need (round 1 of the review): the joiners of emoji sequences and of some scripts, and
# the tags of subdivision flags. Names are compared without them.
ALLOWED_FORMAT = frozenset(chr(code) for code in (0x200C, 0x200D, *range(0xE0020, 0xE0080)))
# Drawn as nothing, though they are letters or symbols for Unicode: the combining grapheme joiner, the Hangul fillers,
# the inherent vowels of Khmer and the blank braille pattern. A name of them looks empty.
BLANK_IN_NAMES = frozenset(chr(code) for code in (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0))
# Default_Ignorable_Code_Point (Unicode, DerivedCoreProperties): nothing is drawn for them, so two names that differ
# only in them look the same. Among them, the variation selectors that some emoji need.
IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
             (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
             (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
             (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))


def refused_in_name(char):
    if char in ALLOWED_FORMAT:
        return False
    return unicodedata.category(char) in FORBIDDEN_IN_NAMES or char in BLANK_IN_NAMES


def ignorable(char):
    code = ord(char)
    return char in BLANK_IN_NAMES or any(low <= code <= high for low, high in IGNORABLE)


def clean_team_name(value):
    name = ' '.join(unicodedata.normalize('NFKC', value).split())
    if not name or len(name) > 60:
        raise ValidationError(_('Escribe un nombre de equipo de 1 a 60 caracteres.'))
    if any(refused_in_name(char) for char in name):
        raise ValidationError(_('El nombre del equipo no puede llevar caracteres invisibles ni de control.'))
    if not team_name_key(name):
        # Only joiners and selectors: nothing is drawn.
        raise ValidationError(_('Escribe un nombre de equipo de 1 a 60 caracteres.'))
    return name


def team_name_key(name):
    """What two team names are compared by: the same once normalised (NFKC), without what clean_team_name refuses (a
    name from before it may carry it) and without what is not drawn (the ignorable characters), and without telling
    capitals apart."""
    text = ''.join(char for char in unicodedata.normalize('NFKC', name or '')
                   if not refused_in_name(char) and not ignorable(char))
    return ' '.join(text.split()).casefold()


def invitation_cutoff():
    """Invitations sent before this have expired (2026-10-29): a pending one could be accepted
    months later, for example right after its team was authorised for a private contest."""
    return timezone.now() - timedelta(days=getattr(settings, 'CPC_TEAM_INVITATION_DAYS', 30))


def is_expired(invitation, cutoff=None):
    return invitation.created < (cutoff or invitation_cutoff())


def require_teams_enabled():
    if not getattr(settings, 'CPC_TEAMS_ENABLED', True):
        raise ValidationError(_('Las nuevas inscripciones e invitaciones de equipos están pausadas temporalmente.'))


def _owner(team, actor):
    if not team.is_active or team.owner_id != actor.pk:
        raise PermissionDenied


def _active_entries(team, member_id=None):
    """The participations of the team that keep its roster as it is: live or virtual ones still running, and a
    registration made ahead once its contest has started (2026-10-31), because it becomes live on the first request of
    its roster. Before the start a registration does not hold the team: leaving takes the person off it
    (_leave_registrations)."""
    from judge.models import ContestParticipation
    now = timezone.now()
    entries = (team.participations.filter(Q(virtual__gte=0) | Q(virtual=ContestParticipation.REGISTERED))
               .select_related('contest'))
    if member_id is not None:
        entries = entries.filter(team_roster__profile_id=member_id)
    return [entry for entry in entries
            if (entry.virtual == 0 and entry.contest.end_time > now) or (entry.virtual > 0 and entry.end_time > now) or
            (entry.virtual == ContestParticipation.REGISTERED and
             entry.contest.start_time <= now < entry.contest.end_time)]


def _waiting_registrations(team):
    """The registrations of the team for contests that have not started."""
    from judge.models import ContestParticipation
    return team.participations.filter(virtual=ContestParticipation.REGISTERED, contest__start_time__gt=timezone.now())


def _leave_registrations(team, actor, profile_id):
    """Leaving the team, or being removed from it, before a contest the team registered for also takes the person off
    that roster (2026-10-31): they would otherwise compete for a team they are no longer in. Whoever made a
    registration cancels it first, from the page of the contest: it is theirs."""
    from judge.contest_registration import forget_hints_on_commit
    from judge.models.team import ContestTeamMember
    waiting = list(_waiting_registrations(team).filter(team_roster__profile_id=profile_id).select_related('contest'))
    for entry in waiting:
        if entry.user_id == profile_id:
            if profile_id == actor.pk:
                raise ValidationError(_('Registraste a este equipo en «%(contest)s». Para salir, cancela antes ese '
                                        'registro desde la página del concurso.') % {'contest': entry.contest.name})
            raise ValidationError(_('Esa persona registró al equipo en «%(contest)s». Puede cancelar ese registro '
                                    'desde la página del concurso; después podrás expulsarla.')
                                  % {'contest': entry.contest.name})
    if waiting:
        ContestTeamMember.objects.filter(participation__in=waiting, profile_id=profile_id).delete()
        forget_hints_on_commit([profile_id])


def _require_idle(team):
    if _active_entries(team):
        raise ValidationError(_('No se puede cambiar la lista de integrantes ni eliminar el equipo mientras participa en un concurso o simulación activa.'))


def _require_member_idle(team, actor, member_id):
    """Leaving or being removed waits only for the attempts that have this person on their roster
    (equipos-globos-3, 2026-10-29). Before, any attempt of the team blocked everybody: one member alone in a virtual
    participation of a long contest kept the others from leaving, and the owner from removing anyone, for weeks."""
    if _active_entries(team, member_id):
        if member_id == actor.pk:
            raise ValidationError(_('Estás en la lista de una participación del equipo que sigue en curso: podrás '
                                    'salir cuando termine.'))
        raise ValidationError(_('Esa persona está en la lista de una participación del equipo que sigue en curso: '
                                'podrás expulsarla cuando termine.'))


@transaction.atomic
def create_team(actor, name):
    require_teams_enabled()
    Profile.objects.select_for_update().get(pk=actor.pk)
    if Team.objects.filter(owner=actor, is_active=True).count() >= getattr(settings, 'CPC_MAX_OWNED_TEAMS', 20):
        raise ValidationError(_('Ya alcanzaste el límite de equipos activos que puedes crear.'))
    team = Team.objects.create(owner=actor, name=clean_team_name(name))
    TeamMembership.objects.create(team=team, profile=actor)
    return team


@transaction.atomic
def invite_member(actor, team_id, username):
    require_teams_enabled()
    team = Team.objects.select_for_update().get(pk=team_id)
    _owner(team, actor)
    recipient = Profile.objects.select_related('user').filter(user__username=username.strip(), user__is_active=True).first()
    if recipient is None:
        raise ValidationError(_('No se encontró un usuario activo con ese nombre.'))
    # Serialize across teams, too, so sending from many teams cannot bypass the limit.
    list(Profile.objects.select_for_update().filter(pk__in=[actor.pk, recipient.pk]).order_by('pk'))
    if team.members.filter(pk=recipient.pk).exists():
        raise ValidationError(_('Ese usuario ya forma parte del equipo.'))
    cutoff = invitation_cutoff()
    if team.invitations.filter(recipient=recipient, status=TeamInvitation.PENDING, created__gte=cutoff).exists():
        raise ValidationError(_('Ya hay una invitación pendiente para ese usuario.'))
    now = timezone.now()
    recent = TeamInvitation.objects.filter(sender=actor, created__gte=now - timedelta(hours=1)).count()
    if recent >= getattr(settings, 'CPC_TEAM_INVITES_PER_HOUR', 20):
        raise ValidationError(_('Has enviado muchas invitaciones. Inténtalo más tarde.'))
    if TeamInvitation.objects.filter(recipient=recipient, status=TeamInvitation.PENDING, team__is_active=True,
                                     created__gte=cutoff).count() >= 50:
        raise ValidationError(_('Ese usuario tiene demasiadas invitaciones pendientes.'))
    # An expired one from this team gives way to the new one, so the person does not see both.
    team.invitations.filter(recipient=recipient, status=TeamInvitation.PENDING, created__lt=cutoff).update(
        status=TeamInvitation.CANCELLED, responded=now)
    return TeamInvitation.objects.create(team=team, sender=actor, recipient=recipient)


@transaction.atomic
def respond_invitation(actor, invitation_id, action):
    reference = TeamInvitation.objects.filter(pk=invitation_id).values('team_id').first()
    if reference is None:
        raise ValidationError(_('Esta invitación ya no está disponible.'))
    try:
        team = Team.objects.select_for_update().get(pk=reference['team_id'])
        invitation = TeamInvitation.objects.select_for_update().get(pk=invitation_id)
    except (Team.DoesNotExist, TeamInvitation.DoesNotExist):
        raise ValidationError(_('Esta invitación ya no está disponible.'))
    if action == 'cancel':
        _owner(team, actor)
    elif invitation.recipient_id != actor.pk or action not in ('accept', 'decline'):
        raise PermissionDenied
    if not team.is_active or invitation.status != TeamInvitation.PENDING:
        raise ValidationError(_('Esta invitación ya no está pendiente.'))
    if action == 'accept' and is_expired(invitation):
        # Declining and cancelling it still work: they only take it off the lists.
        raise ValidationError(_('Esta invitación venció: pide a quien es propietario del equipo que te invite de '
                                'nuevo.'))
    if action == 'accept':
        require_teams_enabled()
        Profile.objects.select_for_update().get(pk=actor.pk)
        TeamMembership.objects.get_or_create(team=team, profile=actor)
    invitation.status = {'accept': TeamInvitation.ACCEPTED, 'decline': TeamInvitation.DECLINED,
                         'cancel': TeamInvitation.CANCELLED}[action]
    invitation.responded = timezone.now()
    invitation.save(update_fields=['status', 'responded'])
    return team


@transaction.atomic
def remove_member(actor, team_id, profile_id):
    team = Team.objects.select_for_update().get(pk=team_id)
    if not team.is_active:
        raise PermissionDenied
    if actor.pk != profile_id:
        _owner(team, actor)
    _require_member_idle(team, actor, profile_id)
    if profile_id == team.owner_id:
        raise ValidationError(_('Para salir, transfiere primero la propiedad a otro integrante o elimina el equipo.'))
    if not team.memberships.filter(profile_id=profile_id).exists():
        raise ValidationError(_('Ese usuario ya no forma parte del equipo.'))
    _leave_registrations(team, actor, profile_id)
    team.memberships.filter(profile_id=profile_id).delete()


def _give_ownership(team, profile_id):
    """Only to an active account, and within the limit of teams it may own (equipos-globos-5, 2026-10-29): a team
    handed to an account that is not active again had nobody to run it. The profile is locked after the team, as
    create_team locks it, so the limit holds."""
    profile = Profile.objects.select_for_update().select_related('user').get(pk=profile_id)
    if not profile.user.is_active:
        raise ValidationError(_('La cuenta de esa persona no está activa, así que no puede ser propietaria del '
                                'equipo.'))
    if Team.objects.filter(owner=profile, is_active=True).count() >= getattr(settings, 'CPC_MAX_OWNED_TEAMS', 20):
        raise ValidationError(_('Esa persona ya es propietaria del máximo de equipos activos.'))
    team.owner_id = profile.pk
    team.save(update_fields=['owner'])


@transaction.atomic
def transfer_team(actor, team_id, profile_id):
    team = Team.objects.select_for_update().get(pk=team_id)
    _owner(team, actor)
    _require_idle(team)
    if not team.members.filter(pk=profile_id).exists():
        raise ValidationError(_('El nuevo propietario debe formar parte del equipo.'))
    _give_ownership(team, profile_id)


def ownership_heir(team):
    """Who may take over a team whose owner's account is not active anymore (cancelled, or deactivated in the
    administration): the active member who joined first. None while the owner's account is active
    (equipos-globos-5, 2026-10-29). Until then nobody could invite, remove, hand over or delete it but an
    administrator."""
    if team.owner.user.is_active:
        return None
    membership = (team.memberships.filter(profile__user__is_active=True).exclude(profile_id=team.owner_id)
                  .select_related('profile').order_by('joined', 'id').first())
    return membership.profile if membership else None


@transaction.atomic
def take_ownership(actor, team_id):
    team = Team.objects.select_for_update().select_related('owner__user').get(pk=team_id)
    if not team.is_active:
        raise PermissionDenied
    heir = ownership_heir(team)
    if heir is None or heir.pk != actor.pk:
        raise PermissionDenied
    _require_idle(team)
    _give_ownership(team, actor.pk)


@transaction.atomic
def delete_team(actor, team_id):
    team = Team.objects.select_for_update().get(pk=team_id)
    _owner(team, actor)
    _require_idle(team)
    # Its registrations for contests that have not started go with it (2026-10-31): nobody would compete for them.
    from judge.contest_registration import forget_hints_on_commit
    from judge.models.team import ContestTeamMember
    waiting = list(_waiting_registrations(team))
    if waiting:
        roster = ContestTeamMember.objects.filter(participation__in=waiting)
        forget_hints_on_commit(set(roster.values_list('profile_id', flat=True)))
        roster.delete()
        for entry in waiting:
            entry.delete()
    # PROTECT on participation.team is a second line of defence against history loss.
    if team.participations.exists() or team.balloon_locations.exists():
        team.is_active = False
        team.save(update_fields=['is_active'])
        team.memberships.all().delete()
        team.invitations.filter(status=TeamInvitation.PENDING).update(
            status=TeamInvitation.CANCELLED, responded=timezone.now())
    else:
        team.delete()
