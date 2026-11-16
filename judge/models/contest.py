import json
import math
from datetime import datetime, timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import CASCADE, Count, Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext, gettext_lazy as _
from jsonfield import JSONField
from lupa import LuaRuntime
from moss import MOSS_LANG_C, MOSS_LANG_CC, MOSS_LANG_JAVA, MOSS_LANG_PYTHON

from judge import contest_format
from judge.models.problem import Problem
from judge.models.profile import Class, Organization, Profile
from judge.models.submission import Submission
from judge.ratings import rate_contest

__all__ = ['Contest', 'ContestTag', 'ContestParticipation', 'ContestProblem', 'ContestSubmission', 'Rating']


class MinValueOrNoneValidator(MinValueValidator):
    def compare(self, a, b):
        return a is not None and b is not None and super().compare(a, b)


# What decides when a freeze begins, and whether it still hides anything.
FREEZE_SCHEDULE = ('start_time', 'end_time', 'time_limit', 'freeze_minutes', 'scoreboard_revealed')
# The score of a disqualified participation (recompute_results).
DISQUALIFIED_SCORE = -9999


def freeze_cutoff(start_time, end_time, time_limit, freeze_minutes):
    """When the freeze of a contest with this schedule begins, or None without a freeze: the same rule as
    `Contest.submission_freeze_cutoff`, for values that may not be saved yet."""
    if not freeze_minutes or start_time is None or end_time is None:
        return None
    end = min(start_time + time_limit, end_time) if time_limit else end_time
    return end - timedelta(minutes=freeze_minutes)


def _to_the_second(schedule):
    return {field: value.replace(microsecond=0) if isinstance(value, datetime) else value
            for field, value in schedule.items()}


def kept_freeze_minutes(stored, new, now=None, live=None):
    """The freeze minutes for the schedule `new` (a dict of FREEZE_SCHEDULE) that keep the cutoff of `stored`.

    Once a freeze has started, and until the scoreboard is revealed, its cutoff stays where it was
    (congelacion-revelador-2, 2026-10-29). Moving it later lifted the freeze for everybody, sent out the balloons it
    held back, and then mixed two cutoffs on one scoreboard; moving it earlier hid what everybody had already seen.
    Without a time limit per participation, a new end keeps the cutoff with as many more (or fewer) minutes of freeze,
    rounded so that the cutoff never moves later; every other change that moves it is refused until the scoreboard is
    revealed. Returns new['freeze_minutes'] when nothing has to change, and raises ValidationError otherwise.

    `live`, if given, says whether anybody takes part live. Without anybody the freeze hides nothing yet, so the
    schedule changes freely: a contest cloned from a past one, created with past dates by mistake, or postponed before
    anybody joined (round 1 of the review, 2026-10-29). It is only asked when the answer matters.
    """
    now = now or timezone.now()
    old_cutoff = freeze_cutoff(stored['start_time'], stored['end_time'], stored['time_limit'],
                               stored['freeze_minutes'])
    if stored['scoreboard_revealed'] or new['scoreboard_revealed'] or old_cutoff is None or now < old_cutoff:
        return new['freeze_minutes']
    # To the second: the administration shows and sends times without microseconds, so a contest created with them
    # (from code) would otherwise look changed every time its form is saved, and be refused.
    stored, new, old_cutoff = _to_the_second(stored), _to_the_second(new), old_cutoff.replace(microsecond=0)
    if all(new[field] == stored[field] for field in FREEZE_SCHEDULE):
        return new['freeze_minutes']
    if live is not None and not live():
        return new['freeze_minutes']
    started = gettext('The freeze started at %(cutoff)s and the scoreboard has not been revealed.') % {
        'cutoff': timezone.localtime(old_cutoff).strftime('%Y-%m-%d %H:%M:%S')}
    if not new['freeze_minutes']:
        raise ValidationError({'freeze_minutes': '%s %s' % (started, gettext(
            'To lift the freeze, reveal the scoreboard.'))})
    if new['time_limit'] or stored['time_limit'] or new['start_time'] != stored['start_time']:
        raise ValidationError('%s %s' % (started, gettext(
            'Until it is revealed, the start, the end, the time limit and the freeze cannot change: each '
            'participation would leave or enter its freeze at another moment.')))
    if new['end_time'] - timedelta(minutes=new['freeze_minutes']) == old_cutoff:
        return new['freeze_minutes']
    minutes = math.ceil((new['end_time'] - old_cutoff).total_seconds() / 60)
    # The minutes this same rule gives for that end: the form adjusts them, and Contest.save checks again. With an end
    # that is not a whole number of minutes from the cutoff, the cutoff moves up to 59 seconds earlier, never later
    # (before round 1 of the review the second check refused it, and the administration answered 500).
    if minutes >= 1 and new['freeze_minutes'] == minutes:
        return minutes
    if new['freeze_minutes'] != stored['freeze_minutes']:
        raise ValidationError({'freeze_minutes': '%s %s' % (started, gettext(
            'Until it is revealed, the freeze cannot start at another moment. To change the end of the '
            'contest, change only the end: the freeze minutes follow by themselves.'))})
    if minutes < 1:
        raise ValidationError({'end_time': '%s %s' % (started, gettext(
            'The contest cannot end before the freeze began.'))})
    return minutes


class ContestTag(models.Model):
    color_validator = RegexValidator('^#(?:[A-Fa-f0-9]{3}){1,2}$', _('Invalid colour.'))

    name = models.CharField(max_length=20, verbose_name=_('tag name'), unique=True,
                            validators=[RegexValidator(r'^[a-z-]+$', message=_('Lowercase letters and hyphens only.'))])
    color = models.CharField(max_length=7, verbose_name=_('tag colour'), validators=[color_validator])
    description = models.TextField(verbose_name=_('tag description'), blank=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('contest_tag', args=[self.name])

    @property
    def text_color(self, cache={}):
        if self.color not in cache:
            if len(self.color) == 4:
                r, g, b = [ord(bytes.fromhex(i * 2)) for i in self.color[1:]]
            else:
                r, g, b = [i for i in bytes.fromhex(self.color[1:])]
            cache[self.color] = '#000' if 299 * r + 587 * g + 144 * b > 140000 else '#fff'
        return cache[self.color]

    class Meta:
        verbose_name = _('contest tag')
        verbose_name_plural = _('contest tags')


class Contest(models.Model):
    INDIVIDUAL, TEAM, MIXED = 'individual', 'team', 'mixed'
    participation_mode = models.CharField(
        _('Modalidad de participación'), max_length=10, default=INDIVIDUAL,
        choices=((INDIVIDUAL, _('Individual')), (TEAM, _('Equipos')), (MIXED, _('Mixto'))))
    team_min_size = models.PositiveSmallIntegerField(_('Mínimo de integrantes'), default=1,
                                                     validators=[MinValueValidator(1), MaxValueValidator(100)])
    team_max_size = models.PositiveSmallIntegerField(_('Máximo de integrantes'), default=3,
                                                     validators=[MinValueValidator(1), MaxValueValidator(100)])
    # Where it is competed (2026-10-31): only with both does the scoreboard split and does whoever organizes place each
    # participant, who starts online (ContestParticipation.onsite). The value in the database is for the code from
    # before, between migrating and installing.
    ONLINE, ONSITE, HYBRID = 'online', 'onsite', 'hybrid'
    attendance_mode = models.CharField(
        _('Presencial o en línea'), max_length=10, default=ONLINE, db_default=ONLINE,
        choices=((ONLINE, _('Sólo en línea')), (ONSITE, _('Sólo presencial')), (HYBRID, _('Presencial y en línea'))),
        help_text=_('Con «Presencial y en línea», el ranking se separa en Global, Presencial y En línea, y en la '
                    'pestaña «Registrados» del concurso colocas a cada quien en su modalidad; todos empiezan en '
                    'línea, salvo quienes ya pusiste en la lista de la sede. Con las otras dos, todos compiten '
                    'juntos, como siempre. «Presencial y en línea» se puede poner en cualquier momento, pero no '
                    'quitar una vez empezado el concurso.'))
    # Who competes on site, named ahead in the tab «Registrados» (2026-10-31): the profiles and the teams it was given,
    # also before they register or join, so that they start on site when they do (designated_onsite). JSON written
    # only by its own UPDATE under the lock of this row (change_onsite_list), never by a form; save() keeps it.
    onsite_list = models.TextField(null=True, blank=True, editable=False)
    # Since 2026-10-29 it keeps every freeze that is not revealed, started or not (frozen_windows_for); the name
    # changed with that, so that code from before never reads the new list with the old meaning.
    FROZEN_WINDOWS_CACHE_KEY = 'frozen_contest_windows_v2'
    SCOREBOARD_VISIBLE = 'V'
    SCOREBOARD_AFTER_CONTEST = 'C'
    SCOREBOARD_AFTER_PARTICIPATION = 'P'
    SCOREBOARD_HIDDEN = 'H'
    SCOREBOARD_VISIBILITY = (
        (SCOREBOARD_VISIBLE, _('Visible')),
        (SCOREBOARD_AFTER_CONTEST, _('Hidden for duration of contest')),
        (SCOREBOARD_AFTER_PARTICIPATION, _('Hidden for duration of participation')),
        (SCOREBOARD_HIDDEN, _('Hidden permanently')),
    )
    key = models.CharField(max_length=20, verbose_name=_('contest id'), unique=True,
                           validators=[RegexValidator('^[a-z0-9]+$', _('Contest id must be ^[a-z0-9]+$'))])
    name = models.CharField(max_length=100, verbose_name=_('contest name'), db_index=True)
    authors = models.ManyToManyField(Profile, verbose_name=_('authors'),
                                     help_text=_('These users will be able to edit the contest.'),
                                     related_name='authored_contests')
    curators = models.ManyToManyField(Profile, verbose_name=_('curators'),
                                      help_text=_('These users will be able to edit the contest, '
                                                  'but will not be listed as authors.'),
                                      related_name='curated_contests', blank=True)
    testers = models.ManyToManyField(Profile, verbose_name=_('testers'),
                                     help_text=_('These users will be able to view the contest, but not edit it.'),
                                     blank=True, related_name='tested_contests')
    tester_see_scoreboard = models.BooleanField(verbose_name=_('testers see scoreboard'), default=False,
                                                help_text=_('If testers can see the scoreboard.'))
    tester_see_submissions = models.BooleanField(verbose_name=_('testers see submissions'), default=False,
                                                 help_text=_('If testers can see in-contest submissions.'))
    spectators = models.ManyToManyField(Profile, verbose_name=_('spectators'),
                                        help_text=_('These users will be able to spectate the contest, '
                                                    'but not see the problems ahead of time.'),
                                        blank=True, related_name='spectated_contests')
    description = models.TextField(verbose_name=_('description'), blank=True)
    problems = models.ManyToManyField(Problem, verbose_name=_('problems'), through='ContestProblem')
    start_time = models.DateTimeField(verbose_name=_('start time'), db_index=True)
    end_time = models.DateTimeField(verbose_name=_('end time'), db_index=True)
    time_limit = models.DurationField(verbose_name=_('time limit'), blank=True, null=True)
    is_visible = models.BooleanField(verbose_name=_('publicly visible'), default=False,
                                     help_text=_('Should be set even for organization-private contests, where it '
                                                 'determines whether the contest is visible to members of the '
                                                 'specified organizations.'))
    is_rated = models.BooleanField(verbose_name=_('contest rated'), help_text=_('Whether this contest can be rated.'),
                                   default=False)
    view_contest_scoreboard = models.ManyToManyField(Profile, verbose_name=_('view contest scoreboard'), blank=True,
                                                     related_name='view_contest_scoreboard',
                                                     help_text=_('These users will be able to view the scoreboard.'))
    view_contest_submissions = models.ManyToManyField(Profile, verbose_name=_('can see contest submissions'),
                                                      blank=True, related_name='view_contest_submissions',
                                                      help_text=_('These users will be able '
                                                                  'to see in-contest submissions.'))
    scoreboard_visibility = models.CharField(verbose_name=_('scoreboard visibility'), default=SCOREBOARD_VISIBLE,
                                             help_text=_('Scoreboard visibility through the duration of the contest.'),
                                             max_length=1, choices=SCOREBOARD_VISIBILITY)
    use_clarifications = models.BooleanField(verbose_name=_('no comments'),
                                             help_text=_('Use clarification system instead of comments.'),
                                             default=True)
    rating_floor = models.IntegerField(verbose_name=_('rating floor'),
                                       help_text=_('Do not rate users who have a lower rating.'), null=True, blank=True)
    rating_ceiling = models.IntegerField(verbose_name=_('rating ceiling'),
                                         help_text=_('Do not rate users who have a higher rating.'),
                                         null=True, blank=True)
    rate_all = models.BooleanField(verbose_name=_('rate all'),
                                   help_text=_('Rate users even if they make no submissions.'),
                                   default=False)
    rate_exclude = models.ManyToManyField(Profile, verbose_name=_('exclude from ratings'), blank=True,
                                          related_name='rate_exclude+')
    # Who of a contest on site and online at once gets a rating (2026-11-04): everybody, as always, or only one of
    # the two. Anywhere else everybody does (clean). The value in the database is for the code from before.
    RATE_EVERYBODY = 'all'
    rating_modality = models.CharField(
        _('Cuentan para el rating'), max_length=10, default=RATE_EVERYBODY, db_default=RATE_EVERYBODY,
        choices=((RATE_EVERYBODY, _('Todos')), (ONSITE, _('Sólo presencial')), (ONLINE, _('Sólo en línea'))),
        help_text=_('Sólo en concursos «Presencial y en línea». En los demás cuentan todos.'))
    is_private = models.BooleanField(verbose_name=_('private to specific users'), default=False)
    private_teams = models.ManyToManyField('Team', blank=True, related_name='private_contests',
                                          verbose_name=_('Equipos autorizados'),
                                          help_text=_('Equipos que pueden acceder e inscribirse en este concurso privado.'))
    private_contestants = models.ManyToManyField(Profile, blank=True, verbose_name=_('private contestants'),
                                                 help_text=_('If non-empty, only these users may see the contest.'),
                                                 related_name='private_contestants+')
    # Whether the editor suggests words of the code, keywords and headers inside this contest (2026-11-08): on the
    # submit page and the custom test while someone is in it. Off unless whoever organizes allows it; outside
    # contests it is on. The value in the database is for the code from before.
    autocomplete = models.BooleanField(
        verbose_name=_('allow autocompletion'), default=False, db_default=False,
        help_text=_('The editor suggests words of the code, keywords and headers while typing. Each person can '
                    'turn it off in their editor.'))
    # A page that anybody can open without an account, with the scoreboard of this contest as it happens
    # (2026-11-10): /marcador/<key>/ (judge/public_scoreboard.py). Off unless whoever organizes turns it on. It is of
    # private and of hidden contests too (the owner, 2026-11-10: those are the ones family and friends cannot see any
    # other way); a clone starts without it. It shows the user name, the name and the picture of each participant,
    # and it follows the freeze. The value in the database is for the code from before. The help text is not the one
    # of the migration 0168, which stays as it was applied: a help text is not part of the table.
    public_scoreboard = models.BooleanField(
        verbose_name='marcador público', default=False, db_default=False,
        help_text='Cualquiera puede ver el marcador en vivo, sin cuenta, en /marcador/ seguido de la clave del '
                  'concurso: usuario, nombre y foto de cada participante. Respeta la congelación. También con el '
                  'concurso privado u oculto.')
    hide_problem_tags = models.BooleanField(verbose_name=_('hide problem tags'),
                                            help_text=_('Whether problem tags should be hidden by default.'),
                                            default=False)
    hide_problem_authors = models.BooleanField(verbose_name=_('hide problem authors'),
                                               help_text=_('Whether problem authors should be hidden by default.'),
                                               default=False)
    run_pretests_only = models.BooleanField(verbose_name=_('run pretests only'),
                                            help_text=_('Whether judges should grade pretests only, versus all '
                                                        'testcases. Commonly set during a contest, then unset '
                                                        'prior to rejudging user submissions when the contest ends.'),
                                            default=False)
    show_short_display = models.BooleanField(verbose_name=_('show short form settings display'),
                                             help_text=_('Whether to show a section containing contest settings '
                                                         'on the contest page or not.'),
                                             default=False)
    is_organization_private = models.BooleanField(verbose_name=_('private to organizations'), default=False)
    organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('organizations'),
                                           help_text=_('If non-empty, only these organizations may see the contest.'))
    limit_join_organizations = models.BooleanField(verbose_name=_('limit organizations that can join'), default=False)
    join_organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('join organizations'),
                                                help_text=_('If non-empty, only these organizations may join '
                                                            'the contest.'), related_name='join_only_contests')
    classes = models.ManyToManyField(Class, blank=True, verbose_name=_('classes'),
                                     help_text=_('If organization private, only these classes may see the contest.'))
    og_image = models.CharField(verbose_name=_('OpenGraph image'), default='', max_length=150, blank=True)
    logo_override_image = models.CharField(verbose_name=_('logo override image'), default='', max_length=150,
                                           blank=True,
                                           help_text=_('This image will replace the default site logo for users '
                                                       'inside the contest.'))
    tags = models.ManyToManyField(ContestTag, verbose_name=_('contest tags'), blank=True, related_name='contests')
    user_count = models.IntegerField(verbose_name=_('the amount of live participants'), default=0)
    summary = models.TextField(blank=True, verbose_name=_('contest summary'),
                               help_text=_('Plain-text, shown in meta description tag, e.g. for social media.'))
    access_code = models.CharField(verbose_name=_('access code'), blank=True, default='', max_length=255,
                                   help_text=_('An optional code to prompt contestants before they are allowed '
                                               'to join the contest. Leave it blank to disable.'))
    banned_users = models.ManyToManyField(Profile, verbose_name=_('personae non gratae'), blank=True,
                                          help_text=_('Bans the selected users from joining this contest.'))
    format_name = models.CharField(verbose_name=_('contest format'), default='default', max_length=32,
                                   choices=contest_format.choices(), help_text=_('The contest format module to use.'))
    format_config = JSONField(verbose_name=_('contest format configuration'), null=True, blank=True,
                              help_text=_('A JSON object to serve as the configuration for the chosen contest format '
                                          'module. Leave empty to use None. Exact format depends on the contest format '
                                          'selected.'))
    problem_label_script = models.TextField(verbose_name=_('contest problem label script'), blank=True,
                                            help_text=_('A custom Lua function to generate problem labels. Requires a '
                                                        'single function with an integer parameter, the zero-indexed '
                                                        'contest problem index, and returns a string, the label.'))
    locked_after = models.DateTimeField(verbose_name=_('contest lock'), null=True, blank=True,
                                        help_text=_('Prevent submissions from this contest '
                                                    'from being rejudged after this date.'))
    freeze_minutes = models.PositiveIntegerField(
        verbose_name=_('scoreboard freeze'), null=True, blank=True,
        help_text=_('Freeze the public scoreboard over the last this many minutes of each participation. Leave '
                    'empty not to freeze it. A virtual participation is frozen over its own last minutes, not '
                    'over the clock of the live contest.'))
    scoreboard_revealed = models.BooleanField(
        verbose_name=_('scoreboard revealed'), default=False,
        help_text=_('A frozen scoreboard stays frozen after the contest ends, so the result can be revealed at a '
                    'ceremony. Check this to lift the freeze and show everyone the real scoreboard and the '
                    'submissions it was hiding.'))
    balloon_staff = models.ManyToManyField(Profile, verbose_name=_('balloon staff'), blank=True,
                                           related_name='balloon_contests',
                                           help_text=_('These users will be able to see which balloons are due and '
                                                       'mark them as delivered, without being able to edit the '
                                                       'contest or see its submissions.'))
    points_precision = models.IntegerField(verbose_name=_('precision points'), default=3,
                                           validators=[MinValueValidator(0), MaxValueValidator(10)],
                                           help_text=_('Number of digits to round points to.'))

    @cached_property
    def format_class(self):
        return contest_format.formats[self.format_name]

    @cached_property
    def format(self):
        return self.format_class(self, self.format_config)

    @cached_property
    def get_label_for_problem(self):
        if not self.problem_label_script:
            return self.format.get_label_for_problem

        def DENY_ALL(obj, attr_name, is_setting):
            raise AttributeError()
        lua = LuaRuntime(attribute_filter=DENY_ALL, register_eval=False, register_builtins=False)
        return lua.eval(self.problem_label_script)

    def clean(self):
        if self.team_min_size and self.team_max_size and self.team_min_size > self.team_max_size:
            raise ValidationError({'team_max_size': _('El máximo debe ser mayor o igual al mínimo.')})
        if self.participation_mode == self.TEAM and self.is_rated:
            raise ValidationError({'is_rated': _('Los concursos exclusivos de equipos no pueden ser calificados.')})
        # Not on the field: whoever cannot rate sees it read-only, and an error on a field the form lacks is a 500.
        if self.rating_modality != self.RATE_EVERYBODY and self.attendance_mode != self.HYBRID:
            raise ValidationError(_('«Cuentan para el rating» sólo puede ser «Sólo presencial» o «Sólo en línea» en '
                                    'un concurso «Presencial y en línea».'))
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values('participation_mode', 'attendance_mode',
                                                                    'start_time').first()
            # Taking «Presencial y en línea» away once the contest started, with somebody in it, would merge the
            # scoreboard and send balloons also to whoever competes online (2026-10-31). Putting it is always fine:
            # everybody stays online until whoever organizes places them.
            if previous and previous['attendance_mode'] == self.HYBRID and self.attendance_mode != self.HYBRID and \
                    previous['start_time'] <= timezone.now() and \
                    self.users.filter(virtual=ContestParticipation.LIVE).exists():
                raise ValidationError({'attendance_mode': _(
                    'Con el concurso ya empezado no se puede quitar «Presencial y en línea»: el ranking se juntaría y '
                    'los globos irían también a quienes compiten en línea. Si todos están en la sede, pásalos a '
                    'presencial en «Registrados».')})
            if previous and previous['participation_mode'] != self.participation_mode:
                if self.users.filter(virtual__gte=0).exists():
                    raise ValidationError({'participation_mode': _(
                        'No se puede cambiar la modalidad después de la primera participación.')})
                # A registration was made for that mode: a team cannot stay registered in an individual contest.
                if self.users.filter(virtual=ContestParticipation.REGISTERED).exists():
                    raise ValidationError({'participation_mode': _(
                        'No se puede cambiar la modalidad de participación después del primer registro.')})
        # Django will complain if you didn't fill in start_time or end_time, so we don't have to.
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError('What is this? A contest that ended before it starts?')
        self.format_class.validate(self.format_config)

        try:
            # a contest should have at least one problem, with contest problem index 0
            # so test it to see if the script returns a valid label.
            label = self.get_label_for_problem(0)
        except Exception as e:
            raise ValidationError('Contest problem label script: %s' % e)
        else:
            if not isinstance(label, str):
                raise ValidationError('Contest problem label script: script should return a string.')

    def is_in_contest(self, user):
        if user.is_authenticated:
            profile = user.profile
            return profile and profile.current_contest is not None and profile.current_contest.contest == self
        return False

    def can_see_own_scoreboard(self, user):
        if self.can_see_full_scoreboard(user):
            return True
        if not self.started:
            return False
        if not self.show_scoreboard and not self.is_in_contest(user) and not self.has_completed_contest(user):
            return False
        return True

    def can_see_full_scoreboard(self, user):
        if self.show_scoreboard:
            return True
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return True
        if user.profile.id in self.editor_ids:
            return True
        if self.tester_see_scoreboard and user.profile.id in self.tester_ids:
            return True
        if self.started and user.profile.id in self.spectator_ids:
            return True
        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return True
        if self.scoreboard_visibility == self.SCOREBOARD_AFTER_PARTICIPATION and self.has_completed_contest(user):
            return True
        return False

    def has_completed_contest(self, user):
        if user.is_authenticated:
            participation = self.users.for_profile(user.profile).filter(virtual=ContestParticipation.LIVE).first()
            if participation and participation.ended:
                return True
        return False

    @cached_property
    def show_scoreboard(self):
        if not self.started:
            return False
        if (self.scoreboard_visibility in (self.SCOREBOARD_AFTER_CONTEST, self.SCOREBOARD_AFTER_PARTICIPATION) and
                not self.ended):
            return False
        return self.scoreboard_visibility != self.SCOREBOARD_HIDDEN

    @cached_property
    def freeze_delta(self):
        return timedelta(minutes=self.freeze_minutes) if self.freeze_minutes else None

    @cached_property
    def freeze_active(self):
        """Whether this contest freezes anything at all right now.

        A revealed scoreboard never freezes again, and an unrevealed one stays frozen after the contest ends:
        that is what leaves something to reveal.
        """
        return self.freeze_delta is not None and not self.scoreboard_revealed

    @cached_property
    def submission_freeze_cutoff(self):
        """Earliest moment at which any participation of this contest can have entered its freeze window.

        Used to hide submissions, where a per-participation cutoff is not available in one query. For a contest
        with a per-user time limit this hides a little more than strictly necessary for whoever started late.
        It never hides less.
        """
        if self.freeze_delta is None:
            return None
        end = min(self.start_time + self.time_limit, self.end_time) if self.time_limit else self.end_time
        return end - self.freeze_delta

    @cached_property
    def freeze_started(self):
        cutoff = self.submission_freeze_cutoff
        return cutoff is not None and self._now >= cutoff

    def is_frozen_for(self, user):
        """Whether this user has to be shown the frozen scoreboard and the submissions that go with it.

        Only those who can edit the contest see through the freeze. A staff account that can read every
        submission but cannot edit this contest is deliberately included in the freeze.
        """
        return self.freeze_active and self.freeze_started and not self.is_editable_by(user)

    @classmethod
    def frozen_submission_filter(cls, user):
        """Q matching the submissions the freeze hides from this user, or None if it hides nothing.

        This runs on every submission list, so the set of frozen contests — normally empty, at most a handful —
        is cached for half a minute. Revealing a scoreboard drops that cache, so the reveal is immediate.
        """
        query = Q()
        hides_something = False
        for contest_id, cutoff in cls.frozen_windows_for(user):
            query |= Q(contest_object_id=contest_id, date__gte=cutoff)
            hides_something = True
        return query if hides_something else None

    @classmethod
    def unrevealed_freezes(cls):
        """(contest id, cutoff) of every contest with a freeze that is not revealed, whether it started or not. Not
        cached: what is stored from the submissions (the AC rate of a problem, the points of a profile) is worked out
        from this, and a stale list would store what a reveal has just made public as still hidden, or the other way
        round. One small query; normally it finds nothing."""
        contests = cls.objects.filter(freeze_minutes__isnull=False, scoreboard_revealed=False)
        windows = [(contest.id, contest.submission_freeze_cutoff)
                   for contest in contests.only('id', 'start_time', 'end_time', 'time_limit', 'freeze_minutes')]
        return [(contest_id, cutoff) for contest_id, cutoff in windows if cutoff is not None]

    @classmethod
    def public_freeze_filter(cls):
        """Q matching the submissions that some freeze hides from the public right now, or None. For what is stored
        and read by everybody: the AC rate and users of a problem, and the points and solved problems of a profile
        (congelacion-revelador-3, 2026-10-29). A cutoff still in the future matches nothing yet."""
        query, found = Q(), False
        for contest_id, cutoff in cls.unrevealed_freezes():
            query |= Q(contest_object_id=contest_id, date__gte=cutoff)
            found = True
        return query if found else None

    @classmethod
    def frozen_windows_for(cls, user):
        """(contest id, cutoff) of every frozen contest whose freeze this user does not see through.

        The list of freezes is cached for half a minute, and since 2026-10-29 (congelacion-revelador-4) it holds every
        freeze that is not revealed, started or not: whether one has started is decided now, on every read. Before,
        a list taken seconds before a cutoff kept that contest out of it for up to half a minute after the cutoff,
        and the submission lists showed the first seconds of the freeze with their verdicts.
        """
        windows = cache.get(cls.FROZEN_WINDOWS_CACHE_KEY)
        if windows is None:
            windows = cls.unrevealed_freezes()
            cache.set(cls.FROZEN_WINDOWS_CACHE_KEY, windows, 30)
        now = timezone.now()
        windows = [(contest_id, cutoff) for contest_id, cutoff in windows if cutoff <= now]
        if not windows:
            return []

        # Whoever can edit a contest sees through its freeze, so those contests drop out. One query for all of
        # them, with the same authors-or-curators rule `editor_ids` uses.
        seen_through = set()
        if user.is_authenticated:
            if user.has_perm('judge.edit_all_contest'):
                return []
            if user.has_perm('judge.edit_own_contest'):
                profile_id = user.profile.id
                seen_through = set(
                    cls.objects.filter(id__in=[contest_id for contest_id, _cutoff in windows])
                       .filter(Q(authors=profile_id) | Q(curators=profile_id))
                       .values_list('id', flat=True),
                )
        return [(contest_id, cutoff) for contest_id, cutoff in windows if contest_id not in seen_through]

    @property
    def contest_window_length(self):
        return self.end_time - self.start_time

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @cached_property
    def started(self):
        return self.start_time <= self._now

    @property
    def time_before_start(self):
        if self.start_time >= self._now:
            return self.start_time - self._now
        else:
            return None

    @property
    def time_before_end(self):
        if self.end_time >= self._now:
            return self.end_time - self._now
        else:
            return None

    @cached_property
    def ended(self):
        return self.end_time < self._now

    @cached_property
    def author_ids(self):
        return Contest.authors.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    @cached_property
    def editor_ids(self):
        return self.author_ids.union(
            Contest.curators.through.objects.filter(contest=self).values_list('profile_id', flat=True))

    @cached_property
    def tester_ids(self):
        return Contest.testers.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    @cached_property
    def spectator_ids(self):
        return Contest.spectators.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('contest_view', args=(self.key,))

    def update_user_count(self):
        self.user_count = self.users.filter(virtual=0).count()
        self.save()

    update_user_count.alters_data = True

    def recount_users(self):
        """`user_count` again, without saving the whole contest (2026-10-31). Everybody registered enters in the same
        seconds at the start: one full save each would run every signal of the contest that many times. Counted
        inside the UPDATE itself, so that of two entries at once the older count is never the one left."""
        live = (ContestParticipation.objects.filter(contest_id=OuterRef('pk'), virtual=ContestParticipation.LIVE)
                .order_by().values('contest_id').annotate(count=Count('id')).values('count'))
        type(self).objects.filter(pk=self.pk).update(user_count=Coalesce(Subquery(live), 0))
        self.user_count = type(self).objects.filter(pk=self.pk).values_list('user_count', flat=True).first() or 0

    recount_users.alters_data = True

    @property
    def is_hybrid(self):
        """On site and online at once: the scoreboard splits and whoever organizes places each participant."""
        return self.attendance_mode == self.HYBRID

    def onsite_designations(self):
        """(profile ids, team ids) of the list of who competes on site (onsite_list): two empty sets without one, or
        with a value that is not what change_onsite_list writes."""
        try:
            stored = json.loads(self.onsite_list or '{}')
            return {int(pk) for pk in stored.get('profiles', ())}, {int(pk) for pk in stored.get('teams', ())}
        except (TypeError, ValueError, AttributeError):
            return set(), set()

    def designated_onsite(self, profile_ids, team_id=None):
        """Whether a participation of these people (and of this team) starts on site: only in a contest that is on
        site and online, and only when the list names the team or one of them (2026-10-31)."""
        if not self.is_hybrid:
            return False
        profiles, teams = self.onsite_designations()
        return (team_id is not None and team_id in teams) or any(pk in profiles for pk in profile_ids)

    def change_onsite_list(self, add_profiles=(), add_teams=(), remove_profiles=(), remove_teams=()):
        """Name people and teams on the list of who competes on site, or take them off it. The caller is inside a
        transaction: the row is locked first, as join_attempt locks it before reading the list."""
        current = (type(self).objects.select_for_update().filter(pk=self.pk)
                   .values_list('onsite_list', flat=True).first())
        self.onsite_list = current
        profiles, teams = self.onsite_designations()
        profiles = (profiles | set(add_profiles)) - set(remove_profiles)
        teams = (teams | set(add_teams)) - set(remove_teams)
        value = json.dumps({'profiles': sorted(profiles), 'teams': sorted(teams)}) if profiles or teams else None
        if value != current:
            type(self).objects.filter(pk=self.pk).update(onsite_list=value)
        self.onsite_list = value

    change_onsite_list.alters_data = True

    def registered_count(self):
        """How many registrations wait for the start: the number everybody sees, the same as the tab «Registrados»
        counts (ContestParticipationQuerySet.waiting_registrations)."""
        return self.users.waiting_registrations().count()

    def is_registrable_by(self, user):
        """Whether this user may register before the start (2026-10-31), the same rules as joining: an organizer or a
        tester enters early as a spectator instead, and whoever is banned from the contest cannot. Assumes the user
        can access the contest, as is_live_joinable_by; the access code is asked when registering."""
        if self.started or not user.is_authenticated:
            return False
        if self.banned_users.filter(pk=user.profile.pk).exists():
            return False
        editor_or_tester = getattr(self, 'editor_or_tester', None)
        if editor_or_tester is None:
            editor_or_tester = user.profile.id in self.editor_ids or user.profile.id in self.tester_ids
        if editor_or_tester:
            return False
        if self.limit_join_organizations:
            return self.join_organizations.filter(id__in=user.profile.organizations.all()).exists()
        return True

    def registration_for(self, user):
        """The registration of this user, alone or on the roster of a team, that has not entered yet, or None."""
        if not user.is_authenticated:
            return None
        from judge.contest_registration import superseded
        entry = (self.users.for_profile(user.profile).filter(virtual=ContestParticipation.REGISTERED)
                 .select_related('user__user').prefetch_related('team_roster').first())
        return None if entry is None or superseded(entry) else entry

    class Inaccessible(Exception):
        pass

    class PrivateContest(Exception):
        pass

    def access_check(self, user):
        # Do unauthenticated check here so we can skip authentication checks later on.
        if not user.is_authenticated:
            # Unauthenticated users can only see visible, non-private contests
            if not self.is_visible:
                raise self.Inaccessible()
            if self.is_private or self.is_organization_private:
                raise self.PrivateContest()
            return

        # If the user can view or edit all contests
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return

        # User is organizer or curator for contest
        if user.profile.id in self.editor_ids:
            return

        # User is tester for contest
        if user.profile.id in self.tester_ids:
            return

        # User is spectator for contest
        if user.profile.id in self.spectator_ids:
            return

        # Contest is not publicly visible
        if not self.is_visible:
            raise self.Inaccessible()

        # Contest is not private
        if not self.is_private and not self.is_organization_private:
            return

        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return

        in_org = (self.organizations.filter(id__in=user.profile.organizations.all()).exists() or
                  self.classes.filter(id__in=user.profile.classes.all()).exists())
        # Whoever registers a team answers for the whole roster: the code, the privacy, and the organisation limit
        # too (teams.md, 2026-09-19). Until 2026-10-29 (equipos-globos-1) the organisation was asked again of every
        # member, so a teammate from outside it stayed on the roster without being able to open the contest.
        in_roster = self.users.filter(team__isnull=False, team_roster__profile=user.profile).exists()
        # And whoever organizes it answers for whoever they register (2026-11-04, the owner: «sí les da acceso si es
        # privado», also when it is limited to organisations), while the registration lasts.
        vouched = in_roster or self.users.filter(team__isnull=True, user=user.profile, by_organizer=True,
                                                 virtual__in=ContestParticipation.OFFICIAL).exists()
        in_users = (self.private_contestants.filter(id=user.profile.id).exists() or
                    self.private_teams.filter(is_active=True, members=user.profile).exists() or
                    vouched)

        if not self.is_private and self.is_organization_private:
            if in_org or vouched:
                return
            raise self.PrivateContest()

        if self.is_private and not self.is_organization_private:
            if in_users:
                return
            raise self.PrivateContest()

        if self.is_private and self.is_organization_private:
            if (in_org and in_users) or vouched:
                return
            raise self.PrivateContest()

    # Assumes the user can access, to avoid the cost again
    def is_live_joinable_by(self, user):
        if not self.started:
            return False

        if not user.is_authenticated:
            return False

        # Can be populated using annotate for performance in list views.
        editor_or_tester = getattr(self, 'editor_or_tester', None)
        if editor_or_tester is None:
            editor_or_tester = user.profile.id in self.editor_ids or user.profile.id in self.tester_ids

        if editor_or_tester:
            return False

        completed_contest = getattr(self, 'completed_contest', None)
        if completed_contest is None:
            completed_contest = self.has_completed_contest(user)

        if completed_contest:
            return False

        if self.limit_join_organizations:
            return self.join_organizations.filter(id__in=user.profile.organizations.all()).exists()
        return True

    # Also skips access check
    def is_spectatable_by(self, user):
        if not user.is_authenticated:
            return False

        if user.profile.id in self.editor_ids or user.profile.id in self.tester_ids:
            return True

        if self.limit_join_organizations:
            return self.join_organizations.filter(id__in=user.profile.organizations.all()).exists()
        return True

    def is_accessible_by(self, user):
        try:
            self.access_check(user)
        except (self.Inaccessible, self.PrivateContest):
            return False
        else:
            return True

    def is_editable_by(self, user):
        # If the user can edit all contests
        if user.has_perm('judge.edit_all_contest'):
            return True

        # If the user is a contest organizer or curator
        if user.has_perm('judge.edit_own_contest') and user.profile.id in self.editor_ids:
            return True

        return False

    def is_balloon_staff(self, user):
        return user.is_authenticated and self.balloon_staff.filter(id=user.profile.id).exists()

    def can_manage_balloons(self, user):
        """Whether this user may open the balloon sheet and mark balloons as delivered.

        Balloon staff get exactly that and nothing else: the sheet shows who solved which problem outside the
        freeze window, and never what the freeze is hiding.
        """
        return self.is_editable_by(user) or self.is_balloon_staff(user)

    @staticmethod
    def duel_contests():
        """The hidden contests that back the games of the duels (judge/arena). Also those whose game was deleted from
        the admin, which kept their contest until 2026-10-19: they keep the key, the summary and the visibility that
        engine.start_game gives them, and nothing else creates contests like that."""
        return Q(arena_game__isnull=False) | Q(key__regex=r'^arena[0-9]+(x[0-9a-f]{6})?$',
                                                summary__startswith='Arena game ', is_visible=False)

    @classmethod
    def get_visible_contests(cls, user):
        # The hidden contests that back arena games are internal: they are reached from the arena, never listed.
        if not user.is_authenticated:
            return cls.objects.filter(is_visible=True, is_organization_private=False, is_private=False,
                                      arena_game__isnull=True).defer('description').distinct()

        queryset = cls.objects.defer('description').exclude(cls.duel_contests())
        if not (user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest')):
            org_check = (Q(organizations__in=user.profile.organizations.all()) |
                         Q(classes__in=user.profile.classes.all()))
            q = Q(is_visible=True)
            q &= (
                Q(view_contest_scoreboard=user.profile) |
                Q(is_organization_private=False, is_private=False) |
                Q(is_organization_private=False, is_private=True, private_contestants=user.profile) |
                (Q(is_organization_private=True, is_private=False) & org_check) |
                (Q(is_organization_private=True, is_private=True, private_contestants=user.profile) & org_check)
            )

            q |= Q(authors=user.profile)
            q |= Q(curators=user.profile)
            q |= Q(testers=user.profile)
            q |= Q(spectators=user.profile)
            # The two team branches as subqueries (equipos-globos-7, 2026-10-29): as joins inside the OR they
            # multiplied the rows of every contest by its participations and team members before the DISTINCT. And
            # the registered roster opens the contest also when it is private to organisations, as access_check
            # does (equipos-globos-1).
            from judge.models.team import ContestTeamMember
            authorized_team = Contest.private_teams.through.objects.filter(
                contest_id=OuterRef('pk'), team__is_active=True, team__members=user.profile)
            on_roster = ContestTeamMember.objects.filter(participation__contest_id=OuterRef('pk'), profile=user.profile)
            q |= (Q(is_visible=True, is_private=True) & Q(Exists(authorized_team)) &
                  (Q(is_organization_private=False) | org_check))
            q |= Q(is_visible=True) & Q(Exists(on_roster))
            # Registered by whoever organizes it (2026-11-04), as access_check lets them in.
            vouched = ContestParticipation.objects.filter(contest_id=OuterRef('pk'), user=user.profile,
                                                          team__isnull=True, by_organizer=True,
                                                          virtual__in=ContestParticipation.OFFICIAL)
            q |= Q(is_visible=True) & Q(Exists(vouched))
            queryset = queryset.filter(q)
        return queryset.distinct()

    def rate(self):
        """Rate this contest and redo every rated contest that ended after it. A contest whose scoreboard is still
        frozen is passed over by rate_contest and keeps no ratings until it is revealed and rated
        (congelacion-revelador-1)."""
        with transaction.atomic():
            Rating.objects.filter(contest__end_time__range=(self.end_time, self._now)).delete()
            for contest in Contest.objects.filter(
                is_rated=True, end_time__range=(self.end_time, self._now),
            ).order_by('end_time'):
                rate_contest(contest)

    def save(self, *args, **kwargs):
        """Keeps the cutoff of a freeze that already started (kept_freeze_minutes), whatever saves the contest: the
        form of the administration already shows why a change is refused, and adjusts the minutes itself.

        A full save also keeps the list of who competes on site as it is now (2026-10-31): only its own UPDATE writes
        it (change_onsite_list), and an older copy would put it back as it was when the copy was read. Inside a
        transaction it is read under the lock of the row, as that UPDATE takes it."""
        fields = kwargs.get('update_fields')
        if self.pk and (fields is None or set(fields) & set(FREEZE_SCHEDULE)):
            stored = type(self).objects.filter(pk=self.pk)
            if fields is None and transaction.get_connection().in_atomic_block:
                stored = stored.select_for_update()
            stored = stored.values(*FREEZE_SCHEDULE, 'onsite_list').first()
            if stored is not None:
                onsite_list = stored.pop('onsite_list')
                if fields is None:
                    self.onsite_list = onsite_list
                minutes = kept_freeze_minutes(stored, {field: getattr(self, field) for field in FREEZE_SCHEDULE},
                                              getattr(self, '_freeze_checked_at', None), live=self.takes_part_live)
                if minutes != self.freeze_minutes:
                    self.freeze_minutes = minutes
                    if fields is not None and 'freeze_minutes' not in fields:
                        kwargs['update_fields'] = list(fields) + ['freeze_minutes']
                for name in ('freeze_delta', 'freeze_active', 'submission_freeze_cutoff', 'freeze_started'):
                    self.__dict__.pop(name, None)
        super().save(*args, **kwargs)
        # What the form saw holds for this save only: a later save of the same instance looks again.
        for name in ('_freeze_checked_at', '_freeze_live'):
            self.__dict__.pop(name, None)

    def takes_part_live(self):
        """Whether anybody takes part live in this contest (kept_freeze_minutes). The form asks first, and the save
        keeps its answer, as it keeps its moment: somebody who joins in between cannot make the save refuse what the
        form accepted."""
        if getattr(self, '_freeze_live', None) is None:
            self._freeze_live = ContestParticipation.objects.filter(
                contest_id=self.pk, virtual=ContestParticipation.LIVE).exists()
        return self._freeze_live

    @property
    def hides_now(self):
        """Whether a freeze of this contest hides anything right now: configured, started and not revealed."""
        return self.freeze_active and self.freeze_started

    def refresh_after_freeze_change(self):
        """Recompute what is stored from submissions and depends on this freeze: the AC rate and users of the problems
        sent to in this contest, the points and solved problems of whoever sent something to it, and its ratings.

        While a freeze hides something, those stored values leave out what it hides (public_freeze_filter), so they
        have to be recomputed when it starts or stops hiding on purpose: revealing, freezing again, configuring a
        freeze once it is due or removing it. A freeze that begins on its own, at the cutoff, needs nothing: each
        submission judged from then on is left out as it is judged. A rated contest that hides something again
        loses its ratings (rate passes it over) until it is revealed and rated (congelacion-revelador-1).
        """
        contest = type(self).objects.get(pk=self.pk)
        sent = Submission.objects.filter(contest_object=contest)
        problem_ids = set(contest.contest_problems.values_list('problem_id', flat=True))
        problem_ids.update(sent.values_list('problem_id', flat=True).distinct())
        for problem in Problem.objects.filter(id__in=problem_ids):
            problem._updating_stats_only = True
            problem.update_stats()
        for profile in Profile.objects.filter(id__in=sent.values('user_id')):
            profile._updating_stats_only = True
            profile.calculate_points()
        if contest.is_rated and contest.hides_now and contest.ratings.exists():
            contest.rate()

    refresh_after_freeze_change.alters_data = True

    class Meta:
        permissions = (
            ('see_private_contest', _('See private contests')),
            ('edit_own_contest', _('Edit own contests')),
            ('edit_all_contest', _('Edit all contests')),
            ('clone_contest', _('Clone contest')),
            ('moss_contest', _('MOSS contest')),
            ('contest_rating', _('Rate contests')),
            ('contest_access_code', _('Contest access codes')),
            ('create_private_contest', _('Create private contests')),
            ('change_contest_visibility', _('Change contest visibility')),
            ('contest_problem_label', _('Edit contest problem label script')),
            ('lock_contest', _('Change lock status of contest')),
        )
        verbose_name = _('contest')
        verbose_name_plural = _('contests')


class ContestParticipationQuerySet(models.QuerySet):
    def for_profile(self, profile):
        if profile is None:
            return self.none()
        return self.filter(Q(team__isnull=True, user=profile) |
                           Q(pk__in=profile.team_contest_entries.values('participation_id')))

    def waiting_registrations(self):
        """The registrations that wait for the start and can enter (2026-10-31): without those of whoever organizes
        or tests the contest today, nor individual ones of accounts no longer active. A team whose registrant left
        stays: the rest of its roster still enters."""
        return (self.filter(virtual=ContestParticipation.REGISTERED).without_contest_staff()
                .exclude(team__isnull=True, user__user__is_active=False))

    def official(self):
        """The one official registration of each person in a contest: live, or registered before the start and
        waiting for it (2026-10-31)."""
        return self.filter(virtual__in=ContestParticipation.OFFICIAL)

    def without_contest_staff(self):
        """Without the official participations of whoever organizes (author or curator) or tests their contest today,
        alone or on the roster of a team (decision of the owner, 2026-10-29).

        Joining makes an organizer or a tester a spectator, but a participation from before they got the role stays:
        the balloon sheet, the reveal and its awards, the scoreboard, the API and the rating counted it as one more
        contestant. They no longer do, and it counts again by itself if they lose the role. Virtual participations
        are not touched. One subquery per role, so it works on participations of several contests too.
        """
        from judge.models.team import ContestTeamMember
        throughs = (Contest.authors.through, Contest.curators.through, Contest.testers.through)
        # The participant: the person of an individual participation, or whoever registered a team.
        alone = Q()
        for through in throughs:
            alone |= Q(Exists(through.objects.filter(contest_id=OuterRef('contest_id'),
                                                     profile_id=OuterRef('user_id'))))
        # Anybody on the roster of a team, against the roles in the contest of that participation.
        member = Q()
        for through in throughs:
            member |= Q(Exists(through.objects.filter(contest_id=OuterRef(OuterRef('contest_id')),
                                                      profile_id=OuterRef('profile_id'))))
        staff_member = ContestTeamMember.objects.filter(participation_id=OuterRef('pk')).filter(member)
        # Also a registration made before getting the role (2026-10-31): the list of the registered and the seats.
        return self.exclude(Q(virtual__in=ContestParticipation.OFFICIAL) & (alone | Q(Exists(staff_member))))


class ContestParticipation(models.Model):
    LIVE = 0
    SPECTATE = -1
    # Registered before the start (2026-10-31), like SPECTATE a value no reader of the scoreboard takes: the ranking,
    # the API, the reveal, the balloons, the rating, user_count and MOSS all ask for LIVE. The same row becomes LIVE
    # when its contest starts (judge/contest_registration.py); until then it never is anybody's current_contest.
    REGISTERED = -2
    OFFICIAL = (LIVE, REGISTERED)

    contest = models.ForeignKey(Contest, verbose_name=_('associated contest'), related_name='users', on_delete=CASCADE)
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='contest_history', on_delete=CASCADE)
    team = models.ForeignKey('Team', null=True, blank=True, on_delete=models.PROTECT, related_name='participations')
    team_name = models.CharField(max_length=60, blank=True, default='')
    objects = ContestParticipationQuerySet.as_manager()
    real_start = models.DateTimeField(verbose_name=_('start time'), default=timezone.now, db_column='start')
    score = models.FloatField(verbose_name=_('score'), default=0, db_index=True)
    cumtime = models.PositiveIntegerField(verbose_name=_('cumulative time'), default=0)
    is_disqualified = models.BooleanField(verbose_name=_('is disqualified'), default=False,
                                          help_text=_('Whether this participation is disqualified.'))
    tiebreaker = models.FloatField(verbose_name=_('tie-breaking field'), default=0.0)
    virtual = models.IntegerField(verbose_name=_('virtual participation id'), default=LIVE,
                                  help_text=_('0 means non-virtual, otherwise the n-th virtual participation.'))
    format_data = JSONField(verbose_name=_('contest format specific data'), null=True, blank=True)
    frozen_score = models.FloatField(verbose_name=_('score at freeze time'), null=True, blank=True)
    frozen_cumtime = models.PositiveIntegerField(verbose_name=_('cumulative time at freeze time'),
                                                 null=True, blank=True)
    frozen_tiebreaker = models.FloatField(verbose_name=_('tie-breaking field at freeze time'), null=True, blank=True)
    frozen_format_data = JSONField(verbose_name=_('contest format specific data at freeze time'),
                                   null=True, blank=True)
    frozen_at = models.DateTimeField(verbose_name=_('scoreboard frozen at'), null=True, blank=True,
                                     help_text=_('When the frozen copy above was taken. Empty means no copy was '
                                                 'needed, because nothing has changed since the freeze.'))
    # On site, in a contest that is both (Contest.HYBRID); only whoever organizes changes it. The value in the database
    # is for the code from before, between migrating and installing.
    onsite = models.BooleanField(_('Presencial'), default=False, db_default=False)
    # Registered by whoever organizes the contest, from the tab «Registrados» (2026-11-04): they answer for it, so
    # neither the access code, nor the privacy, nor the organisations of the contest are asked of it, when registering
    # or when entering, and the registration itself opens the contest (Contest.access_check). Whoever it is for can
    # still cancel it. The value in the database is for the code from before.
    by_organizer = models.BooleanField(_('Registrado por la organización'), default=False, db_default=False)

    @cached_property
    def freeze_starts_at(self):
        """When this participation enters its own freeze window.

        Measured from this participation's end, so a virtual participation freezes over its own last minutes
        instead of at a clock time that means nothing to it.
        """
        delta = self.contest.freeze_delta
        return None if delta is None else self.end_time - delta

    @property
    def is_frozen(self):
        return (self.contest.freeze_active and self.freeze_starts_at is not None and
                self._now >= self.freeze_starts_at)

    def freeze_scoreboard(self):
        """Keep the scoreboard as it stood when this participation entered its freeze window.

        Called right before recomputing, so what gets stored is the state before the first correction that
        lands after the freeze: exactly what the public has to keep seeing. A participation that is never
        recomputed again needs no copy, because its live fields still hold the frozen state — which is why
        every reader falls back to the live fields when `frozen_at` is empty.
        """
        if self.frozen_at is not None or not self.is_frozen:
            return
        self.frozen_score = self.score
        self.frozen_cumtime = self.cumtime
        self.frozen_tiebreaker = self.tiebreaker
        self.frozen_format_data = self.format_data
        self.frozen_at = self._now
        self.save(update_fields=['frozen_score', 'frozen_cumtime', 'frozen_tiebreaker',
                                 'frozen_format_data', 'frozen_at'])
    freeze_scoreboard.alters_data = True

    def recompute_results(self):
        with transaction.atomic():
            # All team members can finish judging at once. Freeze and scoring must see one serialized state.
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            # The contest formats save the whole row: also what others change on it meanwhile, so it is read from the
            # locked row. Entering turns a registration into LIVE with a new start, and whoever organizes moves people
            # between on site and online at any time (2026-10-31).
            for field in ('frozen_at', 'frozen_score', 'frozen_cumtime', 'frozen_tiebreaker', 'frozen_format_data',
                          'score', 'cumtime', 'tiebreaker', 'format_data', 'is_disqualified', 'virtual', 'real_start',
                          'onsite'):
                setattr(self, field, getattr(locked, field))
            for name in ('start', 'end_time', 'freeze_starts_at'):
                self.__dict__.pop(name, None)
            self.freeze_scoreboard()
            self.contest.format.update_participation(self)
            if self.is_disqualified:
                self.score = DISQUALIFIED_SCORE
                self.cumtime = 0
                self.tiebreaker = 0
                self.save(update_fields=['score', 'cumtime', 'tiebreaker'])
            elif self.frozen_at is not None and self.frozen_score == DISQUALIFIED_SCORE:
                self.repair_frozen_totals()
    recompute_results.alters_data = True

    def repair_frozen_totals(self):
        """The frozen totals of a participation that was disqualified when its copy was taken and is not anymore
        (congelacion-costuras-2, 2026-10-29).

        A disqualification leaves the live totals at DISQUALIFIED_SCORE and keeps the real cells in `format_data`, and
        the copy took both as they were: the frozen scoreboard showed the rehabilitated row last, with -9999 points and
        its solved problems. The totals are added up again from the frozen cells, the way the contest format adds them
        up (the reveal does the same); for a format it does not know, the copy stays as it was.
        """
        from judge.views.contest_reveal import format_totals
        config = getattr(self.contest.format, 'config', None) or {}
        totals = format_totals(self.contest.format_name, config, self.frozen_format_data or {})
        if totals is None:
            return
        self.frozen_score, self.frozen_cumtime, self.frozen_tiebreaker = totals[0], int(totals[1]), totals[2]
        self.save(update_fields=['frozen_score', 'frozen_cumtime', 'frozen_tiebreaker'])
    repair_frozen_totals.alters_data = True

    @transaction.atomic
    def set_disqualified(self, disqualified):
        type(self).objects.select_for_update().get(pk=self.pk)
        self.is_disqualified = disqualified
        self.save(update_fields=['is_disqualified'])
        self.recompute_results()
        if self.contest.is_rated and self.contest.ratings.exists():
            self.contest.rate()
        profiles = list(self.team_roster.values_list('profile_id', flat=True)) if self.team_id else [self.user_id]
        if self.is_disqualified:
            Profile.objects.filter(current_contest=self).update(current_contest=None)
            self.contest.banned_users.add(*profiles)
        else:
            self.contest.banned_users.remove(*profiles)
    set_disqualified.alters_data = True

    @property
    def live(self):
        return self.virtual == self.LIVE

    @property
    def registered(self):
        return self.virtual == self.REGISTERED

    def clean(self):
        super().clean()
        if self.contest_id and self.user_id and self.virtual in self.OFFICIAL and not self.team_id:
            if self.contest.participation_mode == Contest.TEAM:
                raise ValidationError(_('Este concurso sólo admite participaciones oficiales por equipo.'))
            if (type(self).objects.for_profile(self.user).filter(contest_id=self.contest_id).official()
                    .exclude(pk=self.pk).exists()):
                raise ValidationError(_('Este usuario ya tiene una inscripción oficial en el concurso.'))

    def contains_profile(self, profile_id):
        if not self.team_id:
            return self.user_id == profile_id
        return any(member.profile_id == profile_id for member in self.team_roster.all())

    @property
    def display_name(self):
        return self.team_name if self.team_id else self.user.display_name

    def submissions_url(self, problem=None):
        if self.team_id:
            args = [self.contest.key, self.pk]
            if problem:
                args.append(problem.code)
            return reverse('contest_team_problem_submissions' if problem else 'contest_team_submissions', args=args)
        args = [self.contest.key, self.user.user.username]
        if problem:
            args.append(problem.code)
        return reverse('contest_user_submissions' if problem else 'contest_all_user_submissions', args=args)

    @property
    def spectate(self):
        return self.virtual == self.SPECTATE

    @cached_property
    def start(self):
        contest = self.contest
        # A registration has not begun: its clock is the contest's until it enters (2026-10-31).
        if self.registered:
            return contest.start_time
        return contest.start_time if contest.time_limit is None and (self.live or self.spectate) else self.real_start

    @cached_property
    def end_time(self):
        contest = self.contest
        if self.spectate or self.registered:
            return contest.end_time
        if self.virtual:
            if contest.time_limit:
                return self.real_start + contest.time_limit
            else:
                return self.real_start + (contest.end_time - contest.start_time)
        return contest.end_time if contest.time_limit is None else \
            min(self.real_start + contest.time_limit, contest.end_time)

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @property
    def ended(self):
        return self.end_time is not None and self.end_time < self._now

    @property
    def time_remaining(self):
        end = self.end_time
        if end is not None and end >= self._now:
            return end - self._now

    def __str__(self):
        if self.spectate:
            return _('%(user)s spectating in %(contest)s') % {'user': self.display_name, 'contest': self.contest.name}
        if self.registered:
            return _('%(user)s registrado en %(contest)s') % {'user': self.display_name, 'contest': self.contest.name}
        if self.virtual:
            return _('%(user)s in %(contest)s, v%(id)d') % {
                'user': self.display_name, 'contest': self.contest.name, 'id': self.virtual,
            }
        return _('%(user)s in %(contest)s') % {'user': self.display_name, 'contest': self.contest.name}

    class Meta:
        verbose_name = _('contest participation')
        verbose_name_plural = _('contest participations')

        unique_together = (('contest', 'user', 'virtual'), ('contest', 'team', 'virtual'))


class ContestProblem(models.Model):
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='contests', on_delete=CASCADE)
    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='contest_problems', on_delete=CASCADE)
    points = models.IntegerField(verbose_name=_('points'))
    partial = models.BooleanField(default=True, verbose_name=_('partial'))
    is_pretested = models.BooleanField(default=False, verbose_name=_('is pretested'))
    order = models.PositiveIntegerField(db_index=True, verbose_name=_('order'))
    output_prefix_override = models.IntegerField(verbose_name=_('output prefix length override'),
                                                 default=0, null=True, blank=True)
    max_submissions = models.IntegerField(verbose_name=_('max submissions'),
                                          help_text=_('Maximum number of submissions for this problem, '
                                                      'or leave blank for no limit.'),
                                          default=None, null=True, blank=True,
                                          validators=[MinValueOrNoneValidator(1, _('Why include a problem you '
                                                                                   "can't submit to?"))])
    balloon_color = models.CharField(max_length=7, verbose_name=_('balloon colour'), blank=True, default='',
                                     validators=[ContestTag.color_validator],
                                     help_text=_('As #rrggbb. Leave empty if this problem has no balloon colour.'))
    balloon_color_name = models.CharField(max_length=30, verbose_name=_('balloon colour name'), blank=True,
                                          default='', help_text=_('What the balloon staff call it, e.g. "red".'))

    @property
    def balloon_text_color(self):
        if not self.balloon_color:
            return ''
        hex_digits = self.balloon_color[1:]
        if len(hex_digits) == 3:
            hex_digits = ''.join(digit * 2 for digit in hex_digits)
        r, g, b = bytes.fromhex(hex_digits)
        return '#000' if 299 * r + 587 * g + 144 * b > 140000 else '#fff'

    class Meta:
        unique_together = ('problem', 'contest')
        verbose_name = _('contest problem')
        verbose_name_plural = _('contest problems')
        ordering = ('order',)


class ContestSubmission(models.Model):
    submission = models.OneToOneField(Submission, verbose_name=_('submission'),
                                      related_name='contest', on_delete=CASCADE)
    problem = models.ForeignKey(ContestProblem, verbose_name=_('problem'), on_delete=CASCADE,
                                related_name='submissions', related_query_name='submission')
    participation = models.ForeignKey(ContestParticipation, verbose_name=_('participation'), on_delete=CASCADE,
                                      related_name='submissions', related_query_name='submission')
    points = models.FloatField(default=0.0, verbose_name=_('points'))
    is_pretest = models.BooleanField(verbose_name=_('is pretested'),
                                     help_text=_('Whether this submission was ran only on pretests.'),
                                     default=False)

    class Meta:
        verbose_name = _('contest submission')
        verbose_name_plural = _('contest submissions')


class Rating(models.Model):
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='ratings', on_delete=CASCADE)
    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='ratings', on_delete=CASCADE)
    participation = models.OneToOneField(ContestParticipation, verbose_name=_('participation'),
                                         related_name='rating', on_delete=CASCADE)
    rank = models.IntegerField(verbose_name=_('rank'))
    rating = models.IntegerField(verbose_name=_('rating'))
    mean = models.FloatField(verbose_name=_('raw rating'))
    performance = models.FloatField(verbose_name=_('contest performance'))
    last_rated = models.DateTimeField(db_index=True, verbose_name=_('last rated'))

    class Meta:
        unique_together = ('user', 'contest')
        verbose_name = _('contest rating')
        verbose_name_plural = _('contest ratings')


class ContestMoss(models.Model):
    LANG_MAPPING = [
        ('C', MOSS_LANG_C),
        ('C++', MOSS_LANG_CC),
        ('Java', MOSS_LANG_JAVA),
        ('Python', MOSS_LANG_PYTHON),
    ]

    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='moss', on_delete=CASCADE)
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='moss', on_delete=CASCADE)
    language = models.CharField(max_length=10)
    submission_count = models.PositiveIntegerField(default=0)
    url = models.URLField(null=True, blank=True)

    class Meta:
        unique_together = ('contest', 'problem', 'language')
        verbose_name = _('contest moss result')
        verbose_name_plural = _('contest moss results')
