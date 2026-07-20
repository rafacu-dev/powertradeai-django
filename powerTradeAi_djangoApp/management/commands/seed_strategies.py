"""Siembra en base de datos las reglas registradas en el codigo.

Idempotente: actualiza nombre y ``rule_version``, y respeta lo que el operador
haya tocado (``enabled``, ``contracts``, ``params``).

    python manage.py seed_strategies
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from ...models import Strategy
from ...strategies import all_strategies


class Command(BaseCommand):
    help = "Crea o actualiza las Strategy a partir del catalogo del codigo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--disable-new", action="store_true",
            help="Crea las reglas nuevas desactivadas, para revisarlas antes.")

    def handle(self, *args, **options):
        created = updated = 0
        for strategy_id, cls in sorted(all_strategies().items()):
            row, was_created = Strategy.objects.get_or_create(
                strategy_id=strategy_id,
                defaults={
                    "name": cls.name,
                    "symbol": cls.symbol,
                    "rule_version": cls.rule_version,
                    "params": dict(cls.default_params),
                    "enabled": not options["disable_new"],
                },
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  + {strategy_id}"))
                continue

            # Nombre y version vienen del codigo; el resto es del operador.
            changes = []
            if row.name != cls.name:
                row.name = cls.name
                changes.append("name")
            if row.rule_version != cls.rule_version:
                self.stdout.write(self.style.WARNING(
                    f"  ! {strategy_id}: {row.rule_version} -> {cls.rule_version}"))
                row.rule_version = cls.rule_version
                changes.append("rule_version")
            if changes:
                row.save(update_fields=[*changes, "updated_at"])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Listo: {created} creadas, {updated} actualizadas, "
            f"{len(all_strategies())} en el catalogo."))
