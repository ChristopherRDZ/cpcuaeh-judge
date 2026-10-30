import logging
from collections import namedtuple
from random import random
from threading import RLock

from judge.judge_priority import REJUDGE_PRIORITY

try:
    from llist import dllist
except ImportError:
    from pyllist import dllist

logger = logging.getLogger('judge.bridge')

PriorityMarker = namedtuple('PriorityMarker', 'priority')

# CPC (problemas-prueba-pdf-2, 2026-10-30): the custom test (its problems are ct_...) runs on the judges whose name
# starts with this, whichever is free or frees up first; only while none of them can run it (none connected and
# enabled, or none with its language), on any judge. The site used to ask for the first of them by name, so every test
# waited for that one even with the others free, and for good if it was disabled.
CUSTOM_TEST_JUDGES = 'cpc-custom-'


class JudgeList(object):
    priorities = 4

    def __init__(self):
        self.queue = dllist()
        self.priority = [self.queue.append(PriorityMarker(i)) for i in range(self.priorities)]
        self.judges = set()
        self.node_map = {}
        self.submission_map = {}
        self.lock = RLock()
        self.min_tier = None

    def _handle_free_judge(self, judge):
        with self.lock:
            if judge.tier > self.min_tier:
                return

            node = self.queue.first
            priority = 0
            while node:
                if isinstance(node.value, PriorityMarker):
                    priority = node.value.priority + 1
                elif priority >= REJUDGE_PRIORITY and self.should_reserve_judge():
                    return
                else:
                    id, problem, language, source, judge_id = node.value
                    if self._can_judge(judge, problem, language, judge_id):
                        self.submission_map[id] = judge
                        try:
                            judge.submit(id, problem, language, source)
                        except Exception:
                            logger.exception('Failed to dispatch %d (%s, %s) to %s', id, problem, language, judge.name)
                            self.judges.remove(judge)
                            return
                        logger.info('Dispatched queued submission %d: %s', id, judge.name)
                        self.queue.remove(node)
                        del self.node_map[id]
                        break
                node = node.next

    def _update_min_tier(self):
        with self.lock:
            old = self.min_tier
            try:
                self.min_tier = min(judge.tier for judge in self.judges
                                    if judge.tier is not None and not judge.is_disabled)
            except ValueError:
                self.min_tier = None

            if old != self.min_tier:
                logger.info('Minimum tier changed from %s to %s', old, self.min_tier)

            # We must be adding a judge, let register handle the new judge.
            if old is None:
                return

            # If the new min tier is larger, then we should treat all judges of the new tier as free and start grading.
            # This is only possible when removing a judge.
            if self.min_tier is not None and self.min_tier > old:
                for judge in self.current_tier_judges():
                    logger.info('Minimum tier increased, trying to dispatch to judge: %s', judge.name)
                    if not judge.working:
                        self._handle_free_judge(judge)

    def current_tier_judges(self):
        return [judge for judge in self.judges if judge.tier == self.min_tier and not judge.is_disabled]

    def _can_judge(self, judge, problem, language, judge_id):
        if not judge.can_judge(problem, language, judge_id):
            return False
        # A custom test asked of no judge in particular: one of CUSTOM_TEST_JUDGES while any of them can run it.
        if judge_id or not problem.startswith('ct_') or judge.name.startswith(CUSTOM_TEST_JUDGES):
            return True
        return not any(other.name.startswith(CUSTOM_TEST_JUDGES) and other.can_judge(problem, language)
                       for other in self.current_tier_judges())

    def _offer_queue(self):
        # CPC (problemas-prueba-pdf-2): what a free judge may take can change without it freeing up, since a custom test
        # waits for the cpc-custom-* judges only while one of them can run it. When judges are disabled, enabled or
        # leave, the queue is offered again to every free judge of the current tier; _handle_free_judge keeps the
        # reservation for rejudges.
        for judge in self.current_tier_judges():
            if not judge.working:
                self._handle_free_judge(judge)

    def should_reserve_judge(self):
        judges = self.current_tier_judges()
        if len(judges) <= 1:
            return False

        free_judges = sum(not judge.working for judge in judges)
        return free_judges <= 1

    def register(self, judge):
        with self.lock:
            # Disconnect all judges with the same name, see <https://github.com/DMOJ/online-judge/issues/828>
            self.disconnect(judge, force=True)
            self.judges.add(judge)
            self._update_min_tier()
            self._handle_free_judge(judge)

    def disconnect(self, judge_id, force=False):
        with self.lock:
            for judge in self.judges:
                if judge.name == judge_id:
                    judge.disconnect(force=force)

    def update_problems(self, judge):
        with self.lock:
            self._handle_free_judge(judge)

    def update_disable_judge(self, judge_id, is_disabled):
        with self.lock:
            for judge in self.judges:
                if judge.name == judge_id:
                    judge.is_disabled = is_disabled
            self._update_min_tier()
            # CPC (problemas-prueba-pdf-2): a judge enabled again takes at once what it can judge from the queue, as one
            # that connects does, and disabling the custom judges hands what waited for them to the others.
            self._offer_queue()

    def remove(self, judge):
        with self.lock:
            sub = judge.get_current_submission()
            if sub is not None:
                try:
                    del self.submission_map[sub]
                except KeyError:
                    pass
            self.judges.discard(judge)
            self._update_min_tier()

            # Since we reserve a judge for high priority submissions when there are more than one,
            # we'll need to start judging if there is exactly one judge and it's free.
            # CPC (problemas-prueba-pdf-2): and any free judge may now take a custom test that waited for the judges
            # that left, so the queue goes to every free judge (with one left, the same as before).
            self._offer_queue()

    def __iter__(self):
        return iter(self.judges)

    def on_judge_free(self, judge, submission):
        logger.info('Judge available after grading %d: %s', submission, judge.name)
        with self.lock:
            del self.submission_map[submission]
            judge._working = False
            self._handle_free_judge(judge)

    def abort(self, submission):
        logger.info('Abort request: %d', submission)
        with self.lock:
            try:
                self.submission_map[submission].abort()
                return True
            except KeyError:
                try:
                    node = self.node_map[submission]
                except KeyError:
                    pass
                else:
                    self.queue.remove(node)
                    del self.node_map[submission]
                return False

    def check_priority(self, priority):
        return 0 <= priority < self.priorities

    def judge(self, id, problem, language, source, judge_id, priority):
        with self.lock:
            if id in self.submission_map or id in self.node_map:
                # Already judging, don't queue again. This can happen during batch rejudges, rejudges should be
                # idempotent.
                return

            candidates = [judge for judge in self.current_tier_judges()
                          if self._can_judge(judge, problem, language, judge_id)]
            available = [judge for judge in candidates if not judge.working and not judge.is_disabled]
            if judge_id:
                logger.info('Specified judge %s is%savailable', judge_id, ' ' if available else ' not ')
            else:
                logger.info('Free judges: %d', len(available))

            if len(candidates) > 1 and len(available) == 1 and priority >= REJUDGE_PRIORITY:
                available = []

            if available:
                # Schedule the submission on the judge reporting least load.
                judge = min(available, key=lambda judge: (judge.load, random()))
                logger.info('Dispatched submission %d to: %s', id, judge.name)
                self.submission_map[id] = judge
                try:
                    judge.submit(id, problem, language, source)
                except Exception:
                    logger.exception('Failed to dispatch %d (%s, %s) to %s', id, problem, language, judge.name)
                    self.judges.discard(judge)
                    return self.judge(id, problem, language, source, judge_id, priority)
            else:
                self.node_map[id] = self.queue.insert(
                    (id, problem, language, source, judge_id),
                    self.priority[priority],
                )
                logger.info('Queued submission: %d', id)
