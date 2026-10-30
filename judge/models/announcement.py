from django.db import models
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from judge.models.contest import Contest
from judge.models.profile import Profile

__all__ = ['Announcement']

# On the announcement of a public answer, `body` is not text of its own: it is the question as everybody reads it
# above the answer (aclaraciones-anuncios-3, 2026-10-30). The jury can rewrite it when publishing, so that what one
# team asked does not give away how it is solving a problem, while whoever asked keeps seeing their own words. Empty,
# as on every answer published before, means the question as it was asked; a body of nothing but blanks, this one,
# means that the jury left the question out and only the answer is shown. No field was added, so no migration.
QUESTION_LEFT_OUT = '\n'


class Announcement(models.Model):
    contest = models.ForeignKey(Contest, verbose_name=_('contest'), null=True, blank=True,
                                on_delete=models.CASCADE, related_name='announcements',
                                help_text=_('Leave empty to announce to the whole site. Otherwise the announcement '
                                            'only reaches that contest, and is kept on its announcements tab.'))
    body = models.TextField(verbose_name=_('announcement'), blank=True,
                            help_text=_('Shown as plain text: HTML and Markdown are not interpreted. Leave empty '
                                        'only on an announcement that carries a clarification.'))
    # When the jury answers a question in public, the announcement points at the
    # clarification instead of copying its text. That way the page can label
    # which half is the question and which is the answer, in the reader's own
    # language, and the same words are not stored twice.
    clarification = models.ForeignKey('ContestClarification', verbose_name=_('clarification'), null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name='announcements')
    author = models.ForeignKey(Profile, verbose_name=_('author'), null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='announcements')
    created = models.DateTimeField(verbose_name=_('creation time'), auto_now_add=True, db_index=True)
    expires = models.DateTimeField(verbose_name=_('stop showing at'), null=True, blank=True, db_index=True,
                                   help_text=_('After this moment the announcement stops popping up. It is still '
                                               'kept on the contest tab. Empty means it never stops.'))
    is_visible = models.BooleanField(verbose_name=_('is visible'), default=True,
                                     help_text=_('Uncheck to withdraw an announcement without deleting it, so there '
                                                 'is still a record that it was sent.'))

    @property
    def published_question(self):
        """The question this announcement shows above its answer, or '' when it shows the answer alone (and on an
        announcement that carries no clarification). See QUESTION_LEFT_OUT."""
        if self.clarification_id is None:
            return ''
        if not self.body:
            return self.clarification.question
        return self.body.strip()

    def __str__(self):
        where = self.contest.name if self.contest_id else _('Site')
        # What everybody was shown, never the question as asked when the jury rewrote it or left it out.
        if self.clarification_id:
            text = self.published_question or self.clarification.answer
        else:
            text = self.body
        if len(text) > 60:
            text = text[:57] + '...'
        return '%s: %s' % (where, text)

    class Meta:
        ordering = ['-created']
        permissions = (
            ('announce_site', _('Announce to the whole site')),
        )
        verbose_name = _('announcement')
        verbose_name_plural = _('announcements')


@receiver(pre_save, sender=Contest, dispatch_uid='announcements_note_contest_end')
def note_contest_end(sender, instance, raw=False, update_fields=None, **kwargs):
    """Note where the contest ended before this save, when the save moves its end (follow_contest_end)."""
    instance._announcements_old_end = None
    if raw or instance.pk is None or (update_fields is not None and 'end_time' not in update_fields):
        return
    old_end = sender.objects.filter(pk=instance.pk).values_list('end_time', flat=True).first()
    if old_end is not None and old_end != instance.end_time:
        instance._announcements_old_end = old_end


@receiver(post_save, sender=Contest, dispatch_uid='announcements_follow_contest_end')
def follow_contest_end(sender, instance, raw=False, **kwargs):
    """A contest announcement that stops with its contest keeps doing so when the end of the contest moves.

    The end is copied into `expires` when the announcement is written —by the form of the administration and by
    `ContestClarification.sync_announcement`— and nothing moved it afterwards: a contest extended after a crash stopped
    popping up its announcements and public answers at the old end while it went on (aclaraciones-anuncios-5,
    2026-10-30). Only the ones that stopped exactly at the old end follow it; one given a time of its own keeps it. An
    update that skips `save()` (the duels move the end of their hidden contests that way) does not get here, and
    nothing announces to those contests.
    """
    old_end = getattr(instance, '_announcements_old_end', None)
    instance._announcements_old_end = None
    if raw or old_end is None:
        return
    Announcement.objects.filter(contest_id=instance.pk, expires=old_end).update(expires=instance.end_time)
