"""
Circuit breaker pattern implementation for routing modules.
"""

import time
import logging
from typing import Dict, Any, Optional, Callable
from enum import Enum
from dataclasses import dataclass
from threading import Lock
from ..database.connection import get_simple_connection

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"          # Failing, requests blocked
    HALF_OPEN = "half_open" # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5      # Number of failures to open circuit
    recovery_timeout: int = 60      # Seconds before trying half-open
    success_threshold: int = 3      # Successes needed to close from half-open
    timeout_seconds: float = 30.0   # Request timeout


class CircuitBreaker:
    """Circuit breaker for protecting against cascading failures."""
    
    def __init__(self, name: str, config: CircuitBreakerConfig):
        self.name = name
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0
        self.lock = Lock()
        
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        with self.lock:
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time < self.config.recovery_timeout:
                    raise CircuitBreakerOpenError(f"Circuit breaker {self.name} is OPEN")
                else:
                    # Move to half-open state
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info(f"Circuit breaker {self.name} moved to HALF_OPEN")
        
        try:
            # Execute the function with timeout
            start_time = time.time()
            result = func(*args, **kwargs)
            execution_time = time.time() - start_time
            
            # Check for timeout
            if execution_time > self.config.timeout_seconds:
                raise TimeoutError(f"Function execution exceeded {self.config.timeout_seconds}s")
            
            # Record success
            self._record_success()
            return result
            
        except Exception as e:
            self._record_failure()
            raise
    
    def _record_success(self) -> None:
        """Record successful execution."""
        with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    logger.info(f"Circuit breaker {self.name} moved to CLOSED")
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0  # Reset failure count on success
    
    def _record_failure(self) -> None:
        """Record failed execution."""
        with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.state == CircuitState.CLOSED:
                if self.failure_count >= self.config.failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.warning(f"Circuit breaker {self.name} moved to OPEN after {self.failure_count} failures")
            elif self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker {self.name} moved back to OPEN from HALF_OPEN")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current circuit breaker status."""
        with self.lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "last_failure_time": self.last_failure_time,
                "config": {
                    "failure_threshold": self.config.failure_threshold,
                    "recovery_timeout": self.config.recovery_timeout,
                    "success_threshold": self.config.success_threshold,
                    "timeout_seconds": self.config.timeout_seconds
                }
            }
    
    def reset(self) -> None:
        """Manually reset circuit breaker to closed state."""
        with self.lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.last_failure_time = 0
            logger.info(f"Circuit breaker {self.name} manually reset to CLOSED")


class CircuitBreakerOpenError(Exception):
    """Exception raised when circuit breaker is open."""
    pass


class CircuitBreakerManager:
    """Manages circuit breakers for routing modules."""
    
    def __init__(self):
        self.breakers: Dict[str, CircuitBreaker] = {}
        self.lock = Lock()
    
    def get_breaker(self, module_name: str, config: Optional[CircuitBreakerConfig] = None) -> CircuitBreaker:
        """Get or create circuit breaker for module."""
        with self.lock:
            if module_name not in self.breakers:
                if config is None:
                    config = CircuitBreakerConfig()
                self.breakers[module_name] = CircuitBreaker(module_name, config)
            return self.breakers[module_name]
    
    def call_with_breaker(
        self, 
        module_name: str, 
        func: Callable, 
        *args, 
        config: Optional[CircuitBreakerConfig] = None,
        **kwargs
    ) -> Any:
        """Execute function with circuit breaker protection."""
        breaker = self.get_breaker(module_name, config)
        return breaker.call(func, *args, **kwargs)
    
    def get_all_statuses(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all circuit breakers."""
        with self.lock:
            return {name: breaker.get_status() for name, breaker in self.breakers.items()}
    
    def reset_breaker(self, module_name: str) -> bool:
        """Reset specific circuit breaker."""
        with self.lock:
            if module_name in self.breakers:
                self.breakers[module_name].reset()
                return True
            return False
    
    def reset_all_breakers(self) -> None:
        """Reset all circuit breakers."""
        with self.lock:
            for breaker in self.breakers.values():
                breaker.reset()
            logger.info("All circuit breakers reset")
    
    def configure_breaker(self, module_name: str, config: CircuitBreakerConfig) -> None:
        """Configure circuit breaker for a module."""
        with self.lock:
            if module_name in self.breakers:
                # Update existing breaker config
                breaker = self.breakers[module_name]
                breaker.config = config
                logger.info(f"Updated circuit breaker config for {module_name}")
            else:
                # Create new breaker with config
                self.breakers[module_name] = CircuitBreaker(module_name, config)
                logger.info(f"Created circuit breaker for {module_name}")
    
    def load_configurations_from_db(self) -> None:
        """Load circuit breaker configurations from database."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    # Create circuit breaker config table if not exists
                    cursor.execute("""
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
                        )
                    """)
                    
                    # Load configurations
                    cursor.execute("""
                        SELECT module_name, failure_threshold, recovery_timeout, 
                               success_threshold, timeout_seconds
                        FROM circuit_breaker_configs 
                        WHERE enabled = true
                    """)
                    
                    for row in cursor.fetchall():
                        module_name, failure_threshold, recovery_timeout, success_threshold, timeout_seconds = row
                        config = CircuitBreakerConfig(
                            failure_threshold=failure_threshold,
                            recovery_timeout=recovery_timeout,
                            success_threshold=success_threshold,
                            timeout_seconds=timeout_seconds
                        )
                        self.configure_breaker(module_name, config)
                    
                    logger.info("Loaded circuit breaker configurations from database")
                    
        except Exception as e:
            logger.error(f"Failed to load circuit breaker configurations: {e}")
    
    def save_configuration_to_db(self, module_name: str, config: CircuitBreakerConfig) -> None:
        """Save circuit breaker configuration to database."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO circuit_breaker_configs 
                        (module_name, failure_threshold, recovery_timeout, success_threshold, timeout_seconds, updated_at)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (module_name) 
                        DO UPDATE SET 
                            failure_threshold = EXCLUDED.failure_threshold,
                            recovery_timeout = EXCLUDED.recovery_timeout,
                            success_threshold = EXCLUDED.success_threshold,
                            timeout_seconds = EXCLUDED.timeout_seconds,
                            updated_at = NOW()
                    """, (
                        module_name,
                        config.failure_threshold,
                        config.recovery_timeout,
                        config.success_threshold,
                        config.timeout_seconds
                    ))
                    conn.commit()
                    logger.info(f"Saved circuit breaker configuration for {module_name}")
                    
        except Exception as e:
            logger.error(f"Failed to save circuit breaker configuration: {e}")


# Global circuit breaker manager
_circuit_breaker_manager = None


def get_circuit_breaker_manager() -> CircuitBreakerManager:
    """Get the global circuit breaker manager instance."""
    global _circuit_breaker_manager
    if _circuit_breaker_manager is None:
        _circuit_breaker_manager = CircuitBreakerManager()
        # Load configurations from database
        try:
            _circuit_breaker_manager.load_configurations_from_db()
        except Exception as e:
            logger.warning(f"Could not load circuit breaker configs from DB: {e}")
    return _circuit_breaker_manager