"""
Database migration system for schema upgrades.
"""

import logging
import os
from typing import List, Callable, Dict, Any
from .connection import get_simple_connection

logger = logging.getLogger(__name__)


class Migration:
    """Individual database migration."""
    
    def __init__(self, version: int, description: str, up_sql: str, down_sql: str = ""):
        self.version = version
        self.description = description
        self.up_sql = up_sql
        self.down_sql = down_sql
    
    def apply(self, cursor) -> None:
        """Apply the migration."""
        logger.info(f"Applying migration {self.version}: {self.description}")
        cursor.execute(self.up_sql)
    
    def rollback(self, cursor) -> None:
        """Rollback the migration."""
        if self.down_sql:
            logger.info(f"Rolling back migration {self.version}: {self.description}")
            cursor.execute(self.down_sql)
        else:
            logger.warning(f"No rollback SQL for migration {self.version}")


class MigrationManager:
    """Manages database migrations."""
    
    def __init__(self):
        self.migrations: List[Migration] = []
        self._register_migrations()
    
    def _register_migrations(self) -> None:
        """Register all migrations in order."""
        
        # Migration 1: Initial schema
        self.migrations.append(Migration(
            version=1,
            description="Initial schema with routing tables",
            up_sql="""
                CREATE TABLE IF NOT EXISTS routing_rules (
                    id SERIAL PRIMARY KEY,
                    model_name VARCHAR(255) UNIQUE NOT NULL,
                    module_name VARCHAR(255) NOT NULL,
                    enabled BOOLEAN DEFAULT true,
                    priority INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS module_configs (
                    id SERIAL PRIMARY KEY,
                    module_name VARCHAR(255) NOT NULL,
                    config_key VARCHAR(255) NOT NULL,
                    config_value TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(module_name, config_key)
                );

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
                );

                CREATE TABLE IF NOT EXISTS model_cooldowns (
                    id SERIAL PRIMARY KEY,
                    model_key VARCHAR(255) UNIQUE NOT NULL,
                    cooldown_until TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_routing_rules_model_name ON routing_rules(model_name);
                CREATE INDEX IF NOT EXISTS idx_routing_rules_module_name ON routing_rules(module_name);
                CREATE INDEX IF NOT EXISTS idx_module_configs_module_name ON module_configs(module_name);
                CREATE INDEX IF NOT EXISTS idx_model_cooldowns_model_key ON model_cooldowns(model_key);
                CREATE INDEX IF NOT EXISTS idx_model_cooldowns_cooldown_until ON model_cooldowns(cooldown_until);
            """,
            down_sql="""
                DROP TABLE IF EXISTS model_cooldowns;
                DROP TABLE IF EXISTS routing_modules;
                DROP TABLE IF EXISTS module_configs;
                DROP TABLE IF EXISTS routing_rules;
            """
        ))
        
        # Migration 2: Add monitoring tables
        self.migrations.append(Migration(
            version=2,
            description="Add routing metrics table",
            up_sql="""
                CREATE TABLE IF NOT EXISTS routing_metrics (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    model_name VARCHAR(255) NOT NULL,
                    module_name VARCHAR(255) NOT NULL,
                    target_model VARCHAR(255) NOT NULL,
                    response_time_ms FLOAT NOT NULL,
                    success BOOLEAN NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                
                CREATE INDEX IF NOT EXISTS idx_routing_metrics_timestamp 
                ON routing_metrics(timestamp);
                
                CREATE INDEX IF NOT EXISTS idx_routing_metrics_module 
                ON routing_metrics(module_name);
                
                CREATE INDEX IF NOT EXISTS idx_routing_metrics_success 
                ON routing_metrics(success);
            """,
            down_sql="DROP TABLE IF EXISTS routing_metrics;"
        ))
        
        # Migration 3: Add circuit breaker configuration table
        self.migrations.append(Migration(
            version=3,
            description="Add circuit breaker configuration table",
            up_sql="""
                CREATE TABLE IF NOT EXISTS circuit_breaker_configs (
                    id SERIAL PRIMARY KEY,
                    module_name VARCHAR(255) UNIQUE NOT NULL,
                    failure_threshold INTEGER DEFAULT 5,
                    recovery_timeout INTEGER DEFAULT 60,
                    success_threshold INTEGER DEFAULT 3,
                    timeout_seconds FLOAT DEFAULT 30.0,
                    enabled BOOLEAN DEFAULT true,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                
                CREATE INDEX IF NOT EXISTS idx_circuit_breaker_configs_module 
                ON circuit_breaker_configs(module_name);
            """,
            down_sql="DROP TABLE IF EXISTS circuit_breaker_configs;"
        ))
        
        # Migration 4: Add performance indexes and optimizations
        self.migrations.append(Migration(
            version=4,
            description="Add performance optimizations and indexes",
            up_sql="""
                -- Add composite indexes for better query performance
                CREATE INDEX IF NOT EXISTS idx_routing_rules_enabled_priority
                ON routing_rules(enabled, priority DESC) WHERE enabled = true;

                CREATE INDEX IF NOT EXISTS idx_model_cooldowns_active
                ON model_cooldowns(model_key, cooldown_until);

                -- Add updated_at triggers for automatic timestamp updates
                CREATE OR REPLACE FUNCTION update_updated_at_column()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = NOW();
                    RETURN NEW;
                END;
                $$ language 'plpgsql';

                DROP TRIGGER IF EXISTS update_routing_rules_updated_at ON routing_rules;
                CREATE TRIGGER update_routing_rules_updated_at
                    BEFORE UPDATE ON routing_rules
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

                DROP TRIGGER IF EXISTS update_module_configs_updated_at ON module_configs;
                CREATE TRIGGER update_module_configs_updated_at
                    BEFORE UPDATE ON module_configs
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

                DROP TRIGGER IF EXISTS update_routing_modules_updated_at ON routing_modules;
                CREATE TRIGGER update_routing_modules_updated_at
                    BEFORE UPDATE ON routing_modules
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

                DROP TRIGGER IF EXISTS update_model_cooldowns_updated_at ON model_cooldowns;
                CREATE TRIGGER update_model_cooldowns_updated_at
                    BEFORE UPDATE ON model_cooldowns
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

                DROP TRIGGER IF EXISTS update_circuit_breaker_configs_updated_at ON circuit_breaker_configs;
                CREATE TRIGGER update_circuit_breaker_configs_updated_at
                    BEFORE UPDATE ON circuit_breaker_configs
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            """,
            down_sql="""
                DROP TRIGGER IF EXISTS update_routing_rules_updated_at ON routing_rules;
                DROP TRIGGER IF EXISTS update_module_configs_updated_at ON module_configs;
                DROP TRIGGER IF EXISTS update_routing_modules_updated_at ON routing_modules;
                DROP TRIGGER IF EXISTS update_model_cooldowns_updated_at ON model_cooldowns;
                DROP TRIGGER IF EXISTS update_circuit_breaker_configs_updated_at ON circuit_breaker_configs;
                DROP FUNCTION IF EXISTS update_updated_at_column();
                DROP INDEX IF EXISTS idx_routing_rules_enabled_priority;
                DROP INDEX IF EXISTS idx_model_cooldowns_active;
            """
        ))

        # Migration 5: PII token store
        self.migrations.append(Migration(
            version=5,
            description="Add pii_tokens table for bi-directional PII tokenization",
            up_sql="""
                CREATE TABLE IF NOT EXISTS pii_tokens (
                    id SERIAL PRIMARY KEY,
                    token VARCHAR(64) UNIQUE NOT NULL,
                    original_value TEXT NOT NULL,
                    entity_type VARCHAR(50) NOT NULL,
                    request_id VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW(),
                    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pii_tokens_token
                ON pii_tokens(token);

                CREATE INDEX IF NOT EXISTS idx_pii_tokens_expires
                ON pii_tokens(expires_at);

                CREATE INDEX IF NOT EXISTS idx_pii_tokens_request
                ON pii_tokens(request_id) WHERE request_id IS NOT NULL;
            """,
            down_sql="DROP TABLE IF EXISTS pii_tokens;"
        ))

        # Migration 6: per-instance module config on routing rules
        self.migrations.append(Migration(
            version=6,
            description="Add instance_config column to routing_rules for per-rule module overrides",
            up_sql="""
                ALTER TABLE routing_rules
                ADD COLUMN IF NOT EXISTS instance_config JSONB DEFAULT '{}';
            """,
            down_sql="""
                ALTER TABLE routing_rules DROP COLUMN IF EXISTS instance_config;
            """
        ))

        # Migration 7: quality assessor weight store
        self.migrations.append(Migration(
            version=7,
            description="Add quality_assessor_weights table for background scoring",
            up_sql="""
                CREATE TABLE IF NOT EXISTS quality_assessor_weights (
                    id SERIAL PRIMARY KEY,
                    rule_model_name VARCHAR(255) NOT NULL,
                    candidate_model VARCHAR(255) NOT NULL,
                    score FLOAT NOT NULL DEFAULT 0.5,
                    last_assessed_at TIMESTAMP WITH TIME ZONE,
                    assessment_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(rule_model_name, candidate_model)
                );

                CREATE INDEX IF NOT EXISTS idx_qa_weights_rule
                ON quality_assessor_weights(rule_model_name);
            """,
            down_sql="DROP TABLE IF EXISTS quality_assessor_weights;"
        ))

    def get_current_version(self) -> int:
        """Get current database schema version."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT table_name FROM information_schema.tables 
                        WHERE table_name = 'schema_version'
                    """)
                    
                    if not cursor.fetchone():
                        return 0
                    
                    cursor.execute("SELECT MAX(version) FROM schema_version")
                    result = cursor.fetchone()
                    return result[0] if result[0] is not None else 0
        except Exception as e:
            logger.error(f"Failed to get current schema version: {e}")
            return 0
    
    def get_target_version(self) -> int:
        """Get the latest available migration version."""
        return max(m.version for m in self.migrations) if self.migrations else 0
    
    def migrate_to_version(self, target_version: int) -> bool:
        """Migrate database to specific version."""
        current_version = self.get_current_version()
        
        if current_version == target_version:
            logger.info(f"Database already at version {target_version}")
            return True
        
        if target_version < current_version:
            logger.error(f"Cannot migrate backwards from {current_version} to {target_version}")
            return False
        
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    # Create schema_version table if it doesn't exist
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS schema_version (
                            version INTEGER PRIMARY KEY,
                            applied_at TIMESTAMP DEFAULT NOW(),
                            description TEXT
                        )
                    """)
                    
                    # Apply migrations in order
                    for migration in self.migrations:
                        if migration.version > current_version and migration.version <= target_version:
                            try:
                                migration.apply(cursor)
                                
                                # Record migration in schema_version table
                                cursor.execute("""
                                    INSERT INTO schema_version (version, description, applied_at)
                                    VALUES (%s, %s, NOW())
                                    ON CONFLICT (version) DO NOTHING
                                """, (migration.version, migration.description))
                                
                                logger.info(f"Applied migration {migration.version}")
                                
                            except Exception as e:
                                logger.error(f"Failed to apply migration {migration.version}: {e}")
                                conn.rollback()
                                return False
                    
                    conn.commit()
                    logger.info(f"Successfully migrated from version {current_version} to {target_version}")
                    return True
                    
        except Exception as e:
            logger.error(f"Migration failed: {e}")
            return False
    
    def migrate_to_latest(self) -> bool:
        """Migrate database to the latest version."""
        target_version = self.get_target_version()
        return self.migrate_to_version(target_version)
    
    def rollback_to_version(self, target_version: int) -> bool:
        """Rollback database to specific version."""
        current_version = self.get_current_version()
        
        if current_version == target_version:
            logger.info(f"Database already at version {target_version}")
            return True
        
        if target_version > current_version:
            logger.error(f"Cannot rollback forward from {current_version} to {target_version}")
            return False
        
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    # Rollback migrations in reverse order
                    migrations_to_rollback = [
                        m for m in reversed(self.migrations)
                        if m.version > target_version and m.version <= current_version
                    ]
                    
                    for migration in migrations_to_rollback:
                        try:
                            migration.rollback(cursor)
                            
                            # Remove migration record
                            cursor.execute(
                                "DELETE FROM schema_version WHERE version = %s",
                                (migration.version,)
                            )
                            
                            logger.info(f"Rolled back migration {migration.version}")
                            
                        except Exception as e:
                            logger.error(f"Failed to rollback migration {migration.version}: {e}")
                            conn.rollback()
                            return False
                    
                    conn.commit()
                    logger.info(f"Successfully rolled back from version {current_version} to {target_version}")
                    return True
                    
        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            return False
    
    def get_migration_status(self) -> Dict[str, Any]:
        """Get current migration status."""
        current_version = self.get_current_version()
        target_version = self.get_target_version()
        
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT version, description, applied_at 
                        FROM schema_version 
                        ORDER BY version
                    """)
                    applied_migrations = [
                        {
                            "version": row[0],
                            "description": row[1],
                            "applied_at": row[2].isoformat() if row[2] else None
                        }
                        for row in cursor.fetchall()
                    ]
        except Exception:
            applied_migrations = []
        
        pending_migrations = [
            {
                "version": m.version,
                "description": m.description
            }
            for m in self.migrations
            if m.version > current_version
        ]
        
        return {
            "current_version": current_version,
            "target_version": target_version,
            "needs_migration": current_version < target_version,
            "applied_migrations": applied_migrations,
            "pending_migrations": pending_migrations
        }


# Global migration manager instance
_migration_manager = None


def get_migration_manager() -> MigrationManager:
    """Get the global migration manager instance."""
    global _migration_manager
    if _migration_manager is None:
        _migration_manager = MigrationManager()
    return _migration_manager