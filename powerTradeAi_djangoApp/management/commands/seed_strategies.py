"""Siembra en base de datos las reglas registradas en el codigo.

Idempotente: actualiza nombre y ``rule_version``, y respeta lo que el operador
haya tocado (``enabled``, ``contracts``, ``params``).

    python manage.py seed_strategies
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from ...models import Strategy
from ...strategies import all_strategies


class Command(BaseCommand):
    help = "Crea o actualiza las Strategy a partir del catalogo del codigo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--disable-new", action="store_true",
            help="Crea las reglas nuevas desactivadas, para revisarlas antes.")
        parser.add_argument(
            "--preserve-enabled", action="store_true",
            help="No aplica la contencion Investep al campo enabled existente.")

    def handle(self, *args, **options):
        created = updated = 0
        configured = getattr(settings, "POWERTRADEAI", {}).get(
            "INVESTEP_WATCHLIST", ("TSLA", "SPY", "QQQ"))
        if isinstance(configured, str):
            configured = configured.split(",")
        watchlist = {
            str(symbol).strip().upper() for symbol in configured
            if str(symbol).strip()
        }

        def allowed(cls) -> bool:
            academic = any(
                marker in cls.strategy_id for marker in ("_E01_", "_E02_"))
            return academic and cls.symbol.upper() in watchlist

        allowed_ids = {
            strategy_id
            for strategy_id, cls in all_strategies().items()
            if allowed(cls) and not options["disable_new"]
        }
        if not options["preserve_enabled"]:
            # Incluye filas antiguas que ya no existen en el registro de codigo:
            # una regla huérfana no puede quedar activa por omision del loop.
            updated += Strategy.objects.filter(enabled=True).exclude(
                strategy_id__in=allowed_ids).update(enabled=False)

        for strategy_id, cls in sorted(all_strategies().items()):
            should_enable = allowed(cls) and not options["disable_new"]
            row, was_created = Strategy.objects.get_or_create(
                strategy_id=strategy_id,
                defaults={
                    "name": cls.name,
                    "symbol": cls.symbol,
                    "rule_version": cls.rule_version,
                    "params": dict(cls.default_params),
                    "enabled": should_enable,
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
            if (not options["preserve_enabled"]
                    and row.enabled != should_enable):
                row.enabled = should_enable
                changes.append("enabled")
            if changes:
                row.save(update_fields=[*changes, "updated_at"])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Listo: {created} creadas, {updated} actualizadas, "
            f"{len(all_strategies())} en el catalogo; activos solo "
            f"Investep para {', '.join(sorted(watchlist)) or '(ninguno)'}."))
