"""Siembra en base de datos las reglas registradas en el codigo.

Idempotente: actualiza nombre y ``rule_version``, y respeta ``contracts`` y
``params`` del operador.

    python manage.py seed_strategies

QUE REGLAS QUEDAN ACTIVAS
-------------------------
Solo las que aparezcan en ``APTAS_PARA_PAPER``. Esa lista **empieza vacia** por
decision del operador (07-ago-2026): el catalogo se conserva entero como
registro de investigacion, pero ninguna regla opera hasta que se la anade a
mano, una por una, cuando su evidencia lo justifique.

El criterio anterior era estructural — cualquier regla cuyo id llevara ``_E01_``
o ``_E02_`` se activaba sola. Eso hacia que anadir una clase al catalogo la
pusiera a operar sin que nadie lo decidiera. Una lista explicita invierte esa
carga: aparecer en el catalogo no da permiso; darlo es un cambio de codigo
visible en el historial.

COMO ANADIR UNA REGLA
---------------------
1. La regla tiene evidencia causal propia y esta lista para paper money.
2. Se anade su ``strategy_id`` exacto a ``APTAS_PARA_PAPER``, con la fecha y el
   motivo en el comentario de al lado.
3. Se despliega y se ejecuta ``seed_strategies``.

No se activa nada desde el admin como via normal. El admin sigue permitiendolo
para una prueba puntual, pero el siguiente ``seed_strategies`` lo revierte: la
fuente de verdad es esta lista, no la base de datos.

BORRAR NO ES UNA OPCION
-----------------------
``Alert.strategy`` es ``on_delete=PROTECT``: una estrategia con alertas no se
puede borrar, y es deliberado — destruiria el historico que da sentido a los
resultados. "Quitar una regla" significa que deje de operar, no que desaparezca.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from ...models import Strategy
from ...strategies import all_strategies

# Reglas autorizadas a operar en paper money.
#
# Formato: ("SPY_ORB15_0950_RANGE_INVALID", "07-ago-2026: motivo"),
APTAS_PARA_PAPER: tuple[tuple[str, str], ...] = (
    (
        "SPY_ORB15_BASE_CALL_CLOSE80_TP125_STOP15",
        "26-ago-2026: mejor resultado del grid ORB; validar fills en paper",
    ),
    (
        "SPY_ORB15_0950_CALL_CLOSE80_TP125_STOP15",
        "26-ago-2026: mejor ORB15 09:50 CALL; mayor edge anual/promedio por trade",
    ),
    (
        "SPY_ORB15_0950_PUT_BODY70_TP100_STOP15",
        "26-ago-2026: candidato secundario PUT por cuerpo de ruptura",
    ),
    (
        "SPY_ORB5_VALIDATE_2ND_ENTER_3RD_VOL15_STOP15",
        "26-ago-2026: ORB-5 volumen 1.5x en shadow/paper para validar muestra",
    ),
)

_APTAS_IDS = frozenset(strategy_id for strategy_id, _ in APTAS_PARA_PAPER)


class Command(BaseCommand):
    help = ("Crea o actualiza las Strategy del catalogo. Solo deja activas las "
            "de APTAS_PARA_PAPER (hoy: ninguna).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--disable-new", action="store_true",
            help="Crea las reglas nuevas desactivadas, para revisarlas antes.")
        parser.add_argument(
            "--preserve-enabled", action="store_true",
            help="No toca el campo enabled existente. Para una prueba manual "
                 "que no quieres que el seed revierta.")

    def handle(self, *args, **options):
        created = updated = 0
        desconocidas = _APTAS_IDS - set(all_strategies())
        if desconocidas:
            # Un id mal escrito en la lista dejaria la regla apagada en
            # silencio, que es justo el fallo que se quiere evitar.
            raise SystemExit(
                "APTAS_PARA_PAPER nombra reglas que no existen en el catalogo: "
                + ", ".join(sorted(desconocidas)))

        allowed_ids = set() if options["disable_new"] else set(_APTAS_IDS)
        if not options["preserve_enabled"]:
            # Incluye filas antiguas que ya no existen en el registro de codigo:
            # una regla huérfana no puede quedar activa por omision del loop.
            updated += Strategy.objects.filter(enabled=True).exclude(
                strategy_id__in=allowed_ids).update(enabled=False)

        for strategy_id, cls in sorted(all_strategies().items()):
            should_enable = strategy_id in allowed_ids
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

        activas = Strategy.objects.filter(enabled=True).count()
        self.stdout.write(self.style.SUCCESS(
            f"Listo: {created} creadas, {updated} actualizadas, "
            f"{len(all_strategies())} en el catalogo."))
        if activas:
            self.stdout.write(self.style.SUCCESS(
                "Activas: " + ", ".join(
                    Strategy.objects.filter(enabled=True)
                    .values_list("strategy_id", flat=True).order_by("strategy_id"))))
        else:
            self.stdout.write(self.style.WARNING(
                "Activas: NINGUNA. El scanner corre y registra ScanRun, pero no "
                "evalua ninguna regla. Es el estado esperado hasta que se anada "
                "una a APTAS_PARA_PAPER."))
