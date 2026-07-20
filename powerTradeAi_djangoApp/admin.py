from __future__ import annotations

from django.contrib import admin, messages

from .models import Alert, ApiKey, ScanRun, Strategy


@admin.register(Strategy)
class StrategyAdmin(admin.ModelAdmin):
    list_display = ("strategy_id", "symbol", "rule_version", "enabled", "contracts")
    list_filter = ("enabled", "symbol")
    search_fields = ("strategy_id", "name")
    list_editable = ("enabled", "contracts")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = (
        "session_date", "strategy", "direction", "status", "source",
        "strike", "entry_premium", "exit_premium_display",
        "net_dollars_display", "net_pct_display",
    )
    list_filter = ("source", "status", "symbol", "direction", "strategy")
    date_hierarchy = "session_date"
    search_fields = ("occ_symbol", "strategy__strategy_id")
    readonly_fields = ("net_dollars", "net_pct", "created_at", "updated_at")

    @admin.display(description="venta")
    def exit_premium_display(self, obj: Alert):
        return obj.exit_premium if obj.exit_premium is not None else "pending"

    @admin.display(description="neto $", ordering="net_dollars")
    def net_dollars_display(self, obj: Alert):
        return obj.net_dollars if obj.net_dollars is not None else "pending"

    @admin.display(description="neto %", ordering="net_pct")
    def net_pct_display(self, obj: Alert):
        return f"{obj.net_pct}%" if obj.net_pct is not None else "pending"


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "prefix", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    readonly_fields = ("prefix", "key_hash", "created_at", "last_used_at", "revoked_at")
    actions = ["revoke_selected"]

    def get_fields(self, request, obj=None):
        # Al crear solo se pide el nombre: la clave la genera el modelo.
        return ["name"] if obj is None else [
            "name", "prefix", "is_active", "created_at", "last_used_at", "revoked_at",
        ]

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            return
        # Se rellena el propio ``obj`` (no un registro nuevo) para que el admin
        # conserve su PK y pueda redirigir y escribir el LogEntry.
        raw = ApiKey.new_raw_key()
        obj.prefix = raw[: ApiKey.PREFIX_LEN]
        obj.key_hash = ApiKey.hash_key(raw)
        super().save_model(request, obj, form, change)
        # Unica vez que la clave en claro es visible. No se persiste.
        self.message_user(
            request,
            f"API key creada. Copiala ahora, no se vuelve a mostrar: {raw}",
            level=messages.WARNING,
        )

    @admin.action(description="Revocar las claves seleccionadas")
    def revoke_selected(self, request, queryset):
        for key in queryset:
            key.revoke()
        self.message_user(request, f"{queryset.count()} clave(s) revocada(s).")


@admin.register(ScanRun)
class ScanRunAdmin(admin.ModelAdmin):
    list_display = (
        "started_at", "finished_at", "strategies_evaluated",
        "alerts_created", "alerts_closed", "ok",
    )
    list_filter = ("ok",)
    readonly_fields = [f.name for f in ScanRun._meta.fields]
