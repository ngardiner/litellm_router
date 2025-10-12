"""
Command-line interface for the routing platform.
"""

import sys
import logging
import argparse
from .database.schema import init_database
from .router import auto_discover_and_register_modules, health_check, register_routing_rule
from .database.schema import set_module_config

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
    
    args = parser.parse_args()
    
    if args.command == 'init-db':
        cmd_init_db()
    elif args.command == 'discover-modules':
        cmd_discover_modules()
    elif args.command == 'health-check':
        cmd_health_check()
    elif args.command == 'setup-example':
        cmd_setup_example()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()