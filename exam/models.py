import re

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import models


def validate_image_size(image):
    if image.size > 5 * 1024 * 1024:
        raise ValidationError("Изображение не должно превышать 5 МБ.")
    try:
        from PIL import Image

        img = Image.open(image)
        img.verify()
        image.seek(0)
    except Exception:
        raise ValidationError("Загруженный файл не является изображением.")


class ExamType(models.TextChoices):
    OGE = "oge", "ОГЭ"
    EGE_PROFILE = "ege_profile", "ЕГЭ профиль"
    EGE_BASE = "ege_base", "ЕГЭ база"


EXAM_DURATION = {
    ExamType.OGE: 235,  # 3ч 55мин
    ExamType.EGE_PROFILE: 235,  # 3ч 55мин
    ExamType.EGE_BASE: 180,  # 3ч
}

EXAM_TASK_COUNT = {
    ExamType.OGE: 25,
    ExamType.EGE_PROFILE: 18,
    ExamType.EGE_BASE: 21,
}


class TaskSource(models.TextChoices):
    FIPI = "fipi", "ФИПИ"
    SDAMGIA = "sdamgia", "СдамГИА"
    PRINT_SOLVE = "print_solve", "Распечатай и реши"
    DINAMIKA = "dinamika", "Динамика"
    MANUAL = "manual", "Вручную"


class TaskTopic(models.TextChoices):
    ALGEBRA = "algebra", "Алгебра"
    GEOMETRY = "geometry", "Геометрия"
    PROBABILITY = "probability", "Вероятность и статистика"
    FUNCTIONS = "functions", "Функции"
    EQUATIONS = "equations", "Уравнения и неравенства"
    NUMBER_THEORY = "number_theory", "Теория чисел"
    PRACTICAL = "practical", "Практические задачи"
    OTHER = "other", "Другое"


class SchoolClass(models.Model):
    name = models.CharField("Название", max_length=20, unique=True)
    exam_type = models.CharField("Тип экзамена", max_length=20, choices=ExamType.choices)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Класс"
        verbose_name_plural = "Классы"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_exam_type_display()})"


class Student(models.Model):
    full_name = models.CharField("ФИО", max_length=255, unique=True)
    password_hash = models.CharField("Пароль (хэш)", max_length=255)
    school_class = models.ForeignKey(
        SchoolClass, on_delete=models.CASCADE, verbose_name="Класс", related_name="students"
    )
    session_key = models.CharField("Ключ сессии", max_length=255, blank=True, null=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Ученик"
        verbose_name_plural = "Ученики"
        ordering = ["school_class", "full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.school_class.name})"

    def set_password(self, raw_password):
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password_hash)

    @property
    def exam_type(self):
        return self.school_class.exam_type


class Variant(models.Model):
    number = models.CharField("Номер варианта", max_length=20, unique=True)
    exam_type = models.CharField("Тип экзамена", max_length=20, choices=ExamType.choices)
    is_active = models.BooleanField("Активен", default=True)
    max_attempts = models.PositiveIntegerField(
        "Макс. попыток",
        default=3,
        help_text="0 = без ограничений",
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Вариант"
        verbose_name_plural = "Варианты"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Вариант {self.number} ({self.get_exam_type_display()})"

    @property
    def duration_minutes(self):
        return EXAM_DURATION.get(self.exam_type, 235)


class Task(models.Model):
    variant = models.ForeignKey(
        Variant, on_delete=models.CASCADE, verbose_name="Вариант", related_name="tasks"
    )
    number = models.CharField("Номер задания", max_length=20)
    text = models.TextField("Текст задания", blank=True)
    image = models.ImageField(
        "Изображение", upload_to="tasks/", blank=True, null=True, validators=[validate_image_size]
    )
    correct_answer = models.CharField("Правильный ответ", max_length=255)
    source = models.CharField(
        "Источник", max_length=20, choices=TaskSource.choices, default=TaskSource.MANUAL
    )
    topic = models.CharField(
        "Тема",
        max_length=30,
        choices=TaskTopic.choices,
        default=TaskTopic.OTHER,
        blank=True,
    )
    points = models.PositiveIntegerField("Баллы", default=1)
    manual_grading = models.BooleanField("Ручная проверка", default=False)
    shared_context = models.TextField("Общее условие", blank=True)
    shared_context_image = models.ImageField(
        "Изображение общего условия",
        upload_to="contexts/",
        blank=True,
        null=True,
        validators=[validate_image_size],
    )

    class Meta:
        verbose_name = "Задание"
        verbose_name_plural = "Задания"
        ordering = ["variant", "id"]
        unique_together = ["variant", "number"]

    def __str__(self):
        return f"Вариант {self.variant.number}, задание {self.number}"

    @property
    def answer_hint(self):
        """Подсказка формата ответа на основе правильного ответа."""
        if self.manual_grading:
            return ""
        a = self.correct_answer.strip()
        if not a or a.startswith("Критерии"):
            return ""
        if re.match(r"^-?\d+$", a):
            return "Целое число"
        if re.match(r"^-?\d+[.,]\d+$", a):
            return "Десятичная дробь (через запятую)"
        if re.match(r"^[\d;]+$", a) and ";" in a:
            return "Последовательность чисел через ;"
        if "π" in a:
            return "Введите число без π (например, вместо 27π пишите 27)"
        return ""


class Attempt(models.Model):
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, verbose_name="Ученик", related_name="attempts"
    )
    variant = models.ForeignKey(
        Variant, on_delete=models.CASCADE, verbose_name="Вариант", related_name="attempts"
    )
    started_at = models.DateTimeField("Начало", auto_now_add=True)
    finished_at = models.DateTimeField("Конец", blank=True, null=True)
    is_finished = models.BooleanField("Завершена", default=False)
    score = models.PositiveIntegerField("Первичный балл", default=0)
    max_score = models.PositiveIntegerField("Максимальный балл", default=0)
    grade = models.CharField("Оценка", max_length=20, blank=True)

    class Meta:
        verbose_name = "Попытка"
        verbose_name_plural = "Попытки"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.student.full_name} — Вариант {self.variant.number} ({self.started_at:%d.%m.%Y %H:%M})"

    @property
    def duration_seconds(self):
        if self.finished_at and self.started_at:
            return int((self.finished_at - self.started_at).total_seconds())
        return None

    @property
    def duration_display(self):
        s = self.duration_seconds
        if s is None:
            return "—"
        hours, remainder = divmod(s, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}ч {minutes}мин"
        return f"{minutes}мин {seconds}сек"

    @property
    def correct_count(self):
        return self.answers.filter(is_correct=True).count()

    @property
    def total_count(self):
        return self.variant.tasks.count()

    @property
    def percentage(self):
        if self.total_count == 0:
            return 0
        return round(self.correct_count / self.total_count * 100)


class CatalogImportSession(models.Model):
    """История импортов в каталог."""

    source = models.CharField("Источник", max_length=20, choices=TaskSource.choices)
    url = models.TextField("URL", blank=True)
    proj_guid = models.CharField("GUID проекта ФИПИ", max_length=64, blank=True)
    status = models.CharField("Статус", max_length=20, default="running")
    tasks_added = models.PositiveIntegerField("Добавлено", default=0)
    tasks_skipped = models.PositiveIntegerField("Пропущено", default=0)
    tasks_duplicate = models.PositiveIntegerField("Дублей", default=0)
    created_at = models.DateTimeField("Дата", auto_now_add=True)
    notes = models.TextField("Заметки", blank=True)

    class Meta:
        verbose_name = "Сессия импорта"
        verbose_name_plural = "Сессии импорта"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_source_display()} — {self.created_at:%d.%m.%Y %H:%M} (+{self.tasks_added})"

    @property
    def url_short(self):
        return self.url[:80] + "…" if len(self.url) > 80 else self.url


class CatalogTask(models.Model):
    """Задание в каталоге — независимо от конкретного варианта."""

    task_number = models.IntegerField(
        "Номер задания", null=True, blank=True, help_text="1–25, пусто = не определено"
    )
    exam_type = models.CharField("Тип экзамена", max_length=20, choices=ExamType.choices)
    text = models.TextField("Текст задания", blank=True)
    image = models.ImageField(
        "Изображение", upload_to="catalog/", blank=True, null=True, validators=[validate_image_size]
    )
    correct_answer = models.CharField("Правильный ответ", max_length=255, blank=True)
    source = models.CharField(
        "Источник", max_length=20, choices=TaskSource.choices, default=TaskSource.MANUAL
    )
    topic = models.CharField(
        "Тема",
        max_length=30,
        choices=TaskTopic.choices,
        default=TaskTopic.OTHER,
        blank=True,
    )
    points = models.PositiveIntegerField("Баллы", default=1)
    manual_grading = models.BooleanField("Ручная проверка", default=False)
    shared_context = models.TextField("Общее условие", blank=True)
    shared_context_image = models.ImageField(
        "Изображение общего условия",
        upload_to="contexts/",
        blank=True,
        null=True,
        validators=[validate_image_size],
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    sdamgia_id = models.CharField("ID СдамГИА", max_length=20, blank=True, null=True, unique=True)
    fipi_guid = models.CharField("GUID ФИПИ", max_length=64, blank=True, null=True, unique=True)
    text_hash = models.CharField("Хэш текста", max_length=32, blank=True, null=True, db_index=True)
    import_session = models.ForeignKey(
        CatalogImportSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Сессия импорта",
    )

    class Meta:
        verbose_name = "Задание каталога"
        verbose_name_plural = "Задания каталога"
        ordering = ["task_number", "-created_at"]

    def __str__(self):
        num = f"№{self.task_number}" if self.task_number else "Без номера"
        return f"[Каталог] {num} ({self.get_exam_type_display()})"

    @property
    def text_preview(self):
        plain = re.sub(r"<[^>]+>", " ", self.text or "").strip()
        return plain[:120] + "…" if len(plain) > 120 else plain

    @staticmethod
    def compute_hash(text):
        import hashlib

        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip().lower()
        return hashlib.md5(plain.encode("utf-8")).hexdigest() if plain else None


class Answer(models.Model):
    attempt = models.ForeignKey(
        Attempt, on_delete=models.CASCADE, verbose_name="Попытка", related_name="answers"
    )
    task = models.ForeignKey(Task, on_delete=models.CASCADE, verbose_name="Задание", related_name="answers")
    student_answer = models.TextField("Ответ ученика", blank=True)
    is_correct = models.BooleanField("Правильно", default=False, null=True, blank=True)

    class Meta:
        verbose_name = "Ответ"
        verbose_name_plural = "Ответы"
        unique_together = ["attempt", "task"]

    def __str__(self):
        return f"Задание {self.task.number}: {self.student_answer} ({'✓' if self.is_correct else '✗'})"
