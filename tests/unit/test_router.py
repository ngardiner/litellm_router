"""
Unit tests for the main router functionality.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from routing.router import switchboard, register_routing_rule, health_check


class TestSwitchboard:
    """Test the main switchboard routing function."""
    
    @patch('routing.router.get_routing_rules')
    @patch('routing.router.get_module_config')
    @patch('routing.router.get_module_loader')
    def test_switchboard_with_matching_rule(self, mock_get_loader, mock_get_config, mock_get_rules):
        """Test switchboard with a matching routing rule."""
        # Setup mocks
        mock_get_rules.return_value = [('glm-4.5-air', 'openrouter_free', True, 0)]
        mock_get_config.return_value = {'fallback_enabled': 'true'}
        
        mock_loader = Mock()
        mock_loader.call_module_route.return_value = 'glm-4.5-air-free'
        mock_get_loader.return_value = mock_loader
        
        # Test
        result = switchboard('glm-4.5-air', [{'role': 'user', 'content': 'test'}])
        
        # Assertions
        assert result == 'glm-4.5-air-free'
        mock_get_rules.assert_called_once()
        mock_get_config.assert_called_once_with('openrouter_free')
        mock_loader.call_module_route.assert_called_once()
    
    @patch('routing.router.get_routing_rules')
    def test_switchboard_no_matching_rule(self, mock_get_rules):
        """Test switchboard with no matching routing rule."""
        mock_get_rules.return_value = []
        
        result = switchboard('unknown-model', [])
        
        assert result is None
    
    @patch('routing.router.get_routing_rules')
    @patch('routing.router.get_module_config')
    @patch('routing.router.get_module_loader')
    def test_switchboard_module_returns_none(self, mock_get_loader, mock_get_config, mock_get_rules):
        """Test switchboard when module returns None."""
        mock_get_rules.return_value = [('test-model', 'test_module', True, 0)]
        mock_get_config.return_value = {}
        
        mock_loader = Mock()
        mock_loader.call_module_route.return_value = None
        mock_get_loader.return_value = mock_loader
        
        result = switchboard('test-model', [])
        
        assert result is None
    
    @patch('routing.router.get_routing_rules')
    def test_switchboard_exception_handling(self, mock_get_rules):
        """Test switchboard handles exceptions gracefully."""
        mock_get_rules.side_effect = Exception("Database error")
        
        result = switchboard('test-model', [])
        
        assert result is None
    
    @patch('routing.database.connection.get_simple_connection')
    def test_register_routing_rule(self, mock_get_conn):
        """Test registering a routing rule."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        register_routing_rule('test-model', 'test_module', 5)
        
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()


class TestHealthCheck:
    """Test health check functionality."""
    
    @patch('routing.router.get_routing_rules')
    @patch('routing.router.get_module_loader')
    def test_health_check_healthy(self, mock_get_loader, mock_get_rules):
        """Test health check when everything is healthy."""
        mock_get_rules.return_value = [('test-model', 'test_module', True, 0)]
        
        mock_loader = Mock()
        mock_loader.discover_modules.return_value = ['test_module']
        mock_loader.validate_module_interface.return_value = True
        mock_get_loader.return_value = mock_loader
        
        result = health_check()
        
        assert result['status'] == 'healthy'
        assert result['database'] == 'connected'
        assert result['routing_rules_count'] == 1
        assert result['modules']['test_module'] == 'valid'
    
    @patch('routing.router.get_routing_rules')
    def test_health_check_database_error(self, mock_get_rules):
        """Test health check with database error."""
        mock_get_rules.side_effect = Exception("Connection failed")
        
        result = health_check()
        
        assert result['status'] == 'unhealthy'
        assert result['database'] == 'error'
        assert len(result['errors']) > 0
    
    @patch('routing.router.get_routing_rules')
    @patch('routing.router.get_module_loader')
    def test_health_check_module_validation_failure(self, mock_get_loader, mock_get_rules):
        """Test health check with module validation failure."""
        mock_get_rules.return_value = []
        
        mock_loader = Mock()
        mock_loader.discover_modules.return_value = ['invalid_module']
        mock_loader.validate_module_interface.return_value = False
        mock_get_loader.return_value = mock_loader
        
        result = health_check()
        
        assert result['modules']['invalid_module'] == 'invalid'
        assert any('failed validation' in error for error in result['errors'])