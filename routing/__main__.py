"""
Command-line interface for the routing platform.
"""

import sys
import logging
import argparse
from .database.schema import init_database
from .router import auto_discover_and_register_modules, health_check, register_routing_rule
from .database.schema import set_module_config
from .database.migrations import get_migration_manager
from .monitoring.metrics import get_metrics_collector, get_performance_monitor
from .monitoring.circuit_breaker import get_circuit_breaker_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def cmd_init_db():
    """Initialize the database."""
    try:
        init_database()
        logger.info("Database initialization completed successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        sys.exit(1)


def cmd_discover_modules():
    """Discover and register modules."""
    try:
        auto_discover_and_register_modules()
        logger.info("Module discovery completed successfully")
    except Exception as e:
        logger.error(f"Module discovery failed: {e}")
        sys.exit(1)


def cmd_health_check():
    """Perform health check."""
    try:
        result = health_check()
        print(f"Status: {result['status']}")
        print(f"Database: {result['database']}")
        print(f"Modules: {result['modules']}")
        if result['errors']:
            print(f"Errors: {result['errors']}")
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        sys.exit(1)


def cmd_setup_example():
    """Set up example configuration."""
    try:
        # Initialize database
        init_database()
        
        # Discover modules
        auto_discover_and_register_modules()
        
        # Set up OpenRouter Free module configuration
        set_module_config('openrouter_free', 'cooldown_reset_hour', '0')
        set_module_config('openrouter_free', 'cooldown_timezone', 'UTC')
        set_module_config('openrouter_free', 'fallback_enabled', 'true')
        
        # Register example routing rule
        register_routing_rule('glm-4.5-air', 'openrouter_free', 0)
        
        logger.info("Example setup completed successfully")
        logger.info("You can now use 'glm-4.5-air' as a model name with the routing platform")
        
    except Exception as e:
        logger.error(f"Example setup failed: {e}")
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description='LiteLLM Router Platform CLI')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Init database command
    subparsers.add_parser('init-db', help='Initialize the database')
    
    # Discover modules command
    subparsers.add_parser('discover-modules', help='Discover and register modules')
    
    # Health check command
    subparsers.add_parser('health-check', help='Perform health check')
    
    # Setup example command
    subparsers.add_parser('setup-example', help='Set up example configuration')
    
    # Migration commands
    subparsers.add_parser('migrate', help='Run database migrations')
    subparsers.add_parser('migration-status', help='Show migration status')
    
    # Monitoring commands
    subparsers.add_parser('metrics-summary', help='Show routing metrics summary')
    subparsers.add_parser('circuit-breaker-status', help='Show circuit breaker status')
    subparsers.add_parser('performance-report', help='Generate performance report')
    
    args = parser.parse_args()
    
    if args.command == 'init-db':
        cmd_init_db()
    elif args.command == 'discover-modules':
        cmd_discover_modules()
    elif args.command == 'health-check':
        cmd_health_check()
    elif args.command == 'setup-example':
        cmd_setup_example()
    elif args.command == 'migrate':
        cmd_migrate()
    elif args.command == 'migration-status':
        cmd_migration_status()
    elif args.command == 'metrics-summary':
        cmd_metrics_summary()
    elif args.command == 'circuit-breaker-status':
        cmd_circuit_breaker_status()
    elif args.command == 'performance-report':
        cmd_performance_report()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

def cmd_migrate():
    """Run database migrations."""
    try:
        migration_manager = get_migration_manager()
        status = migration_manager.get_migration_status()
        
        if not status["needs_migration"]:
            logger.info("Database is up to date")
            return
        
        logger.info(f"Migrating from version {status['current_version']} to {status['target_version']}")
        success = migration_manager.migrate_to_latest()
        
        if success:
            logger.info("Database migration completed successfully")
        else:
            logger.error("Database migration failed")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)


def cmd_migration_status():
    """Show migration status."""
    try:
        migration_manager = get_migration_manager()
        status = migration_manager.get_migration_status()
        
        print(f"Current version: {status['current_version']}")
        print(f"Target version: {status['target_version']}")
        print(f"Needs migration: {status['needs_migration']}")
        
        if status['applied_migrations']:
            print("\nApplied migrations:")
            for migration in status['applied_migrations']:
                print(f"  {migration['version']}: {migration['description']}")
        
        if status['pending_migrations']:
            print("\nPending migrations:")
            for migration in status['pending_migrations']:
                print(f"  {migration['version']}: {migration['description']}")
    except Exception as e:
        logger.error(f"Failed to get migration status: {e}")
        sys.exit(1)


def cmd_metrics_summary():
    """Show metrics summary."""
    try:
        metrics_collector = get_metrics_collector()
        summary = metrics_collector.get_metrics_summary(since_minutes=60)
        
        print("=== Routing Metrics Summary (Last Hour) ===")
        print(f"Total requests: {summary['total_requests']}")
        print(f"Success rate: {summary['success_rate']:.2%}")
        print(f"Average response time: {summary['avg_response_time_ms']:.1f}ms")
        
        if summary['modules']:
            print("\nModule statistics:")
            for module_name, stats in summary['modules'].items():
                print(f"  {module_name}:")
                print(f"    Requests: {stats['total_requests']}")
                print(f"    Success rate: {stats['success_rate']:.2%}")
                print(f"    Avg response time: {stats['avg_response_time_ms']:.1f}ms")
    except Exception as e:
        logger.error(f"Failed to get metrics summary: {e}")
        sys.exit(1)


def cmd_circuit_breaker_status():
    """Show circuit breaker status."""
    try:
        cb_manager = get_circuit_breaker_manager()
        statuses = cb_manager.get_all_statuses()
        
        if not statuses:
            print("No circuit breakers configured")
            return
        
        print("=== Circuit Breaker Status ===")
        for module_name, status in statuses.items():
            print(f"{module_name}: {status['state'].upper()}")
            print(f"  Failures: {status['failure_count']}")
            if status['last_failure_time'] > 0:
                import datetime
                failure_time = datetime.datetime.fromtimestamp(status['last_failure_time'])
                print(f"  Last failure: {failure_time}")
    except Exception as e:
        logger.error(f"Failed to get circuit breaker status: {e}")
        sys.exit(1)


def cmd_performance_report():
    """Generate performance report."""
    try:
        performance_monitor = get_performance_monitor()
        report = performance_monitor.get_performance_report()
        
        print("=== Performance Report ===")
        
        # Last hour stats
        hour_stats = report['last_hour']
        print(f"\nLast Hour:")
        print(f"  Requests: {hour_stats['total_requests']}")
        print(f"  Success rate: {hour_stats['success_rate']:.2%}")
        print(f"  Avg response time: {hour_stats['avg_response_time_ms']:.1f}ms")
        
        # Last 24 hours stats
        day_stats = report['last_24_hours']
        print(f"\nLast 24 Hours:")
        print(f"  Requests: {day_stats['total_requests']}")
        print(f"  Success rate: {day_stats['success_rate']:.2%}")
        print(f"  Avg response time: {day_stats['avg_response_time_ms']:.1f}ms")
        
        # Active alerts
        if report['active_alerts']:
            print(f"\nActive Alerts ({len(report['active_alerts'])}):")
            for alert in report['active_alerts']:
                print(f"  {alert['severity'].upper()}: {alert['message']}")
        else:
            print("\nNo active alerts")
            
    except Exception as e:
        logger.error(f"Failed to generate performance report: {e}")
        sys.exit(1)
