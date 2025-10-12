# LiteLLM Modular Router Platform

A database-driven, modular routing platform for LiteLLM that enables dynamic routing logic through pluggable modules.

## Features

- **Database-Driven Configuration**: All routing rules stored in PostgreSQL
- **Modular Architecture**: Pluggable routing modules for different strategies
- **Web Management Interface**: Configure routing rules through a web UI
- **Transparent Fallback**: Seamless failover between model tiers
- **Auto-Discovery**: Automatic module detection and registration
- **Enterprise-Ready**: Comprehensive CI/CD, security scanning, and monitoring

## Quick Start

### Prerequisites

- Python 3.9+
- PostgreSQL database
- Docker (optional, for containerized deployment)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/ngardiner/litellm_router
cd litellm_router
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up environment variables:
```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/litellm"
```

4. Initialize the database:
```bash
python -c "from routing.database.schema import init_database; init_database()"
```

### LiteLLM Integration

Configure LiteLLM to use the routing platform:

```yaml
# litellm_config.yaml
router_settings:
  router_logic_file: "/path/to/routing/router.py"
  router_logic: "router.switchboard"

model_list:
  # Free tier models
  - model_name: "glm-4.5-air-free"
    litellm_params:
      model: "openrouter/zhipuai/glm-4.5-air:free"
      api_key: "os.environ/OPENROUTER_API_KEY"
      
  # Paid tier models  
  - model_name: "glm-4.5-air-paid"
    litellm_params:
      model: "openrouter/zhipuai/glm-4.5-air"
      api_key: "os.environ/OPENROUTER_API_KEY"
```

### Docker Deployment

Use the provided Docker configuration:

```bash
# Start the complete stack
docker-compose up -d

# Or start individual services
docker-compose up -d litellm_db litellm litellm_router_web
```

**Services:**
- **LiteLLM Router**: http://localhost:4060 (main routing service)
- **Web Management Interface**: http://localhost:4061 (dashboard)
- **PostgreSQL Database**: localhost:5432

**Environment Configuration:**
```bash
# Set your database password
export POSTGRES_PASSWORD=your_secure_password

# The services will automatically use:
# DATABASE_URL=postgresql://litellm:${POSTGRES_PASSWORD}@litellm_db:5432/litellm
```

## Architecture

### Core Components

- **Router (`routing/router.py`)**: Main switchboard function for routing decisions
- **Database Layer (`routing/database/`)**: Schema management and connections  
- **Modules (`routing/modules/`)**: Pluggable routing strategies
- **Web Interface (`web_interface/`)**: Management dashboard
- **Utils (`routing/utils/`)**: Shared utilities and module loading

### Database Schema

The platform uses PostgreSQL tables for configuration:

- `routing_rules`: Model-to-module mappings
- `module_configs`: Per-module configuration settings
- `routing_modules`: Available modules metadata
- `model_cooldowns`: Cooldown tracking for rate-limited models

## Available Modules

### OpenRouter Free (`openrouter_free`)

Implements transparent fallback from free to paid model tiers:

- Checks cooldown status before routing
- Routes to free tier when available
- Falls back to paid tier during cooldowns
- Automatically sets cooldowns on rate limit errors
- Configurable reset times and timezones

## Development

### Setting Up Development Environment

1. Install development dependencies:
```bash
pip install -r requirements-dev.txt
```

2. Install pre-commit hooks:
```bash
pre-commit install
```

3. Run tests:
```bash
pytest
```

### Creating Custom Modules

See `docs/module_development.md` for detailed instructions on creating custom routing modules.

## Documentation

- [Installation Guide](docs/installation.md)
- [LiteLLM Integration](docs/litellm_integration.md)
- [Configuration Guide](docs/configuration.md)
- [Module Development](docs/module_development.md)
- [API Reference](docs/api_reference.md)

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
