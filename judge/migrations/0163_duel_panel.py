# Written by hand on 2026-11-03, as makemigrations would, for the new battle panel.

from django.db import migrations, models


class Migration(migrations.Migration):
    """The new battle panel of the duels (2026-11-03).

    * `judge_arenascreen.view`: how a projector shows what it shows, chosen from the panel. A default in the database
      too ('combined', what every screen showed until now), so the code before this change can still insert screens.
    * `judge_arenatournament.places`: how many places the bracket of a draft has, so it can be prepared before the
      names are known. NULL, as every tournament before this change, means one place per participant.

    Two tables of a few rows; nothing is copied or rewritten. The code before this change ignores both columns.
    """

    dependencies = [
        ('judge', '0162_contest_attendance'),
    ]

    operations = [
        migrations.AddField(
            model_name='arenascreen',
            name='view',
            field=models.CharField(choices=[('combined', 'Everything'), ('scoreboard', 'Scoreboard'), ('code-a', 'Code A'), ('code-b', 'Code B'), ('statements', 'Statements'), ('bracket', 'Bracket')], db_default='combined', default='combined', max_length=12, verbose_name='view'),
        ),
        migrations.AddField(
            model_name='arenatournament',
            name='places',
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='places'),
        ),
    ]
