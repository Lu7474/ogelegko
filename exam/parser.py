"""
Парсер заданий с sdamgia.ru (Решу ОГЭ / Решу ЕГЭ)
"""

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.db import transaction

from .models import CatalogTask, ExamType, Task, TaskSource, Variant

logger = logging.getLogger(__name__)

# Единицы измерения, которые убираются из конца числового ответа
_UNIT_SUFFIX_RE = re.compile(
    r"^(-?[\d\s,./]+)\s*"
    r"(?:мм²?|см²?|дм²?|км²?|м²?(?!/)|кг|г(?!е|о|р)|т(?!\.)|л(?!е|и)|мл|"
    r"км/ч|м/с(?:ек)?|м/мин|руб\.?|коп\.?|%|°[CcСс]?|сек\.?|мин\.?|ч\.?(?=\s*$)|"
    r"лет|раз|шт\.?)"
    r"\s*$",
    re.IGNORECASE | re.UNICODE,
)


def _strip_measurement_unit(text):
    """Убирает единицы измерения из числового ответа: '0.4 мм' → '0.4'"""
    m = _UNIT_SUFFIX_RE.match(text.strip())
    if m:
        return m.group(1).strip().rstrip(", ")
    return text


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

REQUEST_DELAY = 0.8


class ParserError(Exception):
    pass


def sanitize_html(html):
    """Удаляет опасные теги и атрибуты из HTML (XSS-защита)."""
    # Удаляем опасные теги целиком (с содержимым)
    html = re.sub(
        r"<\s*(?:script|style|iframe|object|embed|form|link|meta)\b[^>]*>.*?"
        r"</\s*(?:script|style|iframe|object|embed|form|link|meta)\s*>",
        "",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Удаляем самозакрывающиеся опасные теги
    html = re.sub(
        r"<\s*(?:script|style|iframe|object|embed|form|link|meta)\b[^>]*/?\s*>",
        "",
        html,
        flags=re.IGNORECASE,
    )
    # Удаляем все обработчики событий (onclick, onerror, onload, etc.)
    html = re.sub(r'\s+on\w+\s*=\s*"[^"]*"', "", html, flags=re.IGNORECASE)
    html = re.sub(r"\s+on\w+\s*=\s*'[^']*'", "", html, flags=re.IGNORECASE)
    html = re.sub(r"\s+on\w+\s*=\s*[^\s>]+", "", html, flags=re.IGNORECASE)
    # Удаляем javascript: в href/src
    html = re.sub(
        r'(href|src)\s*=\s*["\']?\s*javascript\s*:', r'\1="#" data-removed="', html, flags=re.IGNORECASE
    )
    # Удаляем data: в src (кроме data:image)
    html = re.sub(r'src\s*=\s*["\']?\s*data\s*:(?!image/)', 'src="data:removed', html, flags=re.IGNORECASE)
    return html


_thread_local = threading.local()


class SdamgiaParser:
    def _session(self):
        """Возвращает сессию для текущего потока (thread-safe)."""
        if not hasattr(_thread_local, "session"):
            s = requests.Session()
            s.headers.update(HEADERS)
            _thread_local.session = s
        return _thread_local.session

    def _get(self, url, _retry=2):
        try:
            time.sleep(REQUEST_DELAY)
            resp = self._session().get(url, timeout=15)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp
        except requests.exceptions.Timeout:
            # Таймаут — ретраим один раз с паузой
            if _retry > 0:
                logger.warning("Таймаут %s, повтор...", url)
                time.sleep(3)
                return self._get(url, _retry=0)
            raise ParserError("Таймаут при загрузке страницы")
        except requests.exceptions.HTTPError as e:
            # 5xx — временный сбой сервера, ретраим несколько раз
            if e.response is not None and e.response.status_code >= 500 and _retry > 0:
                wait = (3 - _retry) * 4 + 4  # 4s, 8s
                logger.warning("Ошибка %d для %s, повтор через %ds...", e.response.status_code, url, wait)
                time.sleep(wait)
                return self._get(url, _retry=_retry - 1)
            raise ParserError(f"Не удалось загрузить: {e}")
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
        html = html.replace("\u00ad", "")
        # Нормализуем математический минус (U+2212) → ASCII дефис
        html = html.replace("\u2212", "-")
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
        inner = inner.replace("\u00ad", "")
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
            type_m = re.search(r"Тип\s+(\d+)", text)
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
            clean = pb.get_text(strip=True).replace("\u00ad", "")
            # Пропускаем решения (с мягкими переносами: Ре­ше­ние)
            if clean.startswith("Ре") and "Решение" in clean[:15]:
                continue
            if any(clean.startswith(p) for p in skip_prefixes):
                continue
            result.append(pb)
        return result

    def _parse_problem(self, problem_id, base_url, task_number):
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
            # Fallback: ищем любой div с pbody (некоторые страницы не имеют prob_maindiv)
            fallback = soup.find("div", class_=re.compile(r"prob"))
            if fallback:
                target_block = fallback
                blocks = [target_block]
            else:
                raise ParserError(f"Не найден блок задания {problem_id} (страница: {url})")

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
            question_html = "<br><br>".join(self._pbody_to_html(pb, base_url) for pb in question_pbodies)

            if intro_pbodies:
                # Общее условие — в сворачиваемый блок для всех заданий группы
                intro_html = "<br>".join(self._pbody_to_html(pb, base_url) for pb in intro_pbodies)
                text = (
                    '<details class="shared-context">'
                    "<summary>Общее условие (нажмите, чтобы развернуть)</summary>"
                    f'<div class="shared-context-body">{intro_html}</div>'
                    "</details>"
                    f"<br>{question_html}"
                )
            else:
                text = question_html

        else:
            # Одиночное задание — берём всё
            text = self._extract_html_from_block(target_block, base_url)

        # Извлекаем ответ
        answer = self._extract_answer_from_block(target_block)
        if not answer:
            # Пробуем найти «Ответ:» внутри раздела критериев (задание 24 и т.п.)
            answer = self._extract_answer_from_criteria(target_block)
        if not answer:
            # Последний fallback: берём критерии для ручной проверки
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
        # Сначала ищем pbody, начинающийся с "Критерии"
        for pb in block.find_all("div", class_="pbody"):
            text = pb.get_text(" ", strip=True).replace("\u00ad", "")
            if text.startswith("Критерии"):
                return text[:500]
        # Запасной: любой элемент с текстом "Критерии"
        for tag in block.find_all(string=re.compile(r"Критерии")):
            parent = tag.parent
            if parent:
                text = parent.get_text(" ", strip=True).replace("\u00ad", "")
                if "Критерии" in text:
                    return text[:500]
        return ""

    def _clean_answer(self, raw):
        """Очищает извлечённый ответ от мусора и единиц измерения."""
        stop_words = [
            "Аналоги",
            "Источники",
            "Критерии",
            "Спрятать",
            "Раздел",
            "Приведем",
            "Примечание",
            "Решение",
            "Пояснение",
            "В открытом банке",
            "Для ознакомления",
        ]
        raw = raw.replace("\u00ad", "").replace("\u202f", " ").replace("&nbsp;", " ")
        raw = raw.replace("&#8239;", " ")
        # Нормализуем знаки минуса: U+2212 (математический), U+2013 (en-dash) → ASCII
        raw = raw.replace("\u2212", "-").replace("\u2013", "-")
        raw = raw.strip()
        for stop in stop_words:
            idx = raw.find(stop)
            if idx > 0:
                raw = raw[:idx]
        raw = re.sub(r"[\s\-–—.]+$", "", raw).strip()
        # Убираем единицы измерения из числового ответа: "0.4 мм" → "0.4"
        raw = _strip_measurement_unit(raw)
        # Убираем π из ответа: "27π" → "27" (ученик вводит только коэффициент)
        raw = re.sub(r"^(-?\d+(?:[.,]\d+)?)\s*[π]\s*$", r"\1", raw)
        return raw

    def _extract_answer_from_block(self, block):
        """Извлекает ответ из блока задания."""

        # 1. BS4: ищем "Ответ:" и идём вверх по родителям, чтобы захватить
        #    соседние span/b с самим значением ответа.
        for tag in block.find_all(string=re.compile(r"Ответ\s*:", re.IGNORECASE)):
            node = tag.parent
            # Поднимаемся до 3 уровней вверх, пока не найдём текст с ответом
            for _ in range(3):
                if node is None:
                    break
                full = node.get_text(" ", strip=True).replace("\u00ad", "")
                m = re.search(r"Ответ\s*:\s*(.+)", full, re.DOTALL)
                if m:
                    answer = self._clean_answer(m.group(1))
                    if answer and len(answer) < 200:
                        return answer
                node = node.parent

        # 2. Ищем ответ в соседней ячейке таблицы (некоторые задания на СдамГИА
        #    помещают «Ответ:» в одну <td>, а само значение — в следующую).
        for td in block.find_all("td"):
            text = td.get_text(" ", strip=True).replace("\u00ad", "")
            if re.match(r"^Ответ\s*:?\s*$", text, re.IGNORECASE):
                sib = td.find_next_sibling("td")
                if sib:
                    answer = self._clean_answer(sib.get_text(" ", strip=True))
                    if answer:
                        return answer

        # 3. Regex по raw HTML — расширенный набор паттернов
        html = str(block)
        patterns = [
            # Ответ: <span>…</span> или Ответ: <b>…</b>
            r"Ответ\s*(?:</?\w+[^>]*)?\s*:\s*(?:<[^>]+>)?\s*(.+?)(?:<!--|\.\s*<(?:div|p\b|br|tr|td))",
            r"Ответ\s*(?:</?\w+[^>]*)?\s*:\s*(?:<[^>]+>)?\s*(.+?)(?:<div|<p\b|<br|<tr|<a\s)",
            r"Ответ\s*(?:</?\w+[^>]*)?\s*:\s*(?:<[^>]+>)?\s*(.+?)(?:<)",
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if m:
                raw = m.group(1)
                # Если ответ — формула-картинка, берём alt-текст
                img_m = re.search(r'<img[^>]+alt="([^"]+)"', raw)
                if img_m:
                    answer = self._clean_answer(img_m.group(1))
                    if answer:
                        return answer
                answer = self._clean_answer(re.sub(r"<[^>]+>", "", raw))
                if answer:
                    return answer
        return ""

    def _extract_answer_from_criteria(self, block):
        """Пытается найти числовой ответ ВНУТРИ блока критериев.

        Задания 24 и некоторые другие на СдамГИА содержат «Ответ: X»
        прямо в разделе «Критерии оценивания».  Эта функция извлекает
        только числовое значение, игнорируя остальной текст критериев.
        """
        for pb in block.find_all("div", class_="pbody"):
            text = pb.get_text(" ", strip=True).replace("\u00ad", "")
            if not text.startswith("Критерии"):
                continue
            # Ищем «Ответ:» в тексте критериев
            m = re.search(r"Ответ\s*:\s*([^\n.]{1,80})", text)
            if m:
                answer = self._clean_answer(m.group(1))
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

        total = len(problem_ids)

        def fetch_one(args):
            i, display_number, pid = args
            try:
                task = self._parse_problem(pid, base_url, task_number=display_number)
                logger.info(
                    "  %d/%d №%s (ID %s) — ответ: %s",
                    i,
                    total,
                    display_number,
                    pid,
                    task["correct_answer"] or "НЕТ",
                )
                return task
            except ParserError as e:
                logger.warning("  %d/%d (ID %s) ОШИБКА: %s", i, total, pid, e)
                return {
                    "number": display_number,
                    "text": f"[Ошибка загрузки задания {pid}: {e}]",
                    "correct_answer": "",
                    "image_data": None,
                    "source_id": pid,
                }

        jobs = [(i, dn, pid) for i, (dn, pid) in enumerate(problem_ids, start=1)]
        with ThreadPoolExecutor(max_workers=3) as executor:
            tasks = list(executor.map(fetch_one, jobs))

        return {
            "sdamgia_id": sdamgia_id,
            "exam_type": exam_type,
            "tasks": tasks,
        }


_FORMULA_KEYWORDS = [
    "дробь",
    "числитель",
    "знаменатель",
    "корень из",
    "квадратный корень",
    "фигурная скобка",
    "принадлежит",
    "степень",
    "логарифм",
    "синус",
    "косинус",
    "тангенс",
    "котангенс",
    "арксинус",
    "арккосинус",
]


def _is_no_input_task(exam_type, task_number) -> bool:
    """ОГЭ задания 20–25 решаются на бумаге — студент ничего не вводит."""
    if exam_type != ExamType.OGE:
        return False
    try:
        n = int(str(task_number).split(".")[0])
        return n >= 20
    except (ValueError, TypeError):
        return False


def _get_oge_default_points(task_number) -> int:
    """Возвращает максимальный балл для задания ОГЭ по математике.
    Задания 20–25 (развёрнутый ответ): 2 балла. Остальные: 1 балл.
    """
    try:
        n = int(str(task_number).split(".")[0])
        return 2 if n >= 20 else 1
    except (ValueError, TypeError):
        return 1


def _is_formula_answer(answer: str) -> bool:
    """Ответ — alt-текст формулы-картинки, ученик не сможет его ввести."""
    a = answer.lower()
    return any(kw in a for kw in _FORMULA_KEYWORDS)


def import_task_to_catalog(url, task_number=None):
    """Импортирует одно задание по URL /problem?id=XXX в каталог."""
    m = re.search(r"id=(\d+)", url)
    if not m:
        return None, ["Не удалось определить ID задания из URL"]
    problem_id = m.group(1)

    if CatalogTask.objects.filter(sdamgia_id=problem_id).exists():
        return None, [f"Задание с ID {problem_id} уже есть в каталоге"]

    parser = SdamgiaParser()
    base_url = parser._base_url(url)
    if not base_url:
        return None, ["Не удалось определить базовый URL"]
    exam_type = parser._detect_exam_type(url)

    try:
        task_data = parser._parse_problem(problem_id, base_url, task_number=str(task_number or ""))
    except ParserError as e:
        return None, [str(e)]

    correct_answer = task_data["correct_answer"]
    no_input = _is_no_input_task(exam_type, task_number)
    if no_input:
        correct_answer = ""
    manual = (
        no_input
        or not correct_answer
        or correct_answer.startswith("Критерии")
        or _is_formula_answer(correct_answer)
    )
    if not manual and exam_type == ExamType.EGE_PROFILE and task_number:
        try:
            if int(str(task_number).split(".")[0]) >= 13:
                manual = True
        except (ValueError, TypeError):
            pass

    try:
        if exam_type == ExamType.OGE:
            default_pts = _get_oge_default_points(task_number)
        else:
            default_pts = 2 if manual else 1
        ct = CatalogTask(
            task_number=task_number,
            exam_type=exam_type,
            text=task_data["text"],
            correct_answer=correct_answer,
            source=TaskSource.SDAMGIA,
            manual_grading=manual,
            no_student_input=no_input,
            points=default_pts,
            sdamgia_id=problem_id,
        )
        ct.save()
    except Exception as e:
        return None, [f"Ошибка сохранения: {e}"]

    return ct, []


def import_variant_to_catalog(url, session=None):
    """Парсит вариант и добавляет все задания в каталог (не создаёт вариант).
    session — необязательный объект CatalogImportSession для привязки заданий."""
    parser = SdamgiaParser()
    try:
        data = parser.parse_variant(url)
    except ParserError as e:
        return 0, [str(e)]

    exam_type = data["exam_type"]
    added = 0
    duplicate = 0
    errors = []

    with transaction.atomic():
        for td in data["tasks"]:
            problem_id = td.get("source_id")
            if problem_id and CatalogTask.objects.filter(sdamgia_id=problem_id).exists():
                errors.append(f"Задание №{td['number']} (ID {problem_id}) уже в каталоге — пропущено")
                duplicate += 1
                continue

            try:
                task_num_int = int(str(td["number"]).split(".")[0])
            except (ValueError, TypeError):
                task_num_int = None

            correct_answer = td["correct_answer"]
            no_input = _is_no_input_task(exam_type, task_num_int)
            if no_input:
                correct_answer = ""
            manual = no_input or (
                not correct_answer
                or correct_answer.startswith("Критерии")
                or _is_formula_answer(correct_answer)
            )
            if not manual and exam_type == ExamType.EGE_PROFILE:
                try:
                    if int(str(td["number"]).split(".")[0]) >= 13:
                        manual = True
                except (ValueError, TypeError):
                    pass

            if exam_type == ExamType.OGE:
                default_pts = _get_oge_default_points(task_num_int)
            else:
                default_pts = 2 if manual else 1
            ct = CatalogTask(
                task_number=task_num_int,
                exam_type=exam_type,
                text=td["text"],
                correct_answer=correct_answer,
                source=TaskSource.SDAMGIA,
                manual_grading=manual,
                no_student_input=no_input,
                points=default_pts,
                sdamgia_id=problem_id if problem_id else None,
                import_session=session,
            )
            ct.save()
            added += 1

        if session is not None:
            session.tasks_added = added
            session.tasks_duplicate = duplicate
            session.status = "done"
            session.save(update_fields=["tasks_added", "tasks_duplicate", "status"])

    return added, errors


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

    try:
        with transaction.atomic():
            variant = Variant.objects.create(
                number=variant_number,
                exam_type=data["exam_type"],
            )

            exam_type = data["exam_type"]
            no_answer = []
            for td in data["tasks"]:
                correct_answer = td["correct_answer"]
                try:
                    task_num_int = int(str(td["number"]).split(".")[0])
                except (ValueError, TypeError):
                    task_num_int = None
                no_input = _is_no_input_task(exam_type, task_num_int)
                if no_input:
                    correct_answer = ""
                manual = no_input or (
                    not correct_answer
                    or correct_answer.startswith("Критерии")
                    or _is_formula_answer(correct_answer)
                )
                # ЕГЭ профиль: задания 13–19 всегда ручная проверка
                if not manual and exam_type == ExamType.EGE_PROFILE:
                    try:
                        if int(str(td["number"]).split(".")[0]) >= 13:
                            manual = True
                    except (ValueError, TypeError):
                        pass
                if exam_type == ExamType.OGE:
                    default_pts = _get_oge_default_points(task_num_int)
                else:
                    default_pts = 2 if manual else 1
                task = Task(
                    variant=variant,
                    number=td["number"],
                    text=td["text"],
                    correct_answer=correct_answer,
                    source=TaskSource.SDAMGIA,
                    manual_grading=manual,
                    no_student_input=no_input,
                    points=default_pts,
                )
                if td.get("image_data"):
                    img = td["image_data"]
                    task.image.save(img["filename"], ContentFile(img["content"]), save=False)
                task.save()

                if manual:
                    no_answer.append(td["number"])

    except Exception as e:
        logger.exception("Ошибка сохранения варианта в БД")
        return None, [f"Ошибка сохранения: {e}"]

    if no_answer:
        errors.append(
            f"Задания с ручной проверкой: {', '.join(map(str, no_answer))}. Учитель проверяет вручную."
        )

    return variant, errors
