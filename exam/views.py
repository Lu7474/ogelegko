import json
import logging
from functools import wraps
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import Sum

from .models import Student, Variant, Task, Attempt, Answer, SchoolClass, ExamType
from .utils import check_answer, get_grade, get_grade_display

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
                try:
                    student = Student.objects.get(full_name=full_name)
                    if student.check_password(password):
                        _clear_login_fails(request, "student_login")
                        request.session.cycle_key()
                        request.session["student_id"] = student.id
                        request.session.save()
                        student.session_key = request.session.session_key
                        student.save(update_fields=["session_key"])
                        logger.info("Вход ученика: %s (IP: %s)", full_name, _get_client_ip(request))
                        return redirect("choose_variant")
                except Student.DoesNotExist:
                    pass

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

    past_attempts = Attempt.objects.filter(
        student=student, is_finished=True
    ).select_related("variant").order_by("-finished_at")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "random":
            variant = Variant.objects.filter(
                exam_type=student.exam_type, is_active=True
            ).order_by("?").first()
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

    return render(request, "exam/choose_variant.html", {
        "student": student,
        "past_attempts": past_attempts,
        "error": error,
    })


def _check_attempt_limit(student, variant):
    if variant.max_attempts == 0:
        return None
    finished_count = Attempt.objects.filter(
        student=student, variant=variant, is_finished=True
    ).count()
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
        return render(request, "exam/choose_variant.html", {
            "student": student,
            "past_attempts": Attempt.objects.filter(student=student, is_finished=True).select_related("variant"),
            "error": "В этом варианте нет заданий",
        })

    attempt = Attempt.objects.filter(
        student=student, variant=variant, is_finished=False
    ).first()

    if not attempt:
        limit_error = _check_attempt_limit(student, variant)
        if limit_error:
            return render(request, "exam/choose_variant.html", {
                "student": student,
                "past_attempts": Attempt.objects.filter(student=student, is_finished=True).select_related("variant"),
                "error": limit_error,
            })

        max_score = variant.tasks.aggregate(total=Sum("points"))["total"] or 0
        with transaction.atomic():
            attempt = Attempt.objects.create(
                student=student, variant=variant, max_score=max_score,
            )
            Answer.objects.bulk_create([
                Answer(attempt=attempt, task=task)
                for task in variant.tasks.all()
            ], ignore_conflicts=True)

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

    return render(request, "exam/solve.html", {
        "student": student,
        "variant": variant,
        "attempt": attempt,
        "tasks": tasks,
        "answers": answers,
        "remaining_seconds": int(remaining),
        "answer_map_json": json.dumps(answer_map),
    })


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
    total_score = 0
    max_score = 0
    for answer in attempt.answers.select_related("task").all():
        if answer.task is None:
            continue
        max_score += answer.task.points
        if answer.task.manual_grading:
            answer.is_correct = None  # ожидает проверки учителя
            answer.save(update_fields=["is_correct"])
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
    attempt.grade = get_grade(attempt.variant.exam_type, total_score)
    attempt.save()


def _recalculate_attempt_score(attempt):
    """Пересчитывает балл после ручной проверки учителем."""
    total_score = sum(
        a.task.points
        for a in attempt.answers.select_related("task").all()
        if a.is_correct is True
    )
    attempt.score = total_score
    attempt.grade = get_grade(attempt.variant.exam_type, total_score)
    attempt.save(update_fields=["score", "grade"])
    logger.info("Попытка %d завершена: %s — %d баллов",
                attempt.id, attempt.student.full_name, total_score)


@require_POST
@student_required
def finish_exam(request, attempt_id):
    student = request.student
    attempt = get_object_or_404(Attempt, id=attempt_id, student=student, is_finished=False)
    _finish_attempt(attempt)
    return redirect("results", attempt_id=attempt.id)


# --- Итоги ---

@student_required
def results_view(request, attempt_id):
    student = request.student
    attempt = get_object_or_404(
        Attempt, id=attempt_id, student=student, is_finished=True
    )

    answers = attempt.answers.select_related("task").order_by("task__id")
    wrong_answers = answers.filter(is_correct=False)
    manual_answers = answers.filter(task__manual_grading=True)

    previous_attempts = Attempt.objects.filter(
        student=student, variant=attempt.variant, is_finished=True
    ).exclude(id=attempt.id).order_by("-finished_at")

    grade_display = get_grade_display(attempt.variant.exam_type, attempt.grade)

    return render(request, "exam/results.html", {
        "student": student,
        "attempt": attempt,
        "answers": answers,
        "wrong_answers": wrong_answers,
        "manual_answers": manual_answers,
        "previous_attempts": previous_attempts,
        "grade_display": grade_display,
    })


# --- Профиль / Статистика ---

@student_required
def profile_view(request):
    student = request.student
    attempts = Attempt.objects.filter(
        student=student, is_finished=True
    ).select_related("variant").order_by("-finished_at")

    avg_percentage = None
    if attempts.exists():
        percentages = [a.percentage for a in attempts]
        avg_percentage = round(sum(percentages) / len(percentages))

    chart_data = []
    for a in reversed(list(attempts)):
        chart_data.append({
            "date": a.finished_at.strftime("%d.%m.%Y"),
            "variant": a.variant.number,
            "score": a.score,
            "max_score": a.max_score,
            "percentage": a.percentage,
        })

    return render(request, "exam/profile.html", {
        "student": student,
        "attempts": attempts,
        "avg_percentage": avg_percentage,
        "chart_data": json.dumps(chart_data),
    })


# --- Просмотр варианта с ответами ---

@student_required
def view_attempt(request, attempt_id):
    student = request.student
    attempt = get_object_or_404(
        Attempt, id=attempt_id, student=student, is_finished=True
    )
    answers = attempt.answers.select_related("task").order_by("task__id")

    return render(request, "exam/view_attempt.html", {
        "student": student,
        "attempt": attempt,
        "answers": answers,
    })
