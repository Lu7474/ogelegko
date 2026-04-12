"""
Импорт заданий из PDF файлов (Распечатай и реши).

Использование:
    python manage.py import_pdfs <путь_к_папке> [--exam-type oge] [--dry-run]

Пример:
    python manage.py import_pdfs "Распечатай и реши" --exam-type oge

Структура PDF:
    - Каждый файл содержит несколько вариантов на одну тему
    - Каждый вариант: "Задание N. [контекст/условие]", затем задания 1-5
    - Ответов нет — заполняются вручную на сайте после импорта
    - Номера заданий ОГЭ/ЕГЭ читаются из имени файла (например, №01-05)
"""

import re
from pathlib import Path
from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile
from exam.models import CatalogTask, CatalogImportSession, ExamType, TaskSource

# Паттерны строк-«мусора» в контексте PDF (авторы, названия, колонтитулы)
_CONTEXT_BAD_RE = re.compile(
    r'(?:'
    r'[А-ЯЁA-Z]\.[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z][а-яёa-z]+'  # Инициалы + Фамилия
    r'|задачник|сборник|симулятор|simulator'
    r'|учебн|пособие|издание|издательство'
    r'|ОГЭ\s*20\d\d|ЕГЭ\s*20\d\d'
    r'|^\d{1,3}$'                                         # Номер страницы
    r'|^(?:стр|с)\.\s*\d'                                 # "стр. N"
    r')',
    re.IGNORECASE,
)


class Command(BaseCommand):
    help = 'Импорт заданий из PDF файлов (Распечатай и реши)'

    def add_arguments(self, parser):
        parser.add_argument('folder', help='Путь к папке с PDF файлами (рекурсивно)')
        parser.add_argument(
            '--exam-type', default='oge',
            choices=['oge', 'ege_profile', 'ege_base'],
            help='Тип экзамена (по умолчанию: oge)',
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

        if dry_run:
            self.stdout.write(self.style.WARNING('=== РЕЖИМ ПРОСМОТРА (dry-run) — ничего не сохраняется ==='))

        pdf_files = sorted(folder.rglob('*.pdf'))
        self.stdout.write(f'Найдено PDF файлов: {len(pdf_files)}')

        total_added = total_skipped = total_errors = 0

        # Создаём запись в истории импортов (только для реального импорта)
        session = None
        if not dry_run:
            session = CatalogImportSession.objects.create(
                source=TaskSource.PRINT_SOLVE,
                url=str(folder),
                status='running',
            )

        for pdf_path in pdf_files:
            self.stdout.write(f'\n[PDF] {pdf_path.name}')
            try:
                added, skipped = self._process_pdf(pdf_path, exam_type, dry_run, session)
                total_added += added
                total_skipped += skipped
                self.stdout.write(f'   Добавлено: {added}, пропущено дублей: {skipped}')
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'   Ошибка: {e}'))
                total_errors += 1

        if session is not None:
            session.tasks_added = total_added
            session.tasks_duplicate = total_skipped
            session.status = 'done' if total_errors == 0 else 'error'
            if total_errors:
                session.notes = f'Ошибок при обработке PDF: {total_errors}'
            session.save(update_fields=['tasks_added', 'tasks_duplicate', 'status', 'notes'])

        self.stdout.write(self.style.SUCCESS(
            f'\nИтого: добавлено {total_added}, дублей {total_skipped}, ошибок {total_errors}'
        ))
        if total_added > 0 and not dry_run:
            self.stdout.write(
                'Задания добавлены в каталог без ответов. '
                'Заполните ответы на сайте: /admin/catalog/'
            )

    # ------------------------------------------------------------------

    def _clean_context(self, text):
        """Убирает строки-мусор из контекста: авторов, названия, номера страниц."""
        lines = text.splitlines()
        clean = [ln for ln in lines if ln.strip() and not _CONTEXT_BAD_RE.search(ln.strip())]
        return '\n'.join(clean).strip()

    def _process_pdf(self, pdf_path, exam_type, dry_run, session=None):
        import pdfplumber
        import fitz

        task_numbers = self._parse_task_numbers(pdf_path.name)
        added = skipped = 0

        fitz_doc = fitz.open(str(pdf_path))

        with pdfplumber.open(str(pdf_path)) as pdf:
            # Текст по страницам
            pages_text = [(i, page.extract_text() or '') for i, page in enumerate(pdf.pages)]
            full_text = '\n'.join(t for _, t in pages_text)

            blocks = self._parse_blocks(full_text, pages_text)
            self.stdout.write(f'   Блоков (вариантов): {len(blocks)}')

            for block_idx, block in enumerate(blocks):
                page_num = block['page_num']
                img_data = self._render_page(fitz_doc, page_num)
                shared_ctx = self._clean_context(block['context']) if block['context'] else ''
                # Путь к картинке условия (сохраняем один раз, переиспользуем для всех заданий блока)
                ctx_image_path = None

                for i, task_text in enumerate(block['tasks']):
                    if not task_text.strip():
                        continue

                    task_number = task_numbers[i] if i < len(task_numbers) else i + 1
                    full_text_task = task_text.strip()

                    text_hash = CatalogTask.compute_hash(full_text_task)

                    # Пропускаем если нет текста или уже есть в базе
                    if not text_hash:
                        continue
                    if CatalogTask.objects.filter(text_hash=text_hash).exists():
                        skipped += 1
                        continue

                    if dry_run:
                        preview = full_text_task[:80].replace('\n', ' ')
                        self.stdout.write(f'   [Блок {block_idx+1}] Задание {task_number}: {preview}…')
                        added += 1
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
                        # Первое задание: сохраняем картинку условия
                        fname = f'catalog/pdf_{pdf_path.stem[:40]}_b{block_idx+1}.png'
                        obj.shared_context_image.save(fname, ContentFile(img_data), save=False)
                    elif ctx_image_path:
                        # Остальные задания: ссылаемся на ту же картинку
                        obj.__dict__['shared_context_image'] = ctx_image_path

                    obj.save()
                    added += 1

                    # Запоминаем путь к картинке после сохранения первого задания
                    if i == 0 and obj.shared_context_image:
                        ctx_image_path = obj.shared_context_image.name

        fitz_doc.close()
        return added, skipped

    # ------------------------------------------------------------------

    def _parse_task_numbers(self, filename):
        """Из имени файла '№01-05' возвращает [1, 2, 3, 4, 5]."""
        m = re.search(r'№?0*(\d+)-0*(\d+)', filename)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            return list(range(start, end + 1))
        # Попробуем одиночный номер: '№13'
        m = re.search(r'№0*(\d+)', filename)
        if m:
            return [int(m.group(1))]
        return list(range(1, 6))

    def _parse_blocks(self, full_text, pages_text):
        """
        Разбивает текст на блоки (варианты).
        Разделители: 'Задание N.' или 'Вариант N.' или 'Блок N.'
        """
        # Пробуем разные паттерны заголовков блоков
        for pattern_str in [
            r'(?:^|\n)(Задание \d+\.)',
            r'(?:^|\n)(Вариант \d+\.?)',
            r'(?:^|\n)(Блок \d+\.)',
        ]:
            positions = [(m.start(), m.group(1)) for m in re.finditer(pattern_str, full_text)]
            if len(positions) >= 2:
                break

        if not positions:
            # Нет явных заголовков — весь файл как один блок
            positions = [(0, 'Блок 1')]

        blocks = []
        for idx, (pos, title) in enumerate(positions):
            end = positions[idx + 1][0] if idx + 1 < len(positions) else len(full_text)
            block_text = full_text[pos:end]

            context, tasks = self._split_context_and_tasks(block_text)
            page_num = self._find_page_num(title.strip()[:20], pages_text)

            blocks.append({
                'title': title,
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
        # Ищем начало задания '1.' (возможно с пометкой типа '(ОБЗ)')
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
        # Ищем переходы между заданиями
        pattern = re.compile(r'(?:^|\n)(\d+)\.\s*(?:\([^)]*\)\s*)?(.*?)(?=\n\d+\.\s|\Z)', re.DOTALL)
        found = {}
        for m in pattern.finditer(text):
            num = int(m.group(1))
            if 1 <= num <= 25:
                task_text = m.group(2).strip()
                # Убираем поле ответа
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
