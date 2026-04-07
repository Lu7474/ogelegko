from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("exam", "0004_task_number_to_charfield"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="manual_grading",
            field=models.BooleanField(default=False, verbose_name="Ручная проверка"),
        ),
        migrations.AlterField(
            model_name="answer",
            name="is_correct",
            field=models.BooleanField(
                blank=True, default=False, null=True, verbose_name="Правильно"
            ),
        ),
    ]
