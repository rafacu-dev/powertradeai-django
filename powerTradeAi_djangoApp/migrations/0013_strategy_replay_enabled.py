from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("powertradeai", "0012_investepdecision_validacion"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategy",
            name="replay_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Permite evaluar esta regla en replay visual/overlay sin "
                    "activarla en live."
                ),
            ),
        ),
    ]
