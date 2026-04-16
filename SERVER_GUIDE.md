# Гайд по управлению сервером ogelegko.ru

> **Безопасность:** реальные IP, пароли и ключи хранятся в `.env` и GitHub Secrets.
> Перед публичным релизом репозитория — сменить пароли/ключи.

## Подключение по SSH

```bash
ssh root@<SERVER_IP>
# Пароль хранится в GitHub Secret SERVER_PASS
```

---

## База данных PostgreSQL

### Скачать дамп базы данных

**Шаг 1.** Подключись к серверу и создай дамп:
```bash
ssh root@<SERVER_IP>
pg_dump -U egeuser -d egedb > /tmp/egedb_backup.sql
```

**Шаг 2.** Скачай файл на компьютер (в cmd/PowerShell на своём компе):
```bash
scp root@<SERVER_IP>:/tmp/egedb_backup.sql C:\Users\<USERNAME>\Desktop\egedb_backup.sql
```

### Загрузить дамп на сервер

```bash
scp C:\Users\<USERNAME>\Desktop\egedb_backup.sql root@<SERVER_IP>:/tmp/egedb_backup.sql
ssh root@<SERVER_IP>
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

Пример структуры файла (реальные значения — в GitHub Secrets и .env на сервере):
```
DJANGO_SECRET_KEY=<SECRET_KEY>
DJANGO_DEBUG=False
DATABASE_URL=postgres://egeuser:<DB_PASSWORD>@localhost:5432/egedb
ALLOWED_HOSTS=<SERVER_IP>,localhost,127.0.0.1,ogelegko.ru,www.ogelegko.ru
HTTPS_ENABLED=True
ADMIN_USERNAME=<ADMIN_USERNAME>
ADMIN_PASSWORD=<ADMIN_PASSWORD>
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
- `SERVER_HOST` — IP-адрес сервера
- `SERVER_USER` — пользователь SSH (обычно `root`)
- `SERVER_PASS` — пароль SSH

---

## Проверка доступности сайта

```bash
curl -s -o /dev/null -w "%{http_code}" https://ogelegko.ru/
# Должно вернуть 200 или 302
```

---

## Импорт заданий из PDF («Распечатай и реши»)

Импорт выполняется через кнопку **⬇ PDF** в каталоге на сайте. Загрузи файлы, выбери тип экзамена и формат — парсинг запускается фоново.

### После импорта

1. Зайди в `/admin/catalog/` → вкладка **«Без правильного ответа»** — заполни ответы.
2. Зайди в `/admin/variants/` — активируй нужные варианты.

---

## Ручное добавление и редактирование заданий

Форма доступна по `/admin/catalog/` → «Добавить задание» или кнопка «Изм.» у существующего.

### HTML-форматирование текста задания

В форме есть **панель кнопок** (вставка кликом) и **живой предпросмотр** рядом с полем ввода.

| Что нужно | HTML |
|---|---|
| Степень / верхний индекс | `x<sup>2</sup>` |
| Нижний индекс | `a<sub>1</sub>` |
| Дробь | `<span class="math-frac"><span>числитель</span><span>знаменатель</span></span>` |
| Квадратный корень | `√(x+1)` |
| Жирный / курсив | `<b>текст</b>` / `<i>текст</i>` |
| Перенос строки | `<br>` |
| Абзац | `<p>текст</p>` |

**Примеры:**

```html
<!-- Дробь со степенью -->
<span class="math-frac"><span>x<sup>2</sup>+1</span><span>2x-3</span></span>

<!-- Корень из дроби -->
√(<span class="math-frac"><span>a</span><span>b</span></span>)

<!-- Уравнение -->
Решите уравнение x<sup>3</sup> + 3x<sup>2</sup> - 4x - 12 = 0.
```

### Поле «Правильный ответ»

- Одно значение: `42` или `-3.5`
- Несколько допустимых вариантов: `234|243|324` (любой засчитается)
- При ручной проверке (часть 2) — оставить пустым, включить галку «Ручная проверка»
- Пробелы, запятые/точки в десятичных и математический минус (−) нормализуются автоматически

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
