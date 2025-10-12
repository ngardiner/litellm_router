"""
Unit tests for circuit breaker functionality.
"""

import pytest
import time
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from routing.monitoring.circuit_breaker import (
    CircuitBreaker, CircuitBreakerConfig, CircuitBreakerManager, 
    CircuitBreakerOpenError, get_circuit_breaker_manager, CircuitState
)


class TestCircuitBreaker:
    """Test circuit breaker functionality."""
    
    def test_circuit_breaker_normal_operation(self):
        """Test circuit breaker in normal operation."""
        config = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=5)
        breaker = CircuitBreaker("test-breaker", config)
        
        # Test successful calls
        result = breaker.call(lambda x: x * 2, 5)
        assert result == 10
        assert breaker.failure_count == 0
    
    def test_circuit_breaker_failure_threshold(self):
        """Test circuit breaker opening after failure threshold."""
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=5)
        breaker = CircuitBreaker("test-breaker", config)
        
        # Cause failures to reach threshold
        for i in range(2):
            try:
                breaker.call(lambda: 1/0)  # Division by zero
            except ZeroDivisionError:
                pass
        
        # Circuit should now be open
        with pytest.raises(CircuitBreakerOpenError):
            breaker.call(lambda: "should not execute")
    
    def test_circuit_breaker_half_open_recovery(self):
        """Test circuit breaker recovery through half-open state."""
        config = CircuitBreakerConfig(
            failure_threshold=1, 
            recovery_timeout=0,  # Immediate recovery attempt
            success_threshold=2
        )
        breaker = CircuitBreaker("test-breaker", config)
        
        # Cause failure to open circuit
        try:
            breaker.call(lambda: 1/0)
        except ZeroDivisionError:
            pass
        
        # Wait for recovery timeout (0 seconds)
        time.sleep(0.1)
        
        # First success call should move to half-open
        result = breaker.call(lambda: "success1")
        assert result == "success1"
        
        # Second success should close the circuit
        result = breaker.call(lambda: "success2")
        assert result == "success2"
        
        # Verify circuit is closed
        assert breaker.state == CircuitState.CLOSED
    
    def test_circuit_breaker_timeout(self):
        """Test circuit breaker timeout functionality."""
        config = CircuitBreakerConfig(timeout_seconds=0.1)
        breaker = CircuitBreaker("test-breaker", config)
        
        # Function that takes longer than timeout
        def slow_function():
            time.sleep(0.2)
            return "should timeout"
        
        with pytest.raises(TimeoutError):
            breaker.call(slow_function)
    
    def test_circuit_breaker_status(self):
        """Test getting circuit breaker status."""
        config = CircuitBreakerConfig(failure_threshold=5)
        breaker = CircuitBreaker("test-breaker", config)
        
        status = breaker.get_status()
        
        assert status["name"] == "test-breaker"
        assert status["state"] == "closed"
        assert status["failure_count"] == 0
        assert status["config"]["failure_threshold"] == 5
    
    def test_circuit_breaker_reset(self):
        """Test manually resetting circuit breaker."""
        config = CircuitBreakerConfig(failure_threshold=1)
        breaker = CircuitBreaker("test-breaker", config)
        
        # Cause failure to open circuit
        try:
            breaker.call(lambda: 1/0)
        except ZeroDivisionError:
            pass
        
        assert breaker.state == CircuitState.OPEN
        
        # Reset the breaker
        breaker.reset()
        
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0


class TestCircuitBreakerManager:
    """Test circuit breaker manager functionality."""
    
    def test_get_or_create_breaker(self):
        """Test getting or creating circuit breakers."""
        manager = CircuitBreakerManager()
        
        # Get breaker (should create new one)
        breaker1 = manager.get_breaker("test-module")
        assert breaker1.name == "test-module"
        
        # Get same breaker again (should return existing)
        breaker2 = manager.get_breaker("test-module")
        assert breaker1 is breaker2
    
    def test_call_with_breaker(self):
        """Test calling function with circuit breaker protection."""
        manager = CircuitBreakerManager()
        
        # Successful call
        result = manager.call_with_breaker("test-module", lambda x: x * 3, 4)
        assert result == 12
        
        # Failed call
        with pytest.raises(ValueError):
            manager.call_with_breaker("test-module", lambda: int("invalid"))
    
    def test_reset_breaker(self):
        """Test resetting circuit breakers."""
        manager = CircuitBreakerManager()
        
        # Create a breaker and cause it to fail
        config = CircuitBreakerConfig(failure_threshold=1)
        manager.configure_breaker("test-module", config)
        
        try:
            manager.call_with_breaker("test-module", lambda: 1/0)
        except ZeroDivisionError:
            pass
        
        # Reset the breaker
        success = manager.reset_breaker("test-module")
        assert success
        
        # Should be able to call again
        result = manager.call_with_breaker("test-module", lambda: "success")
        assert result == "success"
    
    def test_reset_nonexistent_breaker(self):
        """Test resetting a nonexistent circuit breaker."""
        manager = CircuitBreakerManager()
        success = manager.reset_breaker("nonexistent")
        assert not success
    
    def test_get_all_statuses(self):
        """Test getting status of all circuit breakers."""
        manager = CircuitBreakerManager()
        
        # Create some breakers
        manager.get_breaker("module1")
        manager.get_breaker("module2")
        
        statuses = manager.get_all_statuses()
        
        assert "module1" in statuses
        assert "module2" in statuses
        assert statuses["module1"]["state"] == "closed"
        assert statuses["module2"]["state"] == "closed"
    
    def test_reset_all_breakers(self):
        """Test resetting all circuit breakers."""
        manager = CircuitBreakerManager()
        
        # Create and open some breakers
        config = CircuitBreakerConfig(failure_threshold=1)
        manager.configure_breaker("module1", config)
        manager.configure_breaker("module2", config)
        
        # Cause failures
        for module in ["module1", "module2"]:
            try:
                manager.call_with_breaker(module, lambda: 1/0)
            except ZeroDivisionError:
                pass
        
        # Reset all
        manager.reset_all_breakers()
        
        # Verify all are closed
        statuses = manager.get_all_statuses()
        assert statuses["module1"]["state"] == "closed"
        assert statuses["module2"]["state"] == "closed"
    
    @patch('routing.monitoring.circuit_breaker.get_simple_connection')
    def test_load_configurations_from_db(self, mock_get_conn):
        """Test loading circuit breaker configurations from database."""
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_connection
        
        # Mock database response
        mock_cursor.fetchall.return_value = [
            ("test_module", 5, 60, 3, 30.0)
        ]
        
        manager = CircuitBreakerManager()
        manager.load_configurations_from_db()
        
        # Verify configuration was loaded
        assert "test_module" in manager.breakers
        breaker = manager.breakers["test_module"]
        assert breaker.config.failure_threshold == 5
        assert breaker.config.recovery_timeout == 60
    
    def test_get_circuit_breaker_manager_singleton(self):
        """Test that get_circuit_breaker_manager returns singleton."""
        manager1 = get_circuit_breaker_manager()
        manager2 = get_circuit_breaker_manager()
        assert manager1 is manager2