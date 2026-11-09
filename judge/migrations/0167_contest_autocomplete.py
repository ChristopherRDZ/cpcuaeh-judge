# Written by hand on 2026-11-08, from what makemigrations proposes for this field.

from django.db import migrations, models


class Migration(migrations.Migration):
    """Whether the editor autocompletes inside a contest (2026-11-08).

    * One column on judge_contest: whether whoever organizes the contest allows the editor to suggest words of the
      code, keywords and headers on the submit page and the custom test while someone is in it (`autocomplete`).
      False by default, also in the database, so every contest from before stays without it and the code before this
      change can still create contests; it never reads it.

    Going back drops the column: with the code before this change no editor autocompletes, inside contests or out.

    Only this AddField: the autodetector also proposes the `AlterField`s on existing tables (choices and help texts
    that drifted long before, the same ones left out of 0157 to 0166), which have nothing to do with this.
    """

    dependencies = [
        ('judge', '0166_arena_tournament_cancelled'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='autocomplete',
            field=models.BooleanField(db_default=False, default=False, help_text='The editor suggests words of the code, keywords and headers while typing. Each person can turn it off in their editor.', verbose_name='allow autocompletion'),
        ),
    ]
