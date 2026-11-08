"""Emails to users: write one in the admin, see who will receive it, try it, ask for its approval and send it through
Resend.

Who does what (2026-11-05; until then whoever had the section also sent):

* `judge.send_mass_email`: writes own emails, tries them and asks for their approval. With it, only the emails one
  wrote are listed, as with problems and contests.
* `judge.edit_all_mass_email`: the same with the drafts of everybody (works by itself too).
* `judge.approve_mass_email`: sees every email; changes, sends or sends back with a note the ones waiting for
  approval, and sends one's own without asking anybody. Keeps the list of senders and the social networks.

Superusers have the three. An email waiting for approval is changed only by whoever approves (its author can take
it back, and it is a draft again); what was changed, and by whom, stays in its history. Once an email is sent it
cannot be edited; "Duplicate" makes a new draft from it, written by whoever duplicates it. See judge/mass_email.py
for who receives what.
"""
import json
import uuid
from datetime import datetime, timedelta

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _, ngettext
from django.views.decorators.http import require_POST

from judge.mass_email import FILTER_FIELDS, MANY_FIELDS, MAX_EXTERNAL, NAME_PLACEHOLDERS, SENDERS_KEY, SOCIAL_KEY, \
    SOCIAL_NETWORKS, check_url, default_senders, describe, effective_audience, external_addresses, filters_from_form, \
    filters_of, fingerprint, has_filters, parse_addresses, parse_senders, preview_fragment, read_config, \
    recipient_rows, render_email, reply_to, review_changed, sender, sender_domain, senders, write_config
from judge.models import Contest, MassEmail, MassEmailRecipient, MiscConfig
from judge.utils.celery import task_status_url_by_id
from judge.utils.resend import ResendClient, ResendError, is_configured
from judge.widgets import AdminHeavySelect2MultipleWidget, AdminMartorWidget, AdminSelect2MultipleWidget

PERMISSION = 'judge.send_mass_email'
PERMISSION_ALL = 'judge.edit_all_mass_email'
PERMISSION_APPROVE = 'judge.approve_mass_email'
# The page of the email itself, and the frames of the preview, run no script and load nothing but images.
EMAIL_CSP = "default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'"
SCHEDULE_MIN = timedelta(minutes=5)
SCHEDULE_MAX = timedelta(days=30)
# Handing an email to Resend takes a few minutes at most. Longer than this, the task died on the way (Celery was
# restarted, the machine went down) and retrying is safe: it continues where it stopped.
STUCK_AFTER = timedelta(minutes=15)
# Delivery figures are counted page by page in Resend; above this they are left to its dashboard.
STATS_LIMIT = 3000
STATS_KINDS = ('delivered', 'bounced', 'complained', 'unsubscribed', 'suppressed')
NOTE_LIMIT = 2000


def stuck():
    return Q(status=MassEmail.SENDING, updated__lt=timezone.now() - STUCK_AFTER)


def recipient_widgets():
    return {
        'contests': AdminHeavySelect2MultipleWidget(data_view='contest_select2'),
        'organizations': AdminHeavySelect2MultipleWidget(data_view='organization_select2'),
        'groups': AdminSelect2MultipleWidget,
        'include_users': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
        'exclude_users': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
        # A plain box without JavaScript; with it, change_form.html turns what is pasted into chips.
        'external_emails': forms.Textarea(attrs={'rows': 4, 'spellcheck': 'false', 'autocomplete': 'off'}),
    }


class RecipientForm(forms.ModelForm):
    """Only the filters: validates them for the live count and the list, without needing the rest."""
    # Not required: a form without it (a tab opened before 2026-10-23) keeps the old meaning, effective_audience.
    audience = forms.ChoiceField(label=_('Who receives it'), choices=MassEmail.AUDIENCES, initial=MassEmail.ALL,
                                 widget=forms.RadioSelect, required=False)

    class Meta:
        model = MassEmail
        fields = FILTER_FIELDS

    def __init__(self, *args, request=None, strict=True, **kwargs):
        # Not strict for the live count and the list: a badly written address there is only left out, while it is
        # still being typed. Saving refuses it.
        self.strict = strict
        self.request = request
        super().__init__(*args, **kwargs)
        if request is not None and 'contests' in self.fields:
            # Only the contests this person can see, the same ones the box offers (correos-masivos-5, 2026-10-28):
            # the count and the list took any id, and gave away the names and the participants of hidden contests.
            # The contests a saved draft already has stay, so whoever edits it does not lose them.
            allowed = Q(pk__in=Contest.get_visible_contests(request.user).values('pk'))
            if self.instance.pk:
                allowed |= Q(pk__in=self.instance.contests.values('pk'))
            self.fields['contests'].queryset = Contest.objects.filter(allowed)
        if 'external_emails' in self.fields:
            self.fields['external_emails'].help_text = _('Pega los correos de un formulario. Si alguno ya está en la '
                                                         'lista, le llega una sola vez.')

    def clean_external_emails(self):
        good, bad = parse_addresses(self.cleaned_data.get('external_emails'))
        if bad and self.strict:
            raise forms.ValidationError(gettext('Corrige o quita estos correos: %(emails)s') %
                                        {'emails': ', '.join(bad[:10]) + (', ...' if len(bad) > 10 else '')})
        if len(good) > MAX_EXTERNAL:
            raise forms.ValidationError(gettext('Caben hasta %(count)d correos pegados.') % {'count': MAX_EXTERNAL})
        return '\n'.join(good) or None

    def clean(self):
        cleaned = super().clean()
        after, before = cleaned.get('joined_after'), cleaned.get('joined_before')
        if after and before and after > before:
            raise forms.ValidationError(gettext('The sign-up dates are reversed: the first one is after the second.'))
        low, high = cleaned.get('min_rating'), cleaned.get('max_rating')
        if low is not None and high is not None and low > high:
            raise forms.ValidationError(gettext('The ratings are reversed: the minimum is above the maximum.'))
        audience = cleaned.get('audience')
        if audience == MassEmail.USERS and not cleaned.get('include_users') and not cleaned.get('external_emails') \
                and 'external_emails' not in self.errors:
            raise forms.ValidationError(gettext('Choose the people who will receive it.'))
        if audience == MassEmail.FILTERS and not has_filters(filters_from_form(cleaned)):
            raise forms.ValidationError(gettext('Choose at least one filter, or send it to all accounts with the first '
                                                'choice.'))
        return cleaned


class MassEmailForm(RecipientForm):
    # Not required: a form without it (a tab opened before 2026-11-05) goes from the first sender of the list.
    from_email = forms.ChoiceField(label=_('Remitente'), required=False)

    class Meta:
        model = MassEmail
        fields = ('subject', 'preheader', 'heading', 'from_email', 'language', 'content') + FILTER_FIELDS
        widgets = dict(recipient_widgets(), content=AdminMartorWidget(
            attrs={'data-markdownfy-url': reverse_lazy('admin:judge_massemail_fragment')}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Choosing contests, organizations or groups here must not offer to create one. The admin wraps the widgets
        # after formfield_for_manytomany, so it is switched off here, as in BlogPostForm.
        for name in MANY_FIELDS:
            if name in self.fields and hasattr(self.fields[name].widget, 'can_add_related'):
                self.fields[name].widget.can_add_related = False
        # A new email starts as «all»; a draft saved before `audience` existed shows the choice that means what it
        # always meant.
        if not self.instance.audience:
            self.initial['audience'] = (effective_audience(filters_of(self.instance)) if self.instance.pk
                                        else MassEmail.ALL)
        if 'from_email' in self.fields:
            available = senders()
            field = self.fields['from_email']
            field.choices = [(item, item) for item in available]
            # The one the email has, as the list writes it now; a sender taken off the list has to be chosen again
            # (round 1 of the review): an empty first choice, refused, instead of the browser picking the first one.
            self.initial['from_email'] = sender(self.instance.from_email) if self.instance.pk else available[0]
            if self.instance.pk and self.instance.from_email and self.initial['from_email'] is None:
                field.choices = [('', gettext('Elige un remitente de la lista.'))] + field.choices
                field.required = True
            if reply_to():
                field.help_text = gettext('Las respuestas llegan a %(email)s.') % {'email': reply_to()}

    def clean(self):
        cleaned = super().clean()
        # When the page was opened (round 2 of the review): somebody else saved, took it back, sent it back or sent
        # it since, and saving would undo it. Shown again, the page carries the new moment: saving again is meant.
        seen = self.request.POST.get('mass_email_seen') if self.request is not None else None
        if seen and self.instance.pk:
            now = MassEmail.objects.filter(pk=self.instance.pk).values_list('updated', flat=True).first()
            if now is not None and now.isoformat() != seen:
                raise forms.ValidationError(gettext('Alguien más lo cambió mientras lo editabas. Ábrelo de nuevo para '
                                                    'ver lo suyo; si guardas otra vez, lo tuyo lo reemplaza.'))
        return cleaned

    def clean_from_email(self):
        chosen = sender(self.cleaned_data['from_email'])
        if chosen is None:
            raise forms.ValidationError(gettext('Elige un remitente de la lista.'))
        return chosen

    def clean_subject(self):
        subject = self.cleaned_data['subject']
        if any(placeholder in subject for placeholder in NAME_PLACEHOLDERS):
            raise forms.ValidationError(gettext('The name of each person can only go in the content, not in the '
                                                'subject.'))
        return subject

    def clean_preheader(self):
        # Nor in the preview text (correos-masivos-4, 2026-10-28): Resend got it with the bare placeholder, while the
        # test showed the name.
        preheader = self.cleaned_data['preheader']
        if any(placeholder in preheader for placeholder in NAME_PLACEHOLDERS):
            raise forms.ValidationError(gettext('The name of each person can only go in the content, not in the '
                                                'preview text.'))
        return preheader


class SettingsForm(forms.Form):
    """The page «Remitentes y redes»: what the email form offers as senders, and the networks at the foot."""
    senders = forms.CharField(label=_('Remitentes'), widget=forms.Textarea(attrs={'rows': 6, 'spellcheck': 'false'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['senders'].help_text = gettext('Uno por línea, como los de arriba, todos de %(domain)s. El primero '
                                                   'es el que se elige si no se cambia.') % {'domain': sender_domain()}
        for key, label in SOCIAL_NETWORKS:
            self.fields['social_' + key] = forms.URLField(label=label, required=False, assume_scheme='https',
                                                          validators=[check_url], max_length=500,
                                                          widget=forms.URLInput(attrs={'placeholder': 'https://'}))

    def clean_senders(self):
        good, bad = parse_senders(self.cleaned_data['senders'])
        if bad:
            raise forms.ValidationError(gettext('Estas líneas no son un correo de %(domain)s: %(lines)s') %
                                        {'domain': sender_domain(), 'lines': '; '.join(bad[:5])})
        if not good:
            raise forms.ValidationError(gettext('Deja al menos un remitente.'))
        return good


class MassEmailAdmin(admin.ModelAdmin):
    form = MassEmailForm
    change_form_template = 'admin/judge/massemail/change_form.html'
    fieldsets = (
        (None, {'fields': ('subject', 'preheader', 'heading', 'from_email', 'language')}),
        (_('Content'), {'fields': ('content',)}),
        (_('Recipients'), {
            'fields': ('audience', 'include_users', 'contests', 'include_virtual', 'organizations', 'groups',
                       'active_within_days', ('joined_after', 'joined_before'), 'min_problems',
                       ('min_rating', 'max_rating'), 'exclude_users', 'external_emails'),
            'description': _('Only accounts with a confirmed email receive it. Choose who among them: all, only who '
                             'meets the filters, or only the people you name. With several filters, people must '
                             'meet all of them; inside one filter, one of the choices is enough. «Never include» '
                             'always wins.'),
        }),
    )
    list_display = ('subject', 'status_label', 'recipient_column', 'when', 'created_by')
    list_display_links = ('subject',)
    list_filter = ('status',)
    search_fields = ('subject',)

    def get_search_fields(self, request):
        # A superuser also finds an address among the pasted ones (a person who asks to be forgotten, or whose
        # account is cancelled: cpc-cancelar-cuenta does not read them). The others search only subjects.
        return ('subject', 'external_emails') if request.user.is_superuser else self.search_fields
    date_hierarchy = 'created'

    # --- permissions -------------------------------------------------------------------------------------------

    @staticmethod
    def allowed(request):
        user = request.user
        return user.is_active and (user.has_perm(PERMISSION) or user.has_perm(PERMISSION_ALL) or
                                   user.has_perm(PERMISSION_APPROVE))

    @staticmethod
    def edits_all(request):
        return request.user.is_active and request.user.has_perm(PERMISSION_ALL)

    @staticmethod
    def approves(request):
        return request.user.is_active and request.user.has_perm(PERMISSION_APPROVE)

    def sees_all(self, request):
        return self.edits_all(request) or self.approves(request)

    @staticmethod
    def is_own(request, obj):
        profile = getattr(request, 'profile', None)
        return profile is not None and obj.created_by_id == profile.pk

    def owns(self, request, obj):
        """Whether this person writes this email: its author, or whoever edits all of them."""
        return self.edits_all(request) or self.is_own(request, obj)

    def can_change(self, request, obj):
        if obj.status == MassEmail.DRAFT:
            return self.owns(request, obj)
        if obj.status == MassEmail.REVIEW:
            return self.approves(request)
        return False

    def can_send(self, request, obj):
        return self.approves(request) and obj.status in (MassEmail.DRAFT, MassEmail.REVIEW) and \
            self.can_change(request, obj)

    def can_request(self, request, obj):
        # Whoever approves sends without asking anybody.
        return obj.status == MassEmail.DRAFT and self.owns(request, obj) and not self.approves(request)

    def can_withdraw(self, request, obj):
        return obj.status == MassEmail.REVIEW and self.owns(request, obj)

    def can_return(self, request, obj):
        return obj.status == MassEmail.REVIEW and self.approves(request)

    def has_module_permission(self, request):
        return self.allowed(request)

    def has_view_permission(self, request, obj=None):
        return self.allowed(request) and (obj is None or self.sees_all(request) or self.is_own(request, obj))

    def has_add_permission(self, request):
        return self.allowed(request)

    def has_change_permission(self, request, obj=None):
        return self.allowed(request) and (obj is None or self.can_change(request, obj))

    def has_delete_permission(self, request, obj=None):
        # Sent emails stay as a record of what went out and to whom.
        return self.allowed(request) and (obj is None or (self.owns(request, obj) and
                                                          obj.status in (MassEmail.DRAFT, MassEmail.CANCELLED)))

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related('created_by__user')
        if not self.sees_all(request):
            # Only one's own emails, as with problems and contests: the others are not even listed.
            profile = getattr(request, 'profile', None)
            queryset = queryset.filter(created_by=profile) if profile is not None else queryset.none()
        return queryset

    def mail_or_404(self, request, pk):
        return get_object_or_404(self.get_queryset(request), pk=pk)

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)

        class WithRequest(form):
            def __init__(self, *args, **values):
                values.setdefault('request', request)
                super().__init__(*args, **values)

        return WithRequest

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        # The boxes are select2: the admin's «hold down Control» note does not apply to them.
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        field.help_text = db_field.help_text
        return field

    # --- list --------------------------------------------------------------------------------------------------

    @admin.display(description=_('status'), ordering='status')
    def status_label(self, obj):
        colors = {MassEmail.DRAFT: '#777777', MassEmail.REVIEW: '#1e9fd3', MassEmail.SENDING: '#1958c1',
                  MassEmail.SCHEDULED: '#b07800', MassEmail.SENT: '#00a92a', MassEmail.FAILED: '#d52020',
                  MassEmail.CANCELLED: '#777777'}
        return format_html('<span style="color:{};font-weight:bold;">{}</span>', colors.get(obj.status, '#333'),
                           obj.get_status_display())

    @admin.display(description=_('recipients'), ordering='recipient_count')
    def recipient_column(self, obj):
        return obj.recipient_count if obj.recipient_count is not None else '-'

    @admin.display(description=_('date'))
    def when(self, obj):
        # A scheduled email goes out at its time, also once it is marked as sent.
        moment = obj.scheduled_at or obj.sent_at
        return timezone.localtime(moment).strftime('%Y-%m-%d %H:%M') if moment else '-'

    # --- form --------------------------------------------------------------------------------------------------

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.profile
            super().save_model(request, obj, form, change)
            return
        # The email as it is now, locked (round 1 of the review, 2026-11-05): read at the start of the request, a save
        # wrote its old status back over a send, a withdrawal or a return confirmed meanwhile. Only what the form
        # edits is written; if the status changed, or this person may no longer change it, nothing is.
        current = MassEmail.objects.select_for_update().filter(pk=obj.pk).first()
        if current is None or current.status != obj.status or not self.can_change(request, current):
            raise PermissionDenied(gettext('El correo cambió mientras lo editabas: vuelve a abrirlo.'))
        obj.save(update_fields=[name for name in form.cleaned_data if name not in MANY_FIELDS] + ['updated'])

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        obj = self.get_object(request, object_id) if object_id else None
        extra_context.update({
            'resend_configured': is_configured(),
            'recipient_fields': list(FILTER_FIELDS),
            'mass_email': obj,
            'editable': obj is None or self.can_change(request, obj),
            'approves': self.approves(request),
        })
        if obj is not None:
            extra_context.update(self.status_context(request, obj))
        return super().changeform_view(request, object_id, form_url, extra_context)

    @staticmethod
    def moment(value):
        return timezone.localtime(value).strftime('%Y-%m-%d %H:%M') if value else '-'

    def review_editors(self, mail):
        """Who, besides its author, changed the email while it waited for approval this time (the admin's history
        of each save)."""
        if not mail.review_requested_at:
            return []
        entries = LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(MassEmail),
                                          object_id=str(mail.pk), action_flag=CHANGE,
                                          action_time__gte=mail.review_requested_at,
                                          change_message__startswith='[{')
        if mail.created_by_id:
            entries = entries.exclude(user__profile=mail.created_by_id)
        return sorted(set(entries.values_list('user__username', flat=True)))

    def writer_count(self, mail):
        """How many receive it, for whoever does not approve (round 2 of the review): the accounts it was handed over
        to, and every pasted address but those unsubscribed. A pasted address of an account, or of one in «never
        include», is not on its own row, and the real number would tell which one it was."""
        rows = mail.recipients.exclude(state=MassEmailRecipient.UNSUBSCRIBED)
        pasted = len(external_addresses(mail.external_emails))
        return rows.filter(external=False).count() + pasted - \
            mail.recipients.filter(external=True, state=MassEmailRecipient.UNSUBSCRIBED).count()

    def status_context(self, request, mail):
        moment = self.moment
        count = mail.recipient_count or 0
        reveal = self.approves(request) or not mail.external_emails
        if not reveal and mail.recipient_count is not None:
            count = self.writer_count(mail)
        author = mail.created_by.user.username if mail.created_by_id else '-'
        lines = {
            MassEmail.REVIEW: gettext('Por aprobar desde el %(when)s. Lo escribió %(user)s.') %
            {'when': moment(mail.review_requested_at), 'user': author},
            MassEmail.SCHEDULED: gettext('Scheduled for %(when)s, to %(count)d people.') %
            {'when': moment(mail.scheduled_at), 'count': count},
            MassEmail.SENT: gettext('Handed to Resend on %(when)s, for %(count)d people.') %
            {'when': moment(mail.sent_at), 'count': count},
            MassEmail.FAILED: gettext('It could not be completed. The error is below.'),
            MassEmail.CANCELLED: gettext('Cancelled before it was sent.'),
        }
        skipped = mail.recipients.filter(state=MassEmailRecipient.UNSUBSCRIBED).count()
        line = lines.get(mail.status, '')
        if skipped and mail.status in (MassEmail.SCHEDULED, MassEmail.SENT):
            line += ' ' + ngettext('%(count)d person had unsubscribed and was left out.',
                                   '%(count)d people had unsubscribed and were left out.', skipped) % {'count': skipped}
        if mail.sent_by_id and mail.status not in (MassEmail.DRAFT, MassEmail.REVIEW):
            line += ' ' + gettext('Lo envió %(user)s.') % {'user': mail.sent_by.user.username}
        resend_labels = {'draft': gettext('draft'), 'scheduled': gettext('scheduled'), 'queued': gettext('queued'),
                         'sending': gettext('sending'), 'sent': gettext('sent'), 'canceled': gettext('cancelled'),
                         'deleted': gettext('deleted')}
        # Resend's figures are of the real list: with pasted addresses, only for whoever approves.
        stats = json.loads(mail.stats) if mail.stats and reveal else {}
        stat_labels = {'delivered': gettext('Delivered to the inbox'), 'bounced': gettext('Bounced'),
                       'complained': gettext('Marked as spam'), 'unsubscribed': gettext('Unsubscribed with this email'),
                       'suppressed': gettext('Not sent by Resend (suppressed)')}
        note = ''
        if mail.stats_updated and reveal:
            note = gettext('Figures read from Resend on %(when)s.') % {'when': moment(mail.stats_updated)}
            if not stats:
                note += ' ' + gettext('There are no figures yet, or the list is too long: see them in Resend.')
        return {
            # A draft sent back shows it, with the note if there is one (empty, but not None, without it).
            'show_panel': mail.status != MassEmail.DRAFT or mail.review_note is not None,
            'status_line': line,
            'audience_lines': describe(filters_of(mail)) if mail.status != MassEmail.DRAFT else [],
            'resend_status_label': resend_labels.get(mail.resend_status, mail.resend_status),
            'stats': bool(stats),
            'stats_rows': [(stat_labels[kind], stats[kind]) for kind in STATS_KINDS if kind in stats],
            'stats_note': note,
            'can_retry': self.approves(request) and (mail.status == MassEmail.FAILED or (
                mail.status == MassEmail.SENDING and mail.updated < timezone.now() - STUCK_AFTER)),
            'can_send': self.can_send(request, mail),
            'can_cancel': self.approves(request),
            'can_request': self.can_request(request, mail),
            'can_withdraw': self.can_withdraw(request, mail),
            'can_return': self.can_return(request, mail),
            'review_editors': self.review_editors(mail) if mail.status == MassEmail.REVIEW else [],
        }

    def _after_save(self, request, obj):
        if '_send_test' in request.POST:
            self.send_test(request, obj)
            return HttpResponseRedirect(reverse('admin:judge_massemail_change', args=[obj.pk]))
        if '_to_send' in request.POST:
            if not self.can_send(request, obj):
                messages.error(request, gettext('Sólo quien aprueba lo envía: pide su aprobación.'))
                return HttpResponseRedirect(self.change_url(obj))
            return HttpResponseRedirect(reverse('admin:judge_massemail_send', args=[obj.pk]))
        if '_request_review' in request.POST:
            return self.request_review(request, obj)
        return None

    def response_add(self, request, obj, post_url_continue=None):
        return self._after_save(request, obj) or super().response_add(request, obj, post_url_continue)

    def response_change(self, request, obj):
        return self._after_save(request, obj) or super().response_change(request, obj)

    def send_test(self, request, obj):
        address = request.user.email
        if not is_configured():
            messages.error(request, gettext('The Resend key is not configured yet, so nothing can be sent.'))
            return
        if not address:
            messages.error(request, gettext('Your account has no email address to send the test to.'))
            return
        chosen = sender(obj.from_email)
        if chosen is None:
            messages.error(request, gettext('Elige un remitente de la lista.'))
            return
        subject, html, text = render_email(obj, username=request.user.username)
        payload = {'from': chosen, 'to': [address], 'subject': '[%s] %s' % (gettext('Test'), subject),
                   'html': html, 'text': text}
        if reply_to():
            payload['reply_to'] = reply_to()
        try:
            ResendClient().send_email(payload)
        except ResendError as error:
            messages.error(request, gettext('The test could not be sent: %(error)s') % {'error': error})
        else:
            messages.success(request, gettext('A test was sent to %(email)s. It may take a minute to arrive.') %
                             {'email': address})

    # --- approval ----------------------------------------------------------------------------------------------

    def request_review(self, request, obj):
        if not self.can_request(request, obj):
            messages.warning(request, gettext('This email is no longer a draft.'))
            return HttpResponseRedirect(self.change_url(obj))
        # Counted as the writer's own count counts (round 2 of the review): a pasted address of an account in «never
        # include» made «nobody would receive it» answer whose it was. The send page stops a list that is really empty.
        if not recipient_rows(filters_of(obj), reveal=False):
            messages.error(request, gettext('Nobody would receive it.'))
            return HttpResponseRedirect(self.change_url(obj))
        if sender(obj.from_email) is None:
            messages.error(request, gettext('Elige un remitente de la lista.'))
            return HttpResponseRedirect(self.change_url(obj))
        now = timezone.now()
        if not MassEmail.objects.filter(pk=obj.pk, status=MassEmail.DRAFT).update(
                status=MassEmail.REVIEW, review_requested_at=now, review_note=None, updated=now):
            messages.warning(request, gettext('This email is no longer a draft.'))
            return HttpResponseRedirect(self.change_url(obj))
        transaction.on_commit(review_changed)
        self.log_change(request, obj, gettext('Enviado a aprobar.'))
        messages.success(request, gettext('Enviado a aprobar.'))
        return HttpResponseRedirect(self.change_url(obj))

    def withdraw_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        if not self.can_withdraw(request, mail) or \
                not MassEmail.objects.filter(pk=pk, status=MassEmail.REVIEW).update(status=MassEmail.DRAFT,
                                                                                     updated=timezone.now()):
            messages.warning(request, gettext('Ya no espera aprobación.'))
            return HttpResponseRedirect(self.change_url(mail))
        transaction.on_commit(review_changed)
        self.log_change(request, mail, gettext('Solicitud retirada.'))
        messages.success(request, gettext('Solicitud retirada: vuelve a ser un borrador.'))
        return HttpResponseRedirect(self.change_url(mail))

    def return_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        note = request.POST.get('note', '').strip()[:NOTE_LIMIT]
        if not self.can_return(request, mail) or \
                not MassEmail.objects.filter(pk=pk, status=MassEmail.REVIEW).update(
                    status=MassEmail.DRAFT, review_note=note, updated=timezone.now()):
            messages.warning(request, gettext('Ya no espera aprobación.'))
            return HttpResponseRedirect(self.change_url(mail))
        transaction.on_commit(review_changed)
        self.log_change(request, mail, gettext('Devuelto a quien lo escribió.'))
        messages.success(request, gettext('Devuelto a quien lo escribió.'))
        return HttpResponseRedirect(reverse('admin:judge_massemail_changelist'))

    # --- extra pages -------------------------------------------------------------------------------------------

    def get_urls(self):
        def wrap(view, post_only=False):
            def guarded(request, *args, **kwargs):
                if not self.allowed(request):
                    raise PermissionDenied()
                return view(request, *args, **kwargs)
            return self.admin_site.admin_view(require_POST(guarded) if post_only else guarded)

        return [
            path('fragment/', wrap(self.fragment_view, True), name='judge_massemail_fragment'),
            path('preview/', wrap(self.preview_view, True), name='judge_massemail_preview'),
            path('count/', wrap(self.count_view, True), name='judge_massemail_count'),
            path('recipients/', wrap(self.recipients_view, True), name='judge_massemail_recipients'),
            path('unsubscribed/', wrap(self.unsubscribed_view), name='judge_massemail_unsubscribed'),
            path('settings/', wrap(self.settings_view), name='judge_massemail_settings'),
            path('<int:pk>/email/', wrap(self.email_view), name='judge_massemail_email'),
            path('<int:pk>/sent-to/', wrap(self.sent_to_view), name='judge_massemail_sent_to'),
            path('<int:pk>/send/', wrap(self.send_view), name='judge_massemail_send'),
            path('<int:pk>/retry/', wrap(self.retry_view, True), name='judge_massemail_retry'),
            path('<int:pk>/duplicate/', wrap(self.duplicate_view, True), name='judge_massemail_duplicate'),
            path('<int:pk>/cancel/', wrap(self.cancel_view, True), name='judge_massemail_cancel'),
            path('<int:pk>/stats/', wrap(self.stats_view, True), name='judge_massemail_stats'),
            path('<int:pk>/withdraw/', wrap(self.withdraw_view, True), name='judge_massemail_withdraw'),
            path('<int:pk>/return/', wrap(self.return_view, True), name='judge_massemail_return'),
        ] + super().get_urls()

    def context(self, request, title, **values):
        return dict(self.admin_site.each_context(request), title=title, opts=self.model._meta, **values)

    def change_url(self, obj):
        return reverse('admin:judge_massemail_change', args=[obj.pk])

    @staticmethod
    def unsaved(request):
        """The message part of a form, saved or not, as an object that render_email understands."""
        language = request.POST.get('language')
        return MassEmail(subject=request.POST.get('subject', ''), preheader=request.POST.get('preheader', ''),
                         heading=request.POST.get('heading', ''), content=request.POST.get('content', ''),
                         language=language if language in dict(MassEmail.LANGUAGES) else 'es',
                         from_email=request.POST.get('from_email', ''),
                         external_emails=request.POST.get('external_emails', ''))

    def fragment_view(self, request):
        return HttpResponse(preview_fragment(request.POST.get('content', '')))

    def preview_view(self, request):
        return self.render_preview(request, self.unsaved(request))

    def email_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        return self.render_preview(request, mail)

    def render_preview(self, request, mail):
        subject, html, text = render_email(mail, username=request.user.username)
        response = TemplateResponse(request, 'admin/judge/massemail/preview.html', {
            'subject': subject, 'preheader': mail.preheader, 'html': html, 'text': text,
            'sender': sender(mail.from_email) or mail.from_email, 'username': request.user.username,
        })
        response['Content-Security-Policy'] = EMAIL_CSP
        response['X-Frame-Options'] = 'DENY'
        return response

    def count_view(self, request):
        form = RecipientForm(request.POST, request=request, strict=False)
        if not form.is_valid():
            return JsonResponse({'errors': [error for errors in form.errors.values() for error in errors]})
        filters = filters_from_form(form.cleaned_data)
        rows = recipient_rows(filters, reveal=self.approves(request))
        audience = effective_audience(filters)
        bad = parse_addresses(request.POST.get('external_emails', ''))[1]
        return JsonResponse({'count': len(rows), 'audience': audience, 'bad': bad[:MAX_EXTERNAL],
                             'filtered': audience != MassEmail.ALL or bool(filters['exclude_users']),
                             'summary': describe(filters),
                             'sample': [row[1] or masked(row[2]) for row in rows[:12]]})

    def recipients_view(self, request):
        form = RecipientForm(request.POST, request=request, strict=False)
        rows = recipient_rows(filters_from_form(form.cleaned_data), reveal=self.approves(request)) \
            if form.is_valid() else []
        return TemplateResponse(request, 'admin/judge/massemail/recipients.html', self.context(
            request, gettext('Who will receive it'), errors=form.errors if form.errors else None,
            show_email=request.user.is_superuser,
            rows=[{'username': username, 'email': email, 'shown': email if request.user.is_superuser else masked(email),
                   'external': external} for profile_id, username, email, external in rows]))

    def sent_to_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        recipients = list(mail.recipients.all())
        # Whoever does not approve sees the accounts with their state, and then every pasted address as it was
        # pasted, without one (round 2 of the review): the rows of the pasted ones said which went to an account.
        pasted = []
        if not self.approves(request) and mail.external_emails:
            recipients = [item for item in recipients if not item.external]
            pasted = [masked(address) for address in external_addresses(mail.external_emails)]
        counts = {state: sum(1 for item in recipients if item.state == state)
                  for state, label in MassEmailRecipient.STATES}
        for item in recipients:
            item.shown = item.email if request.user.is_superuser else masked(item.email)
        return TemplateResponse(request, 'admin/judge/massemail/sent_to.html', self.context(
            request, gettext('Who it was sent to'), mail=mail, recipients=recipients, counts=counts,
            show_email=request.user.is_superuser, pasted=pasted, total=len(recipients) + len(pasted)))

    def settings_view(self, request):
        """Senders and social networks. Everybody with the section sees them; whoever approves changes them."""
        can_edit = self.approves(request)
        form = None
        if request.method == 'POST':
            if not can_edit:
                raise PermissionDenied()
            form = SettingsForm(request.POST)
            if form.is_valid():
                write_config(SENDERS_KEY, '\n'.join(form.cleaned_data['senders']))
                for key, label in SOCIAL_NETWORKS:
                    write_config(SOCIAL_KEY % key, form.cleaned_data['social_' + key])
                saved = MiscConfig.objects.filter(key=SENDERS_KEY).first()
                if saved is not None:
                    self.log_change(request, saved, gettext('Remitentes y redes guardados.'))
                messages.success(request, gettext('Guardado.'))
                return HttpResponseRedirect(reverse('admin:judge_massemail_settings'))
        if form is None:
            saved = read_config(SENDERS_KEY)
            initial = {'senders': '\n'.join(parse_senders(saved)[0] if saved is not None else default_senders())}
            stored = dict(MiscConfig.objects.filter(key__in=[SOCIAL_KEY % key for key, label in SOCIAL_NETWORKS])
                          .order_by('-pk').values_list('key', 'value'))
            for key, label in SOCIAL_NETWORKS:
                initial['social_' + key] = stored.get(SOCIAL_KEY % key, '')
            form = SettingsForm(initial=initial)
        if not can_edit:
            for field in form.fields.values():
                field.disabled = True
        networks = [form['social_' + key] for key, label in SOCIAL_NETWORKS]
        return TemplateResponse(request, 'admin/judge/massemail/settings.html', self.context(
            request, gettext('Remitentes y redes'), form=form, networks=networks, can_edit=can_edit,
            reply_to=reply_to()))

    # --- sending -----------------------------------------------------------------------------------------------

    def audience(self, mail):
        """(people, of them unsubscribed in Resend, error, why Resend has no room): the list as it is right now."""
        rows = recipient_rows(filters_of(mail))
        try:
            contacts = ResendClient().contacts()
        except ResendError as error:
            return rows, set(), str(error), None
        gone = {row[2].lower() for row in rows if contacts.get(row[2].lower(), {}).get('unsubscribed')}
        # Each address Resend does not have yet is a new contact, and its free plan keeps 1000 (round 1 of the
        # review, 2026-11-05): past that, the ones that do not fit fail one by one, and the email goes out without
        # them.
        new = sum(1 for row in rows if row[2].lower() not in contacts)
        limit = getattr(settings, 'DMOJ_RESEND_CONTACT_LIMIT', 1000)    # None: a plan without a limit
        room = None
        if new and limit is not None and len(contacts) + new > limit:
            room = gettext('Resend guarda hasta %(limit)d contactos y ya tiene %(have)d: este correo necesita '
                           '%(new)d más. Quita correos pegados o borra contactos viejos en Resend.') % \
                {'limit': limit, 'have': len(contacts), 'new': new}
        return rows, gone, None, room

    def send_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        if not self.can_send(request, mail):
            if mail.status == MassEmail.REVIEW:
                messages.warning(request, gettext('Espera su aprobación: lo envía quien aprueba.'))
            elif mail.status == MassEmail.DRAFT:
                messages.warning(request, gettext('Sólo quien aprueba lo envía: pide su aprobación.'))
            else:
                messages.warning(request, gettext('This email is no longer a draft.'))
            return HttpResponseRedirect(self.change_url(mail))
        configured = is_configured()
        rows, gone, error, room = self.audience(mail) if configured else \
            (recipient_rows(filters_of(mail)), set(), None, None)
        receive = len(rows) - len(gone)
        chosen = sender(mail.from_email)
        version = fingerprint(mail)
        form_errors = []
        typed = request.POST.get('confirm', '').strip()
        when = request.POST.get('when', 'now')
        scheduled_text = request.POST.get('scheduled_at', '')

        if request.method == 'POST' and configured and not error:
            scheduled_at = None
            if request.POST.get('version') != version:
                form_errors.append(gettext('El correo cambió mientras lo revisabas: revísalo otra vez.'))
            if chosen is None:
                form_errors.append(gettext('Su remitente ya no está en la lista: elige otro en el correo.'))
            if when == 'later':
                try:
                    scheduled_at = timezone.make_aware(datetime.strptime(scheduled_text, '%Y-%m-%dT%H:%M'))
                except ValueError:
                    form_errors.append(gettext('Choose the date and time to send it.'))
                else:
                    now = timezone.now()
                    if not now + SCHEDULE_MIN <= scheduled_at <= now + SCHEDULE_MAX:
                        form_errors.append(gettext('It can be scheduled from 5 minutes to 30 days from now.'))
            if typed != str(receive):
                form_errors.append(gettext('Type the number of people who will receive it: %(count)d.') %
                                   {'count': receive})
            if not receive:
                form_errors.append(gettext('Nobody would receive it.'))
            if room:
                form_errors.append(room)
            if not form_errors:
                return self.start_sending(request, mail, rows, gone, scheduled_at, version)

        author = mail.created_by.user.username if mail.created_by_id else ''
        return TemplateResponse(request, 'admin/judge/massemail/send.html', self.context(
            request, gettext('Send: %(subject)s') % {'subject': mail.subject}, mail=mail, configured=configured,
            error=error, total=len(rows), unsubscribed=len(gone), receive=receive,
            unsubscribed_line=ngettext('%(count)d more person matches, but unsubscribed earlier and is left out.',
                                       '%(count)d more people match, but unsubscribed earlier and are left out.',
                                       len(gone)) % {'count': len(gone)},
            sample=[row[1] or masked(row[2]) for row in rows if row[2].lower() not in gone][:40],
            sender=chosen or mail.from_email, reply_to=reply_to(), form_errors=form_errors, typed=typed, when=when,
            scheduled_text=scheduled_text, timezone_name=timezone.get_current_timezone_name(),
            minimum=timezone.localtime(timezone.now() + SCHEDULE_MIN + timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M'),
            filtered=effective_audience(filters_of(mail)) != MassEmail.ALL, summary=describe(filters_of(mail)),
            version=version, author=author if mail.status == MassEmail.REVIEW else '', room=room,
            editors=self.review_editors(mail) if mail.status == MassEmail.REVIEW else []))

    def start_sending(self, request, mail, rows, gone, scheduled_at, version):
        task_id = str(uuid.uuid4())
        with transaction.atomic():
            # Under the lock of the email, what is sent is still what was looked at (fingerprint): sent back, taken
            # back or changed meanwhile, nothing starts.
            current = MassEmail.objects.select_for_update().filter(pk=mail.pk).first()
            if current is None or current.status not in (MassEmail.DRAFT, MassEmail.REVIEW):
                messages.warning(request, gettext('Somebody else is already sending this email.'))
                return HttpResponseRedirect(self.change_url(mail))
            chosen = sender(current.from_email)
            if not self.can_send(request, current) or fingerprint(current) != version or chosen is None:
                messages.warning(request, gettext('El correo cambió mientras lo revisabas: revísalo otra vez.'))
                return HttpResponseRedirect(reverse('admin:judge_massemail_send', args=[mail.pk]))
            MassEmail.objects.filter(pk=mail.pk).update(
                status=MassEmail.SENDING, sent_by=request.profile, sent_at=timezone.now(), scheduled_at=scheduled_at,
                from_email=chosen, error='', task_id=task_id, updated=timezone.now())
            MassEmailRecipient.objects.bulk_create([
                MassEmailRecipient(mass_email=mail, profile_id=profile_id, username=username, email=email,
                                   external=external,
                                   state=MassEmailRecipient.UNSUBSCRIBED if email.lower() in gone
                                   else MassEmailRecipient.PENDING)
                for profile_id, username, email, external in rows])
            # The list was read before this transaction. While cpc-cancelar-cuenta cancels an account, its profile
            # stays locked until the cancellation is committed, so the row of that person is written after it and is
            # seen cancelled here; written before, the tool finds it (2026-10-28, round 4).
            from judge.tasks.mass_email import scrub_cancelled
            scrub_cancelled(MassEmailRecipient.objects.filter(mass_email=mail))
        transaction.on_commit(review_changed)
        count = len(rows) - len(gone)
        self.log_change(request, mail, ngettext('Sent to %(count)d person.', 'Sent to %(count)d people.', count) %
                        {'count': count} if not scheduled_at else
                        gettext('Scheduled for %(when)s, to %(count)d people.') %
                        {'when': timezone.localtime(scheduled_at).strftime('%Y-%m-%d %H:%M'), 'count': count})
        return self.enqueue(request, mail, task_id)

    def enqueue(self, request, mail, task_id):
        from judge.tasks import send_mass_email
        try:
            send_mass_email.apply_async(args=[mail.pk], task_id=task_id)
        except Exception as error:  # The queue (Redis) is down: say so instead of leaving it "sending" forever.
            MassEmail.objects.filter(pk=mail.pk).update(
                status=MassEmail.FAILED, error='%s: %s' % (type(error).__name__, error))
            messages.error(request, gettext('The email could not be queued. Try again in a moment.'))
            return HttpResponseRedirect(self.change_url(mail))
        return HttpResponseRedirect(task_status_url_by_id(
            task_id, message=gettext('Handing the email to Resend...'), redirect=self.change_url(mail),
            back=self.change_url(mail)))

    def retry_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        task_id = str(uuid.uuid4())
        # Retrying only goes on with the list fixed when it was confirmed: it is for whoever may send it.
        if not self.approves(request):
            messages.warning(request, gettext('Sólo quien aprueba lo envía: pide su aprobación.'))
            return HttpResponseRedirect(self.change_url(mail))
        if not MassEmail.objects.filter(Q(status=MassEmail.FAILED) | stuck(), pk=pk).update(
                status=MassEmail.SENDING, task_id=task_id, error='', updated=timezone.now()):
            messages.warning(request, gettext('Only an email that failed, or that has been sending for more than '
                                              '15 minutes, can be retried.'))
            return HttpResponseRedirect(self.change_url(mail))
        self.log_change(request, mail, gettext('Retried after a failure.'))
        return self.enqueue(request, mail, task_id)

    def duplicate_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        # The copy says who it is for explicitly, also when the original was saved before `audience` existed.
        copy = MassEmail(created_by=request.profile, status=MassEmail.DRAFT,
                         audience=effective_audience(filters_of(mail)),
                         **{field: getattr(mail, field) for field in ('subject', 'preheader', 'heading', 'content',
                                                                      'language', 'include_virtual',
                                                                      'active_within_days', 'joined_after',
                                                                      'joined_before', 'min_problems',
                                                                      'min_rating', 'max_rating', 'from_email',
                                                                      'external_emails')})
        copy.save()
        for field in MANY_FIELDS:
            getattr(copy, field).set(getattr(mail, field).all())
        self.log_addition(request, copy, gettext('Copied from email %(id)d.') % {'id': mail.pk})
        messages.success(request, gettext('This is a new draft copied from the previous email. Nothing has been '
                                          'sent yet.'))
        return HttpResponseRedirect(self.change_url(copy))

    def cancel_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        # Cancelling a scheduled email is deciding again when it goes, and that is for whoever approves (2026-11-05).
        if not self.approves(request):
            raise PermissionDenied()
        if mail.status != MassEmail.SCHEDULED or not mail.resend_broadcast_id:
            messages.warning(request, gettext('Only a scheduled email can be cancelled.'))
            return HttpResponseRedirect(self.change_url(mail))
        try:
            ResendClient().cancel_broadcast(mail.resend_broadcast_id)
        except ResendError as error:
            messages.error(request, gettext('Resend did not cancel it: %(error)s') % {'error': error})
            return HttpResponseRedirect(self.change_url(mail))
        MassEmail.objects.filter(pk=pk).update(status=MassEmail.CANCELLED, resend_status='canceled',
                                               updated=timezone.now())
        self.log_change(request, mail, gettext('Cancelled before it was sent.'))
        messages.success(request, gettext('Cancelled: nobody will receive it. Duplicate it to change and send it '
                                          'again.'))
        return HttpResponseRedirect(self.change_url(mail))

    def stats_view(self, request, pk):
        mail = self.mail_or_404(request, pk)
        if not mail.resend_broadcast_id:
            raise Http404()
        try:
            client = ResendClient()
            try:
                status = client.broadcast(mail.resend_broadcast_id).get('status') or ''
            except ResendError as error:
                if error.status != 404:
                    raise
                # Deleted in Resend: read as the task reads it when it frees segments (judge/tasks/mass_email.py).
                status = 'deleted'
            stats = {}
            if (mail.recipient_count or 0) <= STATS_LIMIT and \
                    status not in ('draft', 'scheduled', 'canceled', 'deleted'):
                stats = {kind: client.count_recipients(mail.resend_broadcast_id, kind) for kind in STATS_KINDS}
        except ResendError as error:
            messages.error(request, gettext('Could not read it from Resend: %(error)s') % {'error': error})
            return HttpResponseRedirect(self.change_url(mail))
        values = {'resend_status': status[:20], 'stats': json.dumps(stats), 'stats_updated': timezone.now()}
        if mail.status == MassEmail.SCHEDULED and status == 'sent':
            # Resend sent it at its time: until 2026-10-28 it stayed «scheduled» here for good, with its button to
            # cancel it (correos-masivos-1).
            values.update(status=MassEmail.SENT, updated=timezone.now())
        elif mail.status == MassEmail.SCHEDULED and status in ('canceled', 'deleted', 'draft'):
            # Cancelled in Resend's dashboard (a cancelled broadcast reads as a draft again) or deleted there: it will
            # never go out, and the next email frees its segment (2026-10-28, round 2).
            values.update(status=MassEmail.CANCELLED, updated=timezone.now())
        target = MassEmail.objects.filter(pk=pk)
        if 'status' in values:
            target = target.filter(status=mail.status)
        target.update(**values)
        return HttpResponseRedirect(self.change_url(mail))

    def unsubscribed_view(self, request):
        if not is_configured():
            return TemplateResponse(request, 'admin/judge/massemail/unsubscribed.html', self.context(
                request, gettext('Unsubscribed people'), configured=False))
        client = ResendClient()
        # The list is the same for everybody with the permission; subscribing someone again is for whoever manages
        # all emails. Only a superuser sees the addresses, as in the other lists of this section.
        can_resubscribe = self.sees_all(request)
        show_email = request.user.is_superuser
        if request.method == 'POST':
            if not can_resubscribe:
                raise PermissionDenied()
            contact_id = request.POST.get('contact', '')
            try:
                client.resubscribe(contact_id)
            except ResendError as error:
                messages.error(request, gettext('Resend did not change it: %(error)s') % {'error': error})
            else:
                messages.success(request, gettext('Subscribed again: %(email)s will receive the next emails.') %
                                 {'email': request.POST.get('email', '')})
            return HttpResponseRedirect(reverse('admin:judge_massemail_unsubscribed'))
        try:
            contacts = [contact for contact in client.contacts().values() if contact.get('unsubscribed')]
        except ResendError as error:
            return TemplateResponse(request, 'admin/judge/massemail/unsubscribed.html', self.context(
                request, gettext('Unsubscribed people'), configured=True, error=str(error)))
        users = {}
        for username, email in User.objects.filter(email__contains='@').values_list('username', 'email'):
            users.setdefault(email.strip().lower(), []).append(username)
        rows = sorted(({'id': contact['id'], 'email': contact['email'] if show_email else masked(contact['email']),
                        'usernames': users.get(contact['email'].strip().lower(), [])} for contact in contacts),
                      key=lambda row: (row['usernames'] or [row['email']])[0].lower())
        return TemplateResponse(request, 'admin/judge/massemail/unsubscribed.html', self.context(
            request, gettext('Unsubscribed people'), configured=True, rows=rows, can_resubscribe=can_resubscribe,
            confirm_question=gettext('Subscribe this person again? Do it only if they asked for it.')))


def masked(email):
    """ab***@example.com: enough to tell two people apart, not to write to them."""
    local, _at, domain = (email or '').partition('@')
    return '%s***@%s' % (local[:2], domain) if domain else '***'
