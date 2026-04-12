import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create admin user from env vars (idempotent)"

    def handle(self, *args, **kwargs):
        username = os.environ.get("ADMIN_USERNAME", "admin")
        password = os.environ.get("ADMIN_PASSWORD", "admin123")
        if not User.objects.filter(username=username).exists():
            User.objects.create_superuser(username=username, password=password)
            self.stdout.write(f"Admin '{username}' created.")
        else:
            self.stdout.write(f"Admin '{username}' already exists.")
