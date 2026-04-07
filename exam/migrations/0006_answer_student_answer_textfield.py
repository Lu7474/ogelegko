from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("exam", "0005_manual_grading"),
    ]

    operations = [
        migrations.AlterField(
            model_name="answer",
            name="student_answer",
            field=models.TextField(blank=True, verbose_name="Ответ ученика"),
        ),
    ]
