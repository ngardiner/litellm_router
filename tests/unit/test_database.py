"""
Unit tests for database functionality.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from routing.database.connection import DatabaseConnection, get_db_connection
from routing.database.schema import (
    get_current_schema_version, init_database, get_module_config,
    set_module_config, get_routing_rules, register_module
)


class TestDatabaseConnection:
    """Test database connection management."""
    
    def test_initialize_pool_without_database_url(self):
        """Test pool initialization fails without DATABASE_URL."""
        with patch.dict('os.environ', {}, clear=True):
            with pytest.raises(ValueError, match="DATABASE_URL environment variable is required"):
                DatabaseConnection.initialize_pool()
    
    @patch('psycopg2.pool.ThreadedConnectionPool')
    def test_initialize_pool_success(self, mock_pool_class, mock_env_vars):
        """Test successful pool initialization."""
        mock_pool = Mock()
        mock_pool_class.return_value = mock_pool
        
        DatabaseConnection.initialize_pool(min_conn=2, max_conn=10)
        
        mock_pool_class.assert_called_once()
        assert DatabaseConnection._pool == mock_pool
    
    @patch('psycopg2.pool.ThreadedConnectionPool')
    def test_get_connection(self, mock_pool_class, mock_env_vars):
        """Test getting connection from pool."""
        mock_pool = Mock()
        mock_pool_class.return_value = mock_pool
        mock_conn = Mock()
        mock_pool.getconn.return_value = mock_conn
        
        # Initialize pool
        DatabaseConnection.initialize_pool()
        
        # Get connection
        conn = DatabaseConnection.get_connection()
        
        assert conn == mock_conn
        mock_pool.getconn.assert_called_once()
    
    def test_context_manager(self, mock_env_vars):
        """Test database connection context manager."""
        with patch.object(DatabaseConnection, 'get_connection') as mock_get:
            with patch.object(DatabaseConnection, 'return_connection') as mock_return:
                mock_conn = Mock()
                mock_get.return_value = mock_conn
                
                with get_db_connection() as conn:
                    assert conn == mock_conn
                
                mock_get.assert_called_once()
                mock_return.assert_called_once_with(mock_conn)


class TestDatabaseSchema:
    """Test database schema management."""
    
    @patch('routing.database.schema.get_simple_connection')
    def test_get_current_schema_version(self, mock_get_conn):
        """Test getting current schema version."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        mock_cursor.fetchone.return_value = (1,)
        
        version = get_current_schema_version()
        
        assert version == 1
        mock_cursor.execute.assert_called_once_with("SELECT MAX(version) FROM schema_version")
    
    @patch('routing.database.schema.get_simple_connection')
    def test_get_module_config(self, mock_get_conn):
        """Test getting module configuration."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        mock_cursor.fetchall.return_value = [
            ('cooldown_reset_hour', '0'),
            ('fallback_enabled', 'true')
        ]
        
        config = get_module_config('openrouter_free')
        
        expected_config = {
            'cooldown_reset_hour': '0',
            'fallback_enabled': 'true'
        }
        assert config == expected_config
    
    @patch('routing.database.schema.get_simple_connection')
    def test_set_module_config(self, mock_get_conn):
        """Test setting module configuration."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        set_module_config('openrouter_free', 'cooldown_reset_hour', '2')
        
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()
    
    @patch('routing.database.schema.get_simple_connection')
    def test_get_routing_rules(self, mock_get_conn):
        """Test getting routing rules."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        mock_cursor.fetchall.return_value = [
            ('glm-4.5-air', 'openrouter_free', True, 0)
        ]
        
        rules = get_routing_rules()
        
        assert len(rules) == 1
        assert rules[0] == ('glm-4.5-air', 'openrouter_free', True, 0)