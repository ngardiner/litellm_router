"""
Metrics collection and tracking for routing decisions.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from dataclasses import dataclass
from collections import defaultdict, deque
import threading
from ..database.connection import get_simple_connection

logger = logging.getLogger(__name__)


@dataclass
class RoutingMetric:
    """Individual routing metric entry."""
    timestamp: datetime
    model_name: str
    module_name: str
    target_model: str
    response_time_ms: float
    success: bool
    error_message: Optional[str] = None


class MetricsCollector:
    """Collects and aggregates routing metrics."""
    
    def __init__(self, max_memory_metrics: int = 10000):
        self.max_memory_metrics = max_memory_metrics
        self._metrics_buffer = deque(maxlen=max_memory_metrics)
        self._lock = threading.Lock()
        self._counters = defaultdict(int)
        self._response_times = defaultdict(list)
        
    def record_routing_decision(
        self,
        model_name: str,
        module_name: str,
        target_model: str,
        response_time_ms: float,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> None:
        """Record a routing decision metric."""
        metric = RoutingMetric(
            timestamp=datetime.now(timezone.utc),
            model_name=model_name,
            module_name=module_name,
            target_model=target_model,
            response_time_ms=response_time_ms,
            success=success,
            error_message=error_message
        )
        
        with self._lock:
            self._metrics_buffer.append(metric)
            
            # Update counters
            self._counters[f"routing.{module_name}.total"] += 1
            if success:
                self._counters[f"routing.{module_name}.success"] += 1
            else:
                self._counters[f"routing.{module_name}.error"] += 1
            
            # Track response times (keep last 100 per module)
            rt_key = f"routing.{module_name}.response_time"
            if len(self._response_times[rt_key]) >= 100:
                self._response_times[rt_key].pop(0)
            self._response_times[rt_key].append(response_time_ms)
            
        logger.debug(f"Recorded metric: {model_name} -> {target_model} via {module_name}")
    
    def get_metrics_summary(self, since_minutes: int = 60) -> Dict[str, Any]:
        """Get aggregated metrics summary."""
        cutoff_time = datetime.now(timezone.utc).timestamp() - (since_minutes * 60)
        
        with self._lock:
            recent_metrics = [
                m for m in self._metrics_buffer
                if m.timestamp.timestamp() > cutoff_time
            ]
        
        if not recent_metrics:
            return {
                "total_requests": 0,
                "success_rate": 0.0,
                "avg_response_time_ms": 0.0,
                "modules": {},
                "models": {},
                "period_minutes": since_minutes
            }
        
        # Aggregate by module
        module_stats = defaultdict(lambda: {
            "total": 0, "success": 0, "error": 0, "response_times": []
        })
        
        # Aggregate by model
        model_stats = defaultdict(lambda: {"total": 0, "modules": set()})
        
        for metric in recent_metrics:
            # Module stats
            module_stats[metric.module_name]["total"] += 1
            if metric.success:
                module_stats[metric.module_name]["success"] += 1
            else:
                module_stats[metric.module_name]["error"] += 1
            module_stats[metric.module_name]["response_times"].append(metric.response_time_ms)
            
            # Model stats
            model_stats[metric.model_name]["total"] += 1
            model_stats[metric.model_name]["modules"].add(metric.module_name)
        
        # Calculate summary statistics
        total_requests = len(recent_metrics)
        successful_requests = sum(1 for m in recent_metrics if m.success)
        success_rate = successful_requests / total_requests if total_requests > 0 else 0.0
        
        all_response_times = [m.response_time_ms for m in recent_metrics]
        avg_response_time = sum(all_response_times) / len(all_response_times) if all_response_times else 0.0
        
        # Format module statistics
        modules = {}
        for module_name, stats in module_stats.items():
            response_times = stats["response_times"]
            modules[module_name] = {
                "total_requests": stats["total"],
                "successful_requests": stats["success"],
                "failed_requests": stats["error"],
                "success_rate": stats["success"] / stats["total"] if stats["total"] > 0 else 0.0,
                "avg_response_time_ms": sum(response_times) / len(response_times) if response_times else 0.0,
                "min_response_time_ms": min(response_times) if response_times else 0.0,
                "max_response_time_ms": max(response_times) if response_times else 0.0
            }
        
        # Format model statistics
        models = {}
        for model_name, stats in model_stats.items():
            models[model_name] = {
                "total_requests": stats["total"],
                "modules_used": list(stats["modules"])
            }
        
        return {
            "total_requests": total_requests,
            "successful_requests": successful_requests,
            "failed_requests": total_requests - successful_requests,
            "success_rate": success_rate,
            "avg_response_time_ms": avg_response_time,
            "modules": modules,
            "models": models,
            "period_minutes": since_minutes
        }
    
    def get_recent_errors(self, limit: int = 50) -> list:
        """Get recent error metrics."""
        with self._lock:
            errors = [
                {
                    "timestamp": m.timestamp.isoformat(),
                    "model_name": m.model_name,
                    "module_name": m.module_name,
                    "error_message": m.error_message,
                    "response_time_ms": m.response_time_ms
                }
                for m in reversed(self._metrics_buffer)
                if not m.success
            ]
        
        return errors[:limit]
    
    def persist_metrics_to_db(self) -> None:
        """Persist metrics buffer to database for long-term storage."""
        if not self._metrics_buffer:
            return
        
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    # Create metrics table if it doesn't exist
                    cursor.execute("""
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
                        )
                    """)
                    
                    # Create index for efficient queries
                    cursor.execute("""
                        CREATE INDEX IF NOT EXISTS idx_routing_metrics_timestamp 
                        ON routing_metrics(timestamp)
                    """)
                    
                    cursor.execute("""
                        CREATE INDEX IF NOT EXISTS idx_routing_metrics_module 
                        ON routing_metrics(module_name)
                    """)
                    
                    # Insert metrics
                    with self._lock:
                        metrics_to_persist = list(self._metrics_buffer)
                        self._metrics_buffer.clear()
                    
                    for metric in metrics_to_persist:
                        cursor.execute("""
                            INSERT INTO routing_metrics 
                            (timestamp, model_name, module_name, target_model, 
                             response_time_ms, success, error_message)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (
                            metric.timestamp,
                            metric.model_name,
                            metric.module_name,
                            metric.target_model,
                            metric.response_time_ms,
                            metric.success,
                            metric.error_message
                        ))
                    
                    conn.commit()
                    logger.info(f"Persisted {len(metrics_to_persist)} metrics to database")
                    
        except Exception as e:
            logger.error(f"Failed to persist metrics to database: {e}")
    
    def cleanup_old_metrics(self, days_to_keep: int = 30) -> None:
        """Clean up old metrics from database."""
        try:
            with get_simple_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        DELETE FROM routing_metrics 
                        WHERE timestamp < NOW() - INTERVAL '%s days'
                    """, (days_to_keep,))
                    
                    deleted_count = cursor.rowcount
                    conn.commit()
                    
                    if deleted_count > 0:
                        logger.info(f"Cleaned up {deleted_count} old metrics")
                        
        except Exception as e:
            logger.error(f"Failed to cleanup old metrics: {e}")


class PerformanceMonitor:
    """Monitor performance metrics and detect anomalies."""
    
    def __init__(self, metrics_collector: MetricsCollector):
        self.metrics_collector = metrics_collector
        self.alert_thresholds = {
            "max_response_time_ms": 5000,  # 5 seconds
            "min_success_rate": 0.95,      # 95%
            "max_error_rate": 0.05         # 5%
        }
    
    def check_performance_alerts(self) -> list:
        """Check for performance issues and return alerts."""
        metrics = self.metrics_collector.get_metrics_summary(since_minutes=10)
        alerts = []
        
        # Check overall success rate
        if metrics["success_rate"] < self.alert_thresholds["min_success_rate"]:
            alerts.append({
                "type": "low_success_rate",
                "severity": "warning",
                "message": f"Overall success rate ({metrics['success_rate']:.2%}) below threshold",
                "value": metrics["success_rate"],
                "threshold": self.alert_thresholds["min_success_rate"]
            })
        
        # Check response time
        if metrics["avg_response_time_ms"] > self.alert_thresholds["max_response_time_ms"]:
            alerts.append({
                "type": "high_response_time",
                "severity": "warning",
                "message": f"Average response time ({metrics['avg_response_time_ms']:.1f}ms) above threshold",
                "value": metrics["avg_response_time_ms"],
                "threshold": self.alert_thresholds["max_response_time_ms"]
            })
        
        # Check module-specific issues
        for module_name, module_stats in metrics["modules"].items():
            if module_stats["success_rate"] < self.alert_thresholds["min_success_rate"]:
                alerts.append({
                    "type": "module_low_success_rate",
                    "severity": "warning",
                    "message": f"Module {module_name} success rate ({module_stats['success_rate']:.2%}) below threshold",
                    "module": module_name,
                    "value": module_stats["success_rate"],
                    "threshold": self.alert_thresholds["min_success_rate"]
                })
            
            if module_stats["avg_response_time_ms"] > self.alert_thresholds["max_response_time_ms"]:
                alerts.append({
                    "type": "module_high_response_time",
                    "severity": "warning",
                    "message": f"Module {module_name} response time ({module_stats['avg_response_time_ms']:.1f}ms) above threshold",
                    "module": module_name,
                    "value": module_stats["avg_response_time_ms"],
                    "threshold": self.alert_thresholds["max_response_time_ms"]
                })
        
        return alerts
    
    def get_performance_report(self) -> Dict[str, Any]:
        """Generate comprehensive performance report."""
        metrics_1h = self.metrics_collector.get_metrics_summary(since_minutes=60)
        metrics_24h = self.metrics_collector.get_metrics_summary(since_minutes=1440)
        recent_errors = self.metrics_collector.get_recent_errors(limit=20)
        alerts = self.check_performance_alerts()
        
        return {
            "last_hour": metrics_1h,
            "last_24_hours": metrics_24h,
            "recent_errors": recent_errors,
            "active_alerts": alerts,
            "alert_thresholds": self.alert_thresholds,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }


# Global metrics collector instance
_metrics_collector = None
_performance_monitor = None


def get_metrics_collector() -> MetricsCollector:
    """Get the global metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector


def get_performance_monitor() -> PerformanceMonitor:
    """Get the global performance monitor instance."""
    global _performance_monitor
    if _performance_monitor is None:
        _performance_monitor = PerformanceMonitor(get_metrics_collector())
    return _performance_monitor


def record_routing_metric(
    model_name: str,
    module_name: str,
    target_model: str,
    start_time: float,
    success: bool = True,
    error_message: Optional[str] = None
) -> None:
    """Convenience function to record a routing metric."""
    response_time_ms = (time.time() - start_time) * 1000
    get_metrics_collector().record_routing_decision(
        model_name=model_name,
        module_name=module_name,
        target_model=target_model,
        response_time_ms=response_time_ms,
        success=success,
        error_message=error_message
    )