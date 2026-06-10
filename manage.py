#!/usr/bin/env python
"""Django management entrypoint.

Usage:
    python manage.py runserver        # dashboard + admin (the web process)
    python manage.py run_worker       # browser worker: sequences, replies, lead search
    python manage.py migrate          # run Django migrations
    python manage.py createsuperuser
"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")


if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
