"""
Парсер заданий с sdamgia.ru (Решу ОГЭ / Решу ЕГЭ)
"""
import re
import time
import uuid
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from django.core.files.base import ContentFile

from .models import Variant, Task, ExamType, TaskSource

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

REQUEST_DELAY = 1.5


class ParserError(Exception):
    pass


def sanitize_html(html):
    """Удаляет опасные теги и атрибуты из HTML (XSS-защита)."""
    # Удаляем опасные теги целиком (с содержимым)
    html = re.sub(
        r'<\s*(?:script|style|iframe|object|embed|form|link|meta)\b[^>]*>.*?'
        r'</\s*(?:script|style|iframe|object|embed|form|link|meta)\s*>',
        '', html, flags=re.IGNORECASE | re.DOTALL,
    )
    # Удаляем самозакрывающиеся опасные теги
    html = re.sub(
        r'<\s*(?:script|style|iframe|object|embed|form|link|meta)\b[^>]*/?\s*>',
        '', html, flags=re.IGNORECASE,
    )
    # Удаляем все обработчики событий (onclick, onerror, onload, etc.)
    html = re.sub(r'\s+on\w+\s*=\s*"[^"]*"', '', html, flags=re.IGNORECASE)
    html = re.sub(r"\s+on\w+\s*=\s*'[^']*'", '', html, flags=re.IGNORECASE)
    html = re.sub(r'\s+on\w+\s*=\s*[^\s>]+', '', html, flags=re.IGNORECASE)
    # Удаляем javascript: в href/src
    html = re.sub(r'(href|src)\s*=\s*["\']?\s*javascript\s*:', r'\1="#" data-removed="', html, flags=re.IGNORECASE)
    # Удаляем data: в src (кроме data:image)
    html = re.sub(r'src\s*=\s*["\']?\s*data\s*:(?!image/)', 'src="data:removed', html, flags=re.IGNORECASE)
    return html


class SdamgiaParser:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get(self, url):
        try:
            time.sleep(REQUEST_DELAY)
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp
        except requests.RequestException as e:
            raise ParserError(f"Не удалось загрузить: {e}")

    def _detect_exam_type(self, url):
        if "oge" in url:
            return ExamType.OGE
        if "mathb" in url:
            return ExamType.EGE_BASE
        return ExamType.EGE_PROFILE

    def _base_url(self, url):
        m = re.match(r"(https?://[^/]+)", url)
        return m.group(1) if m else ""

    def _clean_html(self, html):
        """Убирает мягкие переносы и лишние пробелы/переносы."""
        html = html.replace("\u00AD", "")
        html = re.sub(r"(<br\s*/?>){3,}", "<br><br>", html)
        html = re.sub(r"\n{3,}", "\n\n", html)
        html = re.sub(r" {2,}", " ", html)
        return html.strip()

    def _pbody_to_html(self, pb, base_url=""):
        """
        Из��лекает очищенный HTML из pbody: убирает лишние обёртки,
        но сохраняет <img> для формул и изобр��жений.
        Делает относительные URL абсолютными.
        """
        # Удаляем ненужные элементы
        for tag in pb.find_all(["script", "style", "button"]):
            tag.decompose()
        # Удаляем иконки (briefcase, etc.)
        for img in pb.find_all("img", class_="briefcase"):
            img.decompose()
        # Делаем src абсолютными
        if base_url:
            for img in pb.find_all("img"):
                src = img.get("src", "")
                if src and not src.startswith(("http://", "https://")):
                    img["src"] = urljoin(base_url + "/", src)
        # Оставляем только ��онтент внутри pbody
        inner = pb.decode_contents()
        # Убираем мя��кие переносы
        inner = inner.replace("\u00AD", "")
        return inner.strip()

    # ---- Шаг 1: получить problem_ids со страницы варианта ----

    def _get_problem_ids_from_variant(self, variant_url):
        """
        Возвращает список (display_number, problem_id) в порядке HTML.
        Если «Тип N» встречается несколько раз — нумерует 19.1, 19.2, ...
        Если один раз — просто «19».
        """
        resp = self._get(variant_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        seen_pids = set()
        raw = []  # [(type_num_str, pid)]

        for span in soup.find_all("span", class_="prob_nums"):
            text = span.get_text(" ", strip=True)
            type_m = re.search(r'Тип\s+(\d+)', text)
            type_str = type_m.group(1) if type_m else None

            link = span.find("a", href=re.compile(r"/problem\?id=\d+"))
            if link:
                m = re.search(r"id=(\d+)", link.get("href", ""))
                if m and m.group(1) not in seen_pids:
                    seen_pids.add(m.group(1))
                    raw.append((type_str, m.group(1)))

        # Запасной способ
        if not raw:
            for div in soup.find_all("div", class_="prob_maindiv"):
                link = div.find("a", href=re.compile(r"/problem\?id=\d+"))
                if link:
                    m = re.search(r"id=(\d+)", link.get("href", ""))
                    if m and m.group(1) not in seen_pids:
                        seen_pids.add(m.group(1))
                        raw.append((None, m.group(1)))

        # Считаем сколько раз каждый тип встречается
        from collections import Counter
        type_count = Counter(t for t, _ in raw if t is not None)

        # Генерируем display_number
        type_index = {}  # type_str -> текущий счётчик
        result = []
        for i, (type_str, pid) in enumerate(raw, start=1):
            if type_str is None:
                display = str(i)
            elif type_count[type_str] > 1:
                type_index[type_str] = type_index.get(type_str, 0) + 1
                display = f"{type_str}.{type_index[type_str]}"
            else:
                display = type_str
            result.append((display, pid))

        return result

    # ---- Шаг 2: парсить задание со страницы problem?id=X ----

    def _get_question_pbodies(self, block):
        """Возвращает pbody-элементы блока, исключая решения и критерии."""
        skip_prefixes = ["Решение", "Критерии", "Спрятать"]
        result = []
        for pb in block.find_all("div", class_="pbody"):
            clean = pb.get_text(strip=True).replace("\u00AD", "")
            # Пропускаем решения (с мягкими переносами: Ре­ше­ние)
            if clean.startswith("Ре") and "Решение" in clean[:15]:
                continue
            if any(clean.startswith(p) for p in skip_prefixes):
                continue
            result.append(pb)
        return result

    def _parse_problem(self, problem_id, base_url, task_number, is_first_in_group=True):
        """
        Парсит задание. Страница problem?id=X содержит все задания группы
        в блоках .prob_maindiv. sdamgia всегда ставит запрошенное задание
        первым блоком и добавляет к нему общий вводный текст + изображение.
        Текст сохраняется как HTML с формулами-картинками.
        """
        url = f"{base_url}/problem?id={problem_id}"
        resp = self._get(url)
        soup = BeautifulSoup(resp.text, "html.parser")

        blocks = soup.find_all("div", class_="prob_maindiv")

        # Находим .prob_maindiv, содержащий ссылку на наш problem_id
        target_block = None
        for block in blocks:
            link = block.find("a", href=re.compile(rf"/problem\?id={problem_id}\b"))
            if link:
                target_block = block
                break

        if not target_block:
            if blocks:
                target_block = blocks[0]

        if not target_block:
            raise ParserError(f"Не найден блок задания {problem_id}")

        is_grouped = len(blocks) > 1

        if is_grouped:
            our_pbodies = self._get_question_pbodies(target_block)

            # Находим другой блок для сравнения количества pbody
            other_block = None
            for b in blocks:
                if b != target_block:
                    other_block = b
                    break

            intro_pbodies = []
            question_pbodies = our_pbodies

            if other_block:
                other_count = len(self._get_question_pbodies(other_block))
                if len(our_pbodies) > other_count:
                    intro_count = len(our_pbodies) - other_count
                    intro_pbodies = our_pbodies[:intro_count]
                    question_pbodies = our_pbodies[intro_count:]

            # Собираем HTML
            question_html = "<br><br>".join(
                self._pbody_to_html(pb, base_url) for pb in question_pbodies
            )

            if intro_pbodies:
                # Общее условие — в сворачиваемый блок для всех заданий группы
                intro_html = "<br>".join(
                    self._pbody_to_html(pb, base_url) for pb in intro_pbodies
                )
                text = (
                    '<details class="shared-context">'
                    '<summary>Общее условие (нажмите, чтобы развернуть)</summary>'
                    f'<div class="shared-context-body">{intro_html}</div>'
                    '</details>'
                    f'<br>{question_html}'
                )
            else:
                text = question_html

        else:
            # Одиночное задание — берём всё
            text = self._extract_html_from_block(target_block, base_url)

        # Извлекаем ответ
        answer = self._extract_answer_from_block(target_block)
        if not answer:
            answer = self._extract_criteria_from_block(target_block)

        # Картинки уже встроены в HTML текст — отдельно не скачиваем
        return {
            "number": task_number,
            "text": sanitize_html(self._clean_html(text)[:5000]),
            "correct_answer": answer,
            "image_data": None,
            "source_id": problem_id,
        }

    def _extract_html_from_block(self, block, base_url=""):
        """Извлекает HTML задания из .prob_maindiv блока."""
        pbodies = self._get_question_pbodies(block)
        parts = [self._pbody_to_html(pb, base_url) for pb in pbodies]
        return "<br><br>".join(parts)

    def _extract_criteria_from_block(self, block):
        """Извлекает текст критериев проверки (для заданий части 2)."""
        for tag in block.find_all(string=re.compile(r'Критерии')):
            parent = tag.parent
            if parent:
                text = parent.get_text(" ", strip=True)
                m = re.search(r'Критерии[^:]*:\s*(.+)', text, re.DOTALL)
                if m:
                    criteria = m.group(1).strip()[:200]
                    return f"Критерии: {criteria}"
        return ""

    def _extract_answer_from_block(self, block):
        """Извлекает ответ из блока задания."""
        # Сначала ищем через BeautifulSoup — надёжнее для разных форматов
        for tag in block.find_all(string=re.compile(r'Ответ\s*:')):
            parent = tag.parent
            # Берём весь текст после "Ответ:" в этом элементе и следующих
            full = parent.get_text(" ", strip=True) if parent else str(tag)
            m = re.search(r'Ответ\s*:\s*(.+)', full)
            if m:
                answer = m.group(1).strip()
                for stop in ["Аналоги", "Источники", "Критерии", "Спрятать",
                              "Раздел", "Приведем", "Примечание", "Решение"]:
                    idx = answer.find(stop)
                    if idx > 0:
                        answer = answer[:idx]
                answer = answer.replace("\u00AD", "").replace("\u202f", " ").replace("&nbsp;", " ")
                answer = answer.rstrip(". \t\n\r").strip()
                if answer:
                    return answer

        html = str(block)
        # Ищем паттерн: Ответ:</span> VALUE или Ответ: VALUE
        patterns = [
            r'Ответ\s*(?:</span>)?\s*:\s*(?:</span>)?\s*(.+?)(?:<!--|\.\s*<(?:div|p\b|br))',
            r'Ответ\s*(?:</span>)?\s*:\s*(?:</span>)?\s*(.+?)(?:<div|<a\s)',
            r'Ответ\s*(?:</span>)?\s*:\s*(?:</span>)?\s*(.+?)(?:<)',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                raw = m.group(1)
                # Если ответ — формула-картинка, берём alt-текст
                img_match = re.search(r'<img[^>]+alt="([^"]+)"', raw)
                if img_match:
                    answer = img_match.group(1).replace("\u00AD", "").strip()
                    return answer

                answer = re.sub(r'<[^>]+>', '', raw)
                answer = answer.replace("\u00AD", "").replace("&nbsp;", " ")
                answer = answer.replace("&#8239;", " ").replace("\u202f", " ")
                for stop in ["Аналоги", "Источники", "Критерии", "Спрятать",
                              "Раздел", "Приведем", "Примечание", "Решение"]:
                    idx = answer.find(stop)
                    if idx > 0:
                        answer = answer[:idx]
                answer = answer.rstrip(". \t\n\r").strip()
                if answer:
                    return answer
        return ""

    def _download_image(self, img_url):
        """Скачивает изображение и возвращает dict с filename и content."""
        try:
            time.sleep(0.5)
            resp = self.session.get(img_url, timeout=10)
            resp.raise_for_status()

            ct = resp.headers.get("Content-Type", "")
            if "png" in ct:
                ext = ".png"
            elif "gif" in ct:
                ext = ".gif"
            elif "svg" in ct:
                ext = ".svg"
            else:
                ext = ".jpg"

            return {
                "filename": f"task_{uuid.uuid4().hex[:8]}{ext}",
                "content": resp.content,
            }
        except requests.RequestException as e:
            logger.warning("Не удалось скачать: %s", e)
            return None

    def _extract_image_from_block(self, block, base_url):
        """Извлекает основное изображение задания."""
        images = block.find_all("img", src=re.compile(r"/get_file\?id="))
        if not images:
            return None
        img_url = urljoin(base_url, images[0].get("src", ""))
        return self._download_image(img_url)

    def _extract_image_from_pbodies(self, pbodies, base_url):
        """Извлекает изображение из списка pbody-элементов."""
        for pb in pbodies:
            images = pb.find_all("img", src=re.compile(r"/get_file\?id="))
            if images:
                img_url = urljoin(base_url, images[0].get("src", ""))
                return self._download_image(img_url)
        return None

    # ---- Главный метод ----

    def _detect_groups(self, problem_ids, base_url):
        """
        Определяет группы заданий — задания на одной странице.
        Возвращает dict: problem_id → set(all_ids_on_that_page).
        Делаем один запрос для первого ID — если на странице несколько блоков,
        все они в одной группе.
        """
        groups = {}  # pid → set of pids in same group
        visited = set()

        for pid in problem_ids:
            if pid in visited:
                continue
            try:
                url = f"{base_url}/problem?id={pid}"
                resp = self._get(url)
                soup = BeautifulSoup(resp.text, "html.parser")
                blocks = soup.find_all("div", class_="prob_maindiv")

                group_pids = set()
                for block in blocks:
                    prob_nums = block.find("span", class_="prob_nums")
                    if prob_nums:
                        link = prob_nums.find("a", href=re.compile(r"/problem\?id=(\d+)"))
                        if link:
                            m = re.search(r"id=(\d+)", link["href"])
                            if m:
                                group_pids.add(m.group(1))

                for gid in group_pids:
                    groups[gid] = group_pids
                    visited.add(gid)

                if pid not in visited:
                    visited.add(pid)
            except ParserError:
                visited.add(pid)

        return groups

    def parse_variant(self, url):
        base_url = self._base_url(url)
        exam_type = self._detect_exam_type(url)

        vid_match = re.search(r"id=(\d+)", url)
        if not vid_match:
            raise ParserError("Не удалось определить ID варианта")
        sdamgia_id = vid_match.group(1)

        problem_ids = self._get_problem_ids_from_variant(url)
        if not problem_ids:
            raise ParserError("Не найдено заданий на странице")

        logger.info("Вариант %s: %d заданий", sdamgia_id, len(problem_ids))

        pid_list = [pid for _, pid in problem_ids]

        # Определяем группы заданий
        groups = self._detect_groups(pid_list, base_url)

        # Отслеживаем какие группы уже встречались (для is_first_in_group)
        seen_groups = set()

        tasks = []
        for i, (display_number, pid) in enumerate(problem_ids, start=1):
            try:
                group = groups.get(pid, {pid})
                group_key = frozenset(group)
                is_first = group_key not in seen_groups
                seen_groups.add(group_key)

                task = self._parse_problem(pid, base_url, task_number=display_number,
                                           is_first_in_group=is_first)
                tasks.append(task)
                logger.info("  %d/%d №%s (ID %s) — ответ: %s",
                            i, len(problem_ids), display_number, pid, task["correct_answer"] or "НЕТ")
            except ParserError as e:
                logger.warning("  %d/%d (ID %s) ОШИБКА: %s", i, len(problem_ids), pid, e)
                tasks.append({
                    "number": display_number,
                    "text": f"[Ошибка парсинга задания {pid}]",
                    "correct_answer": "",
                    "image_data": None,
                    "source_id": pid,
                })

        return {
            "sdamgia_id": sdamgia_id,
            "exam_type": exam_type,
            "tasks": tasks,
        }


def import_variant_from_sdamgia(url, variant_number=None):
    parser = SdamgiaParser()
    errors = []

    try:
        data = parser.parse_variant(url)
    except ParserError as e:
        return None, [str(e)]

    if not variant_number:
        variant_number = f"sdamgia_{data['sdamgia_id']}"

    if Variant.objects.filter(number=variant_number).exists():
        return None, [f"Вариант с номером '{variant_number}' уже существует"]

    variant = Variant.objects.create(
        number=variant_number,
        exam_type=data["exam_type"],
    )

    no_answer = []
    for td in data["tasks"]:
        manual = not bool(td["correct_answer"])
        task = Task(
            variant=variant,
            number=td["number"],
            text=td["text"],
            correct_answer=td["correct_answer"],
            source=TaskSource.PRINT_SOLVE,
            manual_grading=manual,
        )
        if td.get("image_data"):
            img = td["image_data"]
            task.image.save(img["filename"], ContentFile(img["content"]), save=False)
        task.save()

        if manual:
            no_answer.append(td["number"])

    if no_answer:
        errors.append(f"Задания с ручной проверкой: {', '.join(map(str, no_answer))}. Учитель проверяет вручную.")

    return variant, errors
