import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

import sys as _sys

DEBUG = os.environ.get("DJANGO_DEBUG", "False").lower() in ("true", "1", "yes")
_TESTING = "test" in _sys.argv

_secret_key = os.environ.get("DJANGO_SECRET_KEY", "")
if not _secret_key or "insecure" in _secret_key:
    if DEBUG or _TESTING:
        import secrets as _secrets

        _secret_key = _secrets.token_hex(50)
    else:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured("Установите DJANGO_SECRET_KEY в переменные окружения")
SECRET_KEY = _secret_key

_allowed = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(",") if h.strip()] or (
    ["localhost", "127.0.0.1"] if DEBUG else []
)
if not ALLOWED_HOSTS and not DEBUG and not _TESTING:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("Установите ALLOWED_HOSTS для продакшена")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "exam",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

_DATABASE_URL = os.environ.get("DATABASE_URL")
if _DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(_DATABASE_URL, conn_max_age=600)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Cloudinary — постоянное хранилище медиафайлов (изображения заданий)
_CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if _CLOUDINARY_URL:
    INSTALLED_APPS += ["cloudinary", "cloudinary_storage"]
    STORAGES["default"] = {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"}
    CLOUDINARY_STORAGE = {"CLOUDINARY_URL": _CLOUDINARY_URL}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Настройки сессий
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 86400  # 24 часа

# Кеш (для rate-limiting по IP и состояния фоновых задач)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": BASE_DIR / "cache",
    }
}

# Логирование
_LOGS_DIR = BASE_DIR / "logs"
_LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(_LOGS_DIR / "app.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        # Боты стучатся с чужими доменами — Django правильно отклоняет (400),
        # но без этой настройки каждый такой запрос шлёт email об ошибке.
        "django.security.DisallowedHost": {
            "handlers": [],
            "propagate": False,
        },
    },
}

# --- Безопасность (продакшен) ---
_https = os.environ.get("HTTPS_ENABLED", "False").lower() in ("true", "1", "yes")
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = False
    SECURE_CONTENT_TYPE_NOSNIFF = True
    CSRF_COOKIE_HTTPONLY = True
    # Включать только когда есть HTTPS
    if _https:
        SECURE_HSTS_SECONDS = 31536000
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True
        SESSION_COOKIE_SECURE = True
        CSRF_COOKIE_SECURE = True

# Rate-limiting
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_COOLDOWN_SECONDS = int(os.environ.get("LOGIN_COOLDOWN_SECONDS", "300"))

# Уведомления об ошибках на email (встроено в Django, бесплатно)
# Настройка: задать EMAIL_HOST_USER, EMAIL_HOST_PASSWORD и ADMIN_EMAIL в .env
_admin_email = os.environ.get("ADMIN_EMAIL")
if _admin_email and not DEBUG:
    ADMINS = [("Admin", _admin_email)]
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    DEFAULT_FROM_EMAIL = EMAIL_HOST_USER
    SERVER_EMAIL = EMAIL_HOST_USER
