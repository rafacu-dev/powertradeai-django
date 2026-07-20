"""Diagnostico del proveedor de datos. NO escribe nada en base de datos.

Comprueba una por una las cinco llamadas que la app necesita y valida que lo
devuelto tenga la forma esperada. Existe porque los nombres de columna del
proveedor no se pudieron verificar sin una cuenta activa: este comando es el
sitio donde esa incertidumbre se resuelve, con un mensaje claro en vez de un
KeyError en mitad de una sesion de mercado.

    python manage.py check_provider
    python manage.py check_provider --symbol TSLA --date 2026-07-15
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand

from ...data import get_provider, occ_symbol
from ...engine.session import NY, is_trading_day

REQUIRED_BAR_COLUMNS = ("open", "high", "low", "close")


class Command(BaseCommand):
    help = "Verifica conexion y formato de datos del proveedor configurado."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="SPY")
        parser.add_argument(
            "--date", default=None,
            help="Sesion a consultar (YYYY-MM-DD). Por defecto, la ultima habil.")
        parser.add_argument("--provider", default=None,
                            help="Fuerza un proveedor: thetadata | alpaca.")

    def handle(self, *args, **options):
        symbol = options["symbol"].upper()
        session = self._session_date(options["date"])

        self.stdout.write(f"\nSimbolo   {symbol}")
        self.stdout.write(f"Sesion    {session}")

        try:
            provider = get_provider(options["provider"])
        except Exception as exc:
            self._fail("No se pudo construir el proveedor", exc)
            return
        self.stdout.write(f"Proveedor {provider.name}\n")

        checks = [
            ("velas 1m de la sesion", self._check_bars_1m),
            ("historial multi-dia", self._check_history),
            ("precio actual", self._check_latest_price),
            ("quote de opcion", self._check_option_quote),
            ("tape de operaciones", self._check_trades),
        ]

        results = []
        for label, check in checks:
            self.stdout.write(f"  {label:.<34} ", ending="")
            try:
                detail = check(provider, symbol, session)
            except Exception as exc:
                self.stdout.write(self.style.ERROR("FALLO"))
                results.append((label, False, f"{type(exc).__name__}: {exc}", exc))
                continue
            if detail is None:
                self.stdout.write(self.style.WARNING("SIN DATOS"))
                results.append((label, None, "el proveedor no devolvio nada", None))
            else:
                self.stdout.write(self.style.SUCCESS("OK"))
                results.append((label, True, detail, None))

        self._report(results)

    # --- Comprobaciones -------------------------------------------------

    def _check_bars_1m(self, provider, symbol, session):
        bars = provider.bars_1m(symbol, session)
        if bars.empty:
            return None
        self._validate_bars(bars, "bars_1m")
        first = bars.index[0].tz_convert(NY)
        return (f"{len(bars)} velas, de {first:%H:%M} ET, "
                f"cierre {float(bars['close'].iloc[-1]):.2f}")

    def _check_history(self, provider, symbol, session):
        end = session - timedelta(days=1)
        bars = provider.bars(symbol, end - timedelta(days=10), end, "1h")
        if bars.empty:
            return None
        self._validate_bars(bars, "bars")
        days = len({ts.tz_convert(NY).date() for ts in bars.index})
        return f"{len(bars)} velas 1h en {days} sesiones"

    def _check_latest_price(self, provider, symbol, session):
        price = provider.latest_price(symbol)
        if not price or price <= 0:
            return None
        return f"{price:.2f}"

    def _check_option_quote(self, provider, symbol, session):
        """Busca un contrato vivo cerca del dinero, como hacen las reglas."""
        spot = provider.latest_price(symbol)
        base = int(spot)
        for days_ahead in range(0, 8):
            expiration = session + timedelta(days=days_ahead)
            if expiration.weekday() >= 5:
                continue
            for offset in range(6):
                occ = occ_symbol(symbol, expiration, "CALL", float(base - offset))
                quote = provider.option_quote(occ)
                if quote is not None and quote.is_live:
                    return (f"{occ.strip()} bid {quote.bid:.2f} "
                            f"ask {quote.ask:.2f}")
        return None

    def _check_trades(self, provider, symbol, session):
        start = datetime.combine(session, time(10, 0), tzinfo=NY)
        tape = provider.trades(symbol, start, start + timedelta(minutes=1))
        if tape.empty:
            return None
        missing = {"price", "size"}.difference(tape.columns)
        if missing:
            raise ValueError(f"trades() sin columnas {sorted(missing)}")
        return f"{len(tape)} operaciones en 1 minuto"

    # --- Validacion comun -----------------------------------------------

    def _validate_bars(self, bars, source: str) -> None:
        missing = set(REQUIRED_BAR_COLUMNS).difference(bars.columns)
        if missing:
            raise ValueError(
                f"{source}() devolvio columnas {list(bars.columns)}; "
                f"faltan {sorted(missing)}")
        if bars.index.tz is None:
            raise ValueError(f"{source}() devolvio un indice sin zona horaria")
        if not bars.index.is_monotonic_increasing:
            raise ValueError(f"{source}() devolvio un indice sin ordenar")
        if bars.index.has_duplicates:
            raise ValueError(f"{source}() devolvio timestamps duplicados")

    # --- Informe --------------------------------------------------------

    def _session_date(self, raw: str | None) -> date:
        if raw:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        day = datetime.now(NY).date()
        for _ in range(10):
            if is_trading_day(day):
                return day
            day -= timedelta(days=1)
        return day

    def _fail(self, message: str, exc: Exception) -> None:
        self.stdout.write(self.style.ERROR(f"\n{message}: {exc}\n"))

    def _report(self, results) -> None:
        ok = sum(1 for _, status, _, _ in results if status is True)
        failed = [r for r in results if r[1] is False]
        empty = [r for r in results if r[1] is None]

        self.stdout.write("")
        for label, status, detail, _ in results:
            if status is True:
                self.stdout.write(f"  {label}: {detail}")

        if empty:
            self.stdout.write(self.style.WARNING("\nSin datos:"))
            for label, _, detail, _ in empty:
                self.stdout.write(f"  {label}: {detail}")
            self.stdout.write(
                "  (normal fuera de horario o en una sesion sin actividad;\n"
                "   vuelve a probar con --date de una sesion habil reciente)")

        if failed:
            self.stdout.write(self.style.ERROR("\nFallos:"))
            for label, _, detail, exc in failed:
                self.stdout.write(f"\n  {label}\n    {detail}")
                # La causa encadenada suele decir mas que el mensaje envuelto.
                cause = exc.__cause__ if exc is not None else None
                if cause is not None and str(cause) not in detail:
                    self.stdout.write(f"    causa: {type(cause).__name__}: {cause}")
            self.stdout.write(self.style.ERROR(
                "\nSi el fallo menciona nombres de columna, ajusta los alias en\n"
                "powerTradeAi_djangoApp/data/thetadata_cloud.py "
                "(_BAR_ALIASES, _BID_ALIASES,\n_ASK_ALIASES, _TS_ALIASES) "
                "con los nombres reales que aparezcan arriba."))

        self.stdout.write("")
        if failed:
            self.stdout.write(self.style.ERROR(
                f"{ok}/{len(results)} comprobaciones OK — hay fallos que "
                "resolver antes de escanear."))
        elif empty:
            self.stdout.write(self.style.WARNING(
                f"{ok}/{len(results)} comprobaciones OK, "
                f"{len(empty)} sin datos."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"{ok}/{len(results)} comprobaciones OK. El proveedor responde "
                "en el formato esperado."))
        self.stdout.write("")
