# Written by hand on 2026-11-10, from what makemigrations proposes for this field.

from django.db import migrations, models


class Migration(migrations.Migration):
    """Whether a contest has a public scoreboard (2026-11-10).

    * One column on judge_contest: whether anybody, without an account, can see the scoreboard of the contest as it
      happens at /marcador/<key>/ (`public_scoreboard`). False by default, also in the database, so every contest
      from before stays without one and the code before this change can still create contests; it never reads it.

    Going back drops the column: with the code before this change there is no such page.

    Only this AddField: the autodetector also proposes the `AlterField`s on existing tables (choices and help texts
    that drifted long before, the same ones left out of 0157 to 0167), which have nothing to do with this.
    """

    dependencies = [
        ('judge', '0167_contest_autocomplete'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='public_scoreboard',
            field=models.BooleanField(db_default=False, default=False, help_text='Cualquiera puede ver el marcador en vivo, sin cuenta, en /marcador/ seguido de la clave del concurso: usuario, nombre y foto de cada participante. Respeta la congelación. Sólo con el concurso visible.', verbose_name='marcador público'),
        ),
    ]
