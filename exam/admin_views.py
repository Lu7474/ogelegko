import json
import logging
import threading
import uuid
from datetime import timedelta
from functools import wraps

from django.conf import settings as django_settings
from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    Answer,
    Attempt,
    CatalogImportSession,
    CatalogTask,
    ExamType,
    SchoolClass,
    Student,
    Task,
    TaskSource,
    Variant,
)
from .parser import sanitize_html

logger = logging.getLogger(__name__)


def admin_required(view_func):
    """Декоратор: требует авторизованного админа (Django User с is_staff)."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            return redirect("admin_login")
        return view_func(request, *args, **kwargs)

    return wrapper


def _safe_int(value, default=0):
    """Безопасное преобразование в int."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _paginate(request, queryset, per_page=25):
    """Пагинация queryset."""
    paginator = Paginator(queryset, per_page)
    page_number = request.GET.get("page", 1)
    return paginator.get_page(page_number)


def _get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "127.0.0.1")


# --- Вход / Выход ---


def _check_admin_rate_limit(request):
    ip = _get_client_ip(request)
    lock_key = f"admin_login_lock:{ip}"

    if cache.get(lock_key):
        return "Слишком много попыток. Попробуйте через несколько минут."
    return None


def _record_admin_failed_login(request):
    max_attempts = getattr(django_settings, "LOGIN_MAX_ATTEMPTS", 5)
    cooldown = getattr(django_settings, "LOGIN_COOLDOWN_SECONDS", 300)
    ip = _get_client_ip(request)
    fail_key = f"admin_login_fails:{ip}"
    lock_key = f"admin_login_lock:{ip}"

    fails = cache.get(fail_key, 0) + 1
    cache.set(fail_key, fails, cooldown)
    if fails >= max_attempts:
        cache.set(lock_key, True, cooldown)


def admin_login(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("admin_dashboard")

    error = ""
    if request.method == "POST":
        rate_error = _check_admin_rate_limit(request)
        if rate_error:
            error = rate_error
        else:
            username = request.POST.get("username", "").strip()
            password = request.POST.get("password", "").strip()
            user = authenticate(request, username=username, password=password)
            if user and user.is_staff:
                ip = _get_client_ip(request)
                cache.delete(f"admin_login_fails:{ip}")
                cache.delete(f"admin_login_lock:{ip}")
                login(request, user)
                logger.info("Успешный вход админа: %s (IP: %s)", username, ip)
                return redirect("admin_dashboard")
            _record_admin_failed_login(request)
            logger.warning("Неудачная попытка входа админа: %s (IP: %s)", username, _get_client_ip(request))
            error = "Неверный логин или пароль"
    return render(request, "admin/login.html", {"error": error})


@require_POST
def admin_logout(request):
    logout(request)
    return redirect("admin_login")


# --- Дашборд ---


@admin_required
def dashboard(request):
    stats = {
        "students_count": Student.objects.count(),
        "variants_count": Variant.objects.count(),
        "attempts_count": Attempt.objects.filter(is_finished=True).count(),
        "classes_count": SchoolClass.objects.count(),
    }
    recent_attempts = (
        Attempt.objects.filter(is_finished=True)
        .select_related("student", "variant", "student__school_class")
        .order_by("-finished_at")[:10]
    )

    pending_grading = Attempt.objects.filter(is_finished=True, answers__is_correct=None).distinct().count()

    # Статистика по классам
    classes = SchoolClass.objects.filter(is_active=True).annotate(student_count=Count("students"))
    class_stats_list = []
    for sc in classes:
        attempts_qs = Attempt.objects.filter(student__school_class=sc, is_finished=True)
        attempts_count = attempts_qs.count()
        if attempts_count > 0:
            percentages = [a.percentage for a in attempts_qs]
            avg_pct = round(sum(percentages) / len(percentages))
        else:
            avg_pct = None
        class_stats_list.append(
            {
                "school_class": sc,
                "attempts_count": attempts_count,
                "avg_percentage": avg_pct,
            }
        )

    return render(
        request,
        "admin/dashboard.html",
        {
            "stats": stats,
            "recent_attempts": recent_attempts,
            "class_stats": class_stats_list,
            "pending_grading": pending_grading,
        },
    )


# --- Классы ---


@admin_required
def class_list(request):
    classes = SchoolClass.objects.annotate(student_count=Count("students"))
    return render(request, "admin/classes.html", {"classes": classes})


@admin_required
def class_add(request):
    error = ""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        exam_type = request.POST.get("exam_type", "")
        if not name:
            error = "Введите название класса"
        elif exam_type not in dict(ExamType.choices):
            error = "Выберите тип экзамена"
        else:
            try:
                SchoolClass.objects.create(name=name, exam_type=exam_type)
                return redirect("admin_classes")
            except IntegrityError:
                error = f"Класс '{name}' уже существует"

    return render(
        request,
        "admin/class_form.html",
        {
            "exam_types": ExamType.choices,
            "error": error,
        },
    )


@admin_required
def class_edit(request, class_id):
    school_class = get_object_or_404(SchoolClass, id=class_id)
    error = ""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        exam_type = request.POST.get("exam_type", "")
        if not name:
            error = "Введите название класса"
        elif exam_type not in dict(ExamType.choices):
            error = "Выберите тип экзамена"
        else:
            try:
                school_class.name = name
                school_class.exam_type = exam_type
                school_class.save()
                return redirect("admin_classes")
            except IntegrityError:
                error = f"Класс '{name}' уже существует"

    return render(
        request,
        "admin/class_form.html",
        {
            "school_class": school_class,
            "exam_types": ExamType.choices,
            "error": error,
        },
    )


@admin_required
@require_POST
def class_delete(request, class_id):
    school_class = get_object_or_404(SchoolClass, id=class_id)
    school_class.delete()
    return redirect("admin_classes")


@admin_required
@require_POST
def class_toggle(request, class_id):
    school_class = get_object_or_404(SchoolClass, id=class_id)
    school_class.is_active = not school_class.is_active
    school_class.save(update_fields=["is_active"])
    return redirect("admin_classes")


@admin_required
def class_stats(request, class_id):
    school_class = get_object_or_404(SchoolClass, id=class_id)
    students = school_class.students.all()

    student_stats_list = []
    total_attempts = 0
    all_percentages = []

    for student in students:
        attempts = (
            Attempt.objects.filter(student=student, is_finished=True)
            .select_related("variant")
            .order_by("-finished_at")
        )
        count = attempts.count()
        total_attempts += count

        if count > 0:
            percentages = [a.percentage for a in attempts]
            avg_pct = round(sum(percentages) / len(percentages))
            all_percentages.extend(percentages)
            last_attempt = attempts.first()
        else:
            avg_pct = None
            last_attempt = None

        student_stats_list.append(
            {
                "student": student,
                "attempts_count": count,
                "avg_percentage": avg_pct,
                "last_attempt": last_attempt,
            }
        )

    class_avg = round(sum(all_percentages) / len(all_percentages)) if all_percentages else None

    return render(
        request,
        "admin/class_stats.html",
        {
            "school_class": school_class,
            "student_stats": student_stats_list,
            "total_attempts": total_attempts,
            "class_avg": class_avg,
        },
    )


# --- Ученики ---


@admin_required
def student_list(request):
    class_filter = request.GET.get("class", "")
    students = Student.objects.select_related("school_class").annotate(
        attempts_count=Count("attempts", filter=Q(attempts__is_finished=True))
    )
    if class_filter:
        filter_id = _safe_int(class_filter)
        if filter_id:
            students = students.filter(school_class_id=filter_id)

    classes = SchoolClass.objects.all()
    return render(
        request,
        "admin/students.html",
        {
            "students": students,
            "classes": classes,
            "class_filter": class_filter,
        },
    )


@admin_required
def student_add(request):
    error = ""
    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        password = request.POST.get("password", "").strip()
        class_id = _safe_int(request.POST.get("school_class", ""))

        if not full_name:
            error = "Введите ФИО"
        elif not password:
            error = "Введите пароль"
        elif not class_id:
            error = "Выберите класс"
        else:
            try:
                school_class = SchoolClass.objects.get(id=class_id)
                student = Student(full_name=full_name, school_class=school_class)
                student.set_password(password)
                student.save()
                return redirect("admin_students")
            except SchoolClass.DoesNotExist:
                error = "Выбранный класс не найден"
            except IntegrityError:
                error = f"Ученик '{full_name}' уже существует"

    classes = SchoolClass.objects.all()
    return render(request, "admin/student_form.html", {"classes": classes, "error": error})


@admin_required
def student_edit(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    error = ""
    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        class_id = _safe_int(request.POST.get("school_class", ""))
        password = request.POST.get("password", "").strip()

        if not full_name:
            error = "Введите ФИО"
        elif not class_id:
            error = "Выберите класс"
        else:
            try:
                school_class = SchoolClass.objects.get(id=class_id)
                student.full_name = full_name
                student.school_class = school_class
                if password:
                    student.set_password(password)
                student.save()
                return redirect("admin_students")
            except SchoolClass.DoesNotExist:
                error = "Выбранный класс не найден"
            except IntegrityError:
                error = f"Ученик '{full_name}' уже существует"

    classes = SchoolClass.objects.all()
    return render(
        request,
        "admin/student_form.html",
        {
            "student": student,
            "classes": classes,
            "error": error,
        },
    )


@admin_required
@require_POST
def student_delete(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    student.delete()
    return redirect("admin_students")


@admin_required
def student_import(request):
    """Массовый импорт учеников из Excel."""
    errors = []
    success_count = 0

    if request.method == "POST" and request.FILES.get("file"):
        try:
            import openpyxl
        except ImportError:
            errors.append("Библиотека openpyxl не установлена. Выполните: pip install openpyxl")
            return render(
                request,
                "admin/student_import.html",
                {
                    "errors": errors,
                    "success_count": success_count,
                },
            )

        uploaded_file = request.FILES["file"]
        if not uploaded_file.name.endswith(".xlsx"):
            errors.append("Поддерживается только формат .xlsx")
        elif uploaded_file.size > 5 * 1024 * 1024:  # 5 МБ
            errors.append("Файл слишком большой (максимум 5 МБ)")
        else:
            try:
                wb = openpyxl.load_workbook(uploaded_file)
                ws = wb.active

                for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                    if not row or not row[0]:
                        continue
                    try:
                        if len(row) < 3 or not row[1] or not row[2]:
                            errors.append(
                                f"Строка {row_num}: не все поля заполнены (нужно: ФИО, пароль, класс)"
                            )
                            continue

                        full_name = str(row[0]).strip()
                        password = str(row[1]).strip()
                        class_name = str(row[2]).strip()

                        school_class = SchoolClass.objects.get(name=class_name)
                        student = Student(full_name=full_name, school_class=school_class)
                        student.set_password(password)
                        student.save()
                        success_count += 1
                    except SchoolClass.DoesNotExist:
                        errors.append(f"Строка {row_num}: класс '{class_name}' не найден")
                    except Exception as e:
                        logger.exception("Ошибка импорта строки %d", row_num)
                        errors.append(f"Строка {row_num}: {str(e)}")

            except Exception as e:
                logger.exception("Ошибка чтения Excel файла")
                errors.append(f"Ошибка чтения файла: {str(e)}")

    return render(
        request,
        "admin/student_import.html",
        {
            "errors": errors,
            "success_count": success_count,
        },
    )


@admin_required
def student_stats(request, student_id):
    student = get_object_or_404(Student.objects.select_related("school_class"), id=student_id)
    attempts = (
        Attempt.objects.filter(student=student, is_finished=True)
        .select_related("variant")
        .order_by("-finished_at")
    )

    avg_percentage = None
    if attempts.exists():
        percentages = [a.percentage for a in attempts]
        avg_percentage = round(sum(percentages) / len(percentages))

    chart_data = []
    for a in reversed(list(attempts)):
        chart_data.append(
            {
                "date": a.finished_at.strftime("%d.%m.%Y"),
                "variant": a.variant.number,
                "score": a.score,
                "max_score": a.max_score,
                "percentage": a.percentage,
            }
        )

    return render(
        request,
        "admin/student_stats.html",
        {
            "student": student,
            "attempts": attempts,
            "avg_percentage": avg_percentage,
            "chart_data": json.dumps(chart_data),
        },
    )


# --- Варианты ---


@admin_required
def variant_list(request):
    exam_filter = request.GET.get("exam_type", "oge")
    variants = Variant.objects.annotate(
        task_count=Count("tasks", distinct=True),
        attempts_count=Count("attempts", filter=Q(attempts__is_finished=True), distinct=True),
    )
    if exam_filter and exam_filter in dict(ExamType.choices):
        variants = variants.filter(exam_type=exam_filter)

    return render(
        request,
        "admin/variants.html",
        {
            "variants": variants,
            "exam_types": ExamType.choices,
            "exam_filter": exam_filter,
        },
    )


def _save_variant_tasks(variant, request):
    """Сохраняет задания варианта из POST-данных.
    Индексы заданий берём из ключей POST — они могут быть не последовательными
    (например, при создании из каталога номера 5, 12, 18).
    """
    import re as _re

    indices = sorted(
        {int(m.group(1)) for key in request.POST for m in [_re.match(r"^task_(\d+)_answer$", key)] if m}
    )
    for task_index in indices:
        text = sanitize_html(request.POST.get(f"task_{task_index}_text", "").strip())
        answer = request.POST.get(f"task_{task_index}_answer", "").strip()
        source = request.POST.get(f"task_{task_index}_source", "manual")
        points = _safe_int(request.POST.get(f"task_{task_index}_points", "1"), default=1)
        manual_grading = bool(request.POST.get(f"task_{task_index}_manual_grading"))
        image = request.FILES.get(f"task_{task_index}_image")
        shared_context = sanitize_html(request.POST.get(f"task_{task_index}_shared_context", "").strip())
        shared_context_image = request.FILES.get(f"task_{task_index}_shared_context_image")

        if points < 1:
            points = 1
        if source not in dict(TaskSource.choices):
            source = "manual"

        # Сохраняем если есть ответ ИЛИ ручная проверка
        if answer or manual_grading:
            task = Task(
                variant=variant,
                number=task_index,
                text=text,
                correct_answer=answer,
                source=source,
                points=points,
                manual_grading=manual_grading,
                shared_context=shared_context,
            )
            if image:
                task.image = image
            if shared_context_image:
                task.shared_context_image = shared_context_image
            task.save()


@admin_required
def variant_add(request):
    error = ""
    if request.method == "POST":
        number = request.POST.get("number", "").strip()
        exam_type = request.POST.get("exam_type", "")

        max_attempts = _safe_int(request.POST.get("max_attempts", "3"), default=3)

        if not number:
            error = "Введите номер варианта"
        elif exam_type not in dict(ExamType.choices):
            error = "Выберите тип экзамена"
        else:
            try:
                variant = Variant.objects.create(
                    number=number, exam_type=exam_type, max_attempts=max_attempts
                )
                _save_variant_tasks(variant, request)
                return redirect("admin_variants")
            except IntegrityError:
                error = f"Вариант с номером '{number}' уже существует"

    return render(
        request,
        "admin/variant_form.html",
        {
            "exam_types": ExamType.choices,
            "sources": TaskSource.choices,
            "error": error,
        },
    )


@admin_required
def variant_edit(request, variant_id):
    variant = get_object_or_404(Variant, id=variant_id)
    tasks = variant.tasks.order_by("id")
    error = ""

    if request.method == "POST":
        number = request.POST.get("number", "").strip()
        exam_type = request.POST.get("exam_type", variant.exam_type)

        max_attempts = _safe_int(request.POST.get("max_attempts", "3"), default=3)

        if not number:
            error = "Введите номер варианта"
        else:
            try:
                variant.number = number
                variant.exam_type = exam_type
                variant.max_attempts = max_attempts
                variant.save()

                variant.tasks.all().delete()
                _save_variant_tasks(variant, request)
                return redirect("admin_variants")
            except IntegrityError:
                error = f"Вариант с номером '{number}' уже существует"

    return render(
        request,
        "admin/variant_form.html",
        {
            "variant": variant,
            "tasks": tasks,
            "exam_types": ExamType.choices,
            "sources": TaskSource.choices,
            "error": error,
        },
    )


@admin_required
@require_POST
def variant_toggle(request, variant_id):
    variant = get_object_or_404(Variant, id=variant_id)
    variant.is_active = not variant.is_active
    variant.save(update_fields=["is_active"])
    return redirect("admin_variants")


@admin_required
@require_POST
def variant_duplicate(request, variant_id):
    original = get_object_or_404(Variant, id=variant_id)
    # Генерируем уникальный номер для копии
    copy_number = f"{original.number}_копия"
    counter = 1
    while Variant.objects.filter(number=copy_number).exists():
        counter += 1
        copy_number = f"{original.number}_копия{counter}"

    new_variant = Variant.objects.create(
        number=copy_number,
        exam_type=original.exam_type,
        max_attempts=original.max_attempts,
    )
    for task in original.tasks.all():
        Task.objects.create(
            variant=new_variant,
            number=task.number,
            text=task.text,
            image=task.image,
            correct_answer=task.correct_answer,
            source=task.source,
            points=task.points,
            shared_context=task.shared_context,
            shared_context_image=task.shared_context_image,
        )
    return redirect("admin_variant_edit", variant_id=new_variant.id)


@admin_required
@require_POST
def variant_delete(request, variant_id):
    variant = get_object_or_404(Variant, id=variant_id)
    variant.delete()
    return redirect("admin_variants")


_JOB_TTL = 7200  # 2 часа


def _run_import_job(job_id, url, variant_number):
    """Фоновый поток для импорта варианта."""
    try:
        from .parser import import_variant_from_sdamgia

        variant, parse_errors = import_variant_from_sdamgia(url, variant_number=variant_number or None)
        cache.set(
            f"vjob:{job_id}",
            {
                "status": "done",
                "variant_id": variant.id if variant else None,
                "errors": parse_errors,
            },
            _JOB_TTL,
        )
    except Exception as e:
        logger.exception("Ошибка импорта варианта")
        cache.set(
            f"vjob:{job_id}",
            {
                "status": "error",
                "variant_id": None,
                "errors": [str(e)],
            },
            _JOB_TTL,
        )
    finally:
        from django.db import connection

        connection.close()


@admin_required
def variant_import(request):
    """Импорт варианта с sdamgia.ru."""
    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        variant_number = request.POST.get("variant_number", "").strip()

        errors = []
        if not url:
            errors.append("Введите URL варианта")
        elif "sdamgia.ru" not in url:
            errors.append("Поддерживается только sdamgia.ru (Решу ОГЭ / Решу ЕГЭ)")

        if errors:
            return render(request, "admin/variant_import.html", {"errors": errors})

        job_id = str(uuid.uuid4())
        cache.set(f"vjob:{job_id}", {"status": "running", "variant_id": None, "errors": []}, _JOB_TTL)
        threading.Thread(target=_run_import_job, args=(job_id, url, variant_number), daemon=True).start()
        return redirect("admin_variant_import_status", job_id=job_id)

    return render(request, "admin/variant_import.html", {"errors": []})


@admin_required
def variant_import_status(request, job_id):
    """Страница/API опроса статуса импорта."""
    job = cache.get(f"vjob:{job_id}") or {
        "status": "unknown",
        "variant_id": None,
        "errors": ["Задание не найдено"],
    }

    if request.GET.get("json"):
        return JsonResponse(job)

    variant = None
    if job["status"] == "done" and job.get("variant_id"):
        variant = Variant.objects.filter(id=job["variant_id"]).first()

    return render(
        request,
        "admin/variant_import_status.html",
        {
            "job_id": job_id,
            "job": job,
            "variant": variant,
        },
    )


# --- Статистика варианта ---


@admin_required
def variant_stats(request, variant_id):
    variant = get_object_or_404(Variant, id=variant_id)
    attempts = (
        Attempt.objects.filter(variant=variant, is_finished=True)
        .select_related("student", "student__school_class")
        .order_by("-finished_at")
    )

    avg_percentage = None
    if attempts.exists():
        percentages = [a.percentage for a in attempts]
        avg_percentage = round(sum(percentages) / len(percentages))

    # Статистика по заданиям
    task_stats = []
    for task in variant.tasks.order_by("id"):
        total = Answer.objects.filter(task=task, attempt__is_finished=True).count()
        correct = Answer.objects.filter(task=task, attempt__is_finished=True, is_correct=True).count()
        pct = round(correct / total * 100) if total > 0 else 0
        task_stats.append(
            {
                "task": task,
                "correct": correct,
                "total": total,
                "percentage": pct,
            }
        )

    return render(
        request,
        "admin/variant_stats.html",
        {
            "variant": variant,
            "attempts": attempts,
            "avg_percentage": avg_percentage,
            "task_stats": task_stats,
        },
    )


# --- Просмотр и ручная проверка попытки ---


@admin_required
def attempt_detail(request, attempt_id):
    attempt = get_object_or_404(Attempt, id=attempt_id, is_finished=True)
    answers = attempt.answers.select_related("task").order_by("task__id")
    has_pending = answers.filter(is_correct=None).exists()
    return render(
        request,
        "admin/attempt_detail.html",
        {
            "attempt": attempt,
            "answers": answers,
            "has_pending": has_pending,
        },
    )


@admin_required
@require_POST
def attempt_grade_answer(request, answer_id):
    from .views import _recalculate_attempt_score

    answer = get_object_or_404(Answer, id=answer_id, task__manual_grading=True)

    if "points" in request.POST:
        try:
            pts = int(request.POST["points"])
            pts = max(0, min(pts, answer.task.points))
            answer.awarded_points = pts
            answer.is_correct = pts > 0
        except (ValueError, TypeError):
            pass
    else:
        value = request.POST.get("is_correct")
        if value == "true":
            answer.is_correct = True
            answer.awarded_points = answer.task.points
        elif value == "false":
            answer.is_correct = False
            answer.awarded_points = 0
        else:
            answer.is_correct = None
            answer.awarded_points = None

    answer.save(update_fields=["is_correct", "awarded_points"])
    _recalculate_attempt_score(answer.attempt)
    return redirect("admin_attempt_detail", attempt_id=answer.attempt_id)


# --- API: уведомления о новых попытках ---


@admin_required
def api_new_attempts(request):
    """JSON: попытки завершённые после переданного ISO-timestamp (или за последние 24ч)."""
    since_str = request.GET.get("since", "")
    try:
        from datetime import datetime

        since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        if timezone.is_naive(since_dt):
            since_dt = timezone.make_aware(since_dt)
    except (ValueError, TypeError, AttributeError):
        since_dt = timezone.now() - timedelta(hours=24)

    attempts = (
        Attempt.objects.filter(is_finished=True, finished_at__gt=since_dt)
        .select_related("student", "variant", "student__school_class")
        .order_by("-finished_at")[:20]
    )

    from django.urls import reverse

    data = {
        "count": attempts.count(),
        "attempts": [
            {
                "student": a.student.full_name,
                "class": a.student.school_class.name,
                "variant": a.variant.number,
                "finished_at": a.finished_at.strftime("%d.%m %H:%M"),
                "grade": a.grade,
                "url": reverse("admin_attempt_detail", args=[a.id]),
            }
            for a in attempts
        ],
    }
    return JsonResponse(data)


# --- Удаление попытки ---


@admin_required
@require_POST
def attempt_delete(request, attempt_id):
    attempt = get_object_or_404(Attempt, id=attempt_id)
    student_id = attempt.student_id
    attempt.delete()
    return redirect("admin_student_stats", student_id=student_id)


# --- Экспорт результатов ---


@admin_required
def export_results(request):
    try:
        import openpyxl
    except ImportError:
        return HttpResponse("openpyxl не установлен. pip install openpyxl", status=500)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Результаты"

    headers = ["ФИО", "Класс", "Тип экзамена", "Вариант", "Дата", "Балл", "Макс. балл", "Оценка", "Время"]
    ws.append(headers)

    # Фильтры
    class_id = _safe_int(request.GET.get("class", ""))
    variant_id = _safe_int(request.GET.get("variant", ""))

    attempts = (
        Attempt.objects.filter(is_finished=True)
        .select_related("student", "student__school_class", "variant")
        .order_by("-finished_at")
    )

    if class_id:
        attempts = attempts.filter(student__school_class_id=class_id)
    if variant_id:
        attempts = attempts.filter(variant_id=variant_id)

    for a in attempts:
        ws.append(
            [
                a.student.full_name,
                a.student.school_class.name,
                a.student.school_class.get_exam_type_display(),
                a.variant.number,
                a.finished_at.strftime("%d.%m.%Y %H:%M") if a.finished_at else "",
                a.score,
                a.max_score,
                a.grade,
                a.duration_display,
            ]
        )

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="results.xlsx"'
    wb.save(response)
    return response


@admin_required
def export_results_docx(request):
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError:
        return HttpResponse("python-docx не установлен.", status=500)

    # Фильтры
    class_id = _safe_int(request.GET.get("class", ""))
    variant_id = _safe_int(request.GET.get("variant", ""))

    attempts = (
        Attempt.objects.filter(is_finished=True)
        .select_related("student", "student__school_class", "variant")
        .prefetch_related("answers__task")
        .order_by("-finished_at")
    )

    if class_id:
        attempts = attempts.filter(student__school_class_id=class_id)
    if variant_id:
        attempts = attempts.filter(variant_id=variant_id)

    attempts = list(attempts)

    # Максимальное количество заданий среди всех попыток
    max_tasks = 0
    for a in attempts:
        max_tasks = max(max_tasks, a.variant.tasks.count())
    max_tasks = max_tasks or 25

    doc = Document()

    # Альбомная ориентация
    section = doc.sections[0]
    section.orientation = 1  # WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = Cm(1.5)
    section.right_margin = Cm(1.5)
    section.top_margin = Cm(1.5)
    section.bottom_margin = Cm(1.5)

    doc.add_heading("Результаты экзаменов", level=1)

    # Колонки: ФИО | Дата | Вариант | 1..N | Итого | Оценка
    col_count = 3 + max_tasks + 2
    table = doc.add_table(rows=1, cols=col_count)
    table.style = "Table Grid"

    # Заголовок
    hdr = table.rows[0].cells
    hdr[0].text = "ФИО"
    hdr[1].text = "Дата"
    hdr[2].text = "Вариант"
    for i in range(max_tasks):
        hdr[3 + i].text = str(i + 1)
    hdr[3 + max_tasks].text = "Итого"
    hdr[3 + max_tasks + 1].text = "Оценка"

    for cell in hdr:
        for para in cell.paragraphs:
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in para.runs:
                run.bold = True
                run.font.size = Pt(8)

    for a in attempts:
        # Строим карту: номер задания → is_correct
        answers_map = {}
        for ans in a.answers.all():
            answers_map[ans.task.number] = ans.is_correct

        # Получаем задания варианта, отсортированные по номеру
        tasks = list(a.variant.tasks.order_by("id"))

        row_cells = table.add_row().cells
        row_cells[0].text = a.student.full_name
        row_cells[1].text = a.finished_at.strftime("%d.%m.%Y") if a.finished_at else ""
        row_cells[2].text = a.variant.number

        for i, task in enumerate(tasks):
            if i >= max_tasks:
                break
            correct = answers_map.get(task.number)
            if correct is True:
                row_cells[3 + i].text = "1"
            elif correct is False:
                row_cells[3 + i].text = "0"
            else:
                row_cells[3 + i].text = ""

        row_cells[3 + max_tasks].text = str(a.score)
        row_cells[3 + max_tasks + 1].text = str(a.grade)

        for cell in row_cells:
            for para in cell.paragraphs:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in para.runs:
                    run.font.size = Pt(8)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    response["Content-Disposition"] = 'attachment; filename="results.docx"'
    doc.save(response)
    return response


# ===== КАТАЛОГ ЗАДАНИЙ =====


def _run_catalog_import_job(job_id, url):
    """Фоновый поток: парсит вариант и добавляет все задания в каталог."""
    try:
        from .parser import import_variant_to_catalog

        sess = CatalogImportSession.objects.create(
            source=TaskSource.SDAMGIA,
            url=url,
            status="running",
        )
        added, errors = import_variant_to_catalog(url, session=sess)
        if errors and not added:
            sess.status = "error"
            sess.notes = "\n".join(errors)
            sess.save(update_fields=["status", "notes"])
        cache.set(
            f"cjob:{job_id}",
            {
                "status": "done",
                "added": added,
                "errors": errors,
                "session_id": sess.id,
            },
            _JOB_TTL,
        )
    except Exception as e:
        logger.exception("Ошибка импорта в каталог")
        cache.set(
            f"cjob:{job_id}",
            {
                "status": "error",
                "added": 0,
                "errors": [str(e)],
            },
            _JOB_TTL,
        )
    finally:
        from django.db import connection

        connection.close()


@admin_required
def catalog_list(request):
    """Список заданий каталога с фильтрацией."""
    exam_type_filter = request.GET.get("exam_type", "oge")
    num_filter = request.GET.get("task_number", "")
    source_filter = request.GET.get("source", "")
    search = request.GET.get("q", "").strip()

    tasks = CatalogTask.objects.exclude(task_number__isnull=True)

    if exam_type_filter:
        tasks = tasks.filter(exam_type=exam_type_filter)
    if num_filter:
        tasks = tasks.filter(task_number=_safe_int(num_filter))
    if source_filter:
        tasks = tasks.filter(source=source_filter)
    if search:
        tasks = tasks.filter(Q(text__icontains=search) | Q(correct_answer__icontains=search))

    tasks = tasks.order_by("task_number", "-created_at")

    # Подсчёт по номерам (для левой панели)
    from django.db.models import Count as _Count

    number_counts = (
        CatalogTask.objects.filter(task_number__isnull=False)
        .values("task_number", "exam_type")
        .annotate(cnt=_Count("id"))
        .order_by("task_number")
    )

    unclassified_count = CatalogTask.objects.filter(
        Q(task_number__isnull=True) | Q(correct_answer="", manual_grading=False)
    ).count()

    page = _paginate(request, tasks, per_page=30)

    return render(
        request,
        "admin/catalog_list.html",
        {
            "page": page,
            "exam_types": ExamType.choices,
            "sources": TaskSource.choices,
            "exam_type_filter": exam_type_filter,
            "num_filter": num_filter,
            "source_filter": source_filter,
            "search": search,
            "number_counts": list(number_counts),
            "unclassified_count": unclassified_count,
        },
    )


@admin_required
def catalog_add(request):
    """Добавить задание в каталог вручную."""
    error = None
    if request.method == "POST":
        task_number_raw = request.POST.get("task_number", "").strip()
        task_number = _safe_int(task_number_raw) if task_number_raw else None
        exam_type = request.POST.get("exam_type", "")
        text = sanitize_html(request.POST.get("text", "").strip())
        correct_answer = request.POST.get("correct_answer", "").strip()
        source = request.POST.get("source", TaskSource.MANUAL)
        points = _safe_int(request.POST.get("points", "1"), 1) or 1
        manual_grading = request.POST.get("manual_grading") == "on"
        image = request.FILES.get("image")
        shared_context = sanitize_html(request.POST.get("shared_context", "").strip())
        shared_context_image = request.FILES.get("shared_context_image")

        if exam_type not in dict(ExamType.choices):
            error = "Выберите тип экзамена"
        elif not text and not image:
            error = "Введите текст задания или загрузите изображение"
        elif not correct_answer and not manual_grading:
            error = "Введите правильный ответ или отметьте 'Ручная проверка'"
        else:
            ct = CatalogTask(
                task_number=task_number,
                exam_type=exam_type,
                text=text,
                correct_answer=correct_answer,
                source=source,
                points=points,
                manual_grading=manual_grading,
                shared_context=shared_context,
            )
            if image:
                ct.image = image
            if shared_context_image:
                ct.shared_context_image = shared_context_image
            ct.save()
            return redirect("admin_catalog")

    return render(
        request,
        "admin/catalog_task_form.html",
        {
            "exam_types": ExamType.choices,
            "sources": TaskSource.choices,
            "error": error,
            "task": None,
        },
    )


@admin_required
def catalog_edit(request, task_id):
    """Редактировать задание каталога."""
    ct = get_object_or_404(CatalogTask, id=task_id)
    error = None
    if request.method == "POST":
        task_number_raw = request.POST.get("task_number", "").strip()
        ct.task_number = _safe_int(task_number_raw) if task_number_raw else None
        ct.exam_type = request.POST.get("exam_type", ct.exam_type)
        ct.text = sanitize_html(request.POST.get("text", "").strip())
        ct.correct_answer = request.POST.get("correct_answer", "").strip()
        ct.source = request.POST.get("source", ct.source)
        ct.points = _safe_int(request.POST.get("points", "1"), 1) or 1
        ct.manual_grading = request.POST.get("manual_grading") == "on"
        ct.shared_context = sanitize_html(request.POST.get("shared_context", "").strip())
        if request.FILES.get("image"):
            ct.image = request.FILES["image"]
        if request.FILES.get("shared_context_image"):
            ct.shared_context_image = request.FILES["shared_context_image"]

        if ct.exam_type not in dict(ExamType.choices):
            error = "Выберите тип экзамена"
        else:
            ct.save()
            return redirect("admin_catalog")

    return render(
        request,
        "admin/catalog_task_form.html",
        {
            "exam_types": ExamType.choices,
            "sources": TaskSource.choices,
            "error": error,
            "task": ct,
        },
    )


@admin_required
@require_POST
def catalog_delete(request, task_id):
    ct = get_object_or_404(CatalogTask, id=task_id)
    ct.delete()
    return redirect("admin_catalog")


@admin_required
@require_POST
def catalog_bulk_delete(request):
    ids = request.POST.getlist("ids")
    if ids:
        deleted, _ = CatalogTask.objects.filter(id__in=ids).delete()
        logger.info("Bulk deleted %d catalog tasks", deleted)
    # Только именованные URL-маршруты, чтобы исключить открытый редирект
    _allowed = {"admin_catalog", "admin_catalog_unclassified"}
    next_url = request.POST.get("next", "admin_catalog")
    return redirect(next_url if next_url in _allowed else "admin_catalog")


@admin_required
def catalog_import(request):
    """Импорт заданий из СдамГИА (URL варианта или отдельного задания)."""
    errors = []
    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        import_type = request.POST.get("import_type", "variant")
        task_number_raw = request.POST.get("task_number", "").strip()

        if not url:
            errors.append("Введите URL")
        elif "sdamgia.ru" not in url:
            errors.append("Поддерживается только sdamgia.ru (Решу ОГЭ / Решу ЕГЭ)")

        if not errors:
            if import_type == "problem":
                # Одно задание — синхронно
                task_number = _safe_int(task_number_raw) if task_number_raw else None
                from .parser import import_task_to_catalog

                ct, parse_errors = import_task_to_catalog(url, task_number=task_number)
                if parse_errors:
                    errors.extend(parse_errors)
                else:
                    return redirect("admin_catalog")
            else:
                # Целый вариант — фоново
                job_id = str(uuid.uuid4())
                cache.set(f"cjob:{job_id}", {"status": "running", "added": 0, "errors": []}, _JOB_TTL)
                threading.Thread(target=_run_catalog_import_job, args=(job_id, url), daemon=True).start()
                return redirect("admin_catalog_import_status", job_id=job_id)

    return render(request, "admin/catalog_import.html", {"errors": errors})


@admin_required
def catalog_import_status(request, job_id):
    job = cache.get(f"cjob:{job_id}") or {"status": "unknown", "added": 0, "errors": ["Задание не найдено"]}
    if request.GET.get("json"):
        return JsonResponse(job)
    return render(
        request,
        "admin/catalog_import_status.html",
        {
            "job_id": job_id,
            "job": job,
        },
    )


@admin_required
def catalog_unclassified(request):
    """Задания требующие внимания: без номера ИЛИ без ответа (не ручная проверка)."""
    exam_type_filter = request.GET.get("exam_type", "oge")
    tab = request.GET.get("tab", "no_number")  # no_number | no_answer
    if tab == "no_answer":
        tasks = CatalogTask.objects.filter(correct_answer="", manual_grading=False)
    else:
        tab = "no_number"
        tasks = CatalogTask.objects.filter(task_number__isnull=True)
    if exam_type_filter:
        tasks = tasks.filter(exam_type=exam_type_filter)
    tasks = tasks.order_by("-created_at")
    page = _paginate(request, tasks, per_page=20)
    no_number_count = CatalogTask.objects.filter(task_number__isnull=True).count()
    no_answer_count = CatalogTask.objects.filter(correct_answer="", manual_grading=False).count()
    return render(
        request,
        "admin/catalog_unclassified.html",
        {
            "page": page,
            "exam_types": ExamType.choices,
            "exam_type_filter": exam_type_filter,
            "task_numbers": list(range(1, 26)),
            "tab": tab,
            "no_number_count": no_number_count,
            "no_answer_count": no_answer_count,
        },
    )


@admin_required
@require_POST
def catalog_assign_number(request, task_id):
    """AJAX/form: назначить номер задания неопределённому заданию."""
    ct = get_object_or_404(CatalogTask, id=task_id)
    task_number_raw = request.POST.get("task_number", "").strip()
    ct.task_number = _safe_int(task_number_raw) if task_number_raw else None
    ct.save(update_fields=["task_number"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "task_number": ct.task_number})
    return redirect("admin_catalog_unclassified")


@admin_required
def api_catalog_tasks(request):
    """JSON API для модального окна: задания каталога по типу, номеру, поиску."""
    exam_type = request.GET.get("exam_type", "oge")
    task_number = request.GET.get("task_number", "")
    search = request.GET.get("q", "").strip()
    source = request.GET.get("source", "")

    tasks = CatalogTask.objects.filter(task_number__isnull=False)
    if exam_type:
        tasks = tasks.filter(exam_type=exam_type)
    if task_number:
        tasks = tasks.filter(task_number=_safe_int(task_number))
    if source:
        tasks = tasks.filter(source=source)
    if search:
        tasks = tasks.filter(Q(text__icontains=search) | Q(correct_answer__icontains=search))

    tasks = tasks.order_by("-created_at")[:200]

    result = []
    for ct in tasks:
        result.append(
            {
                "id": ct.id,
                "task_number": ct.task_number,
                "text_preview": ct.text or "",
                "correct_answer": ct.correct_answer,
                "source": ct.get_source_display(),
                "source_key": ct.source,
                "manual_grading": ct.manual_grading,
                "has_image": bool(ct.image),
                "image_url": ct.image.url if ct.image else None,
                "points": ct.points,
            }
        )

    return JsonResponse({"tasks": result})


@admin_required
@require_POST
def variant_from_catalog(request):
    """Создать вариант из выбранных заданий каталога."""
    variant_number = request.POST.get("variant_number", "").strip()
    exam_type = request.POST.get("exam_type", "").strip()
    selected_json = request.POST.get("selected_tasks", "{}")

    try:
        selected = json.loads(selected_json)  # {"1": catalog_id, "5": catalog_id, ...}
    except (json.JSONDecodeError, ValueError):
        selected = {}

    errors = []
    if not variant_number:
        errors.append("Введите номер/название варианта")
    if exam_type not in dict(ExamType.choices):
        errors.append("Укажите тип экзамена")
    if not selected:
        errors.append("Не выбрано ни одного задания")
    if Variant.objects.filter(number=variant_number).exists():
        errors.append(f"Вариант '{variant_number}' уже существует")

    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    try:
        with transaction.atomic():
            variant = Variant.objects.create(number=variant_number, exam_type=exam_type)
            for task_num_str, catalog_id in sorted(selected.items(), key=lambda x: int(x[0])):
                ct = CatalogTask.objects.filter(id=catalog_id).first()
                if not ct:
                    continue
                from django.core.files.base import ContentFile as _CF

                task = Task(
                    variant=variant,
                    number=task_num_str,
                    text=ct.text,
                    correct_answer=ct.correct_answer,
                    source=ct.source,
                    points=ct.points,
                    manual_grading=ct.manual_grading,
                    no_student_input=ct.no_student_input,
                    shared_context=ct.shared_context,
                )
                if ct.image:
                    try:
                        with ct.image.open("rb") as f:
                            task.image.save(
                                ct.image.name.split("/")[-1],
                                _CF(f.read()),
                                save=False,
                            )
                    except Exception:
                        pass
                if ct.shared_context_image:
                    # Переиспользуем тот же файл (не копируем)
                    task.__dict__["shared_context_image"] = ct.shared_context_image.name
                task.save()
    except IntegrityError as e:
        return JsonResponse({"ok": False, "errors": [f"Ошибка: {e}"]}, status=400)

    from django.urls import reverse

    return JsonResponse({"ok": True, "redirect": reverse("admin_variant_edit", args=[variant.id])})


@admin_required
@require_POST
def variant_auto_generate(request):
    """Автоматически создаёт вариант из каталога — по одному заданию на каждый номер."""
    from django.core.files.base import ContentFile as _CF
    from django.urls import reverse

    from .models import EXAM_TASK_COUNT

    variant_number = request.POST.get("variant_number", "").strip()
    exam_type = request.POST.get("exam_type", "oge").strip()
    strategy = request.POST.get("strategy", "random")  # random | latest

    errors = []
    if not variant_number:
        errors.append("Введите название варианта")
    if exam_type not in dict(ExamType.choices):
        errors.append("Укажите тип экзамена")
    if Variant.objects.filter(number=variant_number).exists():
        errors.append(f"Вариант «{variant_number}» уже существует")
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    task_count = EXAM_TASK_COUNT.get(exam_type, 25)
    task_numbers = range(1, task_count + 1)

    # Проверяем, какие номера отсутствуют в каталоге
    missing = []
    for n in task_numbers:
        if not CatalogTask.objects.filter(exam_type=exam_type, task_number=n).exists():
            missing.append(n)
    if missing:
        return JsonResponse(
            {
                "ok": False,
                "errors": [f"В каталоге нет заданий для номеров: {', '.join(map(str, missing))}"],
            },
            status=400,
        )

    try:
        with transaction.atomic():
            variant = Variant.objects.create(number=variant_number, exam_type=exam_type)
            for n in task_numbers:
                qs = CatalogTask.objects.filter(exam_type=exam_type, task_number=n)
                ct = qs.order_by("?").first() if strategy == "random" else qs.order_by("-created_at").first()
                task = Task(
                    variant=variant,
                    number=str(n),
                    text=ct.text,
                    correct_answer=ct.correct_answer,
                    source=ct.source,
                    points=ct.points,
                    manual_grading=ct.manual_grading,
                    no_student_input=ct.no_student_input,
                    shared_context=ct.shared_context,
                )
                if ct.image:
                    try:
                        with ct.image.open("rb") as f:
                            task.image.save(ct.image.name.split("/")[-1], _CF(f.read()), save=False)
                    except Exception:
                        pass
                if ct.shared_context_image:
                    task.__dict__["shared_context_image"] = ct.shared_context_image.name
                task.save()
    except IntegrityError as e:
        return JsonResponse({"ok": False, "errors": [f"Ошибка: {e}"]}, status=400)

    return JsonResponse({"ok": True, "redirect": reverse("admin_variant_edit", args=[variant.id])})


# ===== ПЕЧАТЬ ВАРИАНТА (DOCX) =====


def _parse_html_segments(html):
    """Разбирает HTML на сегменты: ('text', text, bold, italic, sup, sub) | ('image', src) | ('break',)."""
    from bs4 import BeautifulSoup, NavigableString, Tag

    segments = []

    def walk(node, bold=False, italic=False, sup=False, sub=False):
        if isinstance(node, NavigableString):
            text = str(node).replace("\r", "").replace("\n", " ")
            if text:
                segments.append(("text", text, bold, italic, sup, sub))
            return
        if not isinstance(node, Tag):
            return
        tag = node.name.lower()
        if tag == "img":
            src = node.get("src", "")
            if src:
                segments.append(("image", src))
            return
        if tag == "br":
            segments.append(("break",))
            return
        if tag in ("p", "div", "li"):
            for c in node.children:
                walk(c, bold, italic, sup, sub)
            segments.append(("break",))
            return
        if tag in ("td", "th"):
            for c in node.children:
                walk(c, bold, italic, sup, sub)
            segments.append(("text", "  ", False, False, False, False))
            return
        if tag == "tr":
            for c in node.children:
                walk(c, bold, italic, sup, sub)
            segments.append(("break",))
            return
        if tag in ("b", "strong"):
            for c in node.children:
                walk(c, True, italic, sup, sub)
        elif tag in ("i", "em"):
            for c in node.children:
                walk(c, bold, True, sup, sub)
        elif tag == "sup":
            for c in node.children:
                walk(c, bold, italic, True, False)
        elif tag == "sub":
            for c in node.children:
                walk(c, bold, italic, False, True)
        elif tag == "span":
            classes = node.get("class", [])
            if isinstance(classes, str):
                classes = classes.split()
            if "math-frac" in classes:
                spans = list(node.find_all("span", recursive=False))
                if len(spans) >= 2:
                    num = spans[0].get_text(strip=True)
                    den = spans[1].get_text(strip=True)
                    segments.append(("text", f"({num})/({den})", bold, italic, sup, sub))
                else:
                    for c in node.children:
                        walk(c, bold, italic, sup, sub)
            else:
                for c in node.children:
                    walk(c, bold, italic, sup, sub)
        else:
            for c in node.children:
                walk(c, bold, italic, sup, sub)

    soup = BeautifulSoup(html or "", "html.parser")
    for child in soup.children:
        walk(child)
    while segments and segments[-1][0] == "break":
        segments.pop()
    return segments


def _get_image_bytes(src):
    """Загружает изображение по src (/media/... или http...) и возвращает bytes или None."""
    import os

    import requests as _req
    from django.conf import settings as dj_settings

    try:
        if src.startswith("http://") or src.startswith("https://"):
            r = _req.get(src, timeout=15)
            if r.status_code == 200:
                return r.content
        elif src.startswith("/media/"):
            rel = src[len("/media/") :]
            local = os.path.join(str(getattr(dj_settings, "MEDIA_ROOT", "")), rel.replace("/", os.sep))
            if os.path.exists(local):
                with open(local, "rb") as f:
                    return f.read()
            # Cloudinary или другое хранилище — получаем URL и скачиваем
            from django.core.files.storage import default_storage

            try:
                url = default_storage.url(rel)
                if url.startswith("http://") or url.startswith("https://"):
                    r = _req.get(url, timeout=15)
                    if r.status_code == 200:
                        return r.content
                else:
                    with default_storage.open(rel) as f:
                        return f.read()
            except Exception:
                pass
    except Exception as e:
        logger.warning("Не удалось загрузить изображение %s: %s", src, e)
    return None


def _svg_to_png(svg_bytes):
    """Конвертирует SVG байты в PNG байты через PyMuPDF. Возвращает None при ошибке."""
    try:
        import fitz  # PyMuPDF

        fitz_doc = fitz.open("svg", svg_bytes)
        pix = fitz_doc[0].get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
        return pix.tobytes("png")
    except Exception as e:
        logger.warning("SVG→PNG конвертация не удалась: %s", e)
        return None


def _render_segments(doc, segments, indent=None, font_size=None):
    """Рендерит сегменты в документ, создавая параграфы по мере нужды."""
    import io

    from docx.shared import Cm, Pt

    current = [None]

    def get_para():
        if current[0] is None:
            p = doc.add_paragraph()
            if indent:
                p.paragraph_format.left_indent = indent
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(2)
            current[0] = p
        return current[0]

    def close():
        current[0] = None

    for seg in segments:
        if seg[0] == "text":
            _, text, bold, italic, sup, sub = seg
            run = get_para().add_run(text)
            if bold:
                run.bold = True
            if italic:
                run.italic = True
            run.font.superscript = sup
            run.font.subscript = sub
            if font_size:
                run.font.size = font_size
        elif seg[0] == "break":
            close()
        elif seg[0] == "image":
            close()
            img_src = seg[1]
            img_data = _get_image_bytes(img_src)
            if img_data:
                # Конвертируем SVG → PNG (python-docx не поддерживает SVG)
                is_svg = img_src.lower().endswith(".svg") or img_data[:5] in (b"<svg ", b"<?xml")
                if is_svg:
                    img_data = _svg_to_png(img_data)
                if img_data:
                    try:
                        doc.add_picture(io.BytesIO(img_data), width=Cm(14))
                    except Exception as e:
                        logger.warning("Не удалось вставить изображение: %s", e)
            close()


def _build_variant_docx(variant, include_answers):
    """Строит docx-документ для варианта. Возвращает BytesIO."""
    import io

    import requests as _req
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()

    # Компактные поля
    for section in doc.sections:
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(2)
        section.right_margin = Cm(1.5)

    FS = Pt(11)  # основной размер шрифта

    def _set_para_spacing(p, before=0, after=2):
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)

    # Заголовок
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_para_spacing(title, before=0, after=2)
    r = title.add_run(f"Вариант {variant.number}")
    r.bold = True
    r.font.size = Pt(14)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_para_spacing(sub, before=0, after=4)
    sub.add_run(variant.get_exam_type_display()).font.size = Pt(11)

    tasks = list(variant.tasks.order_by("id"))
    printed_ctx = set()  # ключи уже напечатанных общих условий

    for task in tasks:
        # Общее условие — печатаем ОДИН раз перед первым заданием группы
        has_ctx = task.shared_context or task.shared_context_image
        if has_ctx:
            ctx_key = (
                " ".join((task.shared_context or "").split()),
                str(task.shared_context_image) if task.shared_context_image else "",
            )
            if ctx_key not in printed_ctx:
                printed_ctx.add(ctx_key)

                ctx_h = doc.add_paragraph()
                _set_para_spacing(ctx_h, before=6, after=2)
                cr = ctx_h.add_run("Общее условие")
                cr.bold = True
                cr.font.size = Pt(11)
                cr.font.color.rgb = RGBColor(0x33, 0x66, 0x99)

                if task.shared_context_image:
                    try:
                        ci_url = task.shared_context_image.url
                        if ci_url.startswith("http"):
                            ci_data = _req.get(ci_url, timeout=15).content
                        else:
                            with task.shared_context_image.open("rb") as f:
                                ci_data = f.read()
                        doc.add_picture(io.BytesIO(ci_data), width=Cm(15))
                    except Exception:
                        logger.warning("Не удалось вставить изображение общего условия")

                if task.shared_context:
                    _render_segments(doc, _parse_html_segments(task.shared_context), font_size=FS)

        # Заголовок задания
        th = doc.add_paragraph()
        _set_para_spacing(th, before=5, after=1)
        hr = th.add_run(f"Задание {task.number}")
        hr.bold = True
        hr.font.size = Pt(11)

        # Текст задания
        if task.text:
            _render_segments(doc, _parse_html_segments(task.text), font_size=FS)

        # Основное изображение задания
        if task.image:
            try:
                img_url = task.image.url
                if img_url.startswith("http"):
                    img_data = _req.get(img_url, timeout=15).content
                else:
                    with task.image.open("rb") as f:
                        img_data = f.read()
                doc.add_picture(io.BytesIO(img_data), width=Cm(14))
            except Exception:
                logger.warning("Не удалось вставить картинку задания %s", task.number)

    # ─── Таблица ответов в конце ──────────────────────────────────────────
    auto_tasks = [t for t in tasks if not t.no_student_input]
    if auto_tasks:
        sep = doc.add_paragraph()
        _set_para_spacing(sep, before=10, after=4)
        sr = sep.add_run("Таблица ответов")
        sr.bold = True
        sr.font.size = Pt(12)

        chunk_size = 13
        chunks = [auto_tasks[i : i + chunk_size] for i in range(0, len(auto_tasks), chunk_size)]

        for chunk in chunks:
            cols = len(chunk) + 1
            tbl = doc.add_table(rows=2, cols=cols)
            tbl.style = "Table Grid"

            # Строка с номерами заданий
            num_row = tbl.rows[0].cells
            num_row[0].text = "№"
            for i, t in enumerate(chunk):
                num_row[i + 1].text = str(t.number)

            # Строка с ответами
            ans_row = tbl.rows[1].cells
            ans_row[0].text = "Ответ"
            for i, t in enumerate(chunk):
                ans_row[i + 1].text = (t.correct_answer or "—") if include_answers else ""

            # Форматирование ячеек: мелкий шрифт, компактная высота, центр
            for row in tbl.rows:
                tr_el = row._tr
                trPr = tr_el.get_or_add_trPr()
                trH = OxmlElement("w:trHeight")
                trH.set(qn("w:val"), "320")
                trH.set(qn("w:hRule"), "exact")
                trPr.append(trH)
                for cell in row.cells:
                    for para in cell.paragraphs:
                        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        _set_para_spacing(para, before=0, after=0)
                        for run in para.runs:
                            run.font.size = Pt(9)

            sp = doc.add_paragraph()
            _set_para_spacing(sp, before=0, after=4)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


@admin_required
def variant_print_docx(request, variant_id, mode):
    """Скачать docx: mode='teacher' (с ответами) или 'student' (без)."""
    variant = get_object_or_404(Variant, id=variant_id)
    safe_num = variant.number.replace("/", "-")
    include_answers = mode == "teacher"
    suffix = " (ответы)" if include_answers else ""

    buf = _build_variant_docx(variant, include_answers=include_answers)

    fname = f"{safe_num} вариант{suffix}.docx"
    response = HttpResponse(
        buf,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


@admin_required
@require_POST
def variants_print_zip(request):
    """Скачать ZIP с DOCX-файлами для выбранных вариантов."""
    import io
    import zipfile

    ids = request.POST.getlist("ids")
    if not ids:
        return HttpResponse("Не выбрано ни одного варианта", status=400)

    variants = Variant.objects.filter(id__in=ids)
    if not variants.exists():
        return HttpResponse("Варианты не найдены", status=404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for variant in variants:
            docx_buf = _build_variant_docx(variant, include_answers=True)
            safe_num = variant.number.replace("/", "-")
            zf.writestr(f"{safe_num} вариант (ответы).docx", docx_buf.read())

    buf.seek(0)
    response = HttpResponse(buf, content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="варианты.zip"'
    return response


@admin_required
@require_POST
def variants_bulk_toggle(request):
    """Массовая активация / скрытие вариантов."""
    ids = request.POST.getlist("ids")
    action = request.POST.get("action")
    if ids and action in ("activate", "hide"):
        Variant.objects.filter(id__in=ids).update(is_active=(action == "activate"))
    return redirect("admin_variants")


@admin_required
@require_POST
def variants_bulk_delete(request):
    """Массовое удаление вариантов."""
    ids = request.POST.getlist("ids")
    if ids:
        Variant.objects.filter(id__in=ids).delete()
    return redirect("admin_variants")


# ===== ФИПИ ИМПОРТ =====


def _run_fipi_import_job(job_id, proj, exam_type, theme_filter, session_id):
    """
    Фоновый поток: импортирует задания ФИПИ в каталог.
    theme_filter — строка из кодов тем через запятую, либо пустая (все задания).
    """
    from django.db import connection

    try:
        from .fipi_parser import import_fipi_to_catalog

        # Разбиваем темы; пустой список → один проход без фильтра
        themes = [t.strip() for t in theme_filter.split(",") if t.strip()] or [""]
        for theme in themes:
            import_fipi_to_catalog(proj, exam_type, theme, session_id)

        sess = CatalogImportSession.objects.get(id=session_id)
        cache.set(
            f"fjob:{job_id}",
            {
                "status": "done",
                "session_id": session_id,
                "added": sess.tasks_added,
                "skipped": sess.tasks_skipped,
                "duplicate": sess.tasks_duplicate,
                "errors": [],
            },
            _JOB_TTL,
        )
    except Exception as e:
        logger.exception("Ошибка ФИПИ импорта")
        cache.set(
            f"fjob:{job_id}",
            {
                "status": "error",
                "session_id": session_id,
                "added": 0,
                "skipped": 0,
                "duplicate": 0,
                "errors": [str(e)],
            },
            _JOB_TTL,
        )
        try:
            CatalogImportSession.objects.filter(id=session_id).update(status="error", notes=str(e))
        except Exception:
            pass
    finally:
        connection.close()


@admin_required
def catalog_fipi_import(request):
    """Страница импорта из ФИПИ: превью + запуск."""
    return render(
        request,
        "admin/catalog_import_fipi.html",
        {
            "exam_types": ExamType.choices,
        },
    )


@admin_required
def catalog_fipi_preview(request):
    """AJAX: получить инфо о проекте ФИПИ по URL."""
    url = request.GET.get("url", "").strip()
    if not url:
        return JsonResponse({"error": "Введите URL"}, status=400)
    try:
        from .fipi_parser import fipi_get_preview

        data = fipi_get_preview(url)
        return JsonResponse(data)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@admin_required
@require_POST
def catalog_fipi_start(request):
    """POST: запустить фоновый импорт ФИПИ."""
    proj = request.POST.get("proj", "").strip()
    exam_type = request.POST.get("exam_type", "").strip()
    theme_filter = request.POST.get("theme_filter", "").strip()

    errors = []
    if not proj:
        errors.append("Не найден GUID проекта")
    if exam_type not in dict(ExamType.choices):
        errors.append("Выберите тип экзамена")
    if errors:
        return JsonResponse({"error": "; ".join(errors)}, status=400)

    sess = CatalogImportSession.objects.create(
        source=TaskSource.FIPI,
        url=request.POST.get("url", ""),
        proj_guid=proj,
        status="running",
    )
    job_id = str(uuid.uuid4())
    cache.set(
        f"fjob:{job_id}",
        {
            "status": "running",
            "session_id": sess.id,
            "added": 0,
            "skipped": 0,
            "duplicate": 0,
            "errors": [],
        },
        _JOB_TTL,
    )
    threading.Thread(
        target=_run_fipi_import_job,
        args=(job_id, proj, exam_type, theme_filter, sess.id),
        daemon=True,
    ).start()
    from django.urls import reverse

    return JsonResponse(
        {
            "ok": True,
            "job_id": job_id,
            "redirect": reverse("admin_fipi_import_status", args=[job_id]),
        }
    )


@admin_required
def catalog_fipi_status(request, job_id):
    """Страница/API статуса импорта ФИПИ."""
    job = cache.get(f"fjob:{job_id}") or {
        "status": "unknown",
        "session_id": None,
        "added": 0,
        "skipped": 0,
        "duplicate": 0,
        "errors": ["Задание не найдено"],
    }

    # Дополнить из БД если есть session_id
    if job.get("session_id") and job["status"] == "running":
        try:
            sess = CatalogImportSession.objects.get(id=job["session_id"])
            job = dict(
                job,
                added=sess.tasks_added,
                skipped=sess.tasks_skipped,
                duplicate=sess.tasks_duplicate,
                status=sess.status if sess.status != "running" else "running",
            )
        except CatalogImportSession.DoesNotExist:
            pass

    if request.GET.get("json"):
        return JsonResponse(job)

    session_obj = None
    if job.get("session_id"):
        session_obj = CatalogImportSession.objects.filter(id=job["session_id"]).first()

    return render(
        request,
        "admin/catalog_import_fipi_status.html",
        {
            "job_id": job_id,
            "job": job,
            "session": session_obj,
        },
    )


@admin_required
def catalog_import_list(request):
    """Список всех сессий импорта."""
    sessions = CatalogImportSession.objects.order_by("-created_at")
    return render(
        request,
        "admin/catalog_import_list.html",
        {
            "sessions": sessions,
        },
    )


@admin_required
@require_POST
def catalog_import_session_delete(request, session_id):
    """Удалить сессию импорта (с заданиями или без)."""
    sess = get_object_or_404(CatalogImportSession, id=session_id)
    delete_tasks = request.POST.get("delete_tasks") == "1"
    if delete_tasks:
        CatalogTask.objects.filter(import_session=sess).delete()
    sess.delete()
    return redirect("admin_import_list")
