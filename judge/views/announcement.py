import hashlib
import logging
from datetime import timedelta

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView

from judge import event_poster as event
from judge.models import Announcement, ContestClarification, ContestParticipation
from judge.models.announcement import ANNOUNCEMENT_CHANNEL
from judge.models.clarification import MAX_PENDING_PER_USER, MAX_QUESTION, MIN_SECONDS_BETWEEN
from judge.utils.views import TitleMixin
from judge.views.contests import ContestMixin, _find_contest

__all__ = ['active_announcements', 'ContestAnnouncements', 'ask_clarification', 'answer_clarification',
           'publish_announcement']

MAX_ACTIVE = 20
# What an announcement written on the tab may hold (2026-11-01): it pops up whole on every screen.
MAX_ANNOUNCEMENT = 2000


def _carries_a_public_answer(announcement):
    """Whether an announcement that points at a question may still show it.

    `ContestClarification.sync_announcement` keeps the announcement in step with
    the answer, so this only ever fires on a row that got past it. It is cheap
    and it is the difference between a stale announcement and somebody else's
    answer on everybody's screen.

    Both halves matter. Public means public *to the participants of the question's
    own contest*: an announcement sitting in a different contest than the question
    it points at must not print it, whatever its visibility says.
    """
    clarification = announcement.clarification
    return (clarification is not None and clarification.is_public and bool(clarification.answer) and
            clarification.contest_id == announcement.contest_id)


def _has_something_to_say(announcement):
    """Whether an announcement has any text to put on screen.

    A clarification's announcement prints the answer of its clarification, under the question as the jury published
    it (`Announcement.published_question`). If the clarification is deleted —directly, or along with the contest
    problem it was about— the foreign key is set to null and what is left looks like a hand-written announcement with
    an empty body. That used to pop up as an empty box for every participant until the contest ended. Deleting a
    clarification now withdraws its announcement as well; this is the check that also covers rows left behind before
    that existed.
    """
    if announcement.clarification_id:
        return _carries_a_public_answer(announcement)
    return bool(announcement.body.strip())


_EVENT_SCRIPT = []


def _event_script():
    """The address of event.js with the hash of the copy served, or None without one (2026-11-01).

    base.html loads it by itself, outside the compressed blocks, so as not to change the compression manifest; /static
    is cached for good, and the hash in the address is what brings a changed file to every browser. Kept once found;
    not found, it is looked for again on the next call, so that a worker started in the middle of a deploy does not
    go without it until the next reload.
    """
    if not _EVENT_SCRIPT:
        found = _find_event_script()
        if found is None:
            return None
        _EVENT_SCRIPT.append(found)
    return _EVENT_SCRIPT[0]


def _find_event_script():
    from django.contrib.staticfiles import finders
    from django.contrib.staticfiles.storage import staticfiles_storage
    from django.templatetags.static import static
    try:
        path = staticfiles_storage.path('event.js')
        with open(path, 'rb') as source:
            content = source.read()
    except (OSError, NotImplementedError):
        path = finders.find('event.js')
        if not path:
            return None
        try:
            with open(path, 'rb') as source:
                content = source.read()
        except OSError:
            return None
    return '%s?v=%s' % (static('event.js'), hashlib.sha256(content).hexdigest()[:12])


def active_announcements(request):
    """Everything this caller should be shown in the pop-up box, newest first.

    This is deliberately the only place the text ever comes from. Nothing that
    reaches the event daemon's unauthenticated publishing port can put words on
    anybody's screen, because no text travels that way.
    """
    now = timezone.now()
    items = []

    contest_id = None
    profile = getattr(request, 'profile', None)
    if request.user.is_authenticated and profile is not None and profile.current_contest is not None:
        contest_id = profile.current_contest.contest_id

    # How long each announcement keeps popping up is decided when it is written:
    # thirty minutes for a notice, or the end of the contest for a clarification.
    # An announcement with neither text nor clarification has nothing to show, and would take one of the
    # MAX_ACTIVE places below.
    queryset = Announcement.objects.filter(is_visible=True) \
                                   .filter(Q(expires__isnull=True) | Q(expires__gt=now)) \
                                   .exclude(clarification__isnull=True, body='')
    if contest_id is None:
        queryset = queryset.filter(contest__isnull=True)
    else:
        queryset = queryset.filter(Q(contest__isnull=True) | Q(contest_id=contest_id))

    for announcement in queryset.select_related('contest', 'clarification').order_by('-created')[:MAX_ACTIVE]:
        if announcement.clarification_id:
            # Withdrawing the announcement is what an answer turned private does,
            # but the check is repeated here: this is the only place the text of
            # somebody else's question ever leaves the server.
            if not _carries_a_public_answer(announcement):
                continue
            # Laid out here rather than stored, so each reader gets the labels in
            # their own language. The question is the one the jury published: as
            # asked, rewritten, or left out (aclaraciones-anuncios-3).
            question = announcement.published_question
            body = '%s: %s' % (_('Answer'), announcement.clarification.answer)
            if question:
                body = '%s: %s\n\n%s' % (_('Question'), question, body)
        else:
            if not _has_something_to_say(announcement):
                continue
            body = announcement.body
        items.append({
            'key': 'announcement:%d' % announcement.id,
            'body': body,
            'contest': announcement.contest.name if announcement.contest_id else None,
        })

    # Your own answered questions come back to you the same way, so you do not
    # have to sit refreshing the tab waiting for the jury.
    #
    # Only the private ones. A public answer already travels as an announcement
    # of its own, and returning it here as well showed it to the person who
    # asked twice over.
    if contest_id is not None:
        answered = ContestClarification.objects.filter(
            ContestClarification.ownership_filter(profile),
            contest_id=contest_id, answered__isnull=False, is_public=False,
            contest__end_time__gt=now,
        ).select_related('contest').order_by('-answered')[:MAX_ACTIVE]
        for clarification in answered:
            items.append({
                'key': 'clarification:%d' % clarification.id,
                'body': '%s\n\n%s' % (clarification.question, clarification.answer),
                'contest': clarification.contest.name,
            })

    # A badge in the navigation bar, so the jury notices a question arriving
    # without having to keep the tab open.
    pending_questions = 0
    participation = getattr(request, 'participation', None)
    if participation is not None and participation.contest.is_editable_by(request.user):
        pending_questions = ContestClarification.objects.filter(contest_id=contest_id, answer='').count()

    # Where to hear that there is something new, asked once per page by whoever is in a contest (2026-11-01). Nothing
    # is asked of the daemon here: with a daemon down or that does not answer, this answer, the box and the poll of
    # every minute go on as before. The page starts listening from 0, so the daemon brings back the rings of the
    # channel it still keeps, and at most the page asks once more. Not once the contest ended, as in a virtual
    # participation: what pops up stops with the contest, and the poll still covers the rest.
    listen = None
    if (request.GET.get('listen') and contest_id is not None and getattr(event, 'real', False) and
            not profile.current_contest.contest.ended):
        script = _event_script()
        if script is not None:
            listen = {'channel': ANNOUNCEMENT_CHANNEL % contest_id, 'last': 0, 'script': script}

    return JsonResponse({'announcements': items, 'pending_questions': pending_questions, 'listen': listen})


class ContestProblemChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.problem.name


class ClarificationForm(forms.ModelForm):
    problem = ContestProblemChoiceField(queryset=None, required=False, label=_('problem'),
                                        empty_label=_('About the contest in general'))

    class Meta:
        model = ContestClarification
        fields = ('problem', 'question')
        widgets = {
            'question': forms.Textarea(attrs={'rows': 4, 'maxlength': MAX_QUESTION,
                                              'placeholder': _('Ask the jury a question about this contest.')}),
        }

    def __init__(self, *args, **kwargs):
        contest = kwargs.pop('contest')
        super().__init__(*args, **kwargs)
        self.fields['problem'].queryset = contest.contest_problems.select_related('problem').order_by('order')

    def clean_question(self):
        # A TextField gives a form field with no length limit of its own, so the
        # cap has to be stated here as well as in the textarea attribute, which a
        # browser is free to ignore.
        question = self.cleaned_data['question'].strip()
        if not question:
            raise forms.ValidationError(_('The question is empty.'))
        if len(question) > MAX_QUESTION:
            raise forms.ValidationError(_('Questions are limited to %(limit)d characters.'),
                                        params={'limit': MAX_QUESTION})
        return question


def _runs(contest, user):
    """Whether `user` runs `contest`: one of its authors or curators, also without the permission to edit their own
    contests, or whoever may edit it (is_editable_by). Who runs it writes to everybody from the tab and does not ask
    (the owner, 2026-11-01): one rule for both, so that nobody is left able to do neither."""
    if not user.is_authenticated:
        return False
    return user.profile.pk in contest.editor_ids or contest.is_editable_by(user)


def _may_ask(contest, participation, user):
    """Whether `participation`, the caller's current one, lets them put a question to the jury of `contest`.

    The official participation, alone or on the roster of a team, while it lasts and is not disqualified; and, as a
    spectator, whoever tests the contest, who tries it as a contestant would. Whoever runs it does not ask (the owner,
    2026-11-01; until then they could, to try the form): they answer, and write to everybody with «Post an
    announcement», and a question of theirs only sat among the pending ones. Somebody who both runs and tests it runs
    it. Anybody else spectating asks nothing, also whoever finished their participation and came back to watch: it
    skipped the cut of a team that finished, with three more questions of their own.
    """
    if contest.ended or participation is None or participation.contest_id != contest.id:
        return False
    profile_id = user.profile.pk
    # Also with a participation of their own, from before they ran it or joined as an administrator.
    runs_it = _runs(contest, user)
    if participation.live:
        return (not runs_it and not participation.ended and not participation.is_disqualified and
                participation.contains_profile(profile_id))
    if participation.spectate:
        return profile_id in contest.tester_ids and not runs_it
    return False


def _visible_clarifications(contest, profile, can_edit):
    queryset = contest.clarifications.select_related('user__user', 'answered_by__user', 'problem__problem',
                                                     'team_participation')
    # An answer made public is shown in the announcements above, laid out as
    # question and answer. Leaving it here too put the same text on the page
    # twice, which is what the list is kept clear of. Only the ones that really
    # reached the announcements are dropped: a public answer saved without its
    # announcement used to disappear from both halves of the page.
    # «Really reached» means an announcement of this same contest: one moved elsewhere is not shown here —the
    # check in `_carries_a_public_answer` hides it— and excluding the question anyway made the answer vanish
    # from both halves of the page.
    # And with the question as it was asked: when the jury rewrote it or left it out (aclaraciones-anuncios-3), the
    # words of whoever asked are only here, for them and for the jury.
    announced = Announcement.objects.filter(clarification=OuterRef('pk'), is_visible=True,
                                            contest_id=OuterRef('contest_id'), body='')
    queryset = queryset.annotate(is_announced=Exists(announced)) \
                       .exclude(is_public=True, answered__isnull=False, is_announced=True)
    if can_edit:
        return queryset
    if profile is None:
        return queryset.none()
    # What is left is your own: waiting, or answered just for you.
    return queryset.filter(ContestClarification.ownership_filter(profile)).distinct()


class ContestAnnouncements(ContestMixin, TitleMixin, DetailView):
    template_name = 'contest/announcements.html'
    tab = 'announcements'

    def get_title(self):
        return _('Clarifications for %s') % self.object.name

    def get_content_title(self):
        return self.object.name

    def can_ask(self):
        if not self.request.user.is_authenticated:
            return False
        return _may_ask(self.object, self.request.profile.current_contest, self.request.user)

    def can_read(self, has_joined):
        """Whether this caller may read the clarifications of this contest (aclaraciones-anuncios-1, 2026-10-30).

        The announcements and the public answers name the problems and tell things about them, so while the contest
        runs they are only for whoever is inside it, as the problems are: a participation in it (also on the roster
        of a team, or spectating), running or testing it, or being one of its spectators once it started. Everybody
        else, also straight at the address, is told that they are published when the contest ends; from then on,
        whoever sees the contest reads them, as before. contest-tabs.html shows the tab by the same rule.
        """
        contest = self.object
        return (contest.ended or self.can_edit or self.is_editor or self.is_tester or
                (self.is_spectator and contest.started) or has_joined or contest.is_in_contest(self.request.user))

    def can_announce(self):
        """Whether this caller may write an announcement to this contest from the tab (publish_announcement).

        Whoever runs the contest, as author, curator or with every contest at hand (the owner, 2026-11-01): until then
        the button led to the administration and only showed with its permission to add announcements, so an author
        without it had no way to write to the contestants. Not once the contest ended: nobody is left to read it.
        """
        return _runs(self.object, self.request.user) and not self.object.ended

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tab'] = self.tab
        context['can_read'] = self.can_read(context['has_joined'])
        if not context['can_read']:
            # Only the notice that they are published when the contest ends (contest/announcements.html).
            return context
        profile = self.request.profile if self.request.user.is_authenticated else None
        # Same defence as the pop-up box: the tab prints the question and the
        # answer straight out of the clarification the announcement points at.
        # With the name of the problem of each answer in the same query: it took two more per answer, on every
        # refresh of every open tab (aclaraciones-anuncios-6).
        announcements = (self.object.announcements.filter(is_visible=True)
                             .select_related('author__user', 'clarification__problem__problem')
                             .order_by('-created'))
        context['announcements'] = [announcement for announcement in announcements
                                    if _has_something_to_say(announcement)]
        clarifications = _visible_clarifications(self.object, profile, self.can_edit).order_by('-asked')
        if self.can_edit:
            # The form of a question waiting for an answer comes with the question to publish already written: what
            # its announcement said if it was published and withdrawn since, so that answering again does not put
            # back the question as asked (ContestClarification.question_as_published). One query for all of them.
            clarifications = clarifications.prefetch_related(Prefetch(
                'announcements', queryset=Announcement.objects.order_by('created', 'pk'),
                to_attr='announcements_by_age'))
        context['clarifications'] = clarifications
        context['can_ask'] = self.can_ask()
        context['ask_form'] = ClarificationForm(contest=self.object) if context['can_ask'] else None
        context['can_announce'] = self.can_announce()
        context['max_announcement'] = MAX_ANNOUNCEMENT
        context['max_question'] = MAX_QUESTION
        context['clarification_errors'] = self.request.session.pop('clarification_errors', [])
        return context


def _contest_or_404(request, contest):
    contest, exists = _find_contest(request, contest)
    if not exists:
        raise Http404()
    return contest


@login_required
@require_POST
@transaction.atomic
def ask_clarification(request, contest):
    contest = _contest_or_404(request, contest)
    profile = request.profile

    participation = profile.current_contest
    if contest.ended or participation is None or participation.contest_id != contest.id:
        raise PermissionDenied()

    # Teammates share the jury conversation and its limits. Serialize on the
    # participation so simultaneous questions do not bypass the shared cap.
    # Also for one person on their own: the two caps were counted without a lock there, and questions sent at once
    # got past both (aclaraciones-anuncios-7).
    participation = ContestParticipation.objects.select_for_update().get(pk=participation.pk)
    participation.contest = contest
    if not _may_ask(contest, participation, request.user):
        raise PermissionDenied()
    team_participation = None
    if participation.team_id:
        team_participation = participation
        questions = ContestClarification.objects.filter(team_participation=participation)
    else:
        questions = ContestClarification.objects.filter(user=profile, contest=contest, team_participation__isnull=True)
    now = timezone.now()
    too_soon = questions.filter(asked__gte=now - timedelta(seconds=MIN_SECONDS_BETWEEN)).exists()
    pending = questions.filter(answer='').count()

    form = ClarificationForm(request.POST, contest=contest)
    errors = []
    if too_soon:
        errors.append(_('Wait a few seconds before asking again.'))
    elif pending >= MAX_PENDING_PER_USER:
        errors.append(_('You already have %(limit)d questions waiting for an answer.')
                      % {'limit': MAX_PENDING_PER_USER})
    elif form.is_valid():
        clarification = form.save(commit=False)
        clarification.contest = contest
        clarification.user = profile
        clarification.team_participation = team_participation
        clarification.save()
    else:
        errors = [str(error) for field_errors in form.errors.values() for error in field_errors]

    if errors:
        # Carried in the session because the answer is a redirect, so the tab can
        # say what went wrong without the question being re-posted on refresh.
        request.session['clarification_errors'] = [str(error) for error in errors]
    return HttpResponseRedirect(reverse('contest_announcements', args=[contest.key]))


@login_required
@require_POST
@transaction.atomic
def answer_clarification(request, contest, pk):
    contest = _contest_or_404(request, contest)
    if not contest.is_editable_by(request.user):
        raise PermissionDenied()

    # Locked: two members of the jury answering at once are taken one after the other, and the second one finds the
    # question answered (aclaraciones-anuncios-2).
    clarification = get_object_or_404(ContestClarification.objects.select_for_update(), pk=pk, contest=contest)
    answer = request.POST.get('answer', '').strip()
    errors = []
    if clarification.answer:
        # The tab only offers to answer what nobody answered, but a form left open —the refresh waits while
        # something is being written— or two members of the jury at once replaced the first answer without a
        # trace, and could take it public or private. An answer is changed in the administration, which keeps a
        # record of it. The text is given back, so that it is not lost.
        errors.append(_('This question had already been answered, so your answer was not saved.'))
        if answer:
            errors.append(_('Your answer was: %(answer)s') % {'answer': answer[:MAX_QUESTION]})
    elif not answer:
        # It used to be a 404 page.
        errors.append(_('The answer is empty.'))
    if errors:
        request.session['clarification_errors'] = [str(error) for error in errors]
        return HttpResponseRedirect(reverse('contest_announcements', args=[contest.key]))

    clarification.answer = answer[:MAX_QUESTION]
    clarification.answered = timezone.now()
    clarification.answered_by = request.profile
    clarification.is_public = request.POST.get('is_public') == 'on'
    clarification.save()

    # The question as everybody will read it above a public answer (aclaraciones-anuncios-3): the tab sends it
    # already written, and the jury may rewrite it or empty it. A page from before 2026-10-30 does not send it, and
    # the question goes as it was asked, as it always did.
    published = request.POST.get('published_question') if clarification.is_public else None
    if published is not None:
        published = published.strip()[:MAX_QUESTION]

    # A public answer is exactly what the announcement box is for, so it reuses
    # it instead of growing a second delivery path. Answer and announcement move
    # together, in the same transaction and through the same helper the admin
    # uses: editing an answer down to private has to take the announcement with
    # it, or the new text keeps going out to everybody.
    clarification.sync_announcement(author=request.profile, published_question=published)

    return HttpResponseRedirect(reverse('contest_announcements', args=[contest.key]))


@login_required
@require_POST
def publish_announcement(request, contest):
    """An announcement to everybody in the contest, written on its clarifications tab by whoever runs it (2026-11-01).

    As the form of the administration writes one to a contest: plain text, signed by whoever wrote it, popping up
    until the contest ends. It is withdrawn or changed in the administration, by whoever may.
    """
    from judge.models.clarification import plain_text
    contest = _contest_or_404(request, contest)
    if not _runs(contest, request.user):
        raise PermissionDenied()
    body = plain_text(request.POST.get('body'))[:MAX_ANNOUNCEMENT]
    if contest.ended:
        request.session['clarification_errors'] = [str(_('El concurso ya terminó.'))]
    elif not body:
        request.session['clarification_errors'] = [str(_('An announcement written here needs some text.'))]
    else:
        announcement = Announcement.objects.create(contest=contest, body=body, author=request.profile,
                                                   expires=contest.end_time)
        logging.getLogger('judge.announcement').info('Contest %s: %s published announcement %d', contest.key,
                                                     request.user.username, announcement.pk)
    return HttpResponseRedirect(reverse('contest_announcements', args=[contest.key]))
