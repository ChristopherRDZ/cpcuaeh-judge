# Written by hand on 2026-11-06, from what makemigrations proposes for this field.

from django.db import migrations, models


def forward(apps, schema_editor):
    """Tournaments cancelled before going back to the code before this migration, which archived them (backward),
    are cancelled again: a cancelled tournament leaves a 'tournament_cancel' action, and only a finished one can be
    archived. And never count, even if the code before marked them «counts» meanwhile (then `rate_duels` works the
    ranking out again)."""
    ArenaTournament = apps.get_model('judge', 'ArenaTournament')
    ArenaAction = apps.get_model('judge', 'ArenaAction')
    cancelled = ArenaAction.objects.filter(action='tournament_cancel').values('tournament_id')
    ArenaTournament.objects.filter(status='archived', id__in=cancelled).update(status='cancelled', ranking='excluded')


def backward(apps, schema_editor):
    """The code before this migration knows no cancelled tournament; an archived one is what it shows best: out of
    the panel, and out of the history too, since cancelling hid it."""
    ArenaTournament = apps.get_model('judge', 'ArenaTournament')
    ArenaTournament.objects.filter(status='cancelled').update(status='archived')


class Migration(migrations.Migration):
    """A tournament can be cancelled (2026-11-06): one more status, 'cancelled'.

    Only the choices of `judge_arenatournament.status` change, so the table is not touched (the column was already
    a varchar(10)). The code before this change reads a cancelled tournament as an unknown status; going back
    archives them, and coming forward again cancels them again.
    """

    dependencies = [
        ('judge', '0165_mass_email_review'),
    ]

    operations = [
        migrations.AlterField(
            model_name='arenatournament',
            name='status',
            field=models.CharField(choices=[('draft', 'Draft'), ('running', 'In progress'), ('finished', 'Finished'), ('archived', 'Archived'), ('cancelled', 'Cancelled')], db_index=True, default='draft', max_length=10, verbose_name='status'),
        ),
        migrations.RunPython(forward, backward),
    ]
