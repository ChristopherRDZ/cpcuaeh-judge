from django import forms
from django.contrib import admin
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from judge.models import ContestClarification
from judge.models.clarification import MAX_QUESTION, plain_text


class ContestClarificationForm(forms.ModelForm):
    # The question as everybody reads it above a public answer (aclaraciones-anuncios-3, 2026-10-30): the choice the
    # contest tab offers when answering, also here, where answers are corrected or made public later. Only on a
    # question that exists (ContestClarificationAdmin.get_fields). The page also carries a hidden copy of what it
    # showed (show_hidden_initial), which is what tells a change made on that page (clean).
    published_question = forms.CharField(
        label=_('question as published'), required=False, max_length=MAX_QUESTION, show_hidden_initial=True,
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text=_('What every contestant reads above a public answer. Rewrite it if it gives away how somebody is '
                    'solving a problem, or empty it to publish only the answer. Whoever asked keeps seeing the '
                    'question above as they wrote it.'))

    class Meta:
        model = ContestClarification
        fields = ('contest', 'problem', 'user', 'question', 'answer', 'is_public')
        # The help of the model said nothing of the question, which also goes out.
        help_texts = {
            'is_public': _('A public answer is shown to every contestant, with the question as published below. A '
                           'private one only goes back to whoever asked.'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is not None:
            self.fields['published_question'].initial = self.instance.question_as_published()

    def clean(self):
        cleaned_data = super().clean()
        contest = cleaned_data.get('contest')
        problem = cleaned_data.get('problem')
        # `problem` is a raw id field, so the browser can name any contest problem
        # at all. A question filed against another contest's problem would show
        # that problem's name on this contest's page.
        if contest is not None and problem is not None and problem.contest_id != contest.pk:
            self.add_error('problem', _('That problem belongs to a different contest.'))
        # A question asked by a team belongs to the participation of that team, in its contest. Moved to another
        # contest, it kept pointing at the first one: in the second it was shown as that team's, went back to its
        # members and counted towards their cap (aclaraciones-anuncios-8).
        participation = self.instance.team_participation if self.instance.team_participation_id else None
        if contest is not None and participation is not None and participation.contest_id != contest.pk:
            self.add_error('contest', _('A question asked by a team stays in the contest where the team asked it.'))
        # The question as published changes only when it is changed on the page that is saved: what the page sends is
        # compared with the hidden copy of what it showed, not with what the announcement says by now. Somebody else
        # may have rewritten it or left it out since the page was opened, here or answering from the tab, and saving
        # the answer for anything else from that page put back what it showed. A page that sends it back as it came,
        # or that sends neither (one opened before 2026-10-30), keeps what the announcement says: None to
        # `sync_announcement`. None as well when the field did not validate, and the form is not saved.
        published = cleaned_data.get('published_question')
        shown = self.data.get(self.add_initial_prefix('published_question'))
        if (published is None or shown is None or self.add_prefix('published_question') not in self.data or
                plain_text(published) == plain_text(shown)):
            cleaned_data['published_question'] = None
        elif not (cleaned_data.get('is_public', self.instance.is_public) and
                  plain_text(cleaned_data.get('answer', self.instance.answer))):
            # Only a public answer carries it. Saved with a private or empty one, the change went nowhere: withdrawing
            # the announcement keeps what it said, and a later publication took the question as it was asked.
            self.add_error('published_question', _('The question as published only changes along with a public '
                                                   'answer. Write the answer and make it public, or leave it as it '
                                                   'was.'))
        return cleaned_data


class ContestClarificationAdmin(admin.ModelAdmin):
    form = ContestClarificationForm
    fields = ('contest', 'problem', 'user', 'question', 'answer', 'is_public', 'published_question')
    list_display = ('asked', 'contest', 'user', 'summary', 'answered', 'is_public')
    # Only the contests with a question this person can see: a plain 'contest' offered every contest by name.
    list_filter = ('is_public', ('contest', admin.RelatedOnlyFieldListFilter))
    search_fields = ('question', 'answer')
    date_hierarchy = 'asked'
    raw_id_fields = ('user', 'problem')

    def summary(self, obj):
        return obj.question if len(obj.question) <= 80 else obj.question[:77] + '...'
    summary.short_description = _('question')

    def published_question(self, obj):
        # Shown instead of the field to whoever may only look.
        return obj.question_as_published()
    published_question.short_description = _('question as published')

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        # A new question goes out as it is written; rewriting it for publication is for one that exists.
        return fields if obj is not None else tuple(field for field in fields if field != 'published_question')

    def editable_contests(self, request):
        """The contests this account may write a clarification into.

        Same rule the rest of the admin uses: your own contests only.
        """
        from judge.models import Contest

        contests = Contest.objects.all()
        if request.user.has_perm('judge.edit_all_contest'):
            return contests
        return contests.filter(
            Q(authors=request.profile) | Q(curators=request.profile),
        ).distinct()

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related('contest', 'user__user', 'answered_by__user')
        if request.user.has_perm('judge.edit_all_contest'):
            return queryset
        return queryset.filter(
            Q(contest__authors=request.profile) | Q(contest__curators=request.profile),
        ).distinct()

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Narrowing which rows can be opened was never enough: the destination is
        # chosen by the `contest` field, and the browser can name any contest at
        # all. Restricting the field's queryset is what rejects a foreign one on
        # the server, both when adding and when changing, because that is where
        # a ModelChoiceField validates its value.
        if 'contest' in form.base_fields:
            form.base_fields['contest'].queryset = self.editable_contests(request)
            form.base_fields['contest'].required = True
        return form

    def save_model(self, request, obj, form, change):
        # The tab is the normal way to answer; this keeps the record straight for
        # anyone who answers from the admin instead.
        # The name shown with an answer is whoever wrote the text on show (aclaraciones-anuncios-9): rewriting it
        # makes whoever saves it the one who answered, and its announcement follows. Making it public or private, or
        # touching the question, does not. Who did what stays in the history of the administration.
        before = ContestClarification.objects.filter(pk=obj.pk).values_list('answer', flat=True).first() \
            if change else None
        if obj.answer and (obj.answered is None or plain_text(obj.answer) != plain_text(before)):
            if obj.answered is None:
                obj.answered = timezone.now()
            obj.answered_by = request.profile
        super().save_model(request, obj, form, change)
        # And it publishes or withdraws through the same helper as the tab. Saving
        # a public answer here used to reach nobody —the tab hides it, expecting
        # an announcement that was never created— and turning one private used to
        # leave the announcement behind.
        published = form.cleaned_data.get('published_question') if form is not None else None
        obj.sync_announcement(author=request.profile, published_question=published)
