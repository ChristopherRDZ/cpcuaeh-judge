# coding=utf-8
import hashlib
import logging
import unicodedata
from contextlib import contextmanager
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import get_default_password_validators
from django.contrib.sites.shortcuts import get_current_site
from django.db import IntegrityError, connection, transaction
from django.db.models import DO_NOTHING, ProtectedError, Q, RestrictedError
from django.db.models.deletion import Collector
from django.forms import ChoiceField, ModelChoiceField
from django.shortcuts import render
from django.utils.timezone import now
from django.utils.translation import gettext, gettext_lazy as _, ngettext
from registration.backends.default.views import (ActivationView as OldActivationView,
                                                 RegistrationView as OldRegistrationView)
from registration.forms import RegistrationForm
from registration.models import RegistrationProfile
from sortedm2m.forms import SortedMultipleChoiceField

from judge.models import Language, Organization, Profile, TIMEZONE
from judge.utils.mail import SEND_ERRORS, email_budget_available, spend_email_budget, validate_email_domain
from judge.utils.recaptcha import ReCaptchaField, ReCaptchaWidget
from judge.utils.subscription import Subscription, newsletter_id
from judge.utils.views import generic_message
from judge.widgets import Select2MultipleWidget, Select2Widget

logger = logging.getLogger('judge.mail')

# CPC (2026-10-30): a registration whose activation expired, that nobody ever signed in to and that nothing in the site
# points to gives way to a new one with its email or its username: it is deleted in the transaction that creates the
# new one. Nothing deletes the others; they stay until someone registers with their email or their username. This is
# not cpc-cancelar-cuenta, whose rule of never deleting the User keeps what real accounts left in rankings and
# results: these were never activated nor used, and only what registering made goes with them.

REGISTRATION_LOCK_SECONDS = 30
# An activation link opened just before the deadline checks it and then saves the account, without a lock: an hour
# after the deadline, none can still be on its way.
EXPIRY_MARGIN = timedelta(hours=1)


def _made_by_registering():
    """What registering creates besides the User: its profile, its activation key, the organizations chosen in the
    form and, where the newsletter is installed, its subscription. An organization that an admin added to an account
    that never signed in had no effect, and goes with it: nothing tells it apart from one chosen in the form and
    closed since, and refusing those kept their addresses taken for good."""
    made = {User, Profile, RegistrationProfile, Profile.organizations.through}
    if Subscription is not None:
        made.add(Subscription)
    return made


def _found(objs):
    return objs.exists() if hasattr(objs, 'exists') else bool(objs)


def only_registration_left(user):
    """Whether deleting `user` would take nothing but what registering made. Asked of Django's own collector, the one
    the deletion uses, so that relations added to the site later count as well."""
    made = _made_by_registering()
    collector = Collector(using=user._state.db or 'default')
    try:
        collector.collect([user])
    except (ProtectedError, RestrictedError):
        return False
    if any(instances for model, instances in collector.data.items() if model not in made):
        return False
    if any(_found(queryset) for queryset in collector.fast_deletes if queryset.model not in made):
        return False
    # Rows that would only lose their link to it (SET_NULL and the like) point to it all the same.
    if any(_found(objs) for batches in collector.field_updates.values() for objs in batches):
        return False
    # The collector skips DO_NOTHING relations.
    for instance in [user, *Profile.objects.filter(user=user)]:
        for relation in instance._meta.get_fields(include_hidden=True):
            if getattr(relation, 'on_delete', None) is DO_NOTHING and \
                    relation.related_model._base_manager.filter(**{relation.field.name: instance}).exists():
                return False
    return True


def expired_registration(user):
    """Whether `user` is a registration that a new one may take over: inactive, never signed in, with its activation
    expired an hour ago or more (ACCOUNT_ACTIVATION_DAYS, as RegistrationProfile counts them) and nothing besides
    what registering made.
    Never an active account, one that signed in once, one still within its days, nor one without a pending activation
    key (deactivated by hand after activating, or cancelled with cpc-cancelar-cuenta, which deletes the key)."""
    if user.is_active or user.last_login is not None:
        return False
    if user.date_joined + timedelta(days=settings.ACCOUNT_ACTIVATION_DAYS) + EXPIRY_MARGIN > now():
        return False
    registration = RegistrationProfile.objects.filter(user=user).first()
    if registration is None or registration.activated or not registration.activation_key_expired():
        return False
    return only_registration_left(user)


class RegistrationTaken(Exception):
    """The email or the username belongs to an account that is not an expired registration, as found under the lock:
    one registered since the form was checked, for instance."""

    def __init__(self, field):
        super().__init__(field)
        self.field = field


class RegistrationBusy(Exception):
    pass


def take_over_expired_registrations(email, username):
    """Inside the transaction that creates the new account, before it does: lock every account with this email or
    this username, and their profiles, so that nobody can activate them, sign in to them or point to them meanwhile;
    check them again under the lock, and delete them. Raises RegistrationTaken if one is not an expired registration.
    The form looks them up the same way.

    Found with plain reads, and only then locked, by primary key: auth_user has no index on the email, and a locking
    read by email goes through the whole table and waits on every row it meets that another registration has just
    created and not committed yet (it commits after sending its email), so every registration waited for the one
    before it. What orders registrations with the same email or username is one_registration_at_a_time."""
    lookups = (('email', Q(email=email)), ('username', Q(username__iexact=username)))
    found = set()
    for _field, lookup in lookups:
        found.update(User.objects.filter(lookup).values_list('pk', flat=True))
    if not found:
        return
    locked = list(User.objects.select_for_update().filter(pk__in=found).order_by('pk').values_list('pk', flat=True))
    list(Profile.objects.select_for_update().filter(user_id__in=locked).order_by('pk').values_list('pk', flat=True))
    holders = {}
    for field, lookup in lookups:
        # Read again under the lock: one that changed its email or its username meanwhile is no longer in the way.
        for user in User.objects.filter(lookup, pk__in=locked).order_by('pk'):
            holders.setdefault(user.pk, (field, user))
    for field, user in holders.values():
        if not expired_registration(user):
            raise RegistrationTaken(field)
    for _field, user in holders.values():
        logger.warning('Deleting the expired registration of user %d: a new registration takes its email or username',
                       user.pk)
        user.delete()


def _lock_name(kind, value):
    # Folded about as the database compares (capitals and accents), so that two registrations it takes as the same
    # wait for each other. Not every time: utf8mb4_general_ci takes «ß» as «s», and this folds it into «ss». Two such
    # usernames at once meet at the unique index instead, and the second one gets the error of the form
    # (RegistrationView.form_valid); two such addresses, which no index stops, can both be created, as any two could
    # before these locks. MariaDB allows 64 characters.
    folded = ''.join(char for char in unicodedata.normalize('NFKD', value) if not unicodedata.combining(char))
    return 'dmoj-register-' + hashlib.sha1(('%s:%s' % (kind, folded.casefold())).encode()).hexdigest()


@contextmanager
def one_registration_at_a_time(email, username):
    """One registration at a time per email and per username, across every process: auth_user has no unique index on
    the email, so two at once with the same address would both pass the form and both be created, and with the same
    username the second ended in a 500. Named locks of MariaDB, as in judge.arena.rating, taken before the transaction
    and released once it is committed (ATOMIC_REQUESTS is off), so that whoever waited reads what the first one made."""
    if connection.vendor != 'mysql':
        yield
        return
    taken = []
    try:
        with connection.cursor() as cursor:
            for name in sorted({_lock_name('email', email), _lock_name('username', username)}):
                cursor.execute('SELECT GET_LOCK(%s, %s)', [name, REGISTRATION_LOCK_SECONDS])
                if cursor.fetchone()[0] != 1:
                    raise RegistrationBusy()
                taken.append(name)
        yield
    finally:
        if taken:
            with connection.cursor() as cursor:
                for name in reversed(taken):
                    cursor.execute('SELECT RELEASE_LOCK(%s)', [name])


class CustomRegistrationForm(RegistrationForm):
    username = forms.RegexField(regex=r'^\w+$', max_length=30, label=_('Username'),
                                error_messages={'invalid': _('A username must contain letters, '
                                                             'numbers, or underscores.')})
    first_name = forms.CharField(max_length=50, label=_('Nombre'))
    last_name = forms.CharField(max_length=50, label=_('Apellidos'))
    timezone = ChoiceField(label=_('Timezone'), choices=TIMEZONE,
                           widget=Select2Widget(attrs={'style': 'width:100%'}))
    language = ModelChoiceField(queryset=Language.objects.all(), label=_('Preferred language'), empty_label=None,
                                widget=Select2Widget(attrs={'style': 'width:100%'}))
    organizations = SortedMultipleChoiceField(queryset=Organization.objects.filter(is_open=True),
                                              label=_('Organizations'), required=False,
                                              widget=Select2MultipleWidget(attrs={'style': 'width:100%'}))

    if newsletter_id is not None:
        newsletter = forms.BooleanField(label=_('Subscribe to newsletter?'), initial=True, required=False)

    if ReCaptchaField is not None:
        captcha = ReCaptchaField(widget=ReCaptchaWidget())

    def taken_error(self, field):
        if field == 'email':
            return forms.ValidationError(gettext('The email address "%s" is already taken. Only one registration '
                                                 'is allowed per address.') % self.cleaned_data['email'])
        return self.instance.unique_error_message(User, ['username'])

    def clean_username(self):
        # As UserCreationForm does, regardless of capitals; but an expired registration gives way to this one.
        username = self.cleaned_data['username']
        if not all(map(expired_registration, User.objects.filter(username__iexact=username))):
            raise self.taken_error('username')
        return username

    def clean_email(self):
        if not all(map(expired_registration, User.objects.filter(email=self.cleaned_data['email']))):
            raise self.taken_error('email')
        validate_email_domain(self.cleaned_data['email'])
        return self.cleaned_data['email']

    def validate_unique(self):
        # The model would find the username of an expired registration that this one replaces; clean_username has
        # already looked for it.
        exclude = self._get_validation_exclusions()
        exclude.add('username')
        try:
            self.instance.validate_unique(exclude=exclude)
        except forms.ValidationError as error:
            self._update_errors(error)

    def clean_organizations(self):
        organizations = self.cleaned_data.get('organizations') or []
        max_orgs = settings.DMOJ_USER_MAX_ORGANIZATION_COUNT
        if len(organizations) > max_orgs:
            raise forms.ValidationError(ngettext('You may not be part of more than {count} public organization.',
                                                 'You may not be part of more than {count} public organizations.',
                                                 max_orgs).format(count=max_orgs))
        return self.cleaned_data['organizations']


class ActivationEmailNotSent(Exception):
    pass


class RegistrationView(OldRegistrationView):
    title = _('Register')
    form_class = CustomRegistrationForm
    template_name = 'registration/registration_form.html'
    # register() sends the activation email itself, inside the transaction that creates the account: if
    # it cannot be sent, the account is not created and the address can be used to register again.
    SEND_ACTIVATION_EMAIL = False

    def form_valid(self, form):
        if not email_budget_available(reserve=settings.DMOJ_EMAIL_DAILY_RESERVE):
            return self.activation_email_not_sent(status=429)
        try:
            with one_registration_at_a_time(form.cleaned_data['email'], form.cleaned_data['username']):
                return super(RegistrationView, self).form_valid(form)
        except ActivationEmailNotSent:
            return self.activation_email_not_sent(status=503)
        except RegistrationTaken as taken:
            # Taken while this one was being checked: the answer the form gives.
            form.add_error(taken.field, form.taken_error(taken.field))
            return self.form_invalid(form)
        except IntegrityError:
            # Two usernames that the database takes as one and the locks do not (_lock_name), at once: the unique
            # index stops the second when the first is committed. The same answer; anything else is raised as before.
            if not User.objects.filter(username__iexact=form.cleaned_data['username']).exists():
                raise
            form.add_error('username', form.taken_error('username'))
            return self.form_invalid(form)
        except RegistrationBusy:
            return generic_message(self.request, gettext('Account not created'),
                                   gettext('Another registration with this email address or username is being '
                                           'processed. Please try again in a minute.'), status=503)

    def activation_email_not_sent(self, status):
        return generic_message(self.request, gettext('Account not created'),
                               gettext('We could not send the activation email, so your account was not created. '
                                       'Please try again later.'), status=status)

    def get_context_data(self, **kwargs):
        if 'title' not in kwargs:
            kwargs['title'] = self.title
        kwargs['TIMEZONE_MAP'] = settings.TIMEZONE_MAP
        kwargs['password_validators'] = get_default_password_validators()
        kwargs['tos_url'] = settings.TERMS_OF_SERVICE_URL
        return super(RegistrationView, self).get_context_data(**kwargs)

    def register(self, form):
        with transaction.atomic():
            # If the activation email cannot be sent, the expired registration comes back with the rest.
            take_over_expired_registrations(form.cleaned_data['email'], form.cleaned_data['username'])
            user = super(RegistrationView, self).register(form)

            # Aqui asignamos first_name y last_name del formulario al usuario
            cleaned_data = form.cleaned_data
            user.first_name = cleaned_data['first_name']
            user.last_name = cleaned_data['last_name']
            user.save()  # con esto se guardan

            profile, _ = Profile.objects.get_or_create(user=user, defaults={
                'language': Language.get_default_language(),
            })

            profile.timezone = cleaned_data['timezone']
            profile.language = cleaned_data['language']
            profile.organizations.add(*cleaned_data['organizations'])
            profile.save()

            if newsletter_id is not None and cleaned_data.get('newsletter'):
                Subscription(user=user, newsletter_id=newsletter_id, subscribed=True).save()

            try:
                registration = self.registration_profile.objects.get(user=user)
                registration.send_activation_email(get_current_site(self.request), self.request)
            except SEND_ERRORS as error:
                logger.exception('Activation email could not be sent; the account was not created')
                raise ActivationEmailNotSent from error

        spend_email_budget()
        return user

    def get_initial(self, *args, **kwargs):
        initial = super(RegistrationView, self).get_initial(*args, **kwargs)
        initial['timezone'] = settings.DEFAULT_USER_TIME_ZONE
        initial['language'] = Language.objects.get(key=settings.DEFAULT_USER_LANGUAGE)
        return initial


class ActivationView(OldActivationView):
    title = _('Activation Key Invalid')
    template_name = 'registration/activate.html'

    def get_context_data(self, **kwargs):
        if 'title' not in kwargs:
            kwargs['title'] = self.title
        return super(ActivationView, self).get_context_data(**kwargs)


def social_auth_error(request):
    return render(request, 'generic-message.html', {
        'title': gettext('Authentication failure'),
        'message': request.GET.get('message'),
    })
