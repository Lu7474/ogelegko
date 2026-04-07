import json
import logging
import threading
import uuid
import time as _time
from functools import wraps
from django.conf import settings as django_settings
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.http import require_POST
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q, Avg
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from .models import (
    SchoolClass, Student, Variant, Task, Attempt, Answer,
    ExamType, TaskSource, TaskTopic,
)

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
    max_attempts = getattr(django_settings, "LOGIN_MAX_ATTEMPTS", 5)
    cooldown = getattr(django_settings, "LOGIN_COOLDOWN_SECONDS", 300)
    ip = _get_client_ip(request)
    lock_key = f"admin_login_lock:{ip}"
    fail_key = f"admin_login_fails:{ip}"

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
                return redirect("admin_dashboard")
            _record_admin_failed_login(request)
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
    recent_attempts = Attempt.objects.filter(
        is_finished=True
    ).select_related("student", "variant", "student__school_class").order_by("-finished_at")[:10]

    pending_grading = Attempt.objects.filter(
        is_finished=True, answers__is_correct=None
    ).distinct().count()

    # Статистика по классам
    classes = SchoolClass.objects.filter(is_active=True).annotate(
        student_count=Count("students")
    )
    class_stats_list = []
    for sc in classes:
        attempts_qs = Attempt.objects.filter(
            student__school_class=sc, is_finished=True
        )
        attempts_count = attempts_qs.count()
        if attempts_count > 0:
            percentages = [a.percentage for a in attempts_qs]
            avg_pct = round(sum(percentages) / len(percentages))
        else:
            avg_pct = None
        class_stats_list.append({
            "school_class": sc,
            "attempts_count": attempts_count,
            "avg_percentage": avg_pct,
        })

    return render(request, "admin/dashboard.html", {
        "stats": stats,
        "recent_attempts": recent_attempts,
        "class_stats": class_stats_list,
        "pending_grading": pending_grading,
    })


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

    return render(request, "admin/class_form.html", {
        "exam_types": ExamType.choices,
        "error": error,
    })


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

    return render(request, "admin/class_form.html", {
        "school_class": school_class,
        "exam_types": ExamType.choices,
        "error": error,
    })


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
        attempts = Attempt.objects.filter(
            student=student, is_finished=True
        ).select_related("variant").order_by("-finished_at")
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

        student_stats_list.append({
            "student": student,
            "attempts_count": count,
            "avg_percentage": avg_pct,
            "last_attempt": last_attempt,
        })

    class_avg = round(sum(all_percentages) / len(all_percentages)) if all_percentages else None

    return render(request, "admin/class_stats.html", {
        "school_class": school_class,
        "student_stats": student_stats_list,
        "total_attempts": total_attempts,
        "class_avg": class_avg,
    })


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
    return render(request, "admin/students.html", {
        "students": students,
        "classes": classes,
        "class_filter": class_filter,
    })


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
    return render(request, "admin/student_form.html", {
        "student": student,
        "classes": classes,
        "error": error,
    })


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
            return render(request, "admin/student_import.html", {
                "errors": errors, "success_count": success_count,
            })

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
                            errors.append(f"Строка {row_num}: не все поля заполнены (нужно: ФИО, пароль, класс)")
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

    return render(request, "admin/student_import.html", {
        "errors": errors,
        "success_count": success_count,
    })


@admin_required
def student_stats(request, student_id):
    student = get_object_or_404(Student.objects.select_related("school_class"), id=student_id)
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

    return render(request, "admin/student_stats.html", {
        "student": student,
        "attempts": attempts,
        "avg_percentage": avg_percentage,
        "chart_data": json.dumps(chart_data),
    })


# --- Варианты ---

@admin_required
def variant_list(request):
    exam_filter = request.GET.get("exam_type", "")
    variants = Variant.objects.annotate(
        task_count=Count("tasks", distinct=True),
        attempts_count=Count("attempts", filter=Q(attempts__is_finished=True), distinct=True),
    )
    if exam_filter and exam_filter in dict(ExamType.choices):
        variants = variants.filter(exam_type=exam_filter)

    return render(request, "admin/variants.html", {
        "variants": variants,
        "exam_types": ExamType.choices,
        "exam_filter": exam_filter,
    })


def _save_variant_tasks(variant, request):
    """Сохраняет задания варианта из POST-данных."""
    task_index = 1
    while f"task_{task_index}_answer" in request.POST:
        text = request.POST.get(f"task_{task_index}_text", "").strip()
        answer = request.POST.get(f"task_{task_index}_answer", "").strip()
        source = request.POST.get(f"task_{task_index}_source", "manual")
        topic = request.POST.get(f"task_{task_index}_topic", "other")
        points = _safe_int(request.POST.get(f"task_{task_index}_points", "1"), default=1)
        image = request.FILES.get(f"task_{task_index}_image")

        if points < 1:
            points = 1

        if source not in dict(TaskSource.choices):
            source = "manual"

        if topic not in dict(TaskTopic.choices):
            topic = "other"

        if answer:
            Task.objects.create(
                variant=variant,
                number=task_index,
                text=text,
                correct_answer=answer,
                source=source,
                topic=topic,
                points=points,
                image=image,
            )
        task_index += 1


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

    return render(request, "admin/variant_form.html", {
        "exam_types": ExamType.choices,
        "sources": TaskSource.choices,
        "topics": TaskTopic.choices,
        "error": error,
    })


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

    return render(request, "admin/variant_form.html", {
        "variant": variant,
        "tasks": tasks,
        "exam_types": ExamType.choices,
        "sources": TaskSource.choices,
        "topics": TaskTopic.choices,
        "error": error,
    })


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
            topic=task.topic,
            points=task.points,
        )
    return redirect("admin_variant_edit", variant_id=new_variant.id)


@admin_required
@require_POST
def variant_delete(request, variant_id):
    variant = get_object_or_404(Variant, id=variant_id)
    variant.delete()
    return redirect("admin_variants")


_import_jobs = {}  # job_id -> {status, variant_id, errors}


def _run_import_job(job_id, url, variant_number):
    """Фоновый поток для импорта варианта."""
    try:
        from .parser import import_variant_from_sdamgia
        variant, parse_errors = import_variant_from_sdamgia(
            url, variant_number=variant_number or None
        )
        _import_jobs[job_id] = {
            "status": "done",
            "variant_id": variant.id if variant else None,
            "errors": parse_errors,
        }
    except Exception as e:
        logger.exception("Ошибка импорта варианта")
        _import_jobs[job_id] = {
            "status": "error",
            "variant_id": None,
            "errors": [str(e)],
        }
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
        _import_jobs[job_id] = {"status": "running", "variant_id": None, "errors": []}
        threading.Thread(
            target=_run_import_job, args=(job_id, url, variant_number), daemon=True
        ).start()
        return redirect("admin_variant_import_status", job_id=job_id)

    return render(request, "admin/variant_import.html", {"errors": []})


@admin_required
def variant_import_status(request, job_id):
    """Страница/API опроса статуса импорта."""
    job = _import_jobs.get(job_id, {"status": "unknown", "variant_id": None, "errors": ["Задание не найдено"]})

    if request.GET.get("json"):
        return JsonResponse(job)

    variant = None
    if job["status"] == "done" and job.get("variant_id"):
        variant = Variant.objects.filter(id=job["variant_id"]).first()

    return render(request, "admin/variant_import_status.html", {
        "job_id": job_id,
        "job": job,
        "variant": variant,
    })


# --- Статистика варианта ---

@admin_required
def variant_stats(request, variant_id):
    variant = get_object_or_404(Variant, id=variant_id)
    attempts = Attempt.objects.filter(
        variant=variant, is_finished=True
    ).select_related("student", "student__school_class").order_by("-finished_at")

    avg_percentage = None
    if attempts.exists():
        percentages = [a.percentage for a in attempts]
        avg_percentage = round(sum(percentages) / len(percentages))

    # Статистика по заданиям
    task_stats = []
    for task in variant.tasks.order_by("id"):
        total = Answer.objects.filter(
            task=task, attempt__is_finished=True
        ).count()
        correct = Answer.objects.filter(
            task=task, attempt__is_finished=True, is_correct=True
        ).count()
        pct = round(correct / total * 100) if total > 0 else 0
        task_stats.append({
            "task": task,
            "correct": correct,
            "total": total,
            "percentage": pct,
        })

    return render(request, "admin/variant_stats.html", {
        "variant": variant,
        "attempts": attempts,
        "avg_percentage": avg_percentage,
        "task_stats": task_stats,
    })


# --- Просмотр и ручная проверка попытки ---

@admin_required
def attempt_detail(request, attempt_id):
    attempt = get_object_or_404(Attempt, id=attempt_id, is_finished=True)
    answers = attempt.answers.select_related("task").order_by("task__id")
    has_pending = answers.filter(is_correct=None).exists()
    return render(request, "admin/attempt_detail.html", {
        "attempt": attempt,
        "answers": answers,
        "has_pending": has_pending,
    })


@admin_required
@require_POST
def attempt_grade_answer(request, answer_id):
    from .views import _recalculate_attempt_score
    answer = get_object_or_404(Answer, id=answer_id, task__manual_grading=True)
    value = request.POST.get("is_correct")
    if value == "true":
        answer.is_correct = True
    elif value == "false":
        answer.is_correct = False
    else:
        answer.is_correct = None
    answer.save(update_fields=["is_correct"])
    _recalculate_attempt_score(answer.attempt)
    return redirect("admin_attempt_detail", attempt_id=answer.attempt_id)


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

    attempts = Attempt.objects.filter(is_finished=True).select_related(
        "student", "student__school_class", "variant"
    ).order_by("-finished_at")

    if class_id:
        attempts = attempts.filter(student__school_class_id=class_id)
    if variant_id:
        attempts = attempts.filter(variant_id=variant_id)

    for a in attempts:
        ws.append([
            a.student.full_name,
            a.student.school_class.name,
            a.student.school_class.get_exam_type_display(),
            a.variant.number,
            a.finished_at.strftime("%d.%m.%Y %H:%M") if a.finished_at else "",
            a.score,
            a.max_score,
            a.grade,
            a.duration_display,
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="results.xlsx"'
    wb.save(response)
    return response
