"""
Импорт заданий из PDF файлов (Распечатай и реши).

Использование:
    python manage.py import_pdfs <путь_к_папке> [опции]

Примеры:
    # Добавить в каталог (по умолчанию)
    python manage.py import_pdfs "Распечатай и реши" --exam-type oge

    # Создать варианты (один вариант на каждый блок PDF)
    python manage.py import_pdfs "Распечатай и реши" --exam-type oge --mode variants

    # И в каталог, и создать варианты
    python manage.py import_pdfs "Распечатай и реши" --exam-type oge --mode both

Структура PDF:
    - Каждый файл содержит несколько вариантов на одну тему
    - Каждый вариант: "Задание N." (= вариант N), затем задания 1-5
    - Номера заданий ОГЭ/ЕГЭ читаются из имени файла (например, №01-05)
    - Ответов нет — заполняются вручную после импорта
"""

import re
from pathlib import Path
from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile
from django.db import transaction
from exam.models import (
    CatalogTask, CatalogImportSession,
    Variant, Task,
    ExamType, TaskSource,
)

# Паттерны строк-«мусора» в контексте PDF (авторы, названия, колонтитулы, заголовки блоков)
_CONTEXT_BAD_RE = re.compile(
    r'(?:'
    r'[А-ЯЁA-Z]\.[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z][а-яёa-z]+'   # Инициалы + Фамилия
    r'|задачник|сборник|симулятор|simulator'
    r'|учебн|пособие|издание|издательство'
    r'|ОГЭ\s*20\d\d|ЕГЭ\s*20\d\d'
    r'|^\d{1,3}$'                                          # Номер страницы
    r'|^(?:стр|с)\.\s*\d'                                  # "стр. N"
    r'|^(?:Задание|Вариант|Блок)\s+\d+\.?\s*$'            # Заголовок блока ("Задание 1.")
    r')',
    re.IGNORECASE | re.MULTILINE,
)


class Command(BaseCommand):
    help = 'Импорт заданий из PDF (Распечатай и реши) — в каталог или как варианты'

    def add_arguments(self, parser):
        parser.add_argument('folder', help='Путь к папке с PDF файлами (рекурсивно)')
        parser.add_argument(
            '--exam-type', default='oge',
            choices=['oge', 'ege_profile', 'ege_base'],
            help='Тип экзамена (по умолчанию: oge)',
        )
        parser.add_argument(
            '--mode', default='catalog',
            choices=['catalog', 'variants', 'both'],
            help=(
                'catalog  — добавить в каталог (по умолчанию)\n'
                'variants — создать Вариант для каждого блока PDF\n'
                'both     — и каталог, и варианты'
            ),
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать что будет импортировано, не сохранять в БД',
        )

    def handle(self, *args, **options):
        try:
            import pdfplumber
        except ImportError:
            self.stderr.write(self.style.ERROR('Установите pdfplumber: pip install pdfplumber'))
            return

        try:
            import fitz  # PyMuPDF
        except ImportError:
            self.stderr.write(self.style.ERROR('Установите PyMuPDF: pip install PyMuPDF'))
            return

        folder = Path(options['folder'])
        if not folder.exists():
            self.stderr.write(self.style.ERROR(f'Папка не найдена: {folder}'))
            return

        exam_type = options['exam_type']
        dry_run = options['dry_run']
        mode = options['mode']

        do_catalog = mode in ('catalog', 'both')
        do_variants = mode in ('variants', 'both')

        if dry_run:
            self.stdout.write(self.style.WARNING('=== РЕЖИМ ПРОСМОТРА (dry-run) — ничего не сохраняется ==='))

        self.stdout.write(f'Режим: {mode} | Тип экзамена: {exam_type}')

        pdf_files = sorted(folder.rglob('*.pdf'))
        self.stdout.write(f'Найдено PDF файлов: {len(pdf_files)}')

        total_cat_added = total_cat_skipped = 0
        total_var_created = total_var_skipped = 0
        total_errors = 0

        # Сессия истории импортов (только для каталог-режима)
        session = None
        if do_catalog and not dry_run:
            session = CatalogImportSession.objects.create(
                source=TaskSource.PRINT_SOLVE,
                url=str(folder),
                status='running',
            )

        for pdf_path in pdf_files:
            self.stdout.write(f'\n[PDF] {pdf_path.name}')
            try:
                cat_added, cat_skipped, var_created, var_skipped = self._process_pdf(
                    pdf_path, exam_type, dry_run, do_catalog, do_variants, session,
                )
                total_cat_added += cat_added
                total_cat_skipped += cat_skipped
                total_var_created += var_created
                total_var_skipped += var_skipped
                if do_catalog:
                    self.stdout.write(f'   Каталог: добавлено {cat_added}, дублей {cat_skipped}')
                if do_variants:
                    self.stdout.write(f'   Варианты: создано {var_created}, пропущено {var_skipped}')
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'   Ошибка: {e}'))
                total_errors += 1

        if session is not None:
            session.tasks_added = total_cat_added
            session.tasks_duplicate = total_cat_skipped
            session.status = 'done' if total_errors == 0 else 'error'
            if total_errors:
                session.notes = f'Ошибок при обработке PDF: {total_errors}'
            session.save(update_fields=['tasks_added', 'tasks_duplicate', 'status', 'notes'])

        self.stdout.write(self.style.SUCCESS('\n=== ИТОГО ==='))
        if do_catalog:
            self.stdout.write(f'Каталог: добавлено {total_cat_added}, дублей {total_cat_skipped}')
        if do_variants:
            self.stdout.write(f'Варианты: создано {total_var_created}, пропущено {total_var_skipped}')
        if total_errors:
            self.stdout.write(self.style.ERROR(f'Ошибок: {total_errors}'))

        if not dry_run:
            if do_catalog and total_cat_added > 0:
                self.stdout.write('Заполните ответы в каталоге: /admin/catalog/')
            if do_variants and total_var_created > 0:
                self.stdout.write(
                    f'Создано вариантов (is_active=False). '
                    'Активируйте их после проверки: /admin/variants/'
                )

    # ------------------------------------------------------------------

    def _clean_context(self, text):
        """Убирает строки-мусор из контекста: авторов, названия, заголовки блоков, номера страниц."""
        lines = text.splitlines()
        clean = [ln for ln in lines if ln.strip() and not _CONTEXT_BAD_RE.search(ln.strip())]
        return '\n'.join(clean).strip()

    def _process_pdf(self, pdf_path, exam_type, dry_run,
                     do_catalog, do_variants, session=None):
        import pdfplumber
        import fitz

        task_numbers = self._parse_task_numbers(pdf_path.name)
        pdf_stem = pdf_path.stem[:40]

        cat_added = cat_skipped = 0
        var_created = var_skipped = 0

        fitz_doc = fitz.open(str(pdf_path))

        with pdfplumber.open(str(pdf_path)) as pdf:
            pages_text = [(i, page.extract_text() or '') for i, page in enumerate(pdf.pages)]
            full_text = '\n'.join(t for _, t in pages_text)

            blocks = self._parse_blocks(full_text, pages_text)
            self.stdout.write(f'   Блоков (вариантов): {len(blocks)}')

            for block_idx, block in enumerate(blocks):
                page_num = block['page_num']
                img_data = self._render_page(fitz_doc, page_num)
                shared_ctx = self._clean_context(block['context']) if block['context'] else ''
                variant_num = f'{pdf_stem} В{block_idx + 1}'

                # --- Режим: каталог ---
                if do_catalog:
                    ctx_image_path = None
                    for i, task_text in enumerate(block['tasks']):
                        if not task_text.strip():
                            continue

                        task_number = task_numbers[i] if i < len(task_numbers) else i + 1
                        full_text_task = task_text.strip()
                        text_hash = CatalogTask.compute_hash(full_text_task)

                        if not text_hash:
                            continue
                        if not dry_run and CatalogTask.objects.filter(text_hash=text_hash).exists():
                            cat_skipped += 1
                            continue

                        if dry_run:
                            preview = full_text_task[:70].replace('\n', ' ')
                            self.stdout.write(
                                f'   [В{block_idx+1} зад.{task_number}] {preview}…'
                            )
                            cat_added += 1
                            continue

                        obj = CatalogTask(
                            task_number=task_number,
                            exam_type=exam_type,
                            text=full_text_task,
                            correct_answer='',
                            source=TaskSource.PRINT_SOLVE,
                            manual_grading=True,
                            text_hash=text_hash,
                            shared_context=shared_ctx,
                            import_session=session,
                        )
                        if i == 0 and img_data:
                            fname = f'catalog/pdf_{pdf_stem}_b{block_idx+1}.png'
                            obj.shared_context_image.save(fname, ContentFile(img_data), save=False)
                        elif ctx_image_path:
                            obj.__dict__['shared_context_image'] = ctx_image_path

                        obj.save()
                        cat_added += 1

                        if i == 0 and obj.shared_context_image:
                            ctx_image_path = obj.shared_context_image.name

                # --- Режим: варианты ---
                if do_variants:
                    if dry_run:
                        tasks_in_block = [t for t in block['tasks'] if t.strip()]
                        self.stdout.write(
                            f'   [Вариант] {variant_num} → {len(tasks_in_block)} заданий'
                        )
                        var_created += 1
                        continue

                    # Проверяем, не создан ли уже такой вариант
                    if Variant.objects.filter(number=variant_num).exists():
                        self.stdout.write(f'   [Вариант] {variant_num} — уже существует, пропуск')
                        var_skipped += 1
                        continue

                    # Сохраняем картинку условия один раз
                    ctx_img_content = img_data
                    ctx_img_fname = f'contexts/pdf_{pdf_stem}_b{block_idx+1}.png' if img_data else None
                    ctx_img_saved = False

                    with transaction.atomic():
                        variant = Variant.objects.create(
                            number=variant_num,
                            exam_type=exam_type,
                            is_active=False,  # активирует учитель после проверки
                            max_attempts=3,
                        )

                        for i, task_text in enumerate(block['tasks']):
                            if not task_text.strip():
                                continue
                            task_number = task_numbers[i] if i < len(task_numbers) else i + 1
                            task = Task(
                                variant=variant,
                                number=str(task_number),
                                text=task_text.strip(),
                                correct_answer='',
                                source=TaskSource.PRINT_SOLVE,
                                manual_grading=True,
                                shared_context=shared_ctx,
                            )
                            if i == 0 and ctx_img_content and ctx_img_fname:
                                task.shared_context_image.save(
                                    ctx_img_fname, ContentFile(ctx_img_content), save=False
                                )
                                ctx_img_saved = True
                            elif ctx_img_saved and ctx_img_fname:
                                task.__dict__['shared_context_image'] = ctx_img_fname
                            task.save()

                    var_created += 1
                    self.stdout.write(f'   [Вариант] создан: {variant_num}')

        fitz_doc.close()
        return cat_added, cat_skipped, var_created, var_skipped

    # ------------------------------------------------------------------

    def _parse_task_numbers(self, filename):
        """Из имени файла '№01-05' возвращает [1, 2, 3, 4, 5]."""
        m = re.search(r'№?0*(\d+)-0*(\d+)', filename)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            return list(range(start, end + 1))
        m = re.search(r'№0*(\d+)', filename)
        if m:
            return [int(m.group(1))]
        return list(range(1, 6))

    def _parse_blocks(self, full_text, pages_text):
        """
        Разбивает текст на блоки (варианты).
        "Задание N." в контексте PDF = вариант N (не номер задания ОГЭ!).
        Разделители: 'Задание N.' / 'Вариант N.' / 'Блок N.'
        """
        positions = []
        for pattern_str in [
            r'(?:^|\n)(Задание \d+\.)',
            r'(?:^|\n)(Вариант \d+\.?)',
            r'(?:^|\n)(Блок \d+\.)',
        ]:
            positions = [(m.start(), m.group(1)) for m in re.finditer(pattern_str, full_text)]
            if len(positions) >= 2:
                break

        if not positions:
            positions = [(0, 'Блок 1')]

        blocks = []
        for idx, (pos, title) in enumerate(positions):
            end = positions[idx + 1][0] if idx + 1 < len(positions) else len(full_text)
            block_text = full_text[pos:end]

            context, tasks = self._split_context_and_tasks(block_text)
            page_num = self._find_page_num(title.strip()[:20], pages_text)

            blocks.append({
                'title': title.strip(),
                'context': context,
                'tasks': tasks,
                'page_num': page_num,
            })

        return blocks

    def _split_context_and_tasks(self, block_text):
        """
        Разделяет текст блока на:
        - context: общее условие/описание до первого '1. '
        - tasks: список текстов заданий 1..5
        """
        first_task_m = re.search(r'\n1\.\s*(?:\([^)]+\)\s*)?', block_text)
        if first_task_m:
            context = block_text[:first_task_m.start()].strip()
            tasks_text = block_text[first_task_m.start():]
        else:
            context = ''
            tasks_text = block_text

        tasks = self._extract_numbered_tasks(tasks_text)
        return context, tasks

    def _extract_numbered_tasks(self, text):
        """Извлекает задания, пронумерованные '1.' .. '5.'"""
        pattern = re.compile(r'(?:^|\n)(\d+)\.\s*(?:\([^)]*\)\s*)?(.*?)(?=\n\d+\.\s|\Z)', re.DOTALL)
        found = {}
        for m in pattern.finditer(text):
            num = int(m.group(1))
            if 1 <= num <= 25:
                task_text = m.group(2).strip()
                task_text = re.sub(r'Ответ:\s*_{3,}\.?', '', task_text).strip()
                found[num] = task_text

        if not found:
            return [text.strip()]

        max_num = max(found.keys())
        return [found.get(i, '') for i in range(1, max_num + 1)]

    def _find_page_num(self, text_fragment, pages_text):
        """Находит номер страницы по фрагменту текста."""
        for page_num, page_text in pages_text:
            if text_fragment and text_fragment in page_text:
                return page_num
        return 0

    def _render_page(self, fitz_doc, page_num, dpi=150):
        """Рендерит страницу PDF в PNG байты."""
        try:
            import fitz
            page = fitz_doc[page_num]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            return pix.tobytes('png')
        except Exception:
            return None
