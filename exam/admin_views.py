import logging
from datetime import timedelta
from functools import wraps

from django.conf import settings as django_settings
from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Attempt, SchoolClass, Student, Variant

logger = logging.getLogger(__name__)


# ===== ОБЩИЕ ХЕЛПЕРЫ =====


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


# ===== ВХОД / ВЫХОД =====


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


# ===== ДАШБОРД =====


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
            {"school_class": sc, "attempts_count": attempts_count, "avg_percentage": avg_pct}
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


# ===== ЭКСПОРТ РЕЗУЛЬТАТОВ =====


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

    max_tasks = max((a.variant.tasks.count() for a in attempts), default=25)

    doc = Document()
    section = doc.sections[0]
    section.orientation = 1
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = Cm(1.5)
    section.right_margin = Cm(1.5)
    section.top_margin = Cm(1.5)
    section.bottom_margin = Cm(1.5)

    doc.add_heading("Результаты экзаменов", level=1)

    col_count = 3 + max_tasks + 2
    table = doc.add_table(rows=1, cols=col_count)
    table.style = "Table Grid"

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
        answers_map = {ans.task.number: ans.is_correct for ans in a.answers.all()}
        tasks = list(a.variant.tasks.order_by("id"))

        row_cells = table.add_row().cells
        row_cells[0].text = a.student.full_name
        row_cells[1].text = a.finished_at.strftime("%d.%m.%Y") if a.finished_at else ""
        row_cells[2].text = a.variant.number

        for i, task in enumerate(tasks):
            if i >= max_tasks:
                break
            correct = answers_map.get(task.number)
            row_cells[3 + i].text = "1" if correct is True else ("0" if correct is False else "")

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
