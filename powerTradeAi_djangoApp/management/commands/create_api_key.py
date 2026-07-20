"""Genera una API key. El valor en claro se imprime una sola vez.

    python manage.py create_api_key "dashboard produccion"
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from ...models import ApiKey


class Command(BaseCommand):
    help = "Crea una API key y muestra su valor (irrecuperable despues)."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Para que es la clave.")

    def handle(self, *args, **options):
        _, raw = ApiKey.generate(options["name"])
        self.stdout.write(self.style.SUCCESS(f"\n  {raw}\n"))
        self.stdout.write(
            "Copiala ahora: solo se guarda su hash, no se puede recuperar.")
        self.stdout.write(f'Uso: curl -H "Authorization: Api-Key {raw}" ...')
