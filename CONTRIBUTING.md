# Contributing to OrionBelt Analytics

Thank you for your interest in contributing! This guide covers the essentials to get started.

## 🎯 Project Overview

OrionBelt Analytics is a production-ready MCP server that analyzes database schemas (PostgreSQL, Snowflake, ClickHouse, Dremio, BigQuery, DuckDB/MotherDuck, Databricks SQL) and generates RDF/OWL ontologies with automatic SQL mappings.

## 🚀 Getting Started

### Prerequisites

- Python 3.13+
- Git
- Access to a supported database for testing

### Setup

```bash
# Fork and clone
git clone https://github.com/your-username/orionbelt-analytics.git
cd orionbelt-analytics

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies (using uv - recommended)
uv sync

# Configure environment
cp .env.template .env
# Edit .env with your database credentials

# Set up pre-commit hooks
pre-commit install

# Verify setup
python -m pytest tests/ -v
python server.py --help
```

## 🏗️ Key Components

- **`src/main.py`**: FastMCP server implementation
- **`src/database_manager.py`**: Database connectivity and schema analysis
- **`src/ontology_generator.py`**: RDF/OWL ontology generation
- **`src/chart_utils.py`**: Chart generation utilities
- **`src/security.py`**: SQL safety validation and fan-trap detection
- **`src/config.py`**: Configuration management
- **`server.py`**: Application entry point

## 📝 Code Standards

### Style

```bash
# Format and check code
black src/ tests/
isort src/ tests/
flake8 src/ tests/
mypy src/

# Or run all checks
pre-commit run --all-files
```

**Requirements:**

- Line length: 88 characters
- Type hints for all public functions
- Docstrings for all public classes/functions (Google style)
- snake_case for functions/variables, PascalCase for classes

### Testing

All contributions must include tests:

```bash
# Run tests
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=src --cov-report=html
```

**Requirements:**

- New features: >90% test coverage
- Bug fixes: Include regression tests
- Test both success and failure paths

## 📋 Commit Guidelines

Follow conventional commits:

```bash
feat: add MySQL database support
fix: prevent SQL injection in table sampling
docs: update configuration examples
test: add database manager tests
refactor: extract connection logic
```

## 🔄 Pull Request Process

1. Ensure all tests pass
2. Run code quality checks
3. Update documentation if needed
4. Create PR with clear description
5. Address review feedback

## 🐛 Reporting Issues

Include:

- Clear description
- Environment (OS, Python version, database)
- Reproduction steps
- Expected vs actual behavior
- Error logs (redact sensitive info)

## 💡 Contribution Ideas

**Easy (Good First Issues):**

- Add SQL type mappings
- Improve error messages
- Write additional tests

**Medium:**

- Implement connection retry logic
- Add caching layer
- Create ontology validation tools

**Advanced:**

- Add new database support (MySQL, Oracle, etc.)
- Implement streaming for large schemas
- Build web-based ontology viewer

## 📚 Resources

- [MCP Specification](https://modelcontextprotocol.io/)
- [RDF/OWL Primer](https://www.w3.org/TR/owl2-primer/)
- [FastMCP Framework](https://github.com/jlowin/fastmcp)

## 🤝 Community

- **Issues**: Bug reports, feature requests
- **Discussions**: Questions, ideas
- **Pull Requests**: Code contributions

### Code of Conduct

Be respectful and inclusive in all interactions.

## 📄 License

This project is licensed under the Business Source License 1.1 (BSL 1.1). By submitting a contribution, you agree to the terms of our [Contributor License Agreement (CLA)](CLA.md), which grants RALFORION d.o.o. the right to license your contributions under BSL 1.1 and future licenses (including Apache 2.0 after the Change Date of 2030-03-16).

**Key Points:**
- All contributions are covered by the [CLA](CLA.md)
- Current license: BSL 1.1 (source-available, production use allowed with restrictions)
- Change Date: 2030-03-16 → converts to Apache 2.0
- Commercial licensing available: contact licensing@ralforion.com

---

Thank you for contributing! 🚀
