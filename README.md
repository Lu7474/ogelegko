# Пробные ОГЭ/ЕГЭ — платформа для учителей математики

Веб-приложение для проведения пробных экзаменов в школе. Учитель создаёт варианты (вручную, парсингом СдамГИА/ФИПИ или из PDF), управляет классами и смотрит аналитику. Ученики решают онлайн с таймером и автосохранением — результаты сохраняются автоматически.

**Сделан для реального использования в школе** — сейчас работает на [ogelegko.ru](https://ogelegko.ru).

---

## Стек

Python 3.12 · Django 5.2 · SQLite / PostgreSQL · Cloudinary · WhiteNoise · BeautifulSoup4 · PyMuPDF · openpyxl

---

## Ключевые фичи

**Учитель:**
- Создание вариантов из каталога, парсингом СдамГИА/ФИПИ по ссылке или загрузкой PDF
- Управление классами, массовый импорт учеников из Excel
- Аналитика по каждому ученику и классу с разбивкой по номерам заданий
- Ручная проверка части 2, массовая проверка нескольких попыток сразу
- Экспорт в `.xlsx`, печать варианта в Word, ZIP-архив для переноса между установками

**Ученик:**
- Решение с таймером, автосохранение при закрытии вкладки
- Результаты с разбором ошибок, «Повторить ошибки» — вариант только из неверных заданий
- Личная статистика и история попыток

---

## Что показывает уровень

- Фоновый парсинг с live-прогрессом (ФИПИ банк — сотни заданий без блокировки UI)
- Нормализация ответов: `0.5 = 0,5 = 1/2`, пробелы и регистр игнорируются
- Защита от дублей при импорте — двойная проверка по GUID и MD5-хэшу текста
- ZIP-архив вариантов: переносимый экспорт/импорт задания + изображения между установками
- Поддержка двух баз данных: SQLite локально, PostgreSQL в проде через `dj-database-url`

---

## Запуск

### Через Docker (рекомендуется)

```bash
git clone https://github.com/Lu7474/ogelegko.git
cd ogelegko
docker compose up --build
```

Создать суперпользователя:
```bash
docker compose exec web python manage.py createsuperuser
```

### Локально без Docker

```bash
git clone https://github.com/Lu7474/ogelegko.git
cd ogelegko
python -m venv env
env\Scripts\activate        # Windows
# source env/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

Скопировать конфиг окружения:
```bash
cp .env.example .env
```

В `.env` всё уже настроено для локального запуска (SQLite, `DEBUG=True`). `DJANGO_SECRET_KEY` при `DEBUG=True` генерируется автоматически — для прода задайте явно.

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Для PostgreSQL задайте `DATABASE_URL` в `.env`.

---

Ученики: [http://localhost:8000](http://localhost:8000) · Админ: [http://localhost:8000/admin/](http://localhost:8000/admin/)

---

## Скриншоты

_скриншоты будут добавлены_

---

## Демо

[ogelegko.ru](https://ogelegko.ru)
