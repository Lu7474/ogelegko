"""
Парсер заданий с oge.fipi.ru (банк заданий ФИПИ).

Примечание: oge.fipi.ru использует SSL-сертификат, выданный российским УЦ
Минцифры (не входит в стандартный certifi). Все запросы отправляются с
verify=False — предупреждения InsecureRequestWarning подавляются.
"""

import logging
import re
import time
import uuid

import requests
import urllib3
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile

from .models import CatalogImportSession, CatalogTask, CatalogTaskImage, TaskSource
from .parser import ParserError, sanitize_html

logger = logging.getLogger(__name__)

# Сертификат ФИПИ выдан российским УЦ, не признаётся стандартным certifi
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FIPI_BASE = "https://oge.fipi.ru"
FIPI_PAGESIZE = 100
FIPI_DELAY = 0.7
FIPI_IMG_DELAY = 0.3

FIPI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{FIPI_BASE}/bank/",
}


def _extract_proj_guid(url):
    m = re.search(r"proj=([A-F0-9]{32})", url, re.IGNORECASE)
    return m.group(1).upper() if m else None


def _get(session, url):
    time.sleep(FIPI_DELAY)
    try:
        resp = session.get(url, headers=FIPI_HEADERS, timeout=20, verify=False)
    except requests.exceptions.ConnectTimeout:
        raise ParserError(
            "Сервер не может подключиться к oge.fipi.ru (таймаут). "
            "Сайт ФИПИ, вероятно, недоступен с IP вашего хостинга."
        )
    except requests.exceptions.ConnectionError:
        raise ParserError(
            "Не удалось подключиться к oge.fipi.ru. Проверьте доступность сайта с вашего сервера."
        )
    resp.raise_for_status()
    enc = "utf-8" if "utf-8" in resp.headers.get("Content-Type", "").lower() else "cp1251"
    return resp.content.decode(enc, errors="replace")


def _post(session, url, data):
    time.sleep(FIPI_DELAY)
    resp = session.post(url, data=data, headers=FIPI_HEADERS, timeout=20, verify=False)
    resp.raise_for_status()
    enc = "utf-8" if "utf-8" in resp.headers.get("Content-Type", "").lower() else "cp1251"
    return resp.content.decode(enc, errors="replace")


def _qs_url(proj, page=0):
    return f"{FIPI_BASE}/bank/questions.php?proj={proj}&page={page}&pagesize={FIPI_PAGESIZE}"


def _qs_post_data(proj, page=0, theme=""):
    return {
        "search": "1",
        "pagesize": str(FIPI_PAGESIZE),
        "proj": proj,
        "theme": theme,
        "qlevel": "",
        "qkind": "",
        "qsstruct": "",
        "qpos": "",
        "qid": "",
        "zid": "",
        "solved": "",
        "favorite": "",
        "blind": "",
        "page": str(page),
    }


def _count(html):
    # Основной паттерн: JavaScript-вызов setQCount(45)
    m = re.search(r"setQCount\((\d+)", html)
    if m:
        return int(m.group(1))
    # Резервный: «Всего: 45» или «Результатов: 45»
    m = re.search(r"(?:Всего|Результатов|всего)[:\s]+(\d+)", html)
    if m:
        return int(m.group(1))
    # Резервный: JSON-поле "total":45
    m = re.search(r'"total"\s*:\s*(\d+)', html)
    if m:
        return int(m.group(1))
    return 0


def _resolve_proj_from_url(url):
    """
    Извлекает proj GUID из URL. Если URL — страница fipi.ru без GUID,
    пробует получить страницу и найти ссылки на банк с proj= внутри.
    Возвращает (proj, subject_links) где subject_links — список найденных проектов.
    """
    proj = _extract_proj_guid(url)
    if proj:
        return proj, []

    # Попробуем найти ссылки на банк внутри страницы fipi.ru
    try:
        resp = requests.get(url, headers=FIPI_HEADERS, timeout=15, verify=False)
        html = resp.text
    except Exception:
        return None, []

    # Ищем все proj= GUID в HTML
    guids = re.findall(r"proj=([A-F0-9]{32})", html, re.IGNORECASE)
    guids = list(dict.fromkeys(g.upper() for g in guids))  # уникальные, порядок сохранён

    # Ищем подписи к ссылкам (текст рядом)
    soup = BeautifulSoup(html, "html.parser")
    subject_links = []
    for tag in soup.find_all(href=re.compile(r"proj=", re.IGNORECASE)):
        g = _extract_proj_guid(tag["href"])
        if g:
            subject_links.append(
                {
                    "proj": g,
                    "name": tag.get_text(" ", strip=True)[:80] or tag["href"],
                }
            )
    # Дедупликация по proj
    seen = set()
    unique_links = []
    for sl in subject_links:
        if sl["proj"] not in seen:
            seen.add(sl["proj"])
            unique_links.append(sl)

    return None, unique_links


def fipi_get_preview(url):
    """
    Анализирует URL банка ФИПИ без начала импорта.
    Возвращает: {proj, total, pages, topics: [{code, name, count}]}
    Если URL — страница fipi.ru без GUID, возвращает {subject_links: [...]}
    """
    proj, subject_links = _resolve_proj_from_url(url)

    if not proj:
        if subject_links:
            return {"subject_links": subject_links}
        raise ParserError(
            "Не удалось найти GUID проекта в URL. "
            "Нужна ссылка вида: https://oge.fipi.ru/bank/index.php?proj=XXXX..."
        )

    sess = requests.Session()

    # Общее кол-во
    html0 = _get(sess, f"{FIPI_BASE}/bank/questions.php?proj={proj}&page=0&pagesize=1")
    total = _count(html0)
    pages = (total + FIPI_PAGESIZE - 1) // FIPI_PAGESIZE

    # Список тем с главной страницы
    idx_html = _get(sess, f"{FIPI_BASE}/bank/index.php?proj={proj}")
    raw_topics = re.findall(
        r"<input type='checkbox' name='theme' value='([^']+)'>\s*([^<\n\r]+)",
        idx_html,
    )
    topics = []
    for code, raw_name in raw_topics:
        code = code.strip()
        raw_name = raw_name.strip().rstrip("</").strip()
        if "." not in code:
            continue  # пропускаем заголовки-разделы (value="1", "2" …)
        # Пробуем вытащить кол-во из конца метки: "Название (45)"
        count = None
        m_cnt = re.search(r"\((\d+)\)\s*$", raw_name)
        if m_cnt:
            count = int(m_cnt.group(1))
            raw_name = raw_name[: m_cnt.start()].strip()
        topics.append({"code": code, "name": raw_name, "count": count})

    # Кол-во заданий по каждой теме — только для тех, где не нашли в метке
    for topic in topics:
        if topic["count"] is not None:
            continue
        try:
            h = _post(
                sess,
                f"{FIPI_BASE}/bank/questions.php",
                {**_qs_post_data(proj, page=0, theme=topic["code"]), "pagesize": "1"},
            )
            c = _count(h)
            topic["count"] = c if c else None
            time.sleep(0.25)
        except Exception:
            topic["count"] = None

    return {"proj": proj, "total": total, "pages": pages, "topics": topics}


def _mathml_to_html(tag):
    """
    Рекурсивно конвертирует MathML-тег в читаемый HTML.
    msup/msub → <sup>/<sub>, mfrac → (a/b), msqrt → √(…).
    """
    name = getattr(tag, "name", None)
    if name is None:
        # Текстовый узел — пропускаем чисто-пробельные (отступы XML)
        text = str(tag).strip()
        return text

    local = name.split(":")[-1].lower() if ":" in name else name.lower()

    # Аннотации содержат LaTeX/MathML дубль — пропускаем
    if local in ("annotation", "annotation-xml"):
        return ""

    # Простые токены — берём текст, убираем краевые пробелы, нормализуем минус
    if local in ("mi", "mn", "mo", "mtext"):
        return tag.get_text().strip().replace("\u2212", "-").replace("\u2013", "-")

    # Пробел
    if local == "mspace":
        return " "

    # Прозрачные контейнеры
    if local in ("math", "mrow", "semantics", "mstyle", "mpadded", "mphantom", "merror"):
        return "".join(_mathml_to_html(c) for c in tag.children)

    # Степень: x² → x<sup>2</sup>
    if local == "msup":
        kids = [c for c in tag.children if getattr(c, "name", None)]
        if len(kids) >= 2:
            return f"{_mathml_to_html(kids[0])}<sup>{_mathml_to_html(kids[1])}</sup>"
        return tag.get_text()

    # Нижний индекс
    if local == "msub":
        kids = [c for c in tag.children if getattr(c, "name", None)]
        if len(kids) >= 2:
            return f"{_mathml_to_html(kids[0])}<sub>{_mathml_to_html(kids[1])}</sub>"
        return tag.get_text()

    # Нижний + верхний индексы
    if local == "msubsup":
        kids = [c for c in tag.children if getattr(c, "name", None)]
        if len(kids) >= 3:
            return (
                f"{_mathml_to_html(kids[0])}"
                f"<sub>{_mathml_to_html(kids[1])}</sub>"
                f"<sup>{_mathml_to_html(kids[2])}</sup>"
            )
        return tag.get_text()

    # Дробь: визуальный стек через CSS-класс math-frac
    if local == "mfrac":
        kids = [c for c in tag.children if getattr(c, "name", None)]
        if len(kids) >= 2:
            num = _mathml_to_html(kids[0])
            den = _mathml_to_html(kids[1])
            return f'<span class="math-frac"><span>{num}</span><span>{den}</span></span>'
        return tag.get_text()

    # Квадратный корень
    if local == "msqrt":
        inner = "".join(_mathml_to_html(c) for c in tag.children)
        return f"√({inner})"

    # Корень n-й степени
    if local == "mroot":
        kids = [c for c in tag.children if getattr(c, "name", None)]
        if len(kids) >= 2:
            return f"<sup>{_mathml_to_html(kids[1])}</sup>√({_mathml_to_html(kids[0])})"
        return tag.get_text()

    # Сумма/интеграл с пределами
    if local in ("munder", "mover", "munderover"):
        return "".join(_mathml_to_html(c) for c in tag.children if getattr(c, "name", None))

    # Всё остальное — берём текст потомков
    return "".join(_mathml_to_html(c) for c in tag.children)


def _process_cell_html(raw_html):
    """
    1. Заменяет все <m:math>…</m:math> блоки на HTML (sup/sub).
    2. Удаляет дублирующийся голый текст перед <p>-тегами.
    """

    def _replace_math(m):
        block_soup = BeautifulSoup(m.group(0), "html.parser")
        math_tag = block_soup.find(re.compile(r"^m:math$", re.IGNORECASE))
        if math_tag is None:
            return m.group(0)
        return _mathml_to_html(math_tag)

    # Конвертируем MathML-блоки
    result = re.sub(
        r"<m:math\b[^>]*>.*?</m:math\s*>",
        _replace_math,
        raw_html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # На всякий случай убираем оставшиеся MathML-теги (без <m:math>)
    result = re.sub(r"</?m:[a-z:]+[^>]*>", "", result, flags=re.IGNORECASE)

    # Если в тексте есть <p>-теги, убираем голый дублирующийся текст перед ними
    if re.search(r"<p\b", result, re.IGNORECASE):
        result = re.sub(r"^\s*[^<]+(?=\s*<)", "", result.lstrip())

    # Делаем относительные <img src="/..."> абсолютными → картинки грузятся с ФИПИ
    result = re.sub(
        r'(<img[^>]+\bsrc=")(/[^"]+)',
        r"\g<1>" + FIPI_BASE + r"\2",
        result,
        flags=re.IGNORECASE,
    )

    # Нормализуем математический минус (U+2212) в тексте → ASCII дефис
    result = result.replace("\u2212", "-")

    # Схлопываем множественные пробелы внутри тегов (артефакты MathML)
    result = re.sub(r" {2,}", " ", result)

    return result.strip()


def _parse_page(sess, proj, page, theme=""):
    """Парсит одну страницу заданий, возвращает список dict."""
    if theme:
        html = _post(sess, f"{FIPI_BASE}/bank/questions.php", _qs_post_data(proj, page=page, theme=theme))
    else:
        html = _get(sess, _qs_url(proj, page))

    soup = BeautifulSoup(html, "html.parser")
    tasks = []

    for block in soup.find_all("div", class_="qblock"):
        task_id = block.get("id", "").lstrip("q")
        guid_input = block.find("input", attrs={"name": "guid"})
        if not guid_input:
            continue
        guid = guid_input.get("value", "")

        # URL картинок из ShowPictureQ — извлекаем ДО удаления скриптов,
        # т.к. картинки ФИПИ встроены именно через <script>ShowPictureQ(...)</script>,
        # а не через обычные <img>. Скрипты живут внутри cell_0 и исчезают
        # из дерева BS4 после decompose(). Поддерживаем оба вида кавычек.
        pics = re.findall(r"ShowPictureQ\w*\(['\"]([^'\"]+)['\"]", str(block))
        show_pic_urls = [f"{FIPI_BASE}/{p.lstrip('/')}" for p in pics]

        # Текст задания (ячейка cell_0) — скрипты удаляем ПОСЛЕ извлечения URL
        cell = block.find("td", class_="cell_0")
        raw_html = ""
        if cell:
            for s in cell.find_all("script"):
                s.decompose()
            raw_html = cell.decode_contents().strip()

        # Обычные <img> в тексте (редко, но бывает)
        inline_srcs = re.findall(r'<img[^>]+\bsrc=["\']?(/[^"\'>\s]+)', raw_html, re.IGNORECASE)
        inline_urls = [f"{FIPI_BASE}{s}" for s in inline_srcs]
        # popup-картинки, которых нет в тексте
        popup_only_urls = [u for u in show_pic_urls if u not in inline_urls]
        image_urls = list(dict.fromkeys(inline_urls + show_pic_urls))

        # Тема из info-панели (следующий div id="i{task_id}")
        info_div = block.find_next_sibling("div", id=f"i{task_id}")
        theme_text = ""
        if info_div:
            for row in info_div.find_all("tr"):
                tds = row.find_all("td")
                if len(tds) >= 2 and "Тема" in (tds[0].get_text() or ""):
                    theme_text = tds[1].get_text(" ", strip=True)
                    break

        tasks.append(
            {
                "task_id": task_id,
                "guid": guid,
                "html": sanitize_html(_process_cell_html(raw_html)),
                "inline_image_urls": list(dict.fromkeys(inline_urls)),
                "popup_image_urls": popup_only_urls,
                "image_urls": image_urls,  # оставляем для обратной совместимости
                "theme": theme_text,
            }
        )

    return tasks


def import_fipi_to_catalog(proj, exam_type, theme_filter, session_id):
    """
    Импортирует одну тему ФИПИ в каталог.
    Счётчики накапливаются поверх уже существующих в CatalogImportSession
    (для последовательного многотемного импорта).
    Не вызывает connection.close() — это делает вызывающий поток.
    """
    import json

    sess = requests.Session()
    import_session = CatalogImportSession.objects.get(id=session_id)

    # Сколько страниц
    try:
        if theme_filter:
            html0 = _post(
                sess,
                f"{FIPI_BASE}/bank/questions.php",
                {**_qs_post_data(proj, page=0, theme=theme_filter), "pagesize": "1"},
            )
        else:
            html0 = _get(sess, f"{FIPI_BASE}/bank/questions.php?proj={proj}&page=0&pagesize=1")
        total_tasks = _count(html0)
    except Exception as e:
        import_session.status = "error"
        import_session.notes = str(e)
        import_session.save()
        raise

    total_pages = (total_tasks + FIPI_PAGESIZE - 1) // FIPI_PAGESIZE
    # Начинаем с уже накопленных значений (при многотемном импорте)
    added = import_session.tasks_added
    skipped = import_session.tasks_skipped
    duplicate = import_session.tasks_duplicate
    duplicate_pairs = []

    for page in range(total_pages):
        try:
            tasks = _parse_page(sess, proj, page, theme_filter)
        except Exception as e:
            logger.warning("ФИПИ страница %d ошибка: %s", page, e)
            tasks = []

        for td in tasks:
            guid = td["guid"]
            text = td["html"]
            text_hash = CatalogTask.compute_hash(text)

            # 1. Дубль по GUID
            if guid and CatalogTask.objects.filter(fipi_guid=guid).exists():
                skipped += 1
                continue

            # 2. Дубль по хэшу текста
            existing = None
            if text_hash:
                existing = CatalogTask.objects.filter(text_hash=text_hash).first()

            if existing:
                duplicate += 1
                if len(duplicate_pairs) < 50:
                    duplicate_pairs.append(
                        {
                            "guid": guid,
                            "html": text[:300],
                            "image_urls": td["image_urls"],
                            "theme": td["theme"],
                            "existing_id": existing.id,
                            "existing_preview": existing.text_preview,
                            "existing_source": existing.get_source_display(),
                        }
                    )
                continue

            # 3. Скачиваем inline-картинки из текста и заменяем FIPI-URLs на локальные
            from django.core.files.storage import default_storage

            processed_text = text
            for img_url in td.get("inline_image_urls", []):
                try:
                    time.sleep(FIPI_IMG_DELAY)
                    img_resp = sess.get(img_url, headers=FIPI_HEADERS, timeout=15, verify=False)
                    if img_resp.status_code == 200:
                        ct_header = img_resp.headers.get("Content-Type", "")
                        ext = ".png" if "png" in ct_header else ".jpg"
                        fname = f"catalog/fipi_{uuid.uuid4().hex[:8]}{ext}"
                        saved_path = default_storage.save(fname, ContentFile(img_resp.content))
                        local_url = default_storage.url(saved_path)
                        processed_text = processed_text.replace(img_url, local_url)
                except Exception:
                    pass

            # 4. Картинки задания (ShowPictureQ → popup_image_urls):
            #    первая — ct.image, остальные — CatalogTaskImage
            popup_urls = td.get("popup_image_urls", [])
            main_image_content = None
            main_image_name = None
            extra_images = []  # [(bytes, ext), ...]
            for i, img_url in enumerate(popup_urls):
                try:
                    time.sleep(FIPI_IMG_DELAY)
                    img_resp = sess.get(img_url, headers=FIPI_HEADERS, timeout=15, verify=False)
                    if img_resp.status_code == 200:
                        ct_header = img_resp.headers.get("Content-Type", "")
                        # Определяем расширение по Content-Type или по имени файла
                        if "png" in ct_header:
                            ext = ".png"
                        elif "gif" in ct_header:
                            ext = ".gif"
                        else:
                            ext = "." + img_url.rsplit(".", 1)[-1].lower() if "." in img_url else ".jpg"
                        if i == 0:
                            main_image_name = f"fipi_{uuid.uuid4().hex[:8]}{ext}"
                            main_image_content = img_resp.content
                        else:
                            extra_images.append((img_resp.content, ext))
                except Exception:
                    pass

            ct = CatalogTask(
                task_number=None,
                exam_type=exam_type,
                text=processed_text,
                correct_answer="",
                source=TaskSource.FIPI,
                manual_grading=True,
                points=2,
                fipi_guid=guid or None,
                text_hash=text_hash,
                import_session=import_session,
            )
            if main_image_content and main_image_name:
                ct.image.save(main_image_name, ContentFile(main_image_content), save=False)
            ct.save()
            for order, (img_bytes, ext) in enumerate(extra_images):
                fname = f"fipi_{uuid.uuid4().hex[:8]}{ext}"
                ci = CatalogTaskImage(task=ct, order=order)
                ci.image.save(fname, ContentFile(img_bytes), save=False)
                ci.save()
            added += 1

        # Обновляем прогресс после каждой страницы
        import_session.tasks_added = added
        import_session.tasks_skipped = skipped
        import_session.tasks_duplicate = duplicate
        import_session.save(update_fields=["tasks_added", "tasks_skipped", "tasks_duplicate"])

    import_session.status = "done"
    import_session.notes = json.dumps(duplicate_pairs, ensure_ascii=False, default=str)
    import_session.save(update_fields=["status", "notes", "tasks_added", "tasks_skipped", "tasks_duplicate"])
