#!/usr/bin/env python3
"""Startup script for OrionBelt Analytics."""

import sys
import signal
import shutil
import asyncio
import warnings
from pathlib import Path

# Add src directory to path for imports
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from src.main import mcp
from src.config import config_manager
from src.utils import setup_logging
from src import __version__, __name__ as SERVER_NAME

# Setup logging
config = config_manager.get_server_config()
logger = setup_logging(config.log_level, structured=False)  # Use simple format for startup

def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown."""
    shutdown_event = asyncio.Event()

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        # Set shutdown event to allow cleanup
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(shutdown_event.set)
        except RuntimeError:
            pass  # No event loop running
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    return shutdown_event

def print_startup_info():
    """Print server startup information."""
    logger.info("="*60)
    logger.info(f"{SERVER_NAME} v{__version__}")
    logger.info("MCP server for database ad hoc analysis with ontology support and interactive charting")
    logger.info("="*60)
    
    logger.info("🔧 Available MCP Tools:")
    tools = [
        "connect_database - Connect to PostgreSQL, Snowflake, or Dremio with security",
        "diagnose_connection_issue - Diagnose and troubleshoot connection problems",
        "list_schemas - List available database schemas",
        "get_analysis_context - Complete schema analysis with automatic ontology generation", 
        "sample_table_data - Sample table data with security controls",
        "generate_ontology - Generate RDF ontology with validation",
        "load_ontology_from_file - Load saved/edited ontology from tmp folder",
        "validate_sql_syntax - Validate SQL queries before execution",
        "execute_sql_query - Execute validated SQL queries safely",
        "generate_chart - Generate interactive charts from query results",
        "get_server_info - Get comprehensive server information"
    ]
    for tool in tools:
        logger.info(f"  • {tool}")
    
    logger.info("")
    logger.info("🗄️ Supported Databases: PostgreSQL, Snowflake, Dremio")
    logger.info("🧠 LLM Enrichment: Available via MCP prompts and tools")
    logger.info("🔒 Security: Credential handling and input validation")
    logger.info("⚡ Performance: Connection pooling and parallel processing")
    logger.info("📊 Observability: Structured logging and comprehensive error handling")
    logger.info("")
    logger.info(f"📋 Configuration:")
    logger.info(f"  • Log Level: {config.log_level}")
    logger.info(f"  • Base URI: {config.ontology_base_uri}")
    logger.info(f"  • Transport: {config.mcp_transport}")
    logger.info(f"  • Host: {config.mcp_server_host}")
    logger.info(f"  • Port: {config.mcp_server_port}")
    logger.info("")

def cleanup_tmp_folder():
    """Clean up temporary files from previous server runs."""
    tmp_dir = Path(__file__).parent / "tmp"
    if tmp_dir.exists():
        try:
            # Remove all files in tmp directory but keep the directory
            for file_path in tmp_dir.glob("*"):
                if file_path.is_file():
                    file_path.unlink()
                    logger.debug(f"Removed temporary file: {file_path.name}")
            
            file_count = len(list(tmp_dir.glob("*")))
            if file_count == 0:
                logger.info("🧹 Cleaned up temporary files from previous runs")
            else:
                logger.info(f"🧹 Cleaned up temporary files, {file_count} items remain")
                
        except Exception as e:
            logger.warning(f"Failed to clean tmp directory: {e}")
    else:
        # Create tmp directory if it doesn't exist
        tmp_dir.mkdir(exist_ok=True)
        logger.info("📁 Created tmp directory for ontology files")

def main():
    """Start the OrionBelt Analytics MCP server."""
    try:
        # Setup signal handlers for graceful shutdown
        shutdown_event = setup_signal_handlers()

        # Clean up temporary files from previous runs
        cleanup_tmp_folder()

        # Print startup information
        print_startup_info()

        transport_name = "streamable-http" if config.mcp_transport == "http" else "SSE"
        logger.info(f"🚀 Starting OrionBelt Analytics MCP server with {transport_name} transport...")
        logger.info("📡 Server ready for MCP protocol messages")

        # Configure FastMCP with shorter shutdown timeout for cleaner exits
        # This reduces the timeout window for SSE connections during shutdown
        import os
        os.environ.setdefault("MCP_SHUTDOWN_TIMEOUT", "2")  # 2 seconds instead of default 5

        mcp.run(transport=config.mcp_transport, host=config.mcp_server_host, port=config.mcp_server_port)

    except KeyboardInterrupt:
        logger.info("⏹️  Server stopped by user (Ctrl+C)")
    except asyncio.CancelledError:
        # Gracefully handle cancelled tasks during shutdown
        logger.debug("Async tasks cancelled during shutdown (expected)")
    except Exception as e:
        # Only log unexpected exceptions
        if "CancelledError" not in str(type(e).__name__):
            logger.error(f"❌ Critical server error: {type(e).__name__}: {e}")
            logger.error("Please check your configuration and try again")
            return 1
    finally:
        logger.info("✅ Server shutdown complete")

    return 0

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
