"""Entrenamiento del agente: opera un dia PASADO en tiempo simulado.

Recorre la sesion de un dia en pasos finos (por defecto 5 min). En cada paso
despierta al agente con un reloj ``as_of``: las skills solo ven datos hasta ese
instante, nunca el futuro (sin look-ahead). El agente analiza, abre, gestiona y
cierra posiciones; cada operacion se marca como ``agent_train`` y se puntua
causalmente contra el precio real.

    python manage.py train_agent --symbol TSLA --date 2026-07-21 --step 5

Cuanto mas fino el paso, mas realista y mas llamadas al LLM (mas costo).
"""
from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Entrena al agente operando un dia pasado en tiempo simulado."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", type=str, default="TSLA")
        parser.add_argument("--date", type=str, required=True,
                            help="Dia a operar, YYYY-MM-DD.")
        parser.add_argument("--step", type=int, default=5,
                            help="Minutos de tiempo simulado entre pasos (def 5).")
        parser.add_argument("--start", type=str, default="09:35",
                            help="Hora de inicio ET (def 09:35).")
        parser.add_argument("--end", type=str, default="15:55",
                            help="Hora de fin ET (def 15:55).")

    def handle(self, *args, **options):
        from ...agent.training import train_day

        try:
            day = datetime.strptime(options["date"], "%Y-%m-%d").date()
        except ValueError:
            raise CommandError("Fecha invalida (YYYY-MM-DD).")
        try:
            summary = train_day(
                options["symbol"], day, step=options["step"],
                start=options["start"], end=options["end"],
                log=lambda m: self.stdout.write(m))
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(
            f"\nEntrenamiento terminado: {summary}"))
