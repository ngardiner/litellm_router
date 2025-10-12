"""
Pytest configuration and shared fixtures.
"""

import os
import pytest
import tempfile
from unittest.mock import Mock, patch


@pytest.fixture
def mock_db_connection():
    """Mock database connection for testing."""
    mock_conn = Mock()
    mock_cursor = Mock()
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


@pytest.fixture
def test_database_url():
    """Test database URL for integration tests."""
    return "postgresql://test_user:test_pass@localhost:5432/test_litellm"


@pytest.fixture
def mock_env_vars():
    """Mock environment variables."""
    with patch.dict(os.environ, {
        'DATABASE_URL': 'postgresql://test_user:test_pass@localhost:5432/test_litellm'
    }):
        yield


@pytest.fixture
def sample_messages():
    """Sample conversation messages for testing."""
    return [
        {"role": "user", "content": "Hello, how are you?"},
        {"role": "assistant", "content": "I'm doing well, thank you!"},
        {"role": "user", "content": "Can you help me with a coding problem?"}
    ]


@pytest.fixture
def sample_module_config():
    """Sample module configuration for testing."""
    return {
        "cooldown_reset_hour": 0,
        "cooldown_timezone": "UTC",
        "fallback_enabled": True
    }