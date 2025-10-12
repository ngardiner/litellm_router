"""
Unit tests for metrics collection functionality.
"""

import pytest
import time
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from routing.monitoring.metrics import (
    MetricsCollector, get_metrics_collector, record_routing_metric
)


class TestMetricsCollector:
    """Test metrics collection functionality."""
    
    def test_record_and_retrieve_metrics(self):
        """Test recording and retrieving metrics."""
        collector = MetricsCollector(max_memory_metrics=100)
        
        # Record some test metrics
        collector.record_routing_decision(
            model_name="test-model",
            module_name="test_module",
            target_model="test-target",
            response_time_ms=150.5,
            success=True
        )
        
        collector.record_routing_decision(
            model_name="test-model-2",
            module_name="test_module",
            target_model="test-target-2",
            response_time_ms=300.0,
            success=False,
            error_message="Test error"
        )
        
        # Get summary
        summary = collector.get_metrics_summary(since_minutes=60)
        
        assert summary["total_requests"] == 2
        assert summary["successful_requests"] == 1
        assert summary["failed_requests"] == 1
        assert summary["success_rate"] == 0.5
        assert "test_module" in summary["modules"]
        
        module_stats = summary["modules"]["test_module"]
        assert module_stats["total_requests"] == 2
        assert module_stats["successful_requests"] == 1
        assert module_stats["failed_requests"] == 1
        assert module_stats["success_rate"] == 0.5
    
    def test_recent_errors(self):
        """Test retrieving recent errors."""
        collector = MetricsCollector()
        
        # Record successful and failed metrics
        collector.record_routing_decision(
            model_name="success-model",
            module_name="test_module",
            target_model="success-target",
            response_time_ms=100.0,
            success=True
        )
        
        collector.record_routing_decision(
            model_name="error-model",
            module_name="test_module",
            target_model="error-target",
            response_time_ms=500.0,
            success=False,
            error_message="Test error message"
        )
        
        errors = collector.get_recent_errors(limit=10)
        
        assert len(errors) == 1
        assert errors[0]["model_name"] == "error-model"
        assert errors[0]["error_message"] == "Test error message"
    
    @patch('routing.monitoring.metrics.get_simple_connection')
    def test_persist_metrics_to_db(self, mock_get_conn):
        """Test persisting metrics to database."""
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.__enter__.return_value = mock_connection
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_connection
        
        collector = MetricsCollector()
        collector.record_routing_decision(
            model_name="test-model",
            module_name="test_module",
            target_model="test-target",
            response_time_ms=100.0,
            success=True
        )
        
        collector.persist_metrics_to_db()
        
        # Verify database operations were called
        assert mock_cursor.execute.call_count >= 2  # CREATE TABLE + INSERT
        mock_connection.commit.assert_called_once()
    
    @patch('routing.monitoring.metrics.get_metrics_collector')
    def test_record_routing_metric_convenience_function(self, mock_get_collector):
        """Test the convenience function for recording metrics."""
        mock_collector = MagicMock()
        mock_get_collector.return_value = mock_collector
        
        start_time = time.time() - 0.1  # 100ms ago
        
        record_routing_metric(
            model_name="test-model",
            module_name="test_module",
            target_model="test-target",
            start_time=start_time,
            success=True
        )
        
        # Verify the collector was called
        mock_collector.record_routing_decision.assert_called_once()
        args = mock_collector.record_routing_decision.call_args[1]
        
        assert args["model_name"] == "test-model"
        assert args["module_name"] == "test_module"
        assert args["target_model"] == "test-target"
        assert args["response_time_ms"] > 90  # Should be around 100ms
        assert args["success"] == True
    
    def test_get_metrics_collector_singleton(self):
        """Test that get_metrics_collector returns singleton."""
        collector1 = get_metrics_collector()
        collector2 = get_metrics_collector()
        assert collector1 is collector2