# Гайд по управлению сервером ogelegko.ru

## Подключение по SSH

```bash
ssh root@85.239.51.213
# Пароль: qLtpM*.UzsU1nE
```

---

## База данных PostgreSQL

### Скачать дамп базы данных

**Шаг 1.** Подключись к серверу и создай дамп:
```bash
ssh root@85.239.51.213
pg_dump -U egeuser -d egedb > /tmp/egedb_backup.sql
```

**Шаг 2.** Скачай файл на компьютер (в cmd/PowerShell на своём компе):
```bash
scp root@85.239.51.213:/tmp/egedb_backup.sql C:\Users\Махач\Desktop\egedb_backup.sql
```

### Загрузить дамп на сервер

```bash
scp C:\Users\Махач\Desktop\egedb_backup.sql root@85.239.51.213:/tmp/egedb_backup.sql
ssh root@85.239.51.213
psql -U egeuser -d egedb < /tmp/egedb_backup.sql
```

### Подключиться к базе данных прямо на сервере

```bash
psql -U egeuser -d egedb
```

Полезные команды внутри psql:
```sql
\dt          -- список таблиц
\q           -- выйти
SELECT COUNT(*) FROM exam_student;   -- количество учеников
SELECT COUNT(*) FROM exam_attempt;   -- количество попыток
```

---

## Управление сайтом

### Перезапустить сайт
```bash
systemctl restart ege
```

### Статус сайта
```bash
systemctl status ege
```

### Логи сайта (последние 50 строк)
```bash
journalctl -u ege -n 50
# или
tail -50 /var/www/ege/logs/app.log
```

### Логи Nginx
```bash
tail -50 /var/log/nginx/error.log
tail -50 /var/log/nginx/access.log
```

---

## Переменные окружения (.env)

Файл находится на сервере: `/var/www/ege/.env`

```bash
# Посмотреть текущий .env
cat /var/www/ege/.env

# Редактировать
nano /var/www/ege/.env
```

Содержимое файла:
```
DJANGO_SECRET_KEY=<секретный ключ>
DJANGO_DEBUG=False
DATABASE_URL=postgres://egeuser:EgePass2024!@localhost:5432/egedb
ALLOWED_HOSTS=85.239.51.213,localhost,127.0.0.1,ogelegko.ru,www.ogelegko.ru
HTTPS_ENABLED=True
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
```

После изменения .env — перезапустить сайт:
```bash
systemctl restart ege
```

---

## SSL-сертификат (Let's Encrypt)

### Обновить сертификат вручную
```bash
certbot renew
systemctl reload nginx
```

Сертификат обновляется автоматически (certbot создаёт cron/systemd timer).

### Проверить срок действия
```bash
certbot certificates
```

---

## Автодеплой (GitHub Actions)

При каждом `git push` в ветку `main`:
1. Файлы копируются на сервер через SCP
2. Устанавливаются зависимости (`pip install`)
3. Применяются миграции (`manage.py migrate`)
4. Собирается статика (`manage.py collectstatic`)
5. Сайт перезапускается (`systemctl restart ege`)

GitHub Secrets (настроены в репозитории):
- `SERVER_HOST` = `85.239.51.213`
- `SERVER_USER` = `root`
- `SERVER_PASS` = `qLtpM*.UzsU1nE`

---

## Проверка доступности сайта

```bash
curl -s -o /dev/null -w "%{http_code}" https://ogelegko.ru/
# Должно вернуть 200 или 302
```

---

## Импорт заданий из PDF («Распечатай и реши»)

PDF-файлы с заданиями хранятся на сервере в папке `/var/www/ege/Распечатай и реши/`.

Команда запускается вручную по SSH:

```bash
cd /var/www/ege
source venv/bin/activate
```

### Режимы работы

| Режим | Что делает |
|-------|-----------|
| `--mode catalog` | Добавляет задания в **каталог** (по умолчанию) |
| `--mode variants` | Создаёт **Варианты** — по одному на каждый блок PDF |
| `--mode both` | И в каталог, и создаёт варианты одновременно |

### Примеры команд

```bash
# Добавить задания в каталог (каждый блок → отдельные задания каталога)
python manage.py import_pdfs "Распечатай и реши" --exam-type oge --mode catalog

# Создать варианты (каждый блок PDF «Задание 1.» → отдельный Вариант)
python manage.py import_pdfs "Распечатай и реши" --exam-type oge --mode variants

# И в каталог, и варианты сразу
python manage.py import_pdfs "Распечатай и реши" --exam-type oge --mode both

# Предпросмотр без сохранения (dry-run) — покажет что будет создано
python manage.py import_pdfs "Распечатай и реши" --exam-type oge --mode variants --dry-run
```

### Как работает

- В PDF-файлах с «Распечатай и реши» блоки называются **«Задание 1.», «Задание 2.»** — это не номера ОГЭ-заданий, а **номера вариантов** внутри файла.
- Номера реальных заданий ОГЭ берутся из **имени файла**: например, `№01-05` → задания 1, 2, 3, 4, 5.
- Заголовки блоков, имена авторов, номера страниц автоматически удаляются из текста.
- Общее условие (текст до первого «1.») прикрепляется ко всем 5 заданиям блока.
- Варианты создаются с `is_active=False` — после проверки активируй их вручную в `/admin/variants/`.
- Правильные ответы не заполняются автоматически — их нужно внести вручную через `/admin/catalog/` → «Без правильного ответа».

### После импорта

1. Зайди в `/admin/catalog/` → вкладка **«Без правильного ответа»** — заполни ответы.
2. Зайди в `/admin/variants/` — активируй нужные варианты.

---

## Полезные пути на сервере

| Что | Путь |
|-----|------|
| Код проекта | `/var/www/ege/` |
| Виртуальное окружение | `/var/www/ege/venv/` |
| Переменные окружения | `/var/www/ege/.env` |
| Логи приложения | `/var/www/ege/logs/app.log` |
| Медиафайлы | `/var/www/ege/media/` |
| Конфиг Nginx | `/etc/nginx/sites-available/ege` |
| SSL-сертификат | `/etc/letsencrypt/live/ogelegko.ru/` |
