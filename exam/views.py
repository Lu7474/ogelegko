import json
import logging
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Answer, Attempt, Student, Task, Variant
from .utils import check_answer, get_grade_display, get_grade_for_attempt

logger = logging.getLogger(__name__)


# --- Rate-limiting (IP-based via cache) ---


def _get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "127.0.0.1")


def _check_rate_limit(request, prefix="login"):
    ip = _get_client_ip(request)
    lock_key = f"{prefix}_lock:{ip}"
    if cache.get(lock_key):
        return "Слишком много попыток. Попробуйте через несколько минут."
    return None


def _record_failed_login(request, prefix="login"):
    max_attempts = getattr(settings, "LOGIN_MAX_ATTEMPTS", 5)
    cooldown = getattr(settings, "LOGIN_COOLDOWN_SECONDS", 300)
    ip = _get_client_ip(request)
    fail_key = f"{prefix}_fails:{ip}"
    lock_key = f"{prefix}_lock:{ip}"

    fails = cache.get(fail_key, 0) + 1
    cache.set(fail_key, fails, cooldown)
    if fails >= max_attempts:
        cache.set(lock_key, True, cooldown)


def _clear_login_fails(request, prefix="login"):
    ip = _get_client_ip(request)
    cache.delete(f"{prefix}_fails:{ip}")
    cache.delete(f"{prefix}_lock:{ip}")


# --- Авторизация ---


def get_student(request):
    student_id = request.session.get("student_id")
    if not student_id:
        return None
    try:
        student = Student.objects.select_related("school_class").get(id=student_id)
        if student.session_key and student.session_key != request.session.session_key:
            return None
        return student
    except Student.DoesNotExist:
        return None


def student_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        student = get_student(request)
        if not student:
            request.session.flush()
            return redirect("login")
        request.student = student
        return view_func(request, *args, **kwargs)

    return wrapper


# --- Вход / Выход ---


def login_view(request):
    if get_student(request):
        return redirect("choose_variant")

    error = ""
    if request.method == "POST":
        rate_error = _check_rate_limit(request, "student_login")
        if rate_error:
            error = rate_error
        else:
            full_name = request.POST.get("full_name", "").strip()
            password = request.POST.get("password", "").strip()

            if full_name and password:
                for student in Student.objects.filter(full_name=full_name).select_related("school_class"):
                    if student.check_password(password):
                        _clear_login_fails(request, "student_login")
                        request.session.cycle_key()
                        request.session["student_id"] = student.id
                        request.session.save()
                        student.session_key = request.session.session_key
                        student.save(update_fields=["session_key"])
                        logger.info("Вход ученика: %s (IP: %s)", full_name, _get_client_ip(request))
                        return redirect("choose_variant")

            _record_failed_login(request, "student_login")
            logger.warning("Неудачный вход ученика: %s (IP: %s)", full_name, _get_client_ip(request))
            error = "Неверное ФИО или пароль"

    return render(request, "exam/login.html", {"error": error})


@require_POST
def logout_view(request):
    student = get_student(request)
    if student:
        student.session_key = None
        student.save(update_fields=["session_key"])
    request.session.flush()
    return redirect("login")


# --- Выбор варианта ---


@student_required
def choose_variant(request):
    student = request.student
    error = ""

    past_attempts = (
        Attempt.objects.filter(student=student, is_finished=True)
        .select_related("variant")
        .order_by("-finished_at")
    )

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "random":
            variant = (
                Variant.objects.filter(exam_type=student.exam_type, is_active=True)
                .exclude(number__startswith="ошибки_")
                .order_by("?")
                .first()
            )
            if variant:
                limit_error = _check_attempt_limit(student, variant)
                if limit_error:
                    error = limit_error
                else:
                    return redirect("start_exam", variant_id=variant.id)
            else:
                error = "Нет доступных вариантов"

        elif action == "by_id":
            variant_number = request.POST.get("variant_number", "").strip()
            if not variant_number:
                error = "Введите ID варианта"
            else:
                try:
                    variant = Variant.objects.get(
                        number=variant_number, exam_type=student.exam_type, is_active=True
                    )
                    limit_error = _check_attempt_limit(student, variant)
                    if limit_error:
                        error = limit_error
                    else:
                        return redirect("start_exam", variant_id=variant.id)
                except Variant.DoesNotExist:
                    error = "Вариант с таким ID не найден"

    return render(
        request,
        "exam/choose_variant.html",
        {
            "student": student,
            "past_attempts": past_attempts,
            "error": error,
        },
    )


def _check_attempt_limit(student, variant):
    if variant.max_attempts == 0:
        return None
    finished_count = Attempt.objects.filter(student=student, variant=variant, is_finished=True).count()
    if finished_count >= variant.max_attempts:
        return f"Исчерпан лимит попыток ({variant.max_attempts}) для этого варианта"
    return None


def _is_attempt_expired(attempt):
    """Проверяет, истекло ли время попытки (серверная проверка)."""
    elapsed = (timezone.now() - attempt.started_at).total_seconds()
    max_time = attempt.variant.duration_minutes * 60 + 30  # 30 сек grace
    return elapsed > max_time


# --- Решение варианта ---


@student_required
def start_exam(request, variant_id):
    student = request.student
    variant = get_object_or_404(Variant, id=variant_id, exam_type=student.exam_type, is_active=True)

    if not variant.tasks.exists():
        return render(
            request,
            "exam/choose_variant.html",
            {
                "student": student,
                "past_attempts": Attempt.objects.filter(student=student, is_finished=True).select_related(
                    "variant"
                ),
                "error": "В этом варианте нет заданий",
            },
        )

    with transaction.atomic():
        # select_for_update блокирует строку студента, предотвращая создание дубликатов попыток
        Student.objects.select_for_update().filter(id=student.id).exists()
        attempt = Attempt.objects.filter(student=student, variant=variant, is_finished=False).first()
        resumed = attempt is not None

        if not attempt:
            limit_error = _check_attempt_limit(student, variant)
            if limit_error:
                return render(
                    request,
                    "exam/choose_variant.html",
                    {
                        "student": student,
                        "past_attempts": Attempt.objects.filter(
                            student=student, is_finished=True
                        ).select_related("variant"),
                        "error": limit_error,
                    },
                )

            max_score = variant.tasks.aggregate(total=Sum("points"))["total"] or 0
            attempt = Attempt.objects.create(
                student=student,
                variant=variant,
                max_score=max_score,
            )
            Answer.objects.bulk_create(
                [Answer(attempt=attempt, task=task) for task in variant.tasks.all()], ignore_conflicts=True
            )

    tasks = variant.tasks.order_by("id")

    # Если ответы не были созданы (например, задания добавлены после старта попытки),
    # создаём недостающие
    existing_answer_task_ids = set(attempt.answers.values_list("task_id", flat=True))
    missing = [t for t in tasks if t.id not in existing_answer_task_ids]
    if missing:
        Answer.objects.bulk_create(
            [Answer(attempt=attempt, task=t) for t in missing],
            ignore_conflicts=True,
        )

    answers = {a.task_id: a for a in attempt.answers.select_related("task").all()}

    elapsed = (timezone.now() - attempt.started_at).total_seconds()
    remaining = max(0, variant.duration_minutes * 60 - elapsed)

    if remaining <= 0:
        _finish_attempt(attempt)
        return redirect("results", attempt_id=attempt.id)

    answer_map = {}
    for a in attempt.answers.select_related("task").all():
        answer_map[str(a.task.number)] = a.id

    return render(
        request,
        "exam/solve.html",
        {
            "student": student,
            "variant": variant,
            "attempt": attempt,
            "tasks": tasks,
            "answers": answers,
            "remaining_seconds": int(remaining),
            "answer_map_json": json.dumps(answer_map),
            "resumed": resumed,
        },
    )


@require_POST
def save_answer(request):
    """AJAX: автосохранение одного ответа с серверной проверкой времени."""
    student = get_student(request)
    if not student:
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body)
        answer_id = data.get("answer_id")
        value = data.get("value", "")

        if not isinstance(answer_id, int):
            return JsonResponse({"error": "invalid answer_id"}, status=400)

        if isinstance(value, str):
            value = value.strip()
        else:
            value = str(value).strip()

        answer = Answer.objects.select_related("attempt", "attempt__variant").get(
            id=answer_id, attempt__student=student, attempt__is_finished=False
        )

        # Серверная проверка времени (#1)
        if _is_attempt_expired(answer.attempt):
            _finish_attempt(answer.attempt)
            return JsonResponse({"error": "time_expired"}, status=403)

        answer.student_answer = value
        answer.save(update_fields=["student_answer"])
        return JsonResponse({"ok": True})

    except Answer.DoesNotExist:
        return JsonResponse({"error": "answer not found"}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)
    except Exception:
        logger.exception("Ошибка при сохранении ответа")
        return JsonResponse({"error": "server error"}, status=500)


def _finish_attempt(attempt):
    if attempt.is_finished:
        return
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().get(id=attempt.id)
        if attempt.is_finished:
            return
        total_score = 0
        max_score = 0
        for answer in attempt.answers.select_related("task").all():
            if answer.task is None:
                continue
            max_score += answer.task.points
            if answer.task.manual_grading:
                answer.is_correct = None  # ожидает проверки учителя
                answer.awarded_points = None
                answer.save(update_fields=["is_correct", "awarded_points"])
            else:
                is_correct = check_answer(answer.student_answer, answer.task.correct_answer)
                answer.is_correct = is_correct
                answer.save(update_fields=["is_correct"])
                if is_correct:
                    total_score += answer.task.points
        attempt.is_finished = True
        attempt.finished_at = timezone.now()
        attempt.score = total_score
        attempt.max_score = max_score
        attempt.save()
    attempt.grade = get_grade_for_attempt(attempt)
    attempt.save(update_fields=["grade"])


def _recalculate_attempt_score(attempt):
    """Пересчитывает балл после ручной проверки учителем."""
    total_score = 0
    for a in attempt.answers.select_related("task").all():
        if a.awarded_points is not None:
            total_score += a.awarded_points
        elif a.is_correct is True:
            total_score += a.task.points
    attempt.score = total_score
    attempt.grade = get_grade_for_attempt(attempt)
    attempt.save(update_fields=["score", "grade"])
    logger.info("Попытка %d пересчитана: %s — %d баллов", attempt.id, attempt.student.full_name, total_score)


@require_POST
@student_required
def finish_exam(request, attempt_id):
    student = request.student
    attempt = get_object_or_404(Attempt, id=attempt_id, student=student, is_finished=False)
    _finish_attempt(attempt)
    return redirect("results", attempt_id=attempt.id)


# --- Итоги ---

_GRADE_PHRASES = {
    "5": "Отличный результат.",
    "4": "Хороший результат.",
    "3": "Результат ниже цели.",
    "2": "Пока не получается.",
}
_OGE_NEXT = {"2": 8, "3": 15, "4": 22}
_EGE_BASE_NEXT = {"2": 7, "3": 12, "4": 17}


def _decline_point(n):
    if 11 <= n % 100 <= 14:
        return "баллов"
    r = n % 10
    if r == 1:
        return "балл"
    if 2 <= r <= 4:
        return "балла"
    return "баллов"


def _grade_phrase(exam_type, grade, score):
    if exam_type == "ege_profile" or grade not in _GRADE_PHRASES:
        return ""
    base = _GRADE_PHRASES[grade]
    thresholds = _OGE_NEXT if exam_type == "oge" else _EGE_BASE_NEXT
    nxt = thresholds.get(grade)
    if nxt and nxt > score:
        gap = nxt - score
        name = {"2": "тройки", "3": "четвёрки", "4": "пятёрки"}.get(grade, "следующей оценки")
        return f"{base} До {name} — ещё {gap} {_decline_point(gap)}."
    return base


@student_required
def results_view(request, attempt_id):
    student = request.student
    attempt = get_object_or_404(Attempt, id=attempt_id, student=student, is_finished=True)

    answers = attempt.answers.select_related("task").order_by("task__id")
    wrong_answers = answers.filter(is_correct=False)
    manual_answers = answers.filter(task__manual_grading=True)
    auto_answers = answers.filter(task__manual_grading=False, task__no_student_input=False)

    previous_attempts = (
        Attempt.objects.filter(student=student, variant=attempt.variant, is_finished=True)
        .exclude(id=attempt.id)
        .order_by("-finished_at")
    )

    exam_type = attempt.variant.exam_type
    grade_display = get_grade_display(exam_type, attempt.grade)
    grade_phrase = _grade_phrase(exam_type, attempt.grade, attempt.score)
    hero_css_class = f"grade-{attempt.grade}" if attempt.grade in ("2", "3", "4", "5") else ""

    return render(
        request,
        "exam/results.html",
        {
            "student": student,
            "attempt": attempt,
            "answers": answers,
            "auto_answers": auto_answers,
            "wrong_answers": wrong_answers,
            "manual_answers": manual_answers,
            "previous_attempts": previous_attempts,
            "grade_display": grade_display,
            "grade_phrase": grade_phrase,
            "hero_css_class": hero_css_class,
        },
    )


# --- Повтор ошибок ---


@student_required
def retry_mistakes(request, attempt_id):
    student = request.student
    attempt = get_object_or_404(Attempt, id=attempt_id, student=student, is_finished=True)

    wrong_tasks = list(
        Task.objects.filter(
            answers__attempt=attempt,
            answers__is_correct=False,
        ).order_by("id")
    )

    if not wrong_tasks:
        return redirect("results", attempt_id=attempt.id)

    # Создаём временный вариант только с неверно решёнными заданиями.
    # is_active=True нужно, чтобы start_exam мог его открыть;
    # число начинается с "ошибки_" — choose_variant исключает такие из случайного выбора.
    review_number = f"ошибки_{attempt.variant.number}_{attempt.id}"
    review_variant, created = Variant.objects.get_or_create(
        number=review_number,
        defaults={
            "exam_type": attempt.variant.exam_type,
            "is_active": True,
            "max_attempts": 0,
        },
    )

    if created:
        from django.core.files.base import ContentFile as _CF

        from .models import TaskImage

        for i, task in enumerate(wrong_tasks, start=1):
            new_task = Task(
                variant=review_variant,
                number=i,
                text=task.text,
                correct_answer=task.correct_answer,
                source=task.source,
                points=task.points,
                manual_grading=task.manual_grading,
                no_student_input=task.no_student_input,
                shared_context=task.shared_context,
            )
            if task.image:
                try:
                    with task.image.open("rb") as f:
                        new_task.image.save(task.image.name.split("/")[-1], _CF(f.read()), save=False)
                except Exception:
                    pass
            if task.shared_context_image:
                try:
                    with task.shared_context_image.open("rb") as f:
                        new_task.shared_context_image.save(
                            task.shared_context_image.name.split("/")[-1], _CF(f.read()), save=False
                        )
                except Exception:
                    pass
            new_task.save()
            for ci in task.extra_images.order_by("order"):
                try:
                    with ci.image.open("rb") as f:
                        ti = TaskImage(task=new_task, order=ci.order)
                        ti.image.save(ci.image.name.split("/")[-1], _CF(f.read()), save=False)
                        ti.save()
                except Exception:
                    pass

    return redirect("start_exam", variant_id=review_variant.id)


# --- Профиль / Статистика ---


def _level_label(pct):
    if pct >= 80:
        return "Отличный уровень"
    if pct >= 60:
        return "Хороший уровень"
    if pct >= 40:
        return "Средний уровень"
    return "Низкий уровень"


def _level_css_class(pct):
    if pct >= 80:
        return "level-great"
    if pct >= 60:
        return "level-good"
    if pct >= 40:
        return "level-mid"
    return "level-low"


def _trend(percentages):
    if len(percentages) < 3:
        return "→"
    recent = sum(percentages[:3]) / 3
    older_slice = percentages[3:6] if len(percentages) >= 4 else []
    if not older_slice:
        return "→"
    older = sum(older_slice) / len(older_slice)
    if recent > older + 3:
        return "↑"
    if recent < older - 3:
        return "↓"
    return "→"


_NEXT_STEP_PHRASES = {
    "5": "Отличный результат. Попробуйте ещё — закрепите уверенность.",
    "4": "Хороший уровень. Несколько попыток — и оценка будет стабильной.",
    "3": "До четвёрки совсем немного. Разберите ошибки и попробуйте снова.",
    "2": "Продолжайте тренироваться. Каждая попытка улучшает результат.",
}


@student_required
def profile_view(request):
    student = request.student
    attempts = (
        Attempt.objects.filter(student=student, is_finished=True)
        .select_related("variant")
        .order_by("-finished_at")
    )

    avg_percentage = None
    level_label = level_css = trend = next_step = grade4_threshold_pct = None
    weak_spots = []

    attempts_list = list(attempts)
    if attempts_list:
        percentages = [a.percentage for a in attempts_list]
        avg_percentage = round(sum(percentages) / len(percentages))
        level_label = _level_label(avg_percentage)
        level_css = _level_css_class(avg_percentage)
        trend = _trend(percentages)
        last_grade = attempts_list[0].grade
        next_step = _NEXT_STEP_PHRASES.get(last_grade, "Продолжайте тренироваться.")

        if len(percentages) >= 3:
            spread = max(percentages[:5]) - min(percentages[:5])
            if spread > 20:
                weak_spots.append("Нестабильный результат в последних попытках")
            else:
                weak_spots.append("Стабильный прогресс")

        exam_type = student.exam_type
        last = attempts_list[0]
        if last.max_score and exam_type in ("oge", "ege_base"):
            t = 15 if exam_type == "oge" else 12
            grade4_threshold_pct = round(t / last.max_score * 100)

    chart_data = []
    for a in reversed(attempts_list):
        chart_data.append(
            {
                "variant": a.variant.number,
                "score": a.score,
                "max_score": a.max_score,
                "percentage": a.percentage,
            }
        )

    return render(
        request,
        "exam/profile.html",
        {
            "student": student,
            "attempts": attempts_list,
            "avg_percentage": avg_percentage,
            "level_label": level_label,
            "level_css": level_css,
            "trend": trend,
            "next_step": next_step,
            "weak_spots": weak_spots,
            "grade4_threshold_pct": grade4_threshold_pct,
            "chart_data": json.dumps(chart_data),
        },
    )


# --- Просмотр варианта с ответами ---


@student_required
def view_attempt(request, attempt_id):
    student = request.student
    attempt = get_object_or_404(Attempt, id=attempt_id, student=student, is_finished=True)
    answers = attempt.answers.select_related("task").order_by("task__id")

    return render(
        request,
        "exam/view_attempt.html",
        {
            "student": student,
            "attempt": attempt,
            "answers": answers,
        },
    )
