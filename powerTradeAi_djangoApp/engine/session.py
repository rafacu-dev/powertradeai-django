"""Horario de mercado.

Calendario minimo: fines de semana y festivos NYSE. Si el proveedor de datos
expone su propio calendario, es preferible; esto es el suelo para no escanear
un sabado ni un 4 de julio.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# Festivos NYSE con mercado cerrado. Ampliar cada ano.
HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
    date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25),
    date(2027, 12, 24),
}

# Medias sesiones: cierre a las 13:00 ET.
HALF_DAYS = {
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}


def now_ny() -> datetime:
    return datetime.now(NY)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def session_close(day: date) -> time:
    return time(13, 0) if day in HALF_DAYS else RTH_CLOSE


def is_market_open(moment: datetime | None = None) -> bool:
    moment = (moment or now_ny()).astimezone(NY)
    if not is_trading_day(moment.date()):
        return False
    return RTH_OPEN <= moment.time() < session_close(moment.date())


def seconds_until_open(moment: datetime | None = None) -> float:
    """Segundos hasta la proxima apertura. 0 si ya esta abierto."""
    moment = (moment or now_ny()).astimezone(NY)
    if is_market_open(moment):
        return 0.0
    day = moment.date()
    for _ in range(10):  # cubre de sobra el puente mas largo
        open_at = datetime.combine(day, RTH_OPEN, tzinfo=NY)
        if is_trading_day(day) and open_at > moment:
            return (open_at - moment).total_seconds()
        day += timedelta(days=1)
    return 3600.0
