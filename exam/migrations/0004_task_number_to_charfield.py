from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("exam", "0003_add_topic_and_class_active"),
    ]

    operations = [
        # Убираем unique_together чтобы можно было изменить тип
        migrations.AlterUniqueTogether(
            name="task",
            unique_together=set(),
        ),
        # Меняем тип поля
        migrations.AlterField(
            model_name="task",
            name="number",
            field=models.CharField(max_length=20, verbose_name="Номер задания"),
        ),
        # Возвращаем unique_together
        migrations.AlterUniqueTogether(
            name="task",
            unique_together={("variant", "number")},
        ),
        # Меняем ordering на id
        migrations.AlterModelOptions(
            name="task",
            options={
                "ordering": ["variant", "id"],
                "verbose_name": "Задание",
                "verbose_name_plural": "Задания",
            },
        ),
    ]
