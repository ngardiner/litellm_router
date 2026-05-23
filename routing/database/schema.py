"""
Database schema definitions and migration management.
"""

import json
import os
import psycopg2
from typing import List, Tuple
from .connection import get_simple_connection


SCHEMA_VERSION = 1

SCHEMA_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_rules (
        id SERIAL PRIMARY KEY,
        model_name VARCHAR(255) UNIQUE NOT NULL,
        module_name VARCHAR(255) NOT NULL,
        enabled BOOLEAN DEFAULT true,
        priority INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS module_configs (
        id SERIAL PRIMARY KEY,
        module_name VARCHAR(255) NOT NULL,
        config_key VARCHAR(255) NOT NULL,
        config_value TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(module_name, config_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_modules (
        id SERIAL PRIMARY KEY,
        module_name VARCHAR(255) UNIQUE NOT NULL,
        display_name VARCHAR(255),
        description TEXT,
        config_schema JSONB,
        version VARCHAR(50),
        enabled BOOLEAN DEFAULT true,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_cooldowns (
        id SERIAL PRIMARY KEY,
        model_key VARCHAR(255) UNIQUE NOT NULL,
        cooldown_until TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )
    """,
]

SCHEMA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_routing_rules_model_name ON routing_rules(model_name)",
    "CREATE INDEX IF NOT EXISTS idx_routing_rules_module_name ON routing_rules(module_name)",
    "CREATE INDEX IF NOT EXISTS idx_module_configs_module_name ON module_configs(module_name)",
    "CREATE INDEX IF NOT EXISTS idx_model_cooldowns_model_key ON model_cooldowns(model_key)",
    "CREATE INDEX IF NOT EXISTS idx_model_cooldowns_cooldown_until ON model_cooldowns(cooldown_until)",
]


def get_current_schema_version() -> int:
    """Get the current schema version from the database."""
    try:
        with get_simple_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT MAX(version) FROM schema_version")
                result = cursor.fetchone()
                return result[0] if result[0] is not None else 0
    except psycopg2.Error:
        return 0


def apply_schema_migration(version: int) -> None:
    """Apply schema migration for a specific version."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            # Create all tables
            for table_sql in SCHEMA_TABLES:
                cursor.execute(table_sql)
            
            # Create all indexes
            for index_sql in SCHEMA_INDEXES:
                cursor.execute(index_sql)
            
            # Record schema version
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
                (version,)
            )
            
            conn.commit()


def init_database() -> None:
    """Initialize the database with the current schema."""
    current_version = get_current_schema_version()
    
    if current_version < SCHEMA_VERSION:
        print(f"Upgrading database schema from version {current_version} to {SCHEMA_VERSION}")
        apply_schema_migration(SCHEMA_VERSION)
        print("Database schema upgrade completed")
    else:
        print(f"Database schema is up to date (version {current_version})")


def get_module_config(module_name: str) -> dict:
    """Get configuration for a specific module."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT config_key, config_value FROM module_configs WHERE module_name = %s",
                (module_name,)
            )
            return {row[0]: row[1] for row in cursor.fetchall()}


def set_module_config(module_name: str, config_key: str, config_value: str) -> None:
    """Set configuration for a specific module."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO module_configs (module_name, config_key, config_value, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (module_name, config_key) 
                DO UPDATE SET config_value = EXCLUDED.config_value, updated_at = NOW()
                """,
                (module_name, config_key, config_value)
            )
            conn.commit()


def get_routing_rules() -> List[Tuple]:
    """Get all active routing rules including instance_config."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_name, module_name, enabled, priority,
                       COALESCE(instance_config, '{}')::jsonb
                FROM routing_rules
                WHERE enabled = true
                ORDER BY priority DESC, model_name
                """
            )
            return cursor.fetchall()


def get_all_routing_rules() -> List[Tuple]:
    """Get all routing rules (enabled and disabled) including instance_config."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_name, module_name, enabled, priority,
                       COALESCE(instance_config, '{}')::jsonb
                FROM routing_rules
                ORDER BY priority DESC, model_name
                """
            )
            return cursor.fetchall()


def get_routing_rule(model_name: str) -> dict:
    """Get a single routing rule by model name."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_name, module_name, enabled, priority,
                       COALESCE(instance_config, '{}')::jsonb
                FROM routing_rules
                WHERE model_name = %s
                """,
                (model_name,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "model_name": row[0],
                "module_name": row[1],
                "enabled": row[2],
                "priority": row[3],
                "instance_config": row[4] or {},
            }


def upsert_routing_rule(model_name: str, module_name: str, enabled: bool = True,
                        priority: int = 0, instance_config: dict = None) -> None:
    """Create or update a routing rule."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO routing_rules
                    (model_name, module_name, enabled, priority, instance_config, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (model_name) DO UPDATE SET
                    module_name = EXCLUDED.module_name,
                    enabled = EXCLUDED.enabled,
                    priority = EXCLUDED.priority,
                    instance_config = EXCLUDED.instance_config,
                    updated_at = NOW()
                """,
                (model_name, module_name, enabled, priority,
                 json.dumps(instance_config or {}))
            )
            conn.commit()


def register_module(module_name: str, display_name: str, description: str, 
                   config_schema: dict = None, version: str = "1.0.0") -> None:
    """Register a routing module in the database."""
    with get_simple_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO routing_modules 
                (module_name, display_name, description, config_schema, version, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (module_name) 
                DO UPDATE SET 
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    config_schema = EXCLUDED.config_schema,
                    version = EXCLUDED.version,
                    updated_at = NOW()
                """,
                (module_name, display_name, description, 
                 psycopg2.extras.Json(config_schema) if config_schema else None, version)
            )
            conn.commit()