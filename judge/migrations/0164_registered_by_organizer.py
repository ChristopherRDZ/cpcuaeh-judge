# Written by hand on 2026-11-04, from what makemigrations proposes for these two fields.

from django.db import migrations, models


class Migration(migrations.Migration):
    """Registering others ahead, by whoever organizes the contest, and who of a contest on site and online is rated
    (2026-11-04).

    * One column on judge_contestparticipation: whether whoever organizes the contest made the registration
      (`by_organizer`), from the tab «Registrados». They answer for it: the access code, the privacy and the
      organisations of the contest are not asked of it. False by default, also in the database, so the code before
      this change can still create participations; it never reads it.
    * One column on judge_contest: who of a contest on site and online at once gets a rating (`rating_modality`):
      everybody, only on site, or only online. «Everybody» by default, also in the database, so every contest from
      before is rated as it was and the code before this change can still create contests.

    Going back drops the two columns. The registrations whoever organizes made stay as registrations, as if each one
    had registered: with the code before this change, entering asks again for the privacy and the organisations of
    the contest, never for the code.

    Only these two AddField: the autodetector also proposes the `AlterField`s on existing tables (choices and help
    texts that drifted long before, the same ones left out of 0157 to 0163), which have nothing to do with this.
    """

    dependencies = [
        ('judge', '0163_duel_panel'),
    ]

    operations = [
        migrations.AddField(
            model_name='contestparticipation',
            name='by_organizer',
            field=models.BooleanField(db_default=False, default=False, verbose_name='Registrado por la organización'),
        ),
        migrations.AddField(
            model_name='contest',
            name='rating_modality',
            field=models.CharField(choices=[('all', 'Todos'), ('onsite', 'Sólo presencial'), ('online', 'Sólo en línea')], db_default='all', default='all', help_text='Sólo en concursos «Presencial y en línea». En los demás cuentan todos.', max_length=10, verbose_name='Cuentan para el rating'),
        ),
    ]
