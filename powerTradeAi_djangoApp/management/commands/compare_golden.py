"""Compara la deteccion de una regla contra un artefacto de backtest.

    python manage.py compare_golden \\
        --strategy SPY_ORB15_BASE \\
        --csv research/runs/2026-07-15_spy_orb15_causal_120sessions_trades.csv

Reconstruye cada sesion desde velas crudas y compara rango, direccion, vela de
disparo y subyacente de entrada. NO compara P&L: un P&L reconstruido es un
limite superior optimista y compararlo solo produce ruido.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ...data import get_provider
from ...engine.golden import compare_artifact


class Command(BaseCommand):
    help = "Verifica que la app detecte las mismas senales que el backtest."

    def add_arguments(self, parser):
        parser.add_argument("--strategy", required=True)
        parser.add_argument("--csv", required=True, dest="artifact")
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Solo las primeras N sesiones (para una prueba rapida).")
        parser.add_argument(
            "--quiet", action="store_true",
            help="Sin barra de progreso, solo el informe final.")

    def handle(self, *args, **options):
        artifact = Path(options["artifact"])
        if not artifact.is_absolute():
            # Comodo desde dev_project/, donde el repo es el directorio padre.
            for base in (Path.cwd(), Path.cwd().parent):
                if (base / artifact).exists():
                    artifact = base / artifact
                    break
        if not artifact.exists():
            raise CommandError(f"No encuentro el artefacto: {artifact}")

        self.stdout.write(f"\nRegla     {options['strategy']}")
        self.stdout.write(f"Artefacto {artifact.name}\n")

        def progress(index, total, diff):
            if options["quiet"]:
                return
            mark = "." if diff.ok else "x"
            self.stdout.write(mark, ending="")
            self.stdout.flush()
            if index % 50 == 0 or index == total:
                self.stdout.write(f"  {index}/{total}")

        try:
            report = compare_artifact(
                options["strategy"], artifact, get_provider(),
                limit=options["limit"], on_progress=progress)
        except KeyError as exc:
            raise CommandError(str(exc))

        self._report(report)

    def _report(self, report) -> None:
        total = len(report.diffs)
        matched = len(report.matched)
        mismatched = report.mismatched

        self.stdout.write("")
        if mismatched:
            self.stdout.write(self.style.ERROR("Divergencias:"))
            for diff in mismatched[:25]:
                if diff.note:
                    self.stdout.write(f"  {diff.day}  {diff.note}")
                    continue
                detail = "  ".join(
                    f"{name}: backtest={want} app={got}"
                    for name, (want, got) in sorted(diff.fields.items()))
                self.stdout.write(f"  {diff.day}  {detail}")
            if len(mismatched) > 25:
                self.stdout.write(f"  ... y {len(mismatched) - 25} mas")

            self.stdout.write("\nCampos que divergen:")
            for name, count in report.field_counts.items():
                self.stdout.write(f"  {name:<20} {count} sesiones")

        self.stdout.write("")
        if not total:
            self.stdout.write(self.style.WARNING(
                "El artefacto no tiene sesiones utilizables."))
        elif matched == total:
            self.stdout.write(self.style.SUCCESS(
                f"{matched}/{total} sesiones coinciden. La deteccion de "
                f"{report.strategy_id} reproduce el backtest."))
        else:
            self.stdout.write(self.style.ERROR(
                f"{matched}/{total} sesiones coinciden "
                f"({len(mismatched)} divergen)."))
            self.stdout.write(
                "\nUna divergencia significa una de dos cosas: la regla esta mal\n"
                "portada, o el feed de hoy no reproduce el que genero el\n"
                "artefacto. Mira primero si el patron es sistematico o disperso.")
        self.stdout.write("")
