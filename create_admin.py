import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.contrib.auth.models import User
if not User.objects.filter(username='chris').exists():
    User.objects.create_superuser('chris', 'chris@chammondlaw.com', 'Hammond2026!')
    print('Created superuser: chris')
else:
    print('User chris already exists')
