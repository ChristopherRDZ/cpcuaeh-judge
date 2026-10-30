from django.db import connection
from django.db.models import OuterRef, Subquery
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _

from judge.models import Contest, Language, Submission
from judge.utils.problems import get_result_data
from judge.utils.raw_sql import join_sql_subquery
from judge.views.submission import ForceContestMixin, ProblemSubmissions

__all__ = ['RankedSubmissions', 'ContestRankedSubmission']


class RankedSubmissions(ProblemSubmissions):
    tab = 'best_submissions_list'
    dynamic_update = False

    def get_queryset(self):
        if self.in_contest:
            from judge.contest_teams import ranking_division
            division = ranking_division(self.request, self.contest)
            team_null = division == 'individual'
            # As the original DMOJ did in contests too, and as the list outside a contest does (equipos-globos-v1).
            queryset = super().get_queryset().filter(contest__participation__team__isnull=team_null,
                                                     user__is_unlisted=False)
            # The best of each participation comes from the plain submissions of this problem in this contest, with
            # the same freeze, languages and results as the list. Built from the list itself, it carried the DISTINCT
            # that whoever cannot see the whole scoreboard gets: Django then wrapped the subquery in a derived table,
            # where MariaDB cannot see the outer column, and the page gave 500 (equipos-globos-2, 2026-10-29).
            best = Submission.objects.filter(problem=self.problem, contest_object=self.contest,
                                             contest__participation_id=OuterRef('contest__participation_id'),
                                             contest__participation__team__isnull=team_null,
                                             user__is_unlisted=False, contest__points__gt=0)
            frozen = Contest.frozen_submission_filter(self.request.user)
            if frozen is not None:
                if self.request.user.is_authenticated:
                    best = best.exclude(frozen & ~Submission.ownership_filter(self.request.profile))
                else:
                    best = best.exclude(frozen)
            if self.selected_languages:
                best = best.filter(language__key__in=self.selected_languages)
            if self.selected_statuses:
                best = best.filter(result__in=self.selected_statuses)
            best = best.order_by('-contest__points', 'time', 'id').values('pk')[:1]
            return queryset.filter(pk=Subquery(best)).order_by('-contest__points', 'time', 'id')
        params = [self.problem.id]
        if self.in_contest:
            contest_join = 'INNER JOIN judge_contestsubmission AS cs ON (sub.id = cs.submission_id)'
            points = 'cs.points'
            constraint = ' AND sub.contest_object_id = %s'
            params.append(self.contest.id)
        else:
            contest_join = ''
            points = 'sub.points'
            constraint = (' AND NOT EXISTS (SELECT 1 FROM judge_contestsubmission team_cs '
                          'INNER JOIN judge_contestparticipation team_cp ON team_cp.id = team_cs.participation_id '
                          'WHERE team_cs.submission_id = sub.id AND team_cp.team_id IS NOT NULL)')
            # The best of each user is chosen without what a frozen scoreboard hides from this reader, the same that
            # the list leaves out (never their own): chosen among all of them, somebody who improved during the freeze
            # dropped out of the list, which already told it (congelacion-revelador-3, 2026-10-29). Written so that a
            # submission outside any contest passes: NOT (contest_object_id = %s AND ...) is NULL for it.
            own = self.request.profile.id if self.request.user.is_authenticated else None
            for contest_id, cutoff in Contest.frozen_windows_for(self.request.user):
                constraint += ' AND (sub.contest_object_id IS NULL OR sub.contest_object_id <> %s OR sub.date < %s'
                params.extend([contest_id, connection.ops.adapt_datetimefield_value(cutoff)])
                if own is not None:
                    constraint += ' OR sub.user_id = %s'
                    params.append(own)
                constraint += ')'

        if self.selected_languages:
            lang_ids = Language.objects.filter(key__in=self.selected_languages).values_list('id', flat=True)
            if lang_ids:
                constraint += f' AND sub.language_id IN ({", ".join(["%s"] * len(lang_ids))})'
                params.extend(lang_ids)
            self.selected_languages = set()

        queryset = super(RankedSubmissions, self).get_queryset().filter(user__is_unlisted=False,
                                                                     contest__participation__team__isnull=True)
        join_sql_subquery(
            queryset,
            subquery="""
                SELECT sub.id AS id
                FROM (
                    SELECT sub.user_id AS uid, MAX(sub.points) AS points
                    FROM judge_submission AS sub {contest_join}
                    WHERE sub.problem_id = %s AND {points} > 0 {constraint}
                    GROUP BY sub.user_id
                ) AS highscore STRAIGHT_JOIN (
                    SELECT sub.user_id AS uid, sub.points, MIN(sub.time) as time
                    FROM judge_submission AS sub {contest_join}
                    WHERE sub.problem_id = %s AND {points} > 0 {constraint}
                    GROUP BY sub.user_id, {points}
                ) AS fastest ON (highscore.uid = fastest.uid AND highscore.points = fastest.points)
                    STRAIGHT_JOIN judge_submission AS sub
                        ON (sub.user_id = fastest.uid AND sub.time = fastest.time)
                WHERE sub.problem_id = %s {constraint}
                GROUP BY sub.user_id
            """.format(points=points, contest_join=contest_join, constraint=constraint),
            params=params * 3, alias='best_subs', join_fields=[('id', 'id')], related_model=Submission,
        )

        if self.in_contest:
            return queryset.order_by('-contest__points', 'time')
        else:
            return queryset.order_by('-points', 'time')

    def get_title(self):
        return _('Best solutions for %s') % self.problem_name

    def get_content_title(self):
        return mark_safe(escape(_('Best solutions for %s')) % (
            format_html('<a href="{1}">{0}</a>', self.problem_name,
                        reverse('problem_detail', args=[self.problem.code])),
        ))

    def _get_result_data(self, queryset=None):
        if queryset is None:
            queryset = super(RankedSubmissions, self).get_queryset()
        return get_result_data(queryset.order_by())


class ContestRankedSubmission(ForceContestMixin, RankedSubmissions):
    def get_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return _('Best solutions for %(problem)s in %(contest)s') % {
                'problem': self.problem_name, 'contest': self.contest.name,
            }
        return _('Best solutions for problem %(number)s in %(contest)s') % {
            'number': self.get_problem_number(self.problem), 'contest': self.contest.name,
        }

    def get_content_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return mark_safe(escape(_('Best solutions for %(problem)s in %(contest)s')) % {
                'problem': format_html('<a href="{1}">{0}</a>', self.problem_name,
                                       reverse('problem_detail', args=[self.problem.code])),
                'contest': format_html('<a href="{1}">{0}</a>', self.contest.name,
                                       reverse('contest_view', args=[self.contest.key])),
            })
        return mark_safe(escape(_('Best solutions for problem %(number)s in %(contest)s')) % {
            'number': self.get_problem_number(self.problem),
            'contest': format_html('<a href="{1}">{0}</a>', self.contest.name,
                                   reverse('contest_view', args=[self.contest.key])),
        })

    def _get_queryset(self):
        return super()._get_queryset().filter(contest_object=self.contest)
