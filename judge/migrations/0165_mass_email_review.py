# Written by hand on 2026-11-05, from what makemigrations proposes for these fields.

from django.db import migrations, models

# The names Django keeps for the two permissions that existed already. A permission's name is written when it is
# created and never again, so changing it in Meta.permissions alone would leave the old one in the admin.
NAMES = {
    'send_mass_email': ('Write and send own emails to users',
                        'Escribir correos a usuarios propios y pedir su aprobación'),
    'edit_all_mass_email': ('Edit and send all emails to users', 'Editar todos los correos a usuarios'),
}
NEW_CODENAMES = ('approve_mass_email',)


def forward(apps, schema_editor):
    Permission = apps.get_model('auth', 'Permission')
    for codename, (old, new) in NAMES.items():
        Permission.objects.filter(content_type__app_label='judge', codename=codename, name=old).update(name=new)


def backward(apps, schema_editor):
    Permission = apps.get_model('auth', 'Permission')
    for codename, (old, new) in NAMES.items():
        Permission.objects.filter(content_type__app_label='judge', codename=codename, name=new).update(name=old)
    # The code before this migration does not know the new permission: it goes, and with it whoever had it.
    Permission.objects.filter(content_type__app_label='judge', codename__in=NEW_CODENAMES).delete()
    # Nor an email waiting for approval: it goes back to being a draft, which that code shows and edits.
    MassEmail = apps.get_model('judge', 'MassEmail')
    MassEmail.objects.filter(status='review').update(status='draft')


class Migration(migrations.Migration):
    """Emails to users with approval, senders and addresses without an account (2026-11-05).

    * Writing is not sending: `judge.send_mass_email` now writes one's own emails and asks for approval, and the new
      `judge.approve_mass_email` sends them (an email waiting for approval, or one's own). The names of the two
      permissions that existed are updated; the new one is created by Django after migrating, also with the code
      before this change (it reads it from here).
    * Three columns on judge_massemail, a table of a few rows: the addresses without an account pasted in the email
      (`external_emails`), when it was sent to be approved and the note of whoever sends it back. All three allow
      NULL, so the code before this change, which never reads them, can still insert emails.
    * One column on judge_massemailrecipient: whether the row is one of those addresses (`external`), False by
      default, also in the database, for the rows the code before this change inserts.

    The status of an email waiting for approval (`review`) is a new value of a column that exists: no change to the
    table. Going back turns those emails into drafts again and drops the new columns and the new permission.

    Only these operations: the autodetector also proposes the `AlterField`s on existing tables (choices and help
    texts that drifted long before, the same ones left out of 0157 to 0164), which have nothing to do with this.
    """

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('judge', '0164_registered_by_organizer'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='massemail',
            options={'ordering': ('-id',), 'permissions': (('send_mass_email', 'Escribir correos a usuarios propios y pedir su aprobación'), ('edit_all_mass_email', 'Editar todos los correos a usuarios'), ('approve_mass_email', 'Aprobar y enviar correos a usuarios')), 'verbose_name': 'email to users', 'verbose_name_plural': 'emails to users'},
        ),
        migrations.AddField(
            model_name='massemail',
            name='external_emails',
            field=models.TextField(blank=True, null=True, verbose_name='Más correos'),
        ),
        migrations.AddField(
            model_name='massemail',
            name='review_requested_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Enviado a aprobar'),
        ),
        migrations.AddField(
            model_name='massemail',
            name='review_note',
            field=models.TextField(blank=True, null=True, verbose_name='Nota de quien aprueba'),
        ),
        migrations.AddField(
            model_name='massemailrecipient',
            name='external',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RunPython(forward, backward),
    ]
