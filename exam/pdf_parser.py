import logging
import re

from django.core.cache import cache
from django.db import transaction

from .models import CatalogImportSession, CatalogTask, Task, TaskSource, Variant

logger = logging.getLogger(__name__)

_JOB_TTL = 7200  # 2 часа

_PDF_CONTEXT_BAD_RE = re.compile(
    r"(?:"
    r"[А-ЯЁA-Z]\.[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z][а-яёa-z]+"
    r"|задачник|сборник|тренажер|симулятор|simulator"
    r"|учебн|пособие|издание|издательство"
    r"|ОГЭ\s*20\d\d|ЕГЭ\s*20\d\d"
    r"|^\d{1,3}$"
    r"|^(?:стр|с)\.\s*\d"
    r"|^(?:Задание|Вариант|Блок)\s+\d+\.?\s*$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _pdf_clean_context(text):
    lines = text.splitlines()
    clean = [ln for ln in lines if ln.strip() and not _PDF_CONTEXT_BAD_RE.search(ln.strip())]
    return "\n".join(clean).strip()


def _pdf_extract_tables_html(page):
    try:
        tables = page.extract_tables()
    except Exception:
        return ""
    if not tables:
        return ""
    parts = []
    for table in tables:
        if not table:
            continue
        html = '<table border="1" cellpadding="4" style="border-collapse:collapse;margin:8px 0;font-size:0.95em;">'
        for row in table:
            html += "<tr>"
            for cell in row or []:
                cell_text = (cell or "").strip().replace("\n", "<br>")
                html += f'<td style="padding:4px;">{cell_text}</td>'
            html += "</tr>"
        html += "</table>"
        parts.append(html)
    return "\n".join(parts)


def _pdf_extract_embedded_images(fitz_doc, page_num, min_bytes=2000):
    result = []
    try:
        page = fitz_doc[page_num]
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                base = fitz_doc.extract_image(xref)
                img_bytes = base.get("image", b"")
                img_ext = base.get("ext", "png")
                if len(img_bytes) < min_bytes:
                    continue
                rects = page.get_image_rects(xref)
                y_top = rects[0].y0 if rects else 0
                result.append((y_top, img_bytes, img_ext))
            except Exception:
                pass
        result.sort(key=lambda x: x[0])
    except Exception:
        pass
    return result


def _pdf_render_page(fitz_doc, page_num, dpi=150):
    try:
        import fitz

        page = fitz_doc[page_num]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    except Exception:
        return None


def _pdf_parse_task_numbers(filename):
    m = re.search(r"№?0*(\d+)-0*(\d+)", filename)
    if m:
        return list(range(int(m.group(1)), int(m.group(2)) + 1))
    m = re.search(r"№0*(\d+)", filename)
    if m:
        return [int(m.group(1))]
    return list(range(1, 6))


def _pdf_extract_numbered_tasks(text):
    pattern = re.compile(r"(?:^|\n)(\d+)\.\s*(?:\([^)]*\)\s*)?(.*?)(?=\n\d+\.\s|\Z)", re.DOTALL)
    found = {}
    for m in pattern.finditer(text):
        num = int(m.group(1))
        if 1 <= num <= 25:
            task_text = m.group(2).strip()
            task_text = re.sub(r"Ответ:\s*_{3,}\.?", "", task_text).strip()
            lines = task_text.splitlines()
            clean_lines = [ln for ln in lines if not ln.strip() or not _PDF_CONTEXT_BAD_RE.search(ln.strip())]
            found[num] = "\n".join(clean_lines).strip()
    if not found:
        return [text.strip()]
    max_num = max(found.keys())
    return [found.get(i, "") for i in range(1, max_num + 1)]


def _pdf_split_context_and_tasks(block_text):
    first_task_m = re.search(r"\n1\.\s*(?:\([^)]+\)\s*)?", block_text)
    if first_task_m:
        context = block_text[: first_task_m.start()].strip()
        tasks_text = block_text[first_task_m.start() :]
    else:
        context = ""
        tasks_text = block_text
    return context, _pdf_extract_numbered_tasks(tasks_text)


def _pdf_find_page_num(text_fragment, pages_text):
    for page_num, page_text in pages_text:
        if text_fragment and text_fragment in page_text:
            return page_num
    return 0


def _pdf_parse_blocks(full_text, pages_text):
    positions = []
    for pattern_str in [
        r"(?:^|\n)(Задание \d+\.)",
        r"(?:^|\n)(Вариант \d+\.?)",
        r"(?:^|\n)(Блок \d+\.)",
    ]:
        positions = [(m.start(), m.group(1)) for m in re.finditer(pattern_str, full_text)]
        if len(positions) >= 2:
            break
    if not positions:
        positions = [(0, "Блок 1")]
    blocks = []
    for idx, (pos, title) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(full_text)
        block_text = full_text[pos:end]
        context, tasks = _pdf_split_context_and_tasks(block_text)
        page_num = _pdf_find_page_num(title.strip()[:20], pages_text)
        blocks.append({"title": title.strip(), "context": context, "tasks": tasks, "page_num": page_num})
    return blocks


def _process_pdf_print_solve(pdf_path, exam_type, do_catalog, do_variants, session):
    """Парсинг PDF формата «Распечатай и реши» (Ширяева и подобные)."""
    import fitz
    import pdfplumber
    from django.core.files.base import ContentFile

    task_numbers = _pdf_parse_task_numbers(pdf_path.name)
    pdf_stem = pdf_path.stem[:40]
    cat_added = cat_skipped = 0
    var_created = var_skipped = 0

    fitz_doc = fitz.open(str(pdf_path))
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages_text = []
        for i, page in enumerate(pdf.pages):
            raw = page.extract_text() or ""
            raw = re.sub(r"([а-яёА-ЯЁ])-\s*\n\s*([а-яёА-ЯЁ])", r"\1\2", raw)
            table_html = _pdf_extract_tables_html(page)
            if table_html:
                raw = raw + "\n" + table_html
            pages_text.append((i, raw))

        full_text = "\n".join(t for _, t in pages_text)
        page_images = {
            page_num: _pdf_extract_embedded_images(fitz_doc, page_num) for page_num in range(len(fitz_doc))
        }
        blocks = _pdf_parse_blocks(full_text, pages_text)
        logger.debug("[PDF] %s: блоков %d", pdf_path.name, len(blocks))

        for block_idx, block in enumerate(blocks):
            page_num = block["page_num"]
            shared_ctx = _pdf_clean_context(block["context"]) if block["context"] else ""
            variant_num = f"{pdf_stem} В{block_idx + 1}"

            embedded = page_images.get(page_num, [])
            if embedded:
                ctx_img_bytes = embedded[0][1]
                ctx_img_ext = embedded[0][2]
                extra_task_imgs = {i: (b, e) for i, (_, b, e) in enumerate(embedded[1:])}
            else:
                ctx_img_bytes = _pdf_render_page(fitz_doc, page_num)
                ctx_img_ext = "png"
                extra_task_imgs = {}

            if do_catalog:
                ctx_image_path = None
                for i, task_text in enumerate(block["tasks"]):
                    if not task_text.strip():
                        continue
                    task_number = task_numbers[i] if i < len(task_numbers) else i + 1
                    full_task_text = task_text.strip()
                    text_hash = CatalogTask.compute_hash(full_task_text)
                    if not text_hash:
                        continue
                    if CatalogTask.objects.filter(text_hash=text_hash).exists():
                        cat_skipped += 1
                        continue
                    obj = CatalogTask(
                        task_number=task_number,
                        exam_type=exam_type,
                        text=full_task_text,
                        correct_answer="",
                        source=TaskSource.PRINT_SOLVE,
                        manual_grading=True,
                        points=2,
                        text_hash=text_hash,
                        shared_context=shared_ctx,
                        import_session=session,
                    )
                    if i == 0 and ctx_img_bytes:
                        fname = f"catalog/pdf_{pdf_stem}_b{block_idx + 1}.{ctx_img_ext}"
                        obj.shared_context_image.save(fname, ContentFile(ctx_img_bytes), save=False)
                    elif ctx_image_path:
                        obj.shared_context_image = ctx_image_path
                    if i in extra_task_imgs:
                        t_bytes, t_ext = extra_task_imgs[i]
                        obj.image.save(
                            f"catalog/pdf_{pdf_stem}_b{block_idx + 1}_t{i + 1}.{t_ext}",
                            ContentFile(t_bytes),
                            save=False,
                        )
                    obj.save()
                    cat_added += 1
                    if i == 0 and obj.shared_context_image:
                        ctx_image_path = obj.shared_context_image.name

            if do_variants:
                if Variant.objects.filter(number=variant_num).exists():
                    var_skipped += 1
                    continue
                ctx_img_fname = (
                    f"contexts/pdf_{pdf_stem}_b{block_idx + 1}.{ctx_img_ext}" if ctx_img_bytes else None
                )
                ctx_img_saved = False
                with transaction.atomic():
                    variant = Variant.objects.create(
                        number=variant_num,
                        exam_type=exam_type,
                        is_active=False,
                        max_attempts=3,
                    )
                    for i, task_text in enumerate(block["tasks"]):
                        if not task_text.strip():
                            continue
                        task_number = task_numbers[i] if i < len(task_numbers) else i + 1
                        task = Task(
                            variant=variant,
                            number=str(task_number),
                            text=task_text.strip(),
                            correct_answer="",
                            source=TaskSource.PRINT_SOLVE,
                            manual_grading=True,
                            points=2,
                            shared_context=shared_ctx,
                        )
                        if i == 0 and ctx_img_bytes and ctx_img_fname:
                            task.shared_context_image.save(
                                ctx_img_fname, ContentFile(ctx_img_bytes), save=False
                            )
                            ctx_img_saved = True
                        elif ctx_img_saved and ctx_img_fname:
                            task.shared_context_image = ctx_img_fname
                        if i in extra_task_imgs:
                            t_bytes, t_ext = extra_task_imgs[i]
                            task.image.save(
                                f"tasks/pdf_{pdf_stem}_b{block_idx + 1}_t{i + 1}.{t_ext}",
                                ContentFile(t_bytes),
                                save=False,
                            )
                        task.save()
                var_created += 1

    fitz_doc.close()
    return cat_added, cat_skipped, var_created, var_skipped


def _process_pdf_universal(pdf_path, exam_type, session):
    """Универсальный режим: каждая страница PDF = одно задание (рендер + текст)."""
    import fitz
    import pdfplumber
    from django.core.files.base import ContentFile

    added = 0
    fitz_doc = fitz.open(str(pdf_path))
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            text_hash = CatalogTask.compute_hash(text)
            if text_hash and CatalogTask.objects.filter(text_hash=text_hash).exists():
                continue
            page_bytes = _pdf_render_page(fitz_doc, page_num)
            obj = CatalogTask(
                task_number=None,
                exam_type=exam_type,
                text=text,
                correct_answer="",
                source=TaskSource.PRINT_SOLVE,
                manual_grading=True,
                points=2,
                text_hash=text_hash,
                import_session=session,
            )
            if page_bytes:
                fname = f"catalog/pdf_{pdf_path.stem}_p{page_num + 1}.png"
                obj.image.save(fname, ContentFile(page_bytes), save=False)
            obj.save()
            added += 1
            session.tasks_added = added
            session.save(update_fields=["tasks_added"])
    fitz_doc.close()
    return added


def run_pdf_import_job(job_id, file_paths, exam_type, mode, fmt, session_id):
    """Фоновый поток: парсит загруженные PDF файлы."""
    from pathlib import Path

    from django.db import connection

    try:
        session = CatalogImportSession.objects.get(id=session_id)
        do_catalog = mode in ("catalog", "both")
        do_variants = mode in ("variants", "both")
        total_added = total_dupl = total_errors = 0

        for file_path in file_paths:
            path = Path(file_path)
            if not path.exists():
                continue
            try:
                if fmt == "universal":
                    total_added += _process_pdf_universal(path, exam_type, session)
                else:
                    cat_added, cat_skipped, _vc, _vs = _process_pdf_print_solve(
                        path, exam_type, do_catalog, do_variants, session
                    )
                    total_added += cat_added
                    total_dupl += cat_skipped
            except Exception:
                logger.exception("Ошибка парсинга PDF: %s", file_path)
                total_errors += 1
            finally:
                path.unlink(missing_ok=True)

        session.tasks_added = total_added
        session.tasks_duplicate = total_dupl
        session.status = "done" if total_errors == 0 else "error"
        if total_errors:
            session.notes = f"Ошибок: {total_errors}"
        session.save(update_fields=["tasks_added", "tasks_duplicate", "status", "notes"])
        cache.set(
            f"pjob:{job_id}",
            {"status": "done", "session_id": session_id, "added": total_added},
            _JOB_TTL,
        )
    except Exception as e:
        logger.exception("Ошибка PDF-импорта")
        cache.set(f"pjob:{job_id}", {"status": "error", "session_id": session_id, "error": str(e)}, _JOB_TTL)
        try:
            CatalogImportSession.objects.filter(id=session_id).update(status="error", notes=str(e))
        except Exception:
            pass
    finally:
        connection.close()
