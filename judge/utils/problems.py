from collections import defaultdict
from math import e

from django.core.cache import cache
from django.db.models import Case, Count, ExpressionWrapper, F, Q, When
from django.db.models.fields import FloatField
from django.utils import timezone
from django.utils.translation import gettext_noop

from judge.models import Contest, Problem, Submission

__all__ = ['contest_completed_ids', 'get_result_data', 'user_completed_ids', 'user_editable_ids', 'user_tester_ids']


def public_submissions_filter(prefix=''):
    """Q matching, through `prefix`, the submissions that no frozen scoreboard hides from the public, or None while no
    freeze hides anything (then the queries of always run).

    Contest.public_freeze_filter seen from another model, for what everybody reads alike: the popular problems and the
    statistics by language (problemas-prueba-pdf-5, 2026-10-30). Inside an aggregate, so that the condition applies
    to each joined submission.
    """
    windows = Contest.unrevealed_freezes()
    hidden = Q()
    for contest_id, cutoff in windows:
        hidden |= Q(**{prefix + 'contest_object_id': contest_id, prefix + 'date__gte': cutoff})
    return ~hidden if windows else None


def user_tester_ids(profile):
    return set(Problem.testers.through.objects.filter(profile=profile).values_list('problem_id', flat=True))


def user_editable_ids(profile):
    return set(Problem.get_editable_problems(profile.user).values_list('id', flat=True))


def contest_completed_ids(participation):
    key = 'contest_complete:%d' % participation.id
    result = cache.get(key)
    if result is None:
        result = set(participation.submissions.filter(submission__result='AC', points__gte=F('problem__points'))
                     .values_list('problem__problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def user_completed_ids(profile):
    key = 'user_complete:%d' % profile.id
    result = cache.get(key)
    if result is None:
        result = set(Submission.objects.filter(user=profile, result='AC', case_points__gte=F('case_total'),
                                              contest__participation__team__isnull=True)
                     .values_list('problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def contest_attempted_ids(participation):
    key = 'contest_attempted:%s' % participation.id
    result = cache.get(key)
    if result is None:
        result = set(participation.submissions.values_list('problem__problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def user_attempted_ids(profile):
    key = 'user_attempted:%s' % profile.id
    result = cache.get(key)
    if result is None:
        result = set(profile.submission_set.filter(contest__participation__team__isnull=True).values_list('problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def _get_result_data(results):
    return {
        'categories': [
            # Using gettext_noop here since this will be tacked into the cache, so it must be language neutral.
            # The caller, SubmissionList.get_result_data will run gettext on the name.
            {'code': 'AC', 'name': gettext_noop('Accepted'), 'count': results['AC']},
            {'code': 'WA', 'name': gettext_noop('Wrong'), 'count': results['WA']},
            {'code': 'CE', 'name': gettext_noop('Compile Error'), 'count': results['CE']},
            {'code': 'TLE', 'name': gettext_noop('Timeout'), 'count': results['TLE']},
            {'code': 'ERR', 'name': gettext_noop('Error'),
             'count': results['MLE'] + results['OLE'] + results['IR'] + results['RTE'] + results['AB'] + results['IE']},
        ],
        'total': sum(results.values()),
    }


def get_result_data(*args, **kwargs):
    if args:
        submissions = args[0]
        if kwargs:
            raise ValueError("Can't pass both queryset and keyword filters")
    else:
        submissions = Submission.objects.filter(**kwargs) if kwargs is not None else Submission.objects
    raw = submissions.values('result').annotate(count=Count('result')).values_list('result', 'count')
    return _get_result_data(defaultdict(int, raw))


def hot_problems(duration, limit):
    cache_key = 'hot_problems:%d:%d' % (duration.total_seconds(), limit)
    qs = cache.get(cache_key)
    if qs is None:
        qs = Problem.get_public_problems() \
                    .filter(submission__date__gt=timezone.now() - duration, points__gt=3, points__lt=25)
        # What a frozen scoreboard still hides does not make a problem popular: the list would tell which problems of
        # the contest are being solved during the freeze (problemas-prueba-pdf-5).
        shown = public_submissions_filter('submission__')
        qs0 = qs.annotate(k=Count('submission__user', distinct=True, filter=shown)).order_by('-k') \
            .values_list('k', flat=True)

        # Nothing at all once every recent submission is behind a freeze.
        if not qs0 or not qs0[0]:
            return []
        # make this an aggregate
        mx = float(qs0[0])

        qs = qs.annotate(unique_user_count=Count('submission__user', distinct=True, filter=shown))
        # fix braindamage in excluding CE
        qs = qs.annotate(submission_volume=Count(Case(
            When(submission__result='AC', then=1),
            When(submission__result='WA', then=1),
            When(submission__result='IR', then=1),
            When(submission__result='RTE', then=1),
            When(submission__result='TLE', then=1),
            When(submission__result='OLE', then=1),
            output_field=FloatField(),
        ), filter=shown))
        qs = qs.annotate(ac_volume=Count(Case(
            When(submission__result='AC', then=1),
            output_field=FloatField(),
        ), filter=shown))
        qs = qs.filter(unique_user_count__gt=max(mx / 3.0, 1))

        qs = qs.annotate(ordering=ExpressionWrapper(
            0.5 * F('points') * (0.4 * F('ac_volume') / F('submission_volume') + 0.6 * F('ac_rate')) +
            100 * e ** (F('unique_user_count') / mx), output_field=FloatField(),
        )).order_by('-ordering').defer('description')[:limit]

        cache.set(cache_key, qs, 900)
    return qs
