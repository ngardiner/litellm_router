"""
Unit tests for the OpenRouter Free module.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, MagicMock
from routing.modules.openrouter_free import (
    route, check_cooldown, set_cooldown, calculate_next_reset_time,
    handle_rate_limit_error, clear_expired_cooldowns, get_cooldown_status
)


class TestOpenRouterFreeRoute:
    """Test the main route function."""
    
    @patch('routing.modules.openrouter_free.check_cooldown')
    def test_route_free_model_available(self, mock_check_cooldown):
        """Test routing to free model when available."""
        mock_check_cooldown.return_value = False
        
        config = {'fallback_enabled': 'true'}
        result = route('glm-4.5-air', [], config)
        
        assert result == 'glm-4.5-air-free'
        mock_check_cooldown.assert_called_once_with('glm-4.5-air-free')
    
    @patch('routing.modules.openrouter_free.check_cooldown')
    def test_route_free_model_on_cooldown_fallback_enabled(self, mock_check_cooldown):
        """Test routing to paid model when free is on cooldown and fallback enabled."""
        mock_check_cooldown.return_value = True
        
        config = {'fallback_enabled': 'true'}
        result = route('glm-4.5-air', [], config)
        
        assert result == 'glm-4.5-air-paid'
    
    @patch('routing.modules.openrouter_free.check_cooldown')
    def test_route_free_model_on_cooldown_fallback_disabled(self, mock_check_cooldown):
        """Test returning None when free is on cooldown and fallback disabled."""
        mock_check_cooldown.return_value = True
        
        config = {'fallback_enabled': 'false'}
        result = route('glm-4.5-air', [], config)
        
        assert result is None
    
    def test_route_with_empty_config(self):
        """Test route function with empty configuration."""
        with patch('routing.modules.openrouter_free.check_cooldown', return_value=False):
            result = route('test-model', [], {})
            assert result == 'test-model-free'


class TestCooldownManagement:
    """Test cooldown management functions."""
    
    @patch('routing.modules.openrouter_free.get_simple_connection')
    def test_check_cooldown_active(self, mock_get_conn):
        """Test checking cooldown when model is on cooldown."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_cursor.fetchone.return_value = (future_time,)
        
        result = check_cooldown('test-model-free')
        
        assert result is True
    
    @patch('routing.modules.openrouter_free.get_simple_connection')
    def test_check_cooldown_not_active(self, mock_get_conn):
        """Test checking cooldown when model is not on cooldown."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        mock_cursor.fetchone.return_value = None
        
        result = check_cooldown('test-model-free')
        
        assert result is False
    
    @patch('routing.modules.openrouter_free.get_simple_connection')
    def test_set_cooldown(self, mock_get_conn):
        """Test setting a cooldown."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        cooldown_time = datetime.now(timezone.utc) + timedelta(hours=1)
        set_cooldown('test-model-free', cooldown_time)
        
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()
    
    def test_calculate_next_reset_time_future(self):
        """Test calculating next reset time when it's in the future today."""
        # Mock current time as 10 AM UTC
        with patch('routing.modules.openrouter_free.datetime') as mock_dt:
            mock_now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now
            
            # Reset at 2 PM (14:00)
            next_reset = calculate_next_reset_time(reset_hour=14)
            
            expected = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
            assert next_reset == expected
    
    def test_calculate_next_reset_time_past(self):
        """Test calculating next reset time when it's in the past today."""
        # Mock current time as 10 AM UTC
        with patch('routing.modules.openrouter_free.datetime') as mock_dt:
            mock_now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now
            
            # Reset at 8 AM (already passed)
            next_reset = calculate_next_reset_time(reset_hour=8)
            
            expected = datetime(2024, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
            assert next_reset == expected


class TestUtilityFunctions:
    """Test utility functions."""
    
    @patch('routing.modules.openrouter_free.calculate_next_reset_time')
    @patch('routing.modules.openrouter_free.set_cooldown')
    def test_handle_rate_limit_error(self, mock_set_cooldown, mock_calc_reset):
        """Test handling rate limit error."""
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_calc_reset.return_value = future_time
        
        config = {'cooldown_reset_hour': '0', 'cooldown_timezone': 'UTC'}
        handle_rate_limit_error('test-model-free', config)
        
        mock_calc_reset.assert_called_once_with(0, 'UTC')
        mock_set_cooldown.assert_called_once_with('test-model-free', future_time)
    
    @patch('routing.modules.openrouter_free.get_simple_connection')
    def test_clear_expired_cooldowns(self, mock_get_conn):
        """Test clearing expired cooldowns."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 3
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        result = clear_expired_cooldowns()
        
        assert result == 3
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()
    
    @patch('routing.modules.openrouter_free.get_simple_connection')
    def test_get_cooldown_status(self, mock_get_conn):
        """Test getting cooldown status."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_cursor.fetchall.return_value = [('test-model-free', future_time)]
        mock_cursor.fetchone.return_value = (5,)
        
        result = get_cooldown_status()
        
        assert result['active_count'] == 1
        assert result['total_cooldown_records'] == 5
        assert 'test-model-free' in result['active_cooldowns']