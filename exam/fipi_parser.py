"""
Парсер заданий с oge.fipi.ru (банк заданий ФИПИ).

Примечание: oge.fipi.ru использует SSL-сертификат, выданный российским УЦ
Минцифры (не входит в стандартный certifi). Все запросы отправляются с
verify=False — предупреждения InsecureRequestWarning подавляются.
"""
import re
import time
import uuid
import logging
import requests
import urllib3
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile

from .parser import sanitize_html, ParserError
from .models import ExamType, TaskSource, CatalogTask, CatalogImportSession

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
            "Не удалось подключиться к oge.fipi.ru. "
            "Проверьте доступность сайта с вашего сервера."
        )
    resp.raise_for_status()
    return resp.content.decode("cp1251", errors="replace")


def _post(session, url, data):
    time.sleep(FIPI_DELAY)
    resp = session.post(url, data=data, headers=FIPI_HEADERS, timeout=20, verify=False)
    resp.raise_for_status()
    return resp.content.decode("cp1251", errors="replace")


def _qs_url(proj, page=0):
    return f"{FIPI_BASE}/bank/questions.php?proj={proj}&page={page}&pagesize={FIPI_PAGESIZE}"


def _qs_post_data(proj, page=0, theme=""):
    return {
        "search": "1",
        "pagesize": str(FIPI_PAGESIZE),
        "proj": proj,
        "theme": theme,
        "qlevel": "", "qkind": "", "qsstruct": "",
        "qpos": "", "qid": "", "zid": "",
        "solved": "", "favorite": "", "blind": "",
        "page": str(page),
    }


def _count(html):
    m = re.search(r"setQCount\((\d+)", html)
    return int(m.group(1)) if m else 0


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
            subject_links.append({
                "proj": g,
                "name": tag.get_text(" ", strip=True)[:80] or tag["href"],
            })
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
    for code, name in raw_topics:
        code = code.strip()
        name = name.strip().rstrip("</")
        if "." not in code:
            continue  # пропускаем заголовки-разделы (value="1", "2" …)
        topics.append({"code": code, "name": name, "count": 0})

    # Кол-во заданий по каждой теме
    for topic in topics:
        try:
            h = _post(sess, f"{FIPI_BASE}/bank/questions.php",
                      _qs_post_data(proj, page=0, theme=topic["code"]))
            # pagesize=1 для скорости
            h = _post(sess, f"{FIPI_BASE}/bank/questions.php", {
                **_qs_post_data(proj, page=0, theme=topic["code"]),
                "pagesize": "1",
            })
            topic["count"] = _count(h)
            time.sleep(0.25)
        except Exception:
            topic["count"] = 0

    return {"proj": proj, "total": total, "pages": pages, "topics": topics}


def _parse_page(sess, proj, page, theme=""):
    """Парсит одну страницу заданий, возвращает список dict."""
    if theme:
        html = _post(sess, f"{FIPI_BASE}/bank/questions.php",
                     _qs_post_data(proj, page=page, theme=theme))
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

        # Текст задания (ячейка cell_0)
        cell = block.find("td", class_="cell_0")
        raw_html = ""
        if cell:
            for s in cell.find_all("script"):
                s.decompose()
            raw_html = cell.decode_contents().strip()

        # URL картинок
        pics = re.findall(r"ShowPictureQ\w*\('([^']+)'", str(block))
        image_urls = [f"{FIPI_BASE}/{p.lstrip('/')}" for p in pics]

        # Тема из info-панели (следующий div id="i{task_id}")
        info_div = block.find_next_sibling("div", id=f"i{task_id}")
        theme_text = ""
        if info_div:
            for row in info_div.find_all("tr"):
                tds = row.find_all("td")
                if len(tds) >= 2 and "Тема" in (tds[0].get_text() or ""):
                    theme_text = tds[1].get_text(" ", strip=True)
                    break

        tasks.append({
            "task_id": task_id,
            "guid": guid,
            "html": sanitize_html(raw_html),
            "image_urls": image_urls,
            "theme": theme_text,
        })

    return tasks


def import_fipi_to_catalog(proj, exam_type, theme_filter, session_id):
    """
    Фоновый импорт ФИПИ в каталог.
    Обновляет CatalogImportSession напрямую.
    """
    import json
    from django.db import connection

    sess = requests.Session()
    import_session = CatalogImportSession.objects.get(id=session_id)

    # Сколько страниц
    try:
        if theme_filter:
            html0 = _post(sess, f"{FIPI_BASE}/bank/questions.php",
                          {**_qs_post_data(proj, page=0, theme=theme_filter), "pagesize": "1"})
        else:
            html0 = _get(sess, f"{FIPI_BASE}/bank/questions.php?proj={proj}&page=0&pagesize=1")
        total_tasks = _count(html0)
    except Exception as e:
        import_session.status = "error"
        import_session.notes = str(e)
        import_session.save()
        connection.close()
        return

    total_pages = (total_tasks + FIPI_PAGESIZE - 1) // FIPI_PAGESIZE
    added = skipped = duplicate = 0
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
                    duplicate_pairs.append({
                        "guid": guid,
                        "html": text[:300],
                        "image_urls": td["image_urls"],
                        "theme": td["theme"],
                        "existing_id": existing.id,
                        "existing_preview": existing.text_preview,
                        "existing_source": existing.get_source_display(),
                    })
                continue

            # 3. Скачиваем картинку
            image_content = None
            image_name = None
            if td["image_urls"]:
                try:
                    time.sleep(FIPI_IMG_DELAY)
                    img_resp = sess.get(td["image_urls"][0], headers=FIPI_HEADERS, timeout=15, verify=False)
                    if img_resp.status_code == 200:
                        ct_header = img_resp.headers.get("Content-Type", "")
                        ext = ".png" if "png" in ct_header else ".jpg"
                        image_name = f"fipi_{uuid.uuid4().hex[:8]}{ext}"
                        image_content = img_resp.content
                except Exception:
                    pass

            ct = CatalogTask(
                task_number=None,
                exam_type=exam_type,
                text=text,
                correct_answer="",
                source=TaskSource.FIPI,
                manual_grading=True,
                fipi_guid=guid or None,
                text_hash=text_hash,
                import_session=import_session,
            )
            if image_content and image_name:
                ct.image.save(image_name, ContentFile(image_content), save=False)
            ct.save()
            added += 1

        # Обновляем прогресс после каждой страницы
        import_session.tasks_added = added
        import_session.tasks_skipped = skipped
        import_session.tasks_duplicate = duplicate
        import_session.save(update_fields=["tasks_added", "tasks_skipped", "tasks_duplicate"])

    import_session.status = "done"
    import_session.notes = json.dumps(duplicate_pairs, ensure_ascii=False, default=str)
    import_session.save(update_fields=["status", "notes",
                                        "tasks_added", "tasks_skipped", "tasks_duplicate"])
    connection.close()
