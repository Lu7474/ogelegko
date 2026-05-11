# EGE/OGE Platform — Claude Configuration

## Behavior
- If the task is small — implement it immediately, no clarifying questions.
- Keep responses short. Do not repeat what has already been done.
- Communication language: Russian. Code, comments, variable names: English.
- Never add "Co-Authored-By" lines to commit messages.

## Tech Stack
- Python 3.11+, Django 5.2
- DB: SQLite (local), PostgreSQL (prod, via dj-database-url)
- Media storage: Cloudinary (prod), local filesystem (dev)
- Static files: WhiteNoise
- Parsing: BeautifulSoup4, PyMuPDF, pdfplumber
- Linter: ruff (config in `ruff.toml`)

## Project Structure
- `manage.py` — entry point
- `config/` — Django config (settings.py, urls.py, wsgi, asgi)
- `exam/` — main app:
  - `models.py` — все модели
  - `views/student.py` — представления для учеников
  - `views/admin_base.py` — хелперы, логин/логаут, дашборд, экспорт
  - `views/admin_students.py` — классы, ученики, попытки
  - `views/admin_variants.py` — варианты, DOCX-печать, ZIP
  - `views/admin_catalog.py` — каталог, импорт (СдамГИА, ФИПИ, PDF)
  - `urls.py`, `utils.py`
  - `parsers/` — sdamgia.py, fipi.py, pdf.py
  - `services/variant_archive.py` — экспорт/импорт вариантов в ZIP
  - `services/docx_builder.py` — рендеринг вариантов и результатов в DOCX
  - `tests/` — тесты (test_auth, test_exam, test_admin, test_catalog, test_security, test_utils, test_variant_archive, test_student_views, test_admin_classes, test_admin_students, test_admin_variants, test_admin_api)
- `templates/` — HTML templates
- `static/` — static files
- `media/` — user uploads (local dev only)
- `env/` — virtual environment (do not touch)

## Commit workflow
ruff-format pre-commit hook reformats files but does not stage them, causing the first commit to fail.
Always do two `git add` calls before committing: once to stage, then after ruff runs — `git add` again, then commit.

## Commands
- Run: `env/Scripts/python.exe manage.py runserver`
- Migrations: `env/Scripts/python.exe manage.py makemigrations && env/Scripts/python.exe manage.py migrate`
- Tests: `env/Scripts/python.exe manage.py test exam`
- Shell: `env/Scripts/python.exe manage.py shell`
- Lint: `env/Scripts/ruff.exe check . --fix`
- Dependencies: `env/Scripts/pip.exe install -r requirements.txt`

## Constraints
- NEVER read `.env`, `db.sqlite3`, `*.log`
- Always use the virtual environment via `./env/Scripts/`
- Do not create documentation or README files unless explicitly asked
