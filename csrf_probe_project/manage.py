#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.management import execute_from_command_line

    args = sys.argv[1:] or ["csrf_matrix"]
    execute_from_command_line(["manage.py", *args])


if __name__ == "__main__":
    main()
