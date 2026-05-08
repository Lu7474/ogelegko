import json
import logging
import os
import re
import threading
import uuid

from django.contrib import messages
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .admin_views import _paginate, _safe_int, admin_required
from .models import (
    CatalogImportSession,
    CatalogTask,
    CatalogTaskImage,
    ExamType,
    Task,
    TaskImage,
    TaskSource,
    Variant,
)
from .parsers.pdf import run_pdf_import_job
from .parsers.sdamgia import sanitize_html

logger = logging.getLogger(__name__)

_JOB_TTL = 7200  # 2 часа


# ===== КАТАЛОГ ЗАДАНИЙ =====


def _run_catalog_import_job(job_id, url):
    """Фоновый поток: парсит вариант и добавляет все задания в каталог."""
    try:
        from .parsers.sdamgia import import_variant_to_catalog

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
            {"status": "done", "added": added, "errors": errors, "session_id": sess.id},
            _JOB_TTL,
        )
    except Exception as e:
        logger.exception("Ошибка импорта в каталог")
        cache.set(
            f"cjob:{job_id}",
            {"status": "error", "added": 0, "errors": [str(e)]},
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

    number_counts = (
        CatalogTask.objects.filter(task_number__isnull=False)
        .values("task_number", "exam_type")
        .annotate(cnt=Count("id"))
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
            for i, f in enumerate(request.FILES.getlist("extra_images")):
                CatalogTaskImage.objects.create(task=ct, image=f, order=i)
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
            extra = request.FILES.getlist("extra_images")
            if extra:
                start_order = ct.extra_images.count()
                for i, f in enumerate(extra):
                    CatalogTaskImage.objects.create(task=ct, image=f, order=start_order + i)
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
                task_number = _safe_int(task_number_raw) if task_number_raw else None
                from .parsers.sdamgia import import_task_to_catalog

                ct, parse_errors = import_task_to_catalog(url, task_number=task_number)
                if parse_errors:
                    errors.extend(parse_errors)
                else:
                    return redirect("admin_catalog")
            else:
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
        {"job_id": job_id, "job": job},
    )


@admin_required
def catalog_unclassified(request):
    """Задания требующие внимания: без номера ИЛИ без ответа (не ручная проверка)."""
    exam_type_filter = request.GET.get("exam_type", "oge")
    source_filter = request.GET.get("source", "")
    tab = request.GET.get("tab", "no_number")
    if tab == "no_answer":
        tasks = CatalogTask.objects.filter(correct_answer="", manual_grading=False)
    else:
        tab = "no_number"
        tasks = CatalogTask.objects.filter(task_number__isnull=True)
    if exam_type_filter:
        tasks = tasks.filter(exam_type=exam_type_filter)
    if source_filter:
        tasks = tasks.filter(source=source_filter)
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
            "sources": TaskSource.choices,
            "exam_type_filter": exam_type_filter,
            "source_filter": source_filter,
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
    from .parsers.sdamgia import _get_oge_default_points, _is_no_input_task

    ct = get_object_or_404(CatalogTask, id=task_id)
    task_number_raw = request.POST.get("task_number", "").strip()
    ct.task_number = _safe_int(task_number_raw) if task_number_raw else None

    # Автоматически выставляем no_student_input и points по номеру задания
    if ct.task_number is not None:
        ct.no_student_input = _is_no_input_task(ct.exam_type, ct.task_number)
        if ct.exam_type == "oge":
            ct.points = _get_oge_default_points(ct.task_number)
    else:
        ct.no_student_input = False

    ct.save(update_fields=["task_number", "no_student_input", "points"])
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

    if task_number:
        tasks = tasks.order_by("-created_at")
    else:
        tasks = tasks.order_by("-created_at")[:200]

    result = [
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
        for ct in tasks
    ]

    return JsonResponse({"tasks": result})


@admin_required
def api_catalog_counts(request):
    """JSON API для левой панели: количество заданий по номерам."""
    exam_type = request.GET.get("exam_type", "oge")
    rows = (
        CatalogTask.objects.filter(task_number__isnull=False, exam_type=exam_type)
        .values("task_number")
        .annotate(cnt=Count("id"))
        .order_by("task_number")
    )
    return JsonResponse({"counts": {row["task_number"]: row["cnt"] for row in rows}})


@admin_required
@require_POST
def variant_from_catalog(request):
    """Создать вариант из выбранных заданий каталога."""
    from django.core.files.base import ContentFile as _CF
    from django.urls import reverse

    variant_number = request.POST.get("variant_number", "").strip()
    exam_type = request.POST.get("exam_type", "").strip()
    selected_json = request.POST.get("selected_tasks", "{}")

    try:
        selected = json.loads(selected_json)
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

    failed_images = []
    try:
        with transaction.atomic():
            variant = Variant.objects.create(number=variant_number, exam_type=exam_type)
            for task_num_str, catalog_id in sorted(selected.items(), key=lambda x: int(x[0])):
                ct = CatalogTask.objects.filter(id=catalog_id).first()
                if not ct:
                    continue
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
                            task.image.save(ct.image.name.split("/")[-1], _CF(f.read()), save=False)
                    except Exception:
                        logger.warning(
                            "Не удалось скопировать изображение ct_id=%s задание №%s", ct.id, task_num_str
                        )
                        failed_images.append(task_num_str)
                if ct.shared_context_image:
                    task.shared_context_image = ct.shared_context_image.name
                task.save()
                for ci in ct.extra_images.order_by("order"):
                    try:
                        with ci.image.open("rb") as f:
                            ti = TaskImage(task=task, order=ci.order)
                            ti.image.save(ci.image.name.split("/")[-1], _CF(f.read()), save=False)
                            ti.save()
                    except Exception:
                        logger.warning(
                            "Не удалось скопировать доп. изображение ct_id=%s задание №%s",
                            ct.id,
                            task_num_str,
                        )
    except IntegrityError as e:
        return JsonResponse({"ok": False, "errors": [f"Ошибка: {e}"]}, status=400)

    if failed_images:
        messages.warning(
            request,
            f"Картинки не скопировались для заданий: {', '.join(failed_images)}. Проверьте вручную.",
        )
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
    strategy = request.POST.get("strategy", "random")

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

    missing = [
        n for n in task_numbers if not CatalogTask.objects.filter(exam_type=exam_type, task_number=n).exists()
    ]
    if missing:
        return JsonResponse(
            {"ok": False, "errors": [f"В каталоге нет заданий для номеров: {', '.join(map(str, missing))}"]},
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
                    task.shared_context_image = ct.shared_context_image.name
                task.save()
                for ci in ct.extra_images.order_by("order"):
                    try:
                        with ci.image.open("rb") as f:
                            ti = TaskImage(task=task, order=ci.order)
                            ti.image.save(ci.image.name.split("/")[-1], _CF(f.read()), save=False)
                            ti.save()
                    except Exception:
                        pass
    except IntegrityError as e:
        return JsonResponse({"ok": False, "errors": [f"Ошибка: {e}"]}, status=400)

    return JsonResponse({"ok": True, "redirect": reverse("admin_variants")})


# ===== ФИПИ ИМПОРТ =====


def _run_fipi_import_job(job_id, proj, exam_type, theme_filter, session_id):
    """Фоновый поток: импортирует задания ФИПИ в каталог."""
    from django.db import connection

    try:
        from .parsers.fipi import import_fipi_to_catalog

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
    return render(request, "admin/catalog_import_fipi.html", {"exam_types": ExamType.choices})


@admin_required
def catalog_fipi_preview(request):
    url = request.GET.get("url", "").strip()
    if not url:
        return JsonResponse({"error": "Введите URL"}, status=400)
    try:
        from .parsers.fipi import fipi_get_preview

        data = fipi_get_preview(url)
        return JsonResponse(data)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@admin_required
@require_POST
def catalog_fipi_start(request):
    from django.urls import reverse

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
        {"status": "running", "session_id": sess.id, "added": 0, "skipped": 0, "duplicate": 0, "errors": []},
        _JOB_TTL,
    )
    threading.Thread(
        target=_run_fipi_import_job,
        args=(job_id, proj, exam_type, theme_filter, sess.id),
        daemon=True,
    ).start()
    return JsonResponse(
        {"ok": True, "job_id": job_id, "redirect": reverse("admin_fipi_import_status", args=[job_id])}
    )


@admin_required
def catalog_fipi_status(request, job_id):
    job = cache.get(f"fjob:{job_id}") or {
        "status": "unknown",
        "session_id": None,
        "added": 0,
        "skipped": 0,
        "duplicate": 0,
        "errors": ["Задание не найдено"],
    }

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
        {"job_id": job_id, "job": job, "session": session_obj},
    )


@admin_required
def catalog_pdf_import(request):
    """Загрузка и парсинг PDF файлов через сайт."""
    errors = []
    if request.method == "POST":
        exam_type = request.POST.get("exam_type", "")
        mode = request.POST.get("mode", "catalog")
        fmt = request.POST.get("format", "print_solve")
        files = request.FILES.getlist("pdf_files")

        if exam_type not in dict(ExamType.choices):
            errors.append("Выберите тип экзамена")
        if not files:
            errors.append("Выберите PDF файл(ы)")
        if not errors:
            from django.conf import settings

            upload_dir = os.path.join(settings.MEDIA_ROOT, "pdf_uploads")
            os.makedirs(upload_dir, exist_ok=True)

            file_paths = []
            MAX_PDF_SIZE = 50 * 1024 * 1024  # 50 МБ
            for f in files:
                if not f.name.lower().endswith(".pdf"):
                    errors.append(f"Файл '{f.name}' не является PDF")
                    continue
                if f.size > MAX_PDF_SIZE:
                    errors.append(f"Файл '{f.name}' слишком большой (макс. 50 МБ)")
                    continue
                dest = os.path.join(upload_dir, f"{uuid.uuid4()}_{f.name}")
                with open(dest, "wb") as out:
                    for chunk in f.chunks():
                        out.write(chunk)
                file_paths.append(dest)

        if not errors and file_paths:
            sess = CatalogImportSession.objects.create(
                source=TaskSource.PRINT_SOLVE,
                url=", ".join(f.name for f in files),
                status="running",
            )
            job_id = str(uuid.uuid4())
            cache.set(
                f"pjob:{job_id}",
                {"status": "running", "session_id": sess.id, "added": 0},
                _JOB_TTL,
            )
            threading.Thread(
                target=run_pdf_import_job,
                args=(job_id, file_paths, exam_type, mode, fmt, sess.id),
                daemon=True,
            ).start()
            return redirect("admin_pdf_import_status", job_id=job_id)

    return render(
        request,
        "admin/catalog_pdf_import.html",
        {"exam_types": ExamType.choices, "errors": errors},
    )


@admin_required
def catalog_pdf_import_status(request, job_id):
    job = cache.get(f"pjob:{job_id}") or {"status": "unknown", "session_id": None, "added": 0}

    if job.get("session_id") and job["status"] == "running":
        try:
            sess = CatalogImportSession.objects.get(id=job["session_id"])
            job = dict(
                job, added=sess.tasks_added, status=sess.status if sess.status != "running" else "running"
            )
        except CatalogImportSession.DoesNotExist:
            pass

    if request.GET.get("json"):
        return JsonResponse(job)

    return render(request, "admin/catalog_pdf_import_status.html", {"job_id": job_id, "job": job})


@admin_required
def catalog_fipi_export_answers(request):
    """Экспорт заданий ФИПИ без ответов в JSON для AI-обработки."""
    import re as _re

    tasks = (
        CatalogTask.objects.filter(
            source=TaskSource.FIPI,
            correct_answer="",
            manual_grading=False,
        )
        .exclude(fipi_guid__isnull=True)
        .exclude(fipi_guid="")
        .prefetch_related("extra_images")
        .order_by("task_number", "id")
    )

    def strip_html(text):
        return _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", text or "")).strip()

    def abs_url(field):
        if not field:
            return None
        url = field.url
        if url.startswith("http"):
            return url
        return request.build_absolute_uri(url)

    data = [
        {
            "fipi_guid": t.fipi_guid,
            "task_number": t.task_number,
            "exam_type": t.exam_type,
            "text": strip_html(t.text),
            "image_url": abs_url(t.image),
            "shared_context": strip_html(t.shared_context),
            "shared_context_image_url": abs_url(t.shared_context_image),
            "extra_image_urls": [abs_url(ei.image) for ei in t.extra_images.all()],
        }
        for t in tasks
    ]

    response = JsonResponse(data, safe=False, json_dumps_params={"ensure_ascii": False, "indent": 2})
    response["Content-Disposition"] = 'attachment; filename="fipi_tasks_no_answer.json"'
    return response


@admin_required
@require_POST
def catalog_fipi_import_answers(request):
    """Импорт ответов ФИПИ из JSON-файла [{fipi_guid, answer}]."""
    from django.contrib import messages
    from django.urls import reverse

    f = request.FILES.get("answers_file")
    if not f:
        messages.error(request, "Выберите JSON-файл с ответами.")
        return redirect(reverse("admin_catalog_unclassified") + "?tab=no_answer")

    try:
        data = json.loads(f.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        messages.error(request, f"Ошибка чтения файла: {e}")
        return redirect(reverse("admin_catalog_unclassified") + "?tab=no_answer")

    if not isinstance(data, list):
        messages.error(request, "JSON должен быть массивом объектов.")
        return redirect(reverse("admin_catalog_unclassified") + "?tab=no_answer")

    updated = skipped = not_found = 0
    for entry in data:
        guid = (entry.get("fipi_guid") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if not guid or not answer:
            skipped += 1
            continue
        count = CatalogTask.objects.filter(fipi_guid=guid).update(correct_answer=answer)
        if count:
            updated += count
        else:
            not_found += 1

    parts = [f"Обновлено: {updated}"]
    if skipped:
        parts.append(f"пропущено (нет guid/ответа): {skipped}")
    if not_found:
        parts.append(f"не найдено в БД: {not_found}")
    messages.success(request, " | ".join(parts))
    return redirect(reverse("admin_catalog_unclassified") + "?tab=no_answer")


@admin_required
def catalog_import_list(request):
    sessions = CatalogImportSession.objects.order_by("-created_at")
    return render(request, "admin/catalog_import_list.html", {"sessions": sessions})


@admin_required
@require_POST
def catalog_import_session_delete(request, session_id):
    sess = get_object_or_404(CatalogImportSession, id=session_id)
    delete_tasks = request.POST.get("delete_tasks") == "1"
    if delete_tasks:
        CatalogTask.objects.filter(import_session=sess).delete()
    sess.delete()
    return redirect("admin_import_list")
