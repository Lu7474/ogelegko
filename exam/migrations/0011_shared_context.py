from django.db import migrations, models

import exam.models


class Migration(migrations.Migration):

    dependencies = [
        ("exam", "0010_alter_catalogimportsession_source_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="shared_context",
            field=models.TextField(blank=True, verbose_name="Общее условие"),
        ),
        migrations.AddField(
            model_name="task",
            name="shared_context_image",
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to="contexts/",
                validators=[exam.models.validate_image_size],
                verbose_name="Изображение общего условия",
            ),
        ),
        migrations.AddField(
            model_name="catalogtask",
            name="shared_context",
            field=models.TextField(blank=True, verbose_name="Общее условие"),
        ),
        migrations.AddField(
            model_name="catalogtask",
            name="shared_context_image",
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to="contexts/",
                validators=[exam.models.validate_image_size],
                verbose_name="Изображение общего условия",
            ),
        ),
    ]
