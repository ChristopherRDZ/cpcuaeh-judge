# Written by hand on 2026-10-31, from what makemigrations proposes for these three fields.

from django.db import migrations, models


class Migration(migrations.Migration):
    """Registering ahead, and contests on site and online (2026-10-31).

    * One column on judge_contest: where it is competed (`attendance_mode`): only online, only on site, or both, the
      only one that splits the scoreboard into Global, Presencial and En línea. «Only online» by default, also in the
      database, so every contest from before stays as it was and the code before this change can still create them.
    * One more on judge_contest: the list of who competes on site (`onsite_list`), named ahead by whoever organizes,
      also before they register or join. Empty (NULL) by default, so the code before this change can still create
      contests; it never reads it.
    * One column on judge_contestparticipation: whether the participation is on site (`onsite`), false by default,
      also in the database, so the code before this change can still create participations.

    A registration made ahead is a participation with `virtual = -2` (ContestParticipation.REGISTERED): no column
    and no table for it. The code before this change never creates one, and reads none: everything it shows filters
    `virtual = 0`, or `virtual >= 0` where it lists the participations of a person. Going back drops the three columns;
    the registrations that were left stay as inert rows, which the code before this change neither shows nor counts.

    Only these three AddField: the autodetector also proposes the `AlterField`s on existing tables (choices and help
    texts that drifted long before, the same ones left out of 0157 to 0161), which have nothing to do with this.
    """

    dependencies = [
        ('judge', '0161_duel_ranking'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='attendance_mode',
            field=models.CharField(choices=[('online', 'Sólo en línea'), ('onsite', 'Sólo presencial'), ('hybrid', 'Presencial y en línea')], db_default='online', default='online', help_text='Con «Presencial y en línea», el ranking se separa en Global, Presencial y En línea, y en la pestaña «Registrados» del concurso colocas a cada quien en su modalidad; todos empiezan en línea, salvo quienes ya pusiste en la lista de la sede. Con las otras dos, todos compiten juntos, como siempre. «Presencial y en línea» se puede poner en cualquier momento, pero no quitar una vez empezado el concurso.', max_length=10, verbose_name='Presencial o en línea'),
        ),
        migrations.AddField(
            model_name='contest',
            name='onsite_list',
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='contestparticipation',
            name='onsite',
            field=models.BooleanField(db_default=False, default=False, verbose_name='Presencial'),
        ),
    ]
