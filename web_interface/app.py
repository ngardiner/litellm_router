import os
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from routing.database.schema import (
    get_routing_rules, get_all_routing_rules, get_routing_rule,
    upsert_routing_rule, get_module_config, set_module_config,
    register_module, get_current_schema_version
)
from routing.database.connection import get_simple_connection
from routing.router import health_check, auto_discover_and_register_modules
from routing.utils.module_loader import get_module_loader
from routing.monitoring.metrics import get_metrics_collector, get_performance_monitor
from routing.monitoring.circuit_breaker import get_circuit_breaker_manager
from routing.database.migrations import get_migration_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="LiteLLM Router Platform",
    description="Web interface for managing LiteLLM routing rules and modules",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_here = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_here, "templates"))

app.mount("/static", StaticFiles(directory=os.path.join(_here, "static")), name="static")


# ─── Pydantic models ────────────────────────────────────────────────────────

class RoutingRule(BaseModel):
    model_name: str
    module_name: str
    enabled: bool = True
    priority: int = 0
    instance_config: Optional[Dict[str, Any]] = None

class RoutingRuleResponse(BaseModel):
    model_name: str
    module_name: str
    enabled: bool
    priority: int
    instance_config: Optional[Dict[str, Any]] = None

class ModuleInfo(BaseModel):
    module_name: str
    display_name: str
    description: str
    version: str
    enabled: bool
    config_schema: Optional[Dict[str, Any]] = None

class HealthStatus(BaseModel):
    status: str
    database: str
    modules: Dict[str, str]
    routing_rules_count: Optional[int] = None
    errors: List[str] = []

class CooldownStatus(BaseModel):
    model_key: str
    cooldown_until: Optional[datetime]
    is_active: bool


# ─── Helpers ────────────────────────────────────────────────────────────────

def _parse_form_config(form_data, prefix: str) -> dict:
    """Extract prefixed config fields from form data: prefix[key] → {key: value}."""
    result = {}
    bracket_prefix = f"{prefix}["
    for key, value in form_data.multi_items():
        if key.startswith(bracket_prefix) and key.endswith("]"):
            field_name = key[len(bracket_prefix):-1]
            result[field_name] = value
    return result


def _coerce_config(raw: dict, schema_props: dict) -> dict:
    """Cast string form values to their schema types."""
    out = {}
    for k, v in raw.items():
        prop = schema_props.get(k, {})
        ptype = prop.get("type", "string")
        try:
            if ptype == "boolean":
                out[k] = v.lower() in ("true", "1", "yes", "on")
            elif ptype == "integer":
                out[k] = int(v)
            elif ptype == "number":
                out[k] = float(v)
            elif ptype in ("array", "object"):
                out[k] = json.loads(v)
            else:
                out[k] = v
        except (ValueError, json.JSONDecodeError):
            out[k] = v
    return out


def _get_module_info_list(include_callbacks: bool = True):
    """Build module info dicts for all discovered modules."""
    module_loader = get_module_loader()
    cb_manager = get_circuit_breaker_manager()

    discovered = module_loader.discover_modules()

    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT module_name, display_name, description, version, enabled, config_schema "
                "FROM routing_modules ORDER BY module_name"
            )
            db_modules = {row[0]: row for row in cursor.fetchall()}

            cursor.execute(
                "SELECT module_name, COUNT(*) FROM routing_rules GROUP BY module_name"
            )
            rule_counts = {row[0]: row[1] for row in cursor.fetchall()}

    routing_modules = []
    callback_modules = []

    cb_statuses = cb_manager.get_all_statuses()

    for name in discovered:
        module = module_loader.load_module(name)
        if module is None:
            continue

        db_row = db_modules.get(name)
        mod_type = module_loader.get_module_type(name)

        config_schema = None
        if db_row and db_row[5]:
            config_schema = db_row[5]
        else:
            raw = getattr(module, "CONFIG_SCHEMA", None)
            if raw:
                config_schema = raw

        pii_stats = None
        if name == "pii_hasher":
            try:
                with get_simple_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT COUNT(*) FROM pii_tokens WHERE expires_at > NOW()")
                        active = cursor.fetchone()[0]
                        cursor.execute("SELECT COUNT(*) FROM pii_tokens")
                        total = cursor.fetchone()[0]
                        pii_stats = {"active": active, "total": total}
            except Exception:
                pass

        cb_info = cb_statuses.get(name, {})
        data = {
            "module_name": name,
            "display_name": db_row[1] if db_row else getattr(module, "MODULE_NAME", name),
            "description": db_row[2] if db_row else getattr(module, "MODULE_DESCRIPTION", ""),
            "version": db_row[3] if db_row else getattr(module, "MODULE_VERSION", "1.0.0"),
            "db_enabled": db_row[4] if db_row else True,
            "config_schema": config_schema,
            "rule_count": rule_counts.get(name, 0),
            "cb_state": cb_info.get("state", "CLOSED"),
            "pii_stats": pii_stats,
        }

        if mod_type == "callback":
            callback_modules.append(data)
        else:
            routing_modules.append(data)

    return routing_modules, callback_modules


# ─── UI partial routes ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/ui/modules", response_class=HTMLResponse)
async def ui_modules(request: Request):
    routing_modules, callback_modules = _get_module_info_list()
    return templates.TemplateResponse(request, "partials/modules.html", {
        "routing_modules": routing_modules,
        "callback_modules": callback_modules,
    })


@app.get("/ui/wiring", response_class=HTMLResponse)
async def ui_wiring(request: Request):
    rules_raw = get_all_routing_rules()
    rules = [
        {
            "model_name": r[0],
            "module_name": r[1],
            "enabled": r[2],
            "priority": r[3],
            "instance_config": r[4] or {},
        }
        for r in rules_raw
    ]

    module_loader = get_module_loader()
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT module_name, display_name FROM routing_modules "
                "WHERE enabled = true ORDER BY display_name"
            )
            available_modules = [
                {"module_name": row[0], "display_name": row[1] or row[0]}
                for row in cursor.fetchall()
            ]
            # Fall back to discovered if DB is empty
            if not available_modules:
                for name in module_loader.discover_modules():
                    mod = module_loader.load_module(name)
                    if mod and module_loader.get_module_type(name) == "routing":
                        available_modules.append({
                            "module_name": name,
                            "display_name": getattr(mod, "MODULE_NAME", name),
                        })

    return templates.TemplateResponse(request, "partials/wiring.html", {
        "rules": rules,
        "available_modules": available_modules,
    })


@app.get("/ui/wiring/rule-form", response_class=HTMLResponse)
async def ui_wiring_rule_form(request: Request, module_name: str = ""):
    """Return instance config form fields for a given module (htmx partial)."""
    if not module_name:
        return HTMLResponse("")

    module_loader = get_module_loader()
    module = module_loader.load_module(module_name)
    if module is None:
        return HTMLResponse("")

    schema = getattr(module, "CONFIG_SCHEMA", None) or {}
    properties = schema.get("properties", {})
    if not properties:
        return HTMLResponse('<p class="text-muted small">No instance configuration for this module.</p>')

    return templates.TemplateResponse(request, "partials/wiring_rule_form.html", {
        "properties": properties,
        "values": {},
        "prefix": "instance_config",
        "read_only": False,
    })


@app.get("/ui/wiring/edit/{model_name:path}", response_class=HTMLResponse)
async def ui_wiring_edit(request: Request, model_name: str):
    rule = get_routing_rule(model_name)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    module_loader = get_module_loader()
    module = module_loader.load_module(rule["module_name"])
    module_schema = getattr(module, "CONFIG_SCHEMA", None) if module else None

    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT module_name, display_name FROM routing_modules "
                "WHERE enabled = true ORDER BY display_name"
            )
            available_modules = [
                {"module_name": row[0], "display_name": row[1] or row[0]}
                for row in cursor.fetchall()
            ]

    return templates.TemplateResponse(request, "partials/wiring_edit_row.html", {
        "rule": rule,
        "module_schema": module_schema,
        "available_modules": available_modules,
    })


@app.get("/ui/observability", response_class=HTMLResponse)
async def ui_observability(request: Request):
    metrics_collector = get_metrics_collector()
    cb_manager = get_circuit_breaker_manager()

    raw_summary = metrics_collector.get_metrics_summary(since_minutes=60)

    # Build per-module metrics list
    metrics_list = []
    for module_name, stats in raw_summary.get("modules", {}).items():
        metrics_list.append({
            "module_name": module_name,
            "total_requests": stats["total_requests"],
            "successful_requests": stats["successful_requests"],
            "failed_requests": stats["failed_requests"],
            "avg_latency_ms": stats.get("avg_response_time_ms", 0),
            "p95_latency_ms": stats.get("max_response_time_ms", 0),
        })

    # Circuit breakers
    cb_all = cb_manager.get_all_statuses()
    circuit_breakers = [
        {"module_name": name, "state": info.get("state", "CLOSED"), **info}
        for name, info in cb_all.items()
    ]

    # Active cooldowns
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT model_key, cooldown_until FROM model_cooldowns "
                "WHERE cooldown_until > NOW() ORDER BY cooldown_until"
            )
            cooldowns = [
                {"model_key": row[0], "cooldown_until": row[1]}
                for row in cursor.fetchall()
            ]
            active_cooldowns_count = len(cooldowns)

    # Recent errors
    raw_errors = metrics_collector.get_recent_errors(limit=20)
    recent_errors = [
        {
            "recorded_at": e.get("timestamp", ""),
            "module_name": e.get("module_name", ""),
            "model_used": e.get("model_name", ""),
            "error_message": e.get("error_message", ""),
        }
        for e in raw_errors
    ]

    summary = {
        "requests_1h": raw_summary.get("total_requests", 0),
        "success_rate": raw_summary.get("success_rate", 1.0),
        "avg_latency_ms": raw_summary.get("avg_response_time_ms", 0),
        "active_cooldowns": active_cooldowns_count,
    }

    return templates.TemplateResponse(request, "partials/observability.html", {
        "summary": summary,
        "metrics": metrics_list,
        "circuit_breakers": circuit_breakers,
        "cooldowns": cooldowns,
        "recent_errors": recent_errors,
    })


@app.get("/ui/system", response_class=HTMLResponse)
async def ui_system(request: Request):
    # Health — convert modules dict to list for template
    try:
        raw_health = health_check()
    except Exception as e:
        raw_health = {"status": "unhealthy", "errors": [str(e)], "modules": {}}

    modules_list = [
        {"module": name, "status": status}
        for name, status in (raw_health.get("modules") or {}).items()
    ]
    health = {
        **raw_health,
        "modules": modules_list,
        "db_connected": raw_health.get("database") == "connected",
        "schema_version": get_current_schema_version(),
    }

    # Migrations — applied_migrations is a list of {version, description, applied_at}
    try:
        migration_manager = get_migration_manager()
        raw_status = migration_manager.get_migration_status()
        applied_vers = {m["version"] for m in raw_status.get("applied_migrations", [])}
        # Build full migration list with applied flag
        mig_list = [
            {
                "version": m["version"],
                "description": m["description"],
                "applied": m["version"] in applied_vers,
                "applied_at": m.get("applied_at"),
            }
            for m in raw_status.get("applied_migrations", [])
        ]
        pending = raw_status.get("target_version", 0) - raw_status.get("current_version", 0)
        migration_status = {
            "applied_count": len(applied_vers),
            "pending_count": max(0, pending),
            "migrations": mig_list,
        }
    except Exception as e:
        migration_status = {"applied_count": 0, "pending_count": 0, "migrations": [], "error": str(e)}

    # Hot reload
    try:
        from routing.utils.hot_reload import get_hot_reloader
        raw_hr = get_hot_reloader().get_status()
        hot_reload = {
            "enabled": raw_hr.get("watching", raw_hr.get("enabled", False)),
            "watch_dir": raw_hr.get("modules_directory", "modules/"),
        }
    except Exception:
        hot_reload = {"enabled": False, "watch_dir": "modules/"}

    module_loader = get_module_loader()
    available_module_names = module_loader.discover_modules()

    return templates.TemplateResponse(request, "partials/system.html", {
        "health": health,
        "migration_status": migration_status,
        "hot_reload": hot_reload,
        "available_module_names": available_module_names,
    })


@app.get("/ui/modules/{module_name}/config-form", response_class=HTMLResponse)
async def ui_module_config_form(request: Request, module_name: str):
    module_loader = get_module_loader()
    module = module_loader.load_module(module_name)
    if module is None:
        return HTMLResponse(f'<div class="alert alert-danger">Module {module_name} not found</div>')

    schema = getattr(module, "CONFIG_SCHEMA", None) or {}
    current_config = get_module_config(module_name)

    return templates.TemplateResponse(request, "partials/module_config_form.html", {
        "module_name": module_name,
        "schema": schema,
        "current_config": current_config,
    })


# ─── API: routing rules ───────────────────────────────────────────────────────

@app.get("/api/routing-rules", response_model=List[RoutingRuleResponse])
async def get_routing_rules_api():
    rules = get_all_routing_rules()
    return [
        RoutingRuleResponse(
            model_name=r[0], module_name=r[1], enabled=r[2],
            priority=r[3], instance_config=r[4]
        )
        for r in rules
    ]


@app.get("/api/routing-rules/{model_name:path}", response_model=RoutingRuleResponse)
async def get_routing_rule_api(model_name: str):
    rule = get_routing_rule(model_name)
    if rule is None:
        raise HTTPException(status_code=404, detail="Routing rule not found")
    return RoutingRuleResponse(**rule)


@app.post("/api/routing-rules", response_model=dict)
async def create_routing_rule(request: Request):
    """Create or update a routing rule (accepts form data or JSON)."""
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            model_name = body["model_name"]
            module_name = body["module_name"]
            enabled = body.get("enabled", True)
            priority = int(body.get("priority", 0))
            instance_config = body.get("instance_config") or {}
        else:
            form = await request.form()
            model_name = form.get("model_name", "").strip()
            module_name = form.get("module_name", "").strip()
            enabled = form.get("enabled") == "true"
            priority = int(form.get("priority", 0) or 0)
            raw_ic = _parse_form_config(form, "instance_config")
            # Coerce types using the module's schema
            module_loader = get_module_loader()
            mod = module_loader.load_module(module_name)
            schema_props = (getattr(mod, "CONFIG_SCHEMA", None) or {}).get("properties", {}) if mod else {}
            instance_config = _coerce_config(raw_ic, schema_props) if raw_ic else {}

        if not model_name or not module_name:
            raise HTTPException(status_code=422, detail="model_name and module_name are required")

        upsert_routing_rule(model_name, module_name, enabled, priority, instance_config)
        return {"message": f"Routing rule for {model_name} saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save routing rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/routing-rules/{model_name:path}", response_model=dict)
async def update_routing_rule(model_name: str, request: Request):
    """Update an existing routing rule (htmx form posts here via _method=PUT)."""
    try:
        form = await request.form()
        new_module = form.get("module_name", "").strip()
        enabled = form.get("enabled") == "true"
        priority = int(form.get("priority", 0) or 0)
        raw_ic = _parse_form_config(form, "instance_config")
        module_loader = get_module_loader()
        target_module = new_module or (get_routing_rule(model_name) or {}).get("module_name", "")
        mod = module_loader.load_module(target_module)
        schema_props = (getattr(mod, "CONFIG_SCHEMA", None) or {}).get("properties", {}) if mod else {}
        instance_config = _coerce_config(raw_ic, schema_props) if raw_ic else {}

        upsert_routing_rule(model_name, target_module, enabled, priority, instance_config)
        return {"message": f"Routing rule for {model_name} updated"}
    except Exception as e:
        logger.error(f"Failed to update routing rule {model_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/routing-rules/{model_name:path}")
async def delete_routing_rule(model_name: str):
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM routing_rules WHERE model_name = %s", (model_name,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Routing rule not found")
            conn.commit()
    return {"message": f"Routing rule for {model_name} deleted"}


# ─── API: modules ─────────────────────────────────────────────────────────────

@app.get("/api/modules", response_model=List[ModuleInfo])
async def get_modules():
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT module_name, display_name, description, version, enabled, config_schema "
                "FROM routing_modules ORDER BY module_name"
            )
            return [
                ModuleInfo(
                    module_name=row[0], display_name=row[1] or row[0],
                    description=row[2] or "", version=row[3] or "1.0.0",
                    enabled=row[4], config_schema=row[5]
                )
                for row in cursor.fetchall()
            ]


@app.post("/api/modules/discover")
async def discover_modules():
    try:
        auto_discover_and_register_modules()
        return {"message": "Module discovery completed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modules/{module_name}/enabled")
async def set_module_enabled(module_name: str, request: Request):
    """Toggle module enabled state (accepts form data or JSON)."""
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            enabled = body.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.lower() == "true"
        else:
            form = await request.form()
            enabled_val = form.get("enabled", "true")
            enabled = enabled_val.lower() == "true"

        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE routing_modules SET enabled = %s, updated_at = NOW() WHERE module_name = %s",
                    (enabled, module_name)
                )
                if cursor.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Module not found")
                conn.commit()
        return {"message": f"Module {module_name} {'enabled' if enabled else 'disabled'}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modules/{module_name}/config")
async def get_module_config_api(module_name: str):
    return get_module_config(module_name)


@app.post("/api/modules/{module_name}/config")
async def set_module_config_api(module_name: str, request: Request):
    """Set module config (accepts form data or JSON)."""
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            config_dict = {str(k): str(v) for k, v in body.items()}
        else:
            form = await request.form()
            config_dict = _parse_form_config(form, "config")
            if not config_dict:
                # Fallback: treat all non-reserved keys as config
                config_dict = {k: v for k, v in form.items()
                               if not k.startswith("_")}

        for key, value in config_dict.items():
            set_module_config(module_name, key, str(value))
        return {"message": f"Configuration updated for {module_name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: health ──────────────────────────────────────────────────────────────

@app.get("/api/health", response_model=HealthStatus)
async def get_health():
    try:
        health_data = health_check()
        return HealthStatus(**health_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: PII stats ───────────────────────────────────────────────────────────

@app.get("/api/pii/stats")
async def get_pii_stats():
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM pii_tokens WHERE expires_at > NOW()")
                active = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM pii_tokens")
                total = cursor.fetchone()[0]
        return {"active": active, "total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: cooldowns ───────────────────────────────────────────────────────────

@app.get("/api/cooldowns")
async def get_cooldowns():
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT model_key, cooldown_until FROM model_cooldowns ORDER BY model_key"
            )
            now = datetime.now()
            return [
                CooldownStatus(
                    model_key=row[0],
                    cooldown_until=row[1],
                    is_active=bool(row[1] and row[1] > now)
                )
                for row in cursor.fetchall()
            ]


@app.delete("/api/cooldowns")
async def clear_all_cooldowns():
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM model_cooldowns WHERE cooldown_until > NOW()")
            conn.commit()
    return {"message": "All active cooldowns cleared"}


@app.delete("/api/cooldowns/{model_key:path}")
async def clear_cooldown(model_key: str):
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM model_cooldowns WHERE model_key = %s", (model_key,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Cooldown not found")
            conn.commit()
    return {"message": f"Cooldown cleared for {model_key}"}


# ─── API: stats ───────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM routing_rules WHERE enabled = true")
            active_rules = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM routing_rules")
            total_rules = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM routing_modules WHERE enabled = true")
            active_modules = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM routing_modules")
            total_modules = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM model_cooldowns WHERE cooldown_until > NOW()")
            active_cooldowns = cursor.fetchone()[0]
    return {
        "routing_rules": {"active": active_rules, "total": total_rules},
        "modules": {"active": active_modules, "total": total_modules},
        "cooldowns": {"active": active_cooldowns},
        "schema_version": get_current_schema_version(),
    }


# ─── API: metrics ─────────────────────────────────────────────────────────────

@app.get("/api/metrics/summary")
async def get_metrics_summary(minutes: int = 60):
    return get_metrics_collector().get_metrics_summary(since_minutes=minutes)


@app.get("/api/metrics/performance")
async def get_performance_report():
    return get_performance_monitor().get_performance_report()


@app.get("/api/metrics/errors")
async def get_recent_errors(limit: int = 50):
    return {"errors": get_metrics_collector().get_recent_errors(limit=limit)}


@app.post("/api/metrics/persist")
async def persist_metrics():
    get_metrics_collector().persist_metrics_to_db()
    return {"message": "Metrics persisted"}


@app.delete("/api/metrics/cleanup")
async def cleanup_old_metrics(days: int = 30):
    get_metrics_collector().cleanup_old_metrics(days_to_keep=days)
    return {"message": f"Cleaned up metrics older than {days} days"}


# ─── API: circuit breakers ────────────────────────────────────────────────────

@app.get("/api/circuit-breakers")
async def get_circuit_breaker_status():
    return {"circuit_breakers": get_circuit_breaker_manager().get_all_statuses()}


@app.post("/api/circuit-breakers/{module_name}/reset")
async def reset_circuit_breaker(module_name: str):
    success = get_circuit_breaker_manager().reset_breaker(module_name)
    if not success:
        raise HTTPException(status_code=404, detail="Circuit breaker not found")
    return {"message": f"Circuit breaker reset for {module_name}"}


@app.post("/api/circuit-breakers/reset-all")
async def reset_all_circuit_breakers():
    get_circuit_breaker_manager().reset_all_breakers()
    return {"message": "All circuit breakers reset"}


# ─── API: migrations ──────────────────────────────────────────────────────────

@app.get("/api/migrations/status")
async def get_migration_status():
    return get_migration_manager().get_migration_status()


@app.post("/api/migrations/migrate")
async def run_migrations():
    success = get_migration_manager().migrate_to_latest()
    if not success:
        raise HTTPException(status_code=500, detail="Migration failed")
    return {"message": "Migrations completed successfully"}


# ─── API: hot reload ──────────────────────────────────────────────────────────

@app.get("/api/hot-reload/status")
async def get_hot_reload_status():
    from routing.utils.hot_reload import get_hot_reloader
    return get_hot_reloader().get_status()


@app.post("/api/hot-reload/start")
async def start_hot_reload():
    from routing.utils.hot_reload import start_hot_reloading
    start_hot_reloading()
    return {"message": "Hot reload started"}


@app.post("/api/hot-reload/stop")
async def stop_hot_reload():
    from routing.utils.hot_reload import stop_hot_reloading
    stop_hot_reloading()
    return {"message": "Hot reload stopped"}


@app.post("/api/hot-reload/reload/{module_name}")
async def manual_reload_module(module_name: str):
    from routing.utils.hot_reload import manual_reload_module
    success = manual_reload_module(module_name)
    if not success:
        raise HTTPException(status_code=400, detail=f"Failed to reload {module_name}")
    return {"message": f"Module {module_name} reloaded"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
