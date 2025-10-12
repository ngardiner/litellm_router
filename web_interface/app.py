"""
FastAPI web application for LiteLLM Router Platform management.
"""

import os
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Import routing components
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from routing.database.schema import (
    get_routing_rules, get_module_config, set_module_config,
    register_module, get_current_schema_version
)
from routing.database.connection import get_simple_connection
from routing.router import health_check, auto_discover_and_register_modules
from routing.utils.module_loader import get_module_loader

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="LiteLLM Router Platform",
    description="Web interface for managing LiteLLM routing rules and modules",
    version="1.0.0"
)

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models for API
class RoutingRule(BaseModel):
    model_name: str = Field(..., description="Model name to route")
    module_name: str = Field(..., description="Module to handle routing")
    enabled: bool = Field(default=True, description="Whether rule is enabled")
    priority: int = Field(default=0, description="Rule priority (higher = more important)")

class RoutingRuleResponse(RoutingRule):
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class ModuleConfig(BaseModel):
    module_name: str = Field(..., description="Module name")
    config_key: str = Field(..., description="Configuration key")
    config_value: str = Field(..., description="Configuration value")

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


# API Routes

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main web interface."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>LiteLLM Router Platform</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
        <div class="container mt-4">
            <h1>LiteLLM Router Platform</h1>
            <p class="lead">Web interface for managing routing rules and modules</p>
            
            <div class="row">
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-body">
                            <h5 class="card-title">API Documentation</h5>
                            <p class="card-text">Explore the REST API endpoints</p>
                            <a href="/docs" class="btn btn-primary">View API Docs</a>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-body">
                            <h5 class="card-title">Health Status</h5>
                            <p class="card-text">Check system health and status</p>
                            <a href="/api/health" class="btn btn-success">Health Check</a>
                        </div>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="card">
                        <div class="card-body">
                            <h5 class="card-title">Routing Rules</h5>
                            <p class="card-text">Manage model routing rules</p>
                            <a href="/api/routing-rules" class="btn btn-info">View Rules</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """


@app.get("/api/health", response_model=HealthStatus)
async def get_health():
    """Get system health status."""
    try:
        health_data = health_check()
        return HealthStatus(**health_data)
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/routing-rules", response_model=List[RoutingRuleResponse])
async def get_routing_rules_api():
    """Get all routing rules."""
    try:
        rules = get_routing_rules()
        return [
            RoutingRuleResponse(
                model_name=rule[0],
                module_name=rule[1],
                enabled=rule[2],
                priority=rule[3]
            )
            for rule in rules
        ]
    except Exception as e:
        logger.error(f"Failed to get routing rules: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/routing-rules", response_model=dict)
async def create_routing_rule(rule: RoutingRule):
    """Create a new routing rule."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO routing_rules (model_name, module_name, enabled, priority, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (model_name) 
                    DO UPDATE SET 
                        module_name = EXCLUDED.module_name,
                        enabled = EXCLUDED.enabled,
                        priority = EXCLUDED.priority,
                        updated_at = NOW()
                    """,
                    (rule.model_name, rule.module_name, rule.enabled, rule.priority)
                )
                conn.commit()
        
        return {"message": f"Routing rule for {rule.model_name} created/updated successfully"}
    except Exception as e:
        logger.error(f"Failed to create routing rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/routing-rules/{model_name}")
async def delete_routing_rule(model_name: str):
    """Delete a routing rule."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM routing_rules WHERE model_name = %s",
                    (model_name,)
                )
                if cursor.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Routing rule not found")
                conn.commit()
        
        return {"message": f"Routing rule for {model_name} deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete routing rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modules", response_model=List[ModuleInfo])
async def get_modules():
    """Get all registered modules."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT module_name, display_name, description, version, enabled, config_schema
                    FROM routing_modules
                    ORDER BY module_name
                    """
                )
                modules = []
                for row in cursor.fetchall():
                    modules.append(ModuleInfo(
                        module_name=row[0],
                        display_name=row[1] or row[0],
                        description=row[2] or "",
                        version=row[3] or "1.0.0",
                        enabled=row[4],
                        config_schema=row[5]
                    ))
                return modules
    except Exception as e:
        logger.error(f"Failed to get modules: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modules/discover")
async def discover_modules():
    """Discover and register new modules."""
    try:
        auto_discover_and_register_modules()
        return {"message": "Module discovery completed successfully"}
    except Exception as e:
        logger.error(f"Module discovery failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modules/{module_name}/config", response_model=Dict[str, str])
async def get_module_config_api(module_name: str):
    """Get configuration for a specific module."""
    try:
        config = get_module_config(module_name)
        return config
    except Exception as e:
        logger.error(f"Failed to get module config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modules/{module_name}/config")
async def set_module_config_api(module_name: str, config: Dict[str, str]):
    """Set configuration for a specific module."""
    try:
        for key, value in config.items():
            set_module_config(module_name, key, value)
        return {"message": f"Configuration updated for {module_name}"}
    except Exception as e:
        logger.error(f"Failed to set module config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cooldowns", response_model=List[CooldownStatus])
async def get_cooldowns():
    """Get all model cooldown statuses."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT model_key, cooldown_until 
                    FROM model_cooldowns 
                    ORDER BY model_key
                    """
                )
                cooldowns = []
                now = datetime.now()
                for row in cursor.fetchall():
                    cooldown_until = row[1]
                    is_active = cooldown_until and cooldown_until > now
                    cooldowns.append(CooldownStatus(
                        model_key=row[0],
                        cooldown_until=cooldown_until,
                        is_active=bool(is_active)
                    ))
                return cooldowns
    except Exception as e:
        logger.error(f"Failed to get cooldowns: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/cooldowns/{model_key}")
async def clear_cooldown(model_key: str):
    """Clear cooldown for a specific model."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM model_cooldowns WHERE model_key = %s",
                    (model_key,)
                )
                if cursor.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Cooldown not found")
                conn.commit()
        
        return {"message": f"Cooldown cleared for {model_key}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to clear cooldown: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats")
async def get_stats():
    """Get platform statistics."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                # Get routing rules count
                cursor.execute("SELECT COUNT(*) FROM routing_rules WHERE enabled = true")
                active_rules = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM routing_rules")
                total_rules = cursor.fetchone()[0]
                
                # Get modules count
                cursor.execute("SELECT COUNT(*) FROM routing_modules WHERE enabled = true")
                active_modules = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM routing_modules")
                total_modules = cursor.fetchone()[0]
                
                # Get active cooldowns count
                cursor.execute("SELECT COUNT(*) FROM model_cooldowns WHERE cooldown_until > NOW()")
                active_cooldowns = cursor.fetchone()[0]
                
                # Get schema version
                schema_version = get_current_schema_version()
                
        return {
            "routing_rules": {
                "active": active_rules,
                "total": total_rules
            },
            "modules": {
                "active": active_modules,
                "total": total_modules
            },
            "cooldowns": {
                "active": active_cooldowns
            },
            "schema_version": schema_version
        }
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)