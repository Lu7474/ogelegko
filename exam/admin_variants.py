import io
import logging
import threading
import uuid
import zipfile

from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .admin_views import _safe_int, admin_required
from .models import Answer, Attempt, ExamType, Task, TaskSource, Variant
from .parser import sanitize_html
from .services.docx_builder import build_variant_docx

logger = logging.getLogger(__name__)

_JOB_TTL = 7200  # 2 часа


# ===== ВАРИАНТЫ =====


@admin_required
def variant_list(request):
    exam_filter = request.GET.get("exam_type", "oge")
    variants = Variant.objects.exclude(number__startswith="ошибки_").annotate(
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
    """Сохраняет задания варианта из POST-данных (для новых вариантов)."""
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


def _update_variant_tasks(variant, request):
    """Обновляет задания существующего варианта без удаления (сохраняет ответы учеников)."""
    import re as _re

    indices = sorted(
        {int(m.group(1)) for key in request.POST for m in [_re.match(r"^task_(\d+)_answer$", key)] if m}
    )
    task_numbers_in_form = set()
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

        if answer or manual_grading:
            number_str = str(task_index)
            task_numbers_in_form.add(number_str)
            task, _ = Task.objects.update_or_create(
                variant=variant,
                number=number_str,
                defaults={
                    "text": text,
                    "correct_answer": answer,
                    "source": source,
                    "points": points,
                    "manual_grading": manual_grading,
                    "shared_context": shared_context,
                },
            )
            if image:
                task.image = image
                task.save(update_fields=["image"])
            if shared_context_image:
                task.shared_context_image = shared_context_image
                task.save(update_fields=["shared_context_image"])

    to_delete = variant.tasks.exclude(number__in=task_numbers_in_form)
    if to_delete.filter(answers__attempt__is_finished=True).exists():
        return "Нельзя удалить задания, по которым уже есть завершённые попытки. Сначала удалите все попытки по этому варианту."
    to_delete.delete()
    return None


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

                update_error = _update_variant_tasks(variant, request)
                if update_error:
                    error = update_error
                else:
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
            manual_grading=task.manual_grading,
            no_student_input=task.no_student_input,
            shared_context=task.shared_context,
            shared_context_image=task.shared_context_image,
        )
    return redirect("admin_variant_edit", variant_id=new_variant.id)


@admin_required
@require_POST
def variant_delete(request, variant_id):
    variant = get_object_or_404(Variant, id=variant_id)
    logger.info(
        "Администратор %s удалил вариант ID=%d ('%s')",
        request.user.username,
        variant.id,
        variant.number,
    )
    variant.delete()
    return redirect("admin_variants")


# ===== ИМПОРТ ВАРИАНТА =====


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
            {"status": "error", "variant_id": None, "errors": [str(e)]},
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
        {"job_id": job_id, "job": job, "variant": variant},
    )


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

    tasks = list(variant.tasks.order_by("id"))
    counts_map = {
        c["task_id"]: c
        for c in Answer.objects.filter(task_id__in=[t.id for t in tasks], attempt__is_finished=True)
        .values("task_id")
        .annotate(total=Count("id"), correct=Count("id", filter=Q(is_correct=True)))
    }
    task_stats = []
    for task in tasks:
        c = counts_map.get(task.id, {"total": 0, "correct": 0})
        pct = round(c["correct"] / c["total"] * 100) if c["total"] > 0 else 0
        task_stats.append({"task": task, "correct": c["correct"], "total": c["total"], "percentage": pct})

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


# ===== ПЕЧАТЬ ВАРИАНТА (DOCX) =====

# DOCX rendering logic lives in services/docx_builder.py


@admin_required
def variant_print_docx(request, variant_id, mode):
    """Скачать docx: mode='teacher' (с ответами) или 'student' (без)."""
    from urllib.parse import quote

    variant = get_object_or_404(Variant, id=variant_id)
    safe_num = variant.number.replace("/", "-")
    include_answers = mode == "teacher"
    suffix = "_answers" if include_answers else ""

    buf = build_variant_docx(variant, include_answers=include_answers)

    fname = f"{safe_num}_variant{suffix}.docx"
    fname_utf8 = quote(f"{safe_num} вариант.docx")
    response = HttpResponse(
        buf,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f"attachment; filename=\"{fname}\"; filename*=UTF-8''{fname_utf8}"
    return response


@admin_required
@require_POST
def variants_print_zip(request):
    """Скачать ZIP с DOCX-файлами для выбранных вариантов."""
    ids = request.POST.getlist("ids")
    if not ids:
        return HttpResponse("Не выбрано ни одного варианта", status=400)

    variants = Variant.objects.filter(id__in=ids)
    if not variants.exists():
        return HttpResponse("Варианты не найдены", status=404)

    from urllib.parse import quote

    numbers = [v.number for v in variants]
    safe_numbers = ", ".join(n.replace("/", "-") for n in numbers)
    zip_name_ascii = f"{safe_numbers}.zip"
    zip_name_utf8 = quote(f"{', '.join(numbers)}.zip")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for variant in variants:
            docx_buf = build_variant_docx(variant, include_answers=True)
            safe_num = variant.number.replace("/", "-")
            zf.writestr(f"{safe_num} вариант.docx", docx_buf.read())

    buf.seek(0)
    response = HttpResponse(buf, content_type="application/zip")
    response["Content-Disposition"] = (
        f"attachment; filename=\"{zip_name_ascii}\"; filename*=UTF-8''{zip_name_utf8}"
    )
    return response


@admin_required
@require_POST
def variants_archive_export(request):
    """Скачать переносимый ZIP-архив выбранных (или всех) вариантов."""
    from datetime import datetime

    from django.contrib import messages
    from django.http import FileResponse

    from .services.variant_archive import export_variants_to_zip

    ids = request.POST.getlist("ids")
    if ids:
        variants = Variant.objects.filter(id__in=ids)
    else:
        variants = Variant.objects.exclude(number__startswith="ошибки_")

    if not variants.exists():
        messages.error(request, "Нет вариантов для экспорта.")
        return redirect("admin_variants")

    buf = export_variants_to_zip(variants)
    filename = f"variants_archive_{datetime.now():%Y-%m-%d_%H-%M}.zip"
    return FileResponse(buf, as_attachment=True, filename=filename, content_type="application/zip")


@admin_required
@require_POST
def variants_archive_import(request):
    """Импортировать варианты из переносимого ZIP-архива."""
    from django.contrib import messages

    from .services.variant_archive import ArchiveImportError, import_variants_from_zip

    uploaded = request.FILES.get("archive")
    if not uploaded:
        messages.error(request, "Файл не выбран.")
        return redirect("admin_variants")

    try:
        result = import_variants_from_zip(uploaded)
    except ArchiveImportError as e:
        messages.error(request, f"Ошибка архива: {e}")
        return redirect("admin_variants")
    except Exception as e:
        logger.exception("Непредвиденная ошибка при импорте архива вариантов")
        messages.error(request, f"Ошибка импорта: {e}")
        return redirect("admin_variants")

    parts = [f"Импортировано: {result['variants_created']} вар., {result['tasks_created']} зад."]
    if result["renamed"]:
        parts.append("Переименованы: " + "; ".join(result["renamed"]))
    if result["errors"]:
        parts.append("Ошибки: " + "; ".join(result["errors"]))

    messages.success(request, " ".join(parts))
    return redirect("admin_variants")


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
