import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth.models import User


username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")

if not username or not password:
    print(
        "Skipping superuser creation. Set DJANGO_SUPERUSER_USERNAME and "
        "DJANGO_SUPERUSER_PASSWORD to enable."
    )
elif not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username, email, password)
    print(f"Created superuser: {username}")
else:
    print(f"User {username} already exists")
