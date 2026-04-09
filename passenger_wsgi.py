import sys
import os

# Путь к virtualenv на сервере
VENV = os.path.join(os.path.dirname(__file__), 'venv')
VENV_SITE = os.path.join(VENV, 'lib', 'python3.10', 'site-packages')

if VENV_SITE not in sys.path:
    sys.path.insert(0, VENV_SITE)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
os.environ.setdefault('DJANGO_DEBUG', 'False')
os.environ.setdefault('ALLOWED_HOSTS', 'maxac1yx.beget.tech,egeoge.beget.tech')

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
