"""
Unit tests for database migration functionality.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from routing.database.migrations import MigrationManager, Migration, get_migration_manager


class TestMigration:
    """Test individual migration functionality."""
    
    def test_migration_creation(self):
        """Test creating a migration."""
        migration = Migration(
            version=1,
            description="Test migration",
            up_sql="CREATE TABLE test (id SERIAL PRIMARY KEY);",
            down_sql="DROP TABLE test;"
        )
        
        assert migration.version == 1
        assert migration.description == "Test migration"
        assert migration.up_sql == "CREATE TABLE test (id SERIAL PRIMARY KEY);"
        assert migration.down_sql == "DROP TABLE test;"
    
    def test_migration_apply(self):
        """Test applying a migration."""
        migration = Migration(
            version=1,
            description="Test migration",
            up_sql="CREATE TABLE test (id SERIAL PRIMARY KEY);"
        )
        
        mock_cursor = MagicMock()
        migration.apply(mock_cursor)
        
        mock_cursor.execute.assert_called_once_with("CREATE TABLE test (id SERIAL PRIMARY KEY);")
    
    def test_migration_rollback_with_sql(self):
        """Test rolling back a migration with rollback SQL."""
        migration = Migration(
            version=1,
            description="Test migration",
            up_sql="CREATE TABLE test (id SERIAL PRIMARY KEY);",
            down_sql="DROP TABLE test;"
        )
        
        mock_cursor = MagicMock()
        migration.rollback(mock_cursor)
        
        mock_cursor.execute.assert_called_once_with("DROP TABLE test;")
    
    def test_migration_rollback_without_sql(self):
        """Test rolling back a migration without rollback SQL."""
        migration = Migration(
            version=1,
            description="Test migration",
            up_sql="CREATE TABLE test (id SERIAL PRIMARY KEY);"
        )
        
        mock_cursor = MagicMock()
        migration.rollback(mock_cursor)
        
        # Should not execute anything
        mock_cursor.execute.assert_not_called()


class TestMigrationManager:
    """Test migration manager functionality."""
    
    @patch('routing.database.migrations.get_simple_connection')
    def test_get_current_version(self, mock_get_conn):
        """Test getting current schema version."""
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_connection
        
        # Test with schema_version table existing
        mock_cursor.fetchone.side_effect = [
            ("schema_version",),  # Table exists
            (3,)  # Current version
        ]
        
        manager = MigrationManager()
        version = manager.get_current_version()
        assert version == 3
    
    @patch('routing.database.migrations.get_simple_connection')
    def test_get_current_version_no_table(self, mock_get_conn):
        """Test getting current version when no schema_version table exists."""
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_connection
        
        # Table doesn't exist
        mock_cursor.fetchone.return_value = None
        
        manager = MigrationManager()
        version = manager.get_current_version()
        assert version == 0
    
    def test_get_target_version(self):
        """Test getting target migration version."""
        manager = MigrationManager()
        target = manager.get_target_version()
        assert target > 0  # Should have migrations defined
    
    @patch('routing.database.migrations.get_simple_connection')
    def test_get_migration_status(self, mock_get_conn):
        """Test getting migration status."""
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_connection
        
        # Mock applied migrations
        mock_cursor.fetchall.return_value = [
            (1, "Initial schema", datetime.now(timezone.utc))
        ]
        
        with patch.object(MigrationManager, 'get_current_version', return_value=1):
            manager = MigrationManager()
            status = manager.get_migration_status()
            
            assert status["current_version"] == 1
            assert status["target_version"] > 1
            assert status["needs_migration"] == True
            assert len(status["applied_migrations"]) == 1
            assert len(status["pending_migrations"]) > 0
    
    @patch('routing.database.migrations.get_simple_connection')
    def test_migrate_to_version_success(self, mock_get_conn):
        """Test successful migration to target version."""
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_connection
        
        with patch.object(MigrationManager, 'get_current_version', return_value=0):
            manager = MigrationManager()
            success = manager.migrate_to_version(1)
            
            assert success
            # Should have created schema_version table and applied migration
            assert mock_cursor.execute.call_count >= 2
            mock_connection.commit.assert_called()
    
    @patch('routing.database.migrations.get_simple_connection')
    def test_migrate_to_version_already_current(self, mock_get_conn):
        """Test migration when already at target version."""
        with patch.object(MigrationManager, 'get_current_version', return_value=2):
            manager = MigrationManager()
            success = manager.migrate_to_version(2)
            
            assert success
            # Should not attempt any database operations
            mock_get_conn.assert_not_called()
    
    def test_migrate_to_version_backward(self):
        """Test that backward migration is rejected."""
        with patch.object(MigrationManager, 'get_current_version', return_value=3):
            manager = MigrationManager()
            success = manager.migrate_to_version(2)
            
            assert not success
    
    @patch('routing.database.migrations.get_simple_connection')
    def test_migrate_to_latest(self, mock_get_conn):
        """Test migrating to latest version."""
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_connection
        
        with patch.object(MigrationManager, 'get_current_version', return_value=0):
            manager = MigrationManager()
            success = manager.migrate_to_latest()
            
            assert success
    
    def test_get_migration_manager_singleton(self):
        """Test that get_migration_manager returns singleton."""
        manager1 = get_migration_manager()
        manager2 = get_migration_manager()
        assert manager1 is manager2