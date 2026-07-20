#!/usr/bin/env python3
"""Entrada de gestion del proyecto de desarrollo."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# La app vive en la raiz del repo, un nivel por encima de este proyecto.
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent))


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "No se encuentra Django. Instala las dependencias con:\n"
            "  pip install django djangorestframework thetadata pandas"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
