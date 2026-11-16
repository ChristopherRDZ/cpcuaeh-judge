from datetime import timedelta

from adminsortable2.admin import SortableAdminBase, SortableInlineAdminMixin
from django.contrib import admin, messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, TextField
from django.forms import ModelForm, ModelMultipleChoiceField, TextInput
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _, ngettext
from django.views.decorators.http import require_POST
from reversion.admin import VersionAdmin

from judge.admin.purge import PurgeMixin, contest_plan, purge_contest
from judge.admin_owner import is_server_owner
from judge.models import Class, Contest, ContestParticipation, ContestProblem, ContestSubmission, Profile, Rating, \
    Submission
from judge.models.contest import FREEZE_SCHEDULE, kept_freeze_minutes
from judge.ratings import rate_contest
from judge.utils.views import NoBatchDeleteMixin
from judge.widgets import AdminAceWidget, AdminHeavySelect2MultipleWidget, AdminHeavySelect2Widget, \
    AdminMartorWidget, AdminSelect2MultipleWidget, AdminSelect2Widget


class AdminHeavySelect2Widget(AdminHeavySelect2Widget):
    @property
    def is_hidden(self):
        return False


class DuelContestFilter(admin.SimpleListFilter):
    """Every game of the duels runs on a hidden contest of its own (judge/arena), and they flooded these lists. They
    have their own entry in the «Battles» section of the menu, which opens the list of contests with ?duelos=si;
    otherwise the lists leave them out. `field` says where the contest is in the rows listed."""
    title = _('duels')
    parameter_name = 'duelos'
    field = 'id'

    def lookups(self, request, model_admin):
        return (('si', _('Only duels')), ('todos', _('All')))

    def choices(self, changelist):
        yield {'selected': self.value() is None, 'display': _('Without duels'),
               'query_string': changelist.get_query_string(remove=[self.parameter_name])}
        for lookup, title in self.lookup_choices:
            yield {'selected': self.value() == lookup, 'display': title,
                   'query_string': changelist.get_query_string({self.parameter_name: lookup})}

    def queryset(self, request, queryset):
        if self.value() == 'todos':
            return queryset
        duels = {self.field + '__in': Contest.objects.filter(Contest.duel_contests()).values('id')}
        return queryset.filter(**duels) if self.value() == 'si' else queryset.exclude(**duels)


class DuelParticipationFilter(DuelContestFilter):
    field = 'contest_id'


class ContestTagForm(ModelForm):
    contests = ModelMultipleChoiceField(
        label=_('Included contests'),
        queryset=Contest.objects.all(),
        required=False,
        widget=AdminHeavySelect2MultipleWidget(data_view='contest_select2'))


class ContestTagAdmin(admin.ModelAdmin):
    fields = ('name', 'color', 'description', 'contests')
    list_display = ('name', 'color')
    actions_on_top = True
    actions_on_bottom = True
    form = ContestTagForm
    formfield_overrides = {
        TextField: {'widget': AdminMartorWidget},
    }

    def save_model(self, request, obj, form, change):
        super(ContestTagAdmin, self).save_model(request, obj, form, change)
        obj.contests.set(form.cleaned_data['contests'])

    def get_form(self, request, obj=None, **kwargs):
        form = super(ContestTagAdmin, self).get_form(request, obj, **kwargs)
        if obj is not None:
            form.base_fields['contests'].initial = obj.contests.all()
        return form


class ContestProblemInlineForm(ModelForm):
    class Meta:
        widgets = {
            'problem': AdminHeavySelect2Widget(data_view='problem_select2'),
            'balloon_color': TextInput(attrs={'size': 7, 'placeholder': '#e74c3c'}),
            'balloon_color_name': TextInput(attrs={'size': 10}),
        }


class ContestProblemInline(SortableInlineAdminMixin, admin.TabularInline):
    model = ContestProblem
    verbose_name = _('Problem')
    verbose_name_plural = _('Problems')
    fields = ('problem', 'points', 'partial', 'is_pretested', 'max_submissions', 'output_prefix_override',
              'balloon_color', 'balloon_color_name', 'order', 'rejudge_column')
    readonly_fields = ('rejudge_column',)
    form = ContestProblemInlineForm
    # Fuera del candado de borrado (judge/admin_owner.py): quien puede cambiar los problemas
    # del concurso también puede quitar uno, que no es más grave que cambiarlo por otro. Con
    # él se van sus envíos dentro del concurso (los envíos siguen en el historial de cada
    # quien), sus aclaraciones y sus globos.
    owner_lock_exempt = True

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    @admin.display(description='')
    def rejudge_column(self, obj):
        if obj.id is None:
            return ''
        return format_html('<a class="button rejudge-link action-link" href="{0}">{1}</a>',
                           reverse('admin:judge_contest_rejudge', args=(obj.contest.id, obj.id)), _('Rejudge'))


class ContestForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ContestForm, self).__init__(*args, **kwargs)
        if 'scoreboard_revealed' in self.fields:
            # The form carries what the box said when it was opened (round 3 of the review): one opened while the
            # scoreboard was revealed, and saved after it was frozen again, revealed it again.
            self.fields['scoreboard_revealed'].show_hidden_initial = True
        if 'private_teams' in self.fields:
            field = self.fields['private_teams']
            visible = Q(is_active=True)
            if self.instance.pk:
                visible |= Q(private_contests=self.instance)
            field.queryset = field.queryset.filter(visible).select_related('owner__user').distinct()
            field.label_from_instance = lambda team: '%s · %s (#%s)' % (team.name, team.owner.username, team.pk)
        if not self.instance.pk and 'participation_mode' in self.fields:
            # Existing contests retain individual mode; new ones must choose explicitly.
            self.fields['participation_mode'].initial = ''
            self.initial['participation_mode'] = ''
            self.fields['participation_mode'].choices = [('', _('Selecciona una modalidad'))] + list(
                self.fields['participation_mode'].choices)
        if not self.instance.pk and 'attendance_mode' in self.fields:
            # The same for where it is competed (2026-10-31): existing contests stay online, new ones say it.
            self.fields['attendance_mode'].initial = ''
            self.initial['attendance_mode'] = ''
            self.fields['attendance_mode'].choices = [('', _('Selecciona dónde se compite'))] + list(
                self.fields['attendance_mode'].choices)
        if 'rate_exclude' in self.fields:
            if self.instance and self.instance.id:
                self.fields['rate_exclude'].queryset = \
                    Profile.objects.filter(contest_history__contest=self.instance,
                                           contest_history__team__isnull=True).distinct()
            else:
                self.fields['rate_exclude'].queryset = Profile.objects.none()
        self.fields['banned_users'].widget.can_add_related = False
        self.fields['view_contest_scoreboard'].widget.can_add_related = False
        self.fields['balloon_staff'].widget.can_add_related = False

    def revealed_when_opened(self):
        """What the box of the revealed scoreboard said when this form was opened (its hidden initial value), or None
        if the form did not send it."""
        return {'True': True, 'False': False}.get(self.data.get(self['scoreboard_revealed'].html_initial_name))

    def clean(self):
        cleaned_data = super().clean()
        self.freeze_adjusted = None
        freeze = cleaned_data.get('freeze_minutes') if 'freeze_minutes' in self.fields else self.instance.freeze_minutes
        # A form opened before the reveal (or before the ceremony published the scoreboard) and saved afterwards for
        # any other change sends the scoreboard as not revealed: it froze the contest again without a word, and took
        # the ratings of a rated one away (rounds 1 and 2 of the review, 2026-10-29; a contest of teams is never
        # rated). A revealed contest is only frozen again with the action of the list, on purpose.
        if self.instance.pk and 'scoreboard_revealed' in self.fields and self.instance.scoreboard_revealed and \
                not cleaned_data.get('scoreboard_revealed') and freeze:
            self.add_error('scoreboard_revealed',
                           _('The scoreboard of this contest is already revealed: to freeze it again, use '
                             '«%(action)s» in the list of contests (a rated contest also loses its ratings until it '
                             'is revealed and rated again). If you did not mean to freeze it, open this page again: '
                             'the scoreboard was revealed after this form was opened.')
                           % {'action': _('Freeze scoreboards again')})
        # And the other way round (round 3 of the review): revealed by mistake during the contest and frozen again with
        # the action, a form opened in between still had the box ticked; saved for another change, it revealed what
        # was sent since and lifted the freeze for the rest of the contest. Only when the box is as it was when the
        # form was opened: ticking it on purpose still reveals the scoreboard. The saved freeze counts too (round 4):
        # with the minutes emptied, or a form opened before the contest had a freeze, the stale box still revealed it.
        elif self.instance.pk and 'scoreboard_revealed' in self.fields and not self.instance.scoreboard_revealed and \
                cleaned_data.get('scoreboard_revealed') and (freeze or self.instance.freeze_minutes) and \
                self.revealed_when_opened() is True:
            self.add_error('scoreboard_revealed',
                           _('The scoreboard of this contest was frozen again after this form was opened: open this '
                             'page again to save it without revealing the scoreboard, or reveal it with «%(action)s» '
                             'in the list of contests.') % {'action': _('Reveal frozen scoreboards')})
        if self.instance.pk and not self.errors:
            # A freeze that already started keeps its cutoff until it is revealed (congelacion-revelador-2). Here the
            # instance still holds what is saved; a field the user cannot change keeps that value.
            stored = {field: getattr(self.instance, field) for field in FREEZE_SCHEDULE}
            new = {field: cleaned_data.get(field) if field in self.fields else stored[field]
                   for field in FREEZE_SCHEDULE}
            now = timezone.now()
            try:
                minutes = kept_freeze_minutes(stored, new, now, live=self.instance.takes_part_live)
            except ValidationError as error:
                self.add_error(None, error)
                return cleaned_data
            # Contest.save checks again with this same moment, so a cutoff reached in between cannot refuse it.
            self.instance._freeze_checked_at = now
            if minutes != new['freeze_minutes']:
                self.freeze_adjusted = (new['freeze_minutes'], minutes)
                cleaned_data['freeze_minutes'] = minutes
        freeze = cleaned_data.get('freeze_minutes')
        if freeze:
            time_limit = cleaned_data.get('time_limit')
            start_time, end_time = cleaned_data.get('start_time'), cleaned_data.get('end_time')
            window = end_time - start_time if start_time and end_time else None
            # The cutoff comes from the shorter of the two (Contest.submission_freeze_cutoff): a time limit longer
            # than the window let a freeze through that began before the contest did (congelacion-revelador-v1).
            if time_limit and (window is None or time_limit < window):
                window = time_limit
            if window is not None and timedelta(minutes=freeze) >= window:
                self.add_error('freeze_minutes',
                               _('The freeze has to be shorter than the contest, or the scoreboard would be '
                                 'frozen from the first minute.'))
        return cleaned_data

    class Meta:
        widgets = {
            'authors': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'curators': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'testers': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'spectators': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'private_contestants': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'private_teams': AdminSelect2MultipleWidget,
            'organizations': AdminHeavySelect2MultipleWidget(data_view='organization_select2'),
            'classes': AdminHeavySelect2MultipleWidget(data_view='class_select2'),
            'join_organizations': AdminHeavySelect2MultipleWidget(data_view='organization_select2'),
            'tags': AdminSelect2MultipleWidget,
            'banned_users': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'view_contest_scoreboard': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'view_contest_submissions': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'balloon_staff': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'description': AdminMartorWidget(attrs={'data-markdownfy-url': reverse_lazy('contest_preview')}),
        }


class ContestAdmin(PurgeMixin, NoBatchDeleteMixin, SortableAdminBase, VersionAdmin):
    fieldsets = (
        (None, {'fields': ('key', 'name', 'authors', 'curators', 'testers', 'tester_see_submissions',
                           'tester_see_scoreboard', 'spectators')}),
        (_('Settings'), {'fields': ('is_visible', 'use_clarifications', 'hide_problem_tags', 'hide_problem_authors',
                                    'show_short_display', 'run_pretests_only', 'locked_after', 'scoreboard_visibility',
                                    'freeze_minutes', 'scoreboard_revealed', 'points_precision', 'autocomplete',
                                    'public_scoreboard')}),
        (_('Participación'), {'fields': ('participation_mode', 'team_min_size', 'team_max_size', 'attendance_mode')}),
        (_('Scheduling'), {'fields': ('start_time', 'end_time', 'time_limit')}),
        (_('Details'), {'fields': ('description', 'og_image', 'logo_override_image', 'tags', 'summary')}),
        (_('Format'), {'fields': ('format_name', 'format_config', 'problem_label_script')}),
        (_('Rating'), {'fields': ('is_rated', 'rating_modality', 'rate_all', 'rating_floor', 'rating_ceiling',
                                  'rate_exclude')}),
        (_('Access'), {'fields': ('access_code', 'private_contestants', 'private_teams', 'organizations', 'classes',
                                  'join_organizations', 'view_contest_scoreboard', 'view_contest_submissions')}),
        (_('Justice'), {'fields': ('banned_users',)}),
        (_('Balloons'), {'fields': ('balloon_staff',)}),
    )
    list_display = ('key', 'name', 'is_visible', 'is_rated', 'locked_after', 'start_time', 'end_time', 'time_limit',
                    'user_count', 'reveal_link')
    search_fields = ('key', 'name')
    # The three choices in sight (the owner, 2026-11-04: «que sí sea notorio que existen las otras opciones»).
    radio_fields = {'rating_modality': admin.VERTICAL}
    list_filter = (DuelContestFilter,)
    inlines = [ContestProblemInline]
    actions_on_top = True
    actions_on_bottom = True
    form = ContestForm
    change_list_template = 'admin/judge/contest/change_list.html'
    filter_horizontal = ['rate_exclude']
    date_hierarchy = 'start_time'
    purge_action_name = 'purge_contests'
    purge_title = _('Eliminar concursos definitivamente')
    purge_warning = _('Se borra el concurso con todo lo que cuelga de él, incluidas las aclaraciones de equipos '
                      'que hoy impiden borrarlo. No se puede deshacer y no queda copia. Los envíos en sí se '
                      'conservan en el historial personal de cada quien; lo que desaparece es su vínculo con el '
                      'concurso.')

    def get_actions(self, request):
        actions = super(ContestAdmin, self).get_actions(request)

        if request.user.has_perm('judge.change_contest_visibility') or \
                request.user.has_perm('judge.create_private_contest'):
            for action in ('make_visible', 'make_hidden'):
                actions[action] = self.get_action(action)

        if request.user.has_perm('judge.lock_contest'):
            for action in ('set_locked', 'set_unlocked'):
                actions[action] = self.get_action(action)

        # Revealing is not a separate power: the changelist already only lists the contests this user may edit.
        for action in ('reveal_scoreboard', 'hide_scoreboard'):
            actions[action] = self.get_action(action)

        # NoBatchDeleteMixin retira `delete_selected`, y de todos modos el borrado
        # normal se atasca en la aclaración de un equipo. Éste es el camino que sí
        # desmonta las dependencias, y sólo lo ve la cuenta dueña del servidor.
        if is_server_owner(request):
            actions['purge_contests'] = self.get_action('purge_contests')

        return actions

    def purge_plan(self, obj):
        return contest_plan(obj)

    def purge_object(self, obj):
        purge_contest(obj)

    @admin.action(description=_('Eliminar definitivamente, con todo lo que cuelga del concurso'))
    def purge_contests(self, request, queryset):
        return self.run_purge(request, queryset)

    def _set_revealed(self, queryset, revealed):
        """Reveal or freeze again, and recompute in the same transaction what is stored and depends on it: the AC
        rate of the problems and the points of whoever took part, and the ratings of a contest frozen again
        (Contest.refresh_after_freeze_change). If that fails, nothing changes."""
        with transaction.atomic():
            # Materialised: the changelist queryset can be distinct.
            ids = list(queryset.filter(freeze_minutes__isnull=False, scoreboard_revealed=not revealed)
                       .values_list('id', flat=True))
            count = Contest.objects.filter(id__in=ids, scoreboard_revealed=not revealed) \
                .update(scoreboard_revealed=revealed)
            for contest in Contest.objects.filter(id__in=ids):
                contest.refresh_after_freeze_change()
            transaction.on_commit(lambda: cache.delete(Contest.FROZEN_WINDOWS_CACHE_KEY))
        cache.delete(Contest.FROZEN_WINDOWS_CACHE_KEY)
        return count

    @admin.display(description=_('Reveal frozen scoreboards'))
    def reveal_scoreboard(self, request, queryset):
        count = self._set_revealed(queryset, True)
        self.message_user(request, ngettext('%d scoreboard revealed.',
                                            '%d scoreboards revealed.', count) % count)

    @admin.display(description=_('Freeze scoreboards again'))
    def hide_scoreboard(self, request, queryset):
        count = self._set_revealed(queryset, False)
        self.message_user(request, ngettext('%d scoreboard frozen again.',
                                            '%d scoreboards frozen again.', count) % count)

    @admin.display(description=_('Reveal'))
    def reveal_link(self, obj):
        # Only where there is something to reveal. The change form links every contest, and the page itself
        # explains what is missing.
        if obj.freeze_minutes is None:
            return ''
        return format_html('<a href="{0}" target="_blank" rel="noopener"><i class="fa fa-trophy"></i> {1}</a>',
                           reverse('contest_reveal', args=[obj.key]), _('Reveal scoreboard'))

    def get_queryset(self, request):
        queryset = Contest.objects.all()
        if request.user.has_perm('judge.edit_all_contest'):
            return queryset
        else:
            return queryset.filter(Q(authors=request.profile) | Q(curators=request.profile)).distinct()

    def get_readonly_fields(self, request, obj=None):
        readonly = []
        if not request.user.has_perm('judge.contest_rating'):
            readonly += ['is_rated', 'rating_modality', 'rate_all', 'rate_exclude']
        if not request.user.has_perm('judge.lock_contest'):
            readonly += ['locked_after']
        if not request.user.has_perm('judge.contest_access_code'):
            readonly += ['access_code']
        if not request.user.has_perm('judge.create_private_contest'):
            readonly += ['private_contestants', 'private_teams', 'organizations']
            if not request.user.has_perm('judge.change_contest_visibility'):
                readonly += ['is_visible']
        if not request.user.has_perm('judge.contest_problem_label'):
            readonly += ['problem_label_script']
        return readonly

    def save_model(self, request, obj, form, change):
        # `private_contestants` and `organizations` will not appear in `cleaned_data` if user cannot edit it
        if form.changed_data:
            if 'private_contestants' in form.changed_data or 'private_teams' in form.changed_data:
                obj.is_private = bool(form.cleaned_data.get('private_contestants') or form.cleaned_data.get('private_teams'))
            if 'organizations' in form.changed_data or 'classes' in form.changed_data:
                obj.is_organization_private = bool(form.cleaned_data['organizations'] or form.cleaned_data['classes'])
            if 'join_organizations' in form.cleaned_data:
                obj.limit_join_organizations = bool(form.cleaned_data['join_organizations'])

        # `is_visible` will not appear in `cleaned_data` if user cannot edit it
        if form.cleaned_data.get('is_visible') and not request.user.has_perm('judge.change_contest_visibility'):
            if not obj.is_private and not obj.is_organization_private:
                raise PermissionDenied
            if not request.user.has_perm('judge.create_private_contest'):
                raise PermissionDenied

        stored = Contest.objects.filter(pk=obj.pk).first() if obj.pk else None
        hid = stored is not None and stored.hides_now
        # Who organizes or tests it, before the form saves its lists (save_related): changing them after the ratings
        # leaves the ratings with another field of contestants. On the form, which is of this request only.
        form.staff_before = self._staff(stored) if stored is not None else None
        super().save_model(request, obj, form, change)
        adjusted = getattr(form, 'freeze_adjusted', None)
        if adjusted:
            self.message_user(request, _('The freeze had already started, so it now lasts %(new)d minutes instead '
                                         'of %(old)d: the scoreboard stays frozen from the same moment.')
                              % {'new': adjusted[1], 'old': adjusted[0]}, messages.WARNING)
        # Revealing, freezing again, or a freeze configured or removed when it is already due: what is stored from
        # the submissions and depends on the freeze is recomputed (Contest.refresh_after_freeze_change). Inside the
        # transaction of the form, like the save itself.
        if stored is not None and Contest.objects.get(pk=obj.pk).hides_now != hid:
            obj.refresh_after_freeze_change()
        # We need this flag because `save_related` deals with the inlines, but does not know if we have already rescored
        self._rescored = False
        if form.changed_data and any(f in form.changed_data for f in ('format_config', 'format_name')):
            self._rescore(obj.key)
            self._rescored = True

        if form.changed_data and 'locked_after' in form.changed_data:
            self.set_locked_after(obj, form.cleaned_data['locked_after'])

        # The list of frozen contests is cached for half a minute; dropping it here makes a reveal immediate. Again
        # once the transaction commits: a page read in between would cache the list from before.
        if form.changed_data and any(f in form.changed_data
                                     for f in ('freeze_minutes', 'scoreboard_revealed', 'start_time', 'end_time',
                                               'time_limit')):
            cache.delete(Contest.FROZEN_WINDOWS_CACHE_KEY)
            transaction.on_commit(lambda: cache.delete(Contest.FROZEN_WINDOWS_CACHE_KEY))

    @staticmethod
    def _staff(contest):
        return {role: set(getattr(contest, role).values_list('id', flat=True))
                for role in ('authors', 'curators', 'testers')}

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # The contest exists and the entire form is valid before changing participation.
        banned_users = form.cleaned_data.get('banned_users')
        if banned_users is not None:
            banned_users.filter(current_contest__contest_id=form.instance.pk).update(current_contest=None)
        # Only rescored if we did not already do so in `save_model`
        if not self._rescored and any(formset.has_changed() for formset in formsets):
            self._rescore(form.cleaned_data['key'])
        # Organizers and testers are left out of the scoreboard and of the rating: the ratings of a contest rated
        # before the change still count them, or leave them out (round 1 of the review, 2026-10-29).
        before = getattr(form, 'staff_before', None)
        if before is not None and before != self._staff(form.instance) and \
                Rating.objects.filter(contest=form.instance).exists():
            self.message_user(request, _('The authors, curators or testers of this contest changed after it was '
                                         'rated: rate it again so that its ratings match its scoreboard.'),
                              messages.WARNING)
        # The same when who of it gets a rating changes (2026-11-04).
        if change and 'rating_modality' in form.changed_data and \
                Rating.objects.filter(contest=form.instance).exists():
            self.message_user(request, _('Cambió quién cuenta para el rating de este concurso, que ya tenía rating: '
                                         'vuelve a calificarlo.'), messages.WARNING)

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm('judge.edit_own_contest'):
            return False
        if obj is None:
            return True
        return obj.is_editable_by(request.user)

    def _rescore(self, contest_key):
        from judge.tasks import rescore_contest
        transaction.on_commit(rescore_contest.s(contest_key).delay)

    @admin.display(description=_('Mark contests as visible'))
    def make_visible(self, request, queryset):
        if not request.user.has_perm('judge.change_contest_visibility'):
            queryset = queryset.filter(Q(is_private=True) | Q(is_organization_private=True))
        count = queryset.update(is_visible=True)
        self.message_user(request, ngettext('%d contest successfully marked as visible.',
                                            '%d contests successfully marked as visible.',
                                            count) % count)

    @admin.display(description=_('Mark contests as hidden'))
    def make_hidden(self, request, queryset):
        if not request.user.has_perm('judge.change_contest_visibility'):
            queryset = queryset.filter(Q(is_private=True) | Q(is_organization_private=True))
        count = queryset.update(is_visible=False)
        self.message_user(request, ngettext('%d contest successfully marked as hidden.',
                                            '%d contests successfully marked as hidden.',
                                            count) % count)

    @admin.display(description=_('Lock contest submissions'))
    def set_locked(self, request, queryset):
        for row in queryset:
            self.set_locked_after(row, timezone.now())
        count = queryset.count()
        self.message_user(request, ngettext('%d contest successfully locked.',
                                            '%d contests successfully locked.',
                                            count) % count)

    @admin.display(description=_('Unlock contest submissions'))
    def set_unlocked(self, request, queryset):
        for row in queryset:
            self.set_locked_after(row, None)
        count = queryset.count()
        self.message_user(request, ngettext('%d contest successfully unlocked.',
                                            '%d contests successfully unlocked.',
                                            count) % count)

    def set_locked_after(self, contest, locked_after):
        with transaction.atomic():
            contest.locked_after = locked_after
            contest.save()
            Submission.objects.filter(contest_object=contest,
                                      contest__participation__virtual=0).update(locked_after=locked_after)

    def get_urls(self):
        return [
            path('rate/all/', self.rate_all_view, name='judge_contest_rate_all'),
            path('<int:id>/rate/', self.rate_view, name='judge_contest_rate'),
            path('<int:contest_id>/judge/<int:problem_id>/', self.rejudge_view, name='judge_contest_rejudge'),
        ] + super(ContestAdmin, self).get_urls()

    @method_decorator(require_POST)
    def rejudge_view(self, request, contest_id, problem_id):
        contest = get_object_or_404(Contest, id=contest_id)
        if not self.has_change_permission(request, contest):
            raise PermissionDenied()
        # The problem has to be one of this contest: the check above is only about the contest in the URL.
        problem = get_object_or_404(ContestProblem, id=problem_id, contest=contest)
        queryset = ContestSubmission.objects.filter(problem=problem).select_related('submission')
        for model in queryset:
            model.submission.judge(rejudge=True, rejudge_user=request.user)

        self.message_user(request, ngettext('%d submission was successfully scheduled for rejudging.',
                                            '%d submissions were successfully scheduled for rejudging.',
                                            len(queryset)) % len(queryset))
        return HttpResponseRedirect(reverse('admin:judge_contest_change', args=(contest_id,)))

    @method_decorator(require_POST)
    def rate_all_view(self, request):
        # It rates every contest, so it also takes being able to edit every contest.
        if not request.user.has_perm('judge.contest_rating') or not request.user.has_perm('judge.edit_all_contest'):
            raise PermissionDenied()
        skipped = []
        with transaction.atomic():
            # Not TRUNCATE: MariaDB commits it on the spot, and a failure halfway left the table empty.
            Rating.objects.all().delete()
            Profile.objects.update(rating=None)
            for contest in Contest.objects.filter(is_rated=True, end_time__lte=timezone.now()).order_by('end_time'):
                if not rate_contest(contest):
                    skipped.append(contest.name)
        if skipped:
            # rate_contest passes over a frozen scoreboard (congelacion-revelador-1).
            self.message_user(request, _('Not rated, because their scoreboard is still frozen: %(contests)s. Reveal '
                                         'it, then rate each one from its page.') % {'contests': ', '.join(skipped)},
                              messages.WARNING)
        return HttpResponseRedirect(reverse('admin:judge_contest_changelist'))

    @method_decorator(require_POST)
    def rate_view(self, request, id):
        if not request.user.has_perm('judge.contest_rating'):
            raise PermissionDenied()
        contest = get_object_or_404(Contest, id=id)
        if not self.has_change_permission(request, contest):
            raise PermissionDenied()
        if not contest.is_rated or not contest.ended:
            raise Http404()
        if contest.freeze_active:
            # The ratings would give away the real standings before the reveal (congelacion-revelador-1).
            self.message_user(request, _('Reveal the scoreboard before rating: while it is frozen, the ratings would '
                                         'show the real standings.'), messages.ERROR)
            return HttpResponseRedirect(reverse('admin:judge_contest_change', args=(contest.id,)))
        with transaction.atomic():
            contest.rate()
        return HttpResponseRedirect(request.headers.get('referer', reverse('admin:judge_contest_changelist')))

    def get_form(self, request, obj=None, **kwargs):
        form = super(ContestAdmin, self).get_form(request, obj, **kwargs)
        if 'problem_label_script' in form.base_fields:
            # form.base_fields['problem_label_script'] does not exist when the user has only view permission
            # on the model.
            form.base_fields['problem_label_script'].widget = AdminAceWidget(
                mode='lua', theme=request.profile.resolved_ace_theme,
            )

        perms = ('edit_own_contest', 'edit_all_contest')
        form.base_fields['curators'].queryset = Profile.objects.filter(
            Q(user__is_superuser=True) |
            Q(user__groups__permissions__codename__in=perms) |
            Q(user__user_permissions__codename__in=perms),
        ).distinct()
        form.base_fields['classes'].queryset = Class.get_visible_classes(request.user)
        return form


class ContestParticipationForm(ModelForm):
    class Meta:
        widgets = {
            'contest': AdminSelect2Widget(),
            'user': AdminHeavySelect2Widget(data_view='profile_select2'),
        }


class ContestParticipationAdmin(admin.ModelAdmin):
    fields = ('contest', 'user', 'team', 'team_name', 'real_start', 'virtual', 'is_disqualified', 'onsite')
    readonly_fields = ('team', 'team_name')

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (('contest', 'user', 'real_start', 'virtual') if obj and obj.team_id else ())

    def has_delete_permission(self, request, obj=None):
        # La participación de un equipo es historial y no se borra suelta. La cuenta
        # dueña del servidor sí puede, porque es la que responde por esa decisión.
        if obj and obj.team_id and not is_server_owner(request):
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        if queryset.filter(team__isnull=False).exists() and not is_server_owner(request):
            raise PermissionDenied(_('Las participaciones de equipos se conservan como historial.'))
        super().delete_queryset(request, queryset)

    list_display = ('contest', 'username', 'show_virtual', 'onsite', 'real_start', 'score', 'cumtime', 'tiebreaker')
    actions = ['recalculate_results']
    actions_on_bottom = actions_on_top = True
    search_fields = ('contest__key', 'contest__name', 'user__user__username', 'team_name')
    list_filter = (DuelParticipationFilter,)
    form = ContestParticipationForm
    date_hierarchy = 'real_start'

    def get_queryset(self, request):
        # The user column is the display name: a team's name, or the profile's override of the username.
        return super(ContestParticipationAdmin, self).get_queryset(request).only(
            'contest__name', 'contest__format_name', 'contest__format_config',
            'user__user__username', 'user__username_display_override', 'team', 'team_name',
            'real_start', 'score', 'cumtime', 'tiebreaker', 'virtual', 'onsite',
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if form.changed_data and 'is_disqualified' in form.changed_data:
            obj.set_disqualified(obj.is_disqualified)

    @admin.display(description=_('Recalculate results'))
    def recalculate_results(self, request, queryset):
        count = 0
        for participation in queryset:
            participation.recompute_results()
            count += 1
        self.message_user(request, ngettext('%d participation recalculated.',
                                            '%d participations recalculated.',
                                            count) % count)

    @admin.display(description=_('username'), ordering='user__user__username')
    def username(self, obj):
        return obj.display_name

    @admin.display(description=_('virtual'), ordering='virtual')
    def show_virtual(self, obj):
        # A registration that waits for the start (2026-10-31), not a virtual participation.
        if obj.virtual == ContestParticipation.REGISTERED:
            return _('Registrado')
        return obj.virtual or '-'
