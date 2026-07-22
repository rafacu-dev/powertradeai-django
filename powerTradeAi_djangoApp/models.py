"""Modelos de PowerTradeAI.

Una alerta es el registro completo de una decision: la senal que la disparo, el
contrato que se compro y —cuando termina— el precio al que se vendio. Mientras
no ha terminado, los campos de salida son NULL y ``status`` es ``pending``: la
app nunca inventa un resultado que todavia no ocurrio.

Convencion de P&L (identica al replay causal que valido las reglas):
entrada al ASK, salida al BID, comision plana por round-trip, y el porcentaje
calculado sobre el coste bruto de entrada.
"""
from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal

from django.db import models
from django.utils import timezone

CONTRACT_MULTIPLIER = Decimal("100")


class Strategy(models.Model):
    """Una regla evaluable. ``strategy_id`` es la clave canonica compartida con
    el motor de research, para poder cruzar alertas contra sus backtests."""

    strategy_id = models.CharField(max_length=80, unique=True)
    name = models.CharField(max_length=200)
    symbol = models.CharField(max_length=16, db_index=True)
    rule_version = models.CharField(max_length=80)
    enabled = models.BooleanField(default=True)
    # Parametros de la regla que pueden variar sin tocar codigo (buffer,
    # hold_minutes, umbrales). Cada regla documenta las claves que consume.
    params = models.JSONField(default=dict, blank=True)
    contracts = models.PositiveIntegerField(
        default=1, help_text="Tamano por senal, en contratos.")
    commission = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("1.30"),
        help_text="Comision total del round-trip, por contrato.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "strategies"
        ordering = ["symbol", "strategy_id"]

    def __str__(self) -> str:
        return f"{self.strategy_id} ({self.symbol})"


class Alert(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CLOSED = "closed", "Closed"
        EXPIRED = "expired", "Expired"
        ERROR = "error", "Error"

    class Direction(models.TextChoices):
        CALL = "CALL", "Call"
        PUT = "PUT", "Put"

    class Source(models.TextChoices):
        LIVE = "live", "En vivo"
        REPLAY = "replay", "Reconstruida"
        AGENT = "agent", "Agente"
        AGENT_TRAIN = "agent_train", "Agente (entrenamiento)"

    strategy = models.ForeignKey(
        Strategy, on_delete=models.PROTECT, related_name="alerts")
    # Se copia de la estrategia al disparar: si manana cambias la regla, el
    # resultado historico sigue atribuido a la version que lo produjo.
    rule_version = models.CharField(max_length=80)

    symbol = models.CharField(max_length=16, db_index=True)
    session_date = models.DateField(db_index=True)
    direction = models.CharField(max_length=4, choices=Direction.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices,
        default=Status.PENDING, db_index=True)
    # Frontera dura entre lo que ocurrio y lo que se reconstruyo. Una alerta
    # ``replay`` se calculo despues de los hechos con datos historicos: no
    # cargaba riesgo, no sufrio latencia y su quote de entrada es la del
    # instante teorico, no la que se habria pagado. Mezclar las dos en un
    # agregado produce un P&L que no significa nada.
    source = models.CharField(
        max_length=16, choices=Source.choices,
        default=Source.LIVE, db_index=True)

    signal_ts = models.DateTimeField(help_text="Cierre de la vela que disparo.")
    detected_at = models.DateTimeField(
        default=timezone.now, help_text="Cuando lo vio el scanner.")
    underlying_at_signal = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True)

    # --- Contrato ---
    occ_symbol = models.CharField(max_length=32, blank=True)
    expiration = models.DateField(null=True, blank=True)
    strike = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    contracts = models.PositiveIntegerField(default=1)

    # --- Compra ---
    entry_ts = models.DateTimeField(null=True, blank=True)
    entry_bid = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True)
    entry_ask = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True)
    entry_premium = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
        help_text="Prima pagada. Por convencion, el ASK.")

    # --- Venta: NULL mientras la alerta sigue viva ---
    exit_ts = models.DateTimeField(null=True, blank=True)
    exit_premium = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
        help_text="Prima cobrada. Por convencion, el BID.")
    exit_reason = models.CharField(max_length=40, blank=True)
    scheduled_exit_ts = models.DateTimeField(
        null=True, blank=True,
        help_text="Cuando le toca cerrar por tiempo. Lo usa el resolver.")

    # --- Resultado, materializado al cerrar ---
    commission = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("1.30"))
    net_dollars = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    net_pct = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True)

    meta = models.JSONField(
        default=dict, blank=True,
        help_text="Contexto de la regla: rango, umbrales, features.")
    # Cuando la genero el agente (source=agent): apunta a la corrida que la
    # decidio, para poder abrir su razonamiento completo desde la alerta.
    agent_run = models.ForeignKey(
        "AgentRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="alerts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-signal_ts"]
        indexes = [
            models.Index(fields=["status", "session_date"]),
            models.Index(fields=["strategy", "session_date"]),
            models.Index(fields=["source", "session_date"]),
        ]
        constraints = [
            # Una regla dispara como mucho una vez por sesion y direccion.
            # Sin esto, un reinicio del worker duplica la alerta del dia.
            # ``source`` entra en la clave para que reconstruir una sesion que
            # ya se opero en vivo no choque contra la alerta real ni la pise.
            # Solo aplica a reglas (live/replay): el agente y su entrenamiento
            # pueden tomar varias operaciones por dia.
            models.UniqueConstraint(
                fields=["strategy", "session_date", "direction", "source"],
                name="uniq_alert_per_strategy_session_direction_source",
                condition=models.Q(source__in=["live", "replay"]),
            ),
        ]

    def __str__(self) -> str:
        # ``self.strategy_id`` es la FK cruda; evita un query si no esta puesta.
        rule = self.strategy.strategy_id if self.strategy_id else "?"
        return f"{rule} {self.direction} {self.session_date}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def gross_entry_cost(self) -> Decimal | None:
        """Coste bruto de la posicion, sin comision. Denominador del %."""
        if self.entry_premium is None:
            return None
        return self.entry_premium * CONTRACT_MULTIPLIER * self.contracts

    def compute_pnl(self) -> tuple[Decimal, Decimal] | None:
        """(neto en dolares, % sobre coste bruto), o None si falta la salida.

        Replica el replay causal: ``(exit - entry) * 100 * n - comision * n``.
        """
        if self.entry_premium is None or self.exit_premium is None:
            return None
        n = Decimal(self.contracts)
        gross = (self.exit_premium - self.entry_premium) * CONTRACT_MULTIPLIER * n
        net = gross - (self.commission * n)
        cost = self.gross_entry_cost
        if not cost:
            return None
        pct = net / cost * Decimal("100")
        return (
            net.quantize(Decimal("0.01")),
            pct.quantize(Decimal("0.01")),
        )

    def close(self, *, exit_premium, exit_ts, reason: str) -> "Alert":
        """Cierra la alerta y materializa el resultado en una sola escritura."""
        self.exit_premium = Decimal(str(exit_premium))
        self.exit_ts = exit_ts
        self.exit_reason = reason
        self.status = self.Status.CLOSED
        pnl = self.compute_pnl()
        if pnl is not None:
            self.net_dollars, self.net_pct = pnl
        self.save(update_fields=[
            "exit_premium", "exit_ts", "exit_reason", "status",
            "net_dollars", "net_pct", "updated_at",
        ])
        return self


class ApiKey(models.Model):
    """Clave de API. Se guarda solo el hash: el valor en claro se muestra una
    unica vez, al crearla. Si el usuario la pierde, se rota, no se recupera."""

    PREFIX_LEN = 8

    name = models.CharField(max_length=120)
    prefix = models.CharField(
        max_length=PREFIX_LEN, db_index=True,
        help_text="Primeros caracteres, para identificarla sin revelarla.")
    key_hash = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        state = "activa" if self.is_active else "revocada"
        return f"{self.name} ({self.prefix}..., {state})"

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def new_raw_key() -> str:
        return f"ptai_{secrets.token_urlsafe(32)}"

    @classmethod
    def generate(cls, name: str) -> tuple["ApiKey", str]:
        """Crea la clave y devuelve (registro, valor_en_claro)."""
        raw = cls.new_raw_key()
        obj = cls.objects.create(
            name=name,
            prefix=raw[: cls.PREFIX_LEN],
            key_hash=cls.hash_key(raw),
        )
        return obj, raw

    def revoke(self) -> None:
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revoked_at"])


class ScanRun(models.Model):
    """Una pasada del scanner. Sin esto no puedes distinguir 'no hubo senal'
    de 'el worker estaba caido'."""

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    strategies_evaluated = models.PositiveIntegerField(default=0)
    alerts_created = models.PositiveIntegerField(default=0)
    alerts_closed = models.PositiveIntegerField(default=0)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"scan {self.started_at:%Y-%m-%d %H:%M:%S} ok={self.ok}"


class AgentRun(models.Model):
    """Una corrida del agente: su consigna, todo su proceso de pensamiento
    (mensajes + llamadas a skills) y el resultado. Es la caja negra abierta:
    cada alerta que el agente lanza queda ligada a la corrida que la penso."""

    class Status(models.TextChoices):
        RUNNING = "running", "En curso"
        DONE = "done", "Terminada"
        ERROR = "error", "Error"

    class Trigger(models.TextChoices):
        SCAN_LOOP = "scan_loop", "Scan loop"
        MANUAL = "manual", "Manual"
        TRAINING = "training", "Entrenamiento"

    trigger = models.CharField(
        max_length=16, choices=Trigger.choices, default=Trigger.MANUAL)
    status = models.CharField(
        max_length=10, choices=Status.choices,
        default=Status.RUNNING, db_index=True)
    model_name = models.CharField(max_length=80, blank=True)
    symbols = models.JSONField(
        default=list, blank=True, help_text="Activos que miro esta corrida.")
    goal = models.TextField(blank=True, help_text="La consigna que recibio.")
    # Traza completa de razonamiento: lista de pasos
    # {role, content, tool, args, result}. Es el 'proceso de pensamiento'.
    transcript = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True, help_text="Conclusion en breve.")
    alerts_created = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"agent {self.started_at:%Y-%m-%d %H:%M:%S} [{self.status}]"


class AgentAnalysis(models.Model):
    """Analisis del agente sobre un activo. Append-only: cada corrida deja una
    entrada nueva, y la siguiente lee las ultimas para tener continuidad y no
    empezar de cero cada vez."""

    symbol = models.CharField(max_length=16, db_index=True)
    analysis = models.TextField(help_text="Lo que el agente concluye del activo.")
    stance = models.CharField(
        max_length=16, blank=True,
        help_text="Sesgo: alcista / bajista / neutral / observando.")
    agent_run = models.ForeignKey(
        AgentRun, on_delete=models.CASCADE, related_name="analyses")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["symbol", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.symbol} {self.created_at:%Y-%m-%d %H:%M} ({self.stance})"


class AgentNote(models.Model):
    """Nota libre del agente: ideas, patrones observados, reglas que quiere
    recordar. A diferencia de AgentAnalysis (atada a un activo y a un sesgo),
    esto es su cuaderno de day-trader, indexado por tema."""

    topic = models.CharField(max_length=80, db_index=True)
    note = models.TextField()
    agent_run = models.ForeignKey(
        AgentRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="notes")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["topic", "-created_at"])]

    def __str__(self) -> str:
        return f"[{self.topic}] {self.note[:40]}"


class AgentTrigger(models.Model):
    """Nivel de precio que el agente pide vigilar. Cuando el precio lo toca, el
    loop despierta al agente para que decida. Es el agente marcando sus propios
    puntos de interes, en vez de reaccionar solo a un umbral fijo de movimiento."""

    class Direction(models.TextChoices):
        ABOVE = "above", "Al subir a/por encima"
        BELOW = "below", "Al bajar a/por debajo"

    symbol = models.CharField(max_length=16, db_index=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    reason = models.TextField(
        blank=True, help_text="Que espera el agente en ese nivel / que hara.")
    ref_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Precio cuando lo fijo, para saber de que lado venia.")
    active = models.BooleanField(default=True, db_index=True)
    agent_run = models.ForeignKey(
        AgentRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="triggers")
    created_at = models.DateTimeField(auto_now_add=True)
    triggered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["symbol", "active"])]

    def __str__(self) -> str:
        return f"{self.symbol} {self.get_direction_display()} {self.price}"

    def is_hit(self, price: float) -> bool:
        p = float(self.price)
        if self.direction == self.Direction.ABOVE:
            return price >= p
        return price <= p
