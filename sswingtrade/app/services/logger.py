"""
Structured logging configuration using loguru.
Supports JSON output for log aggregation.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from loguru import logger as loguru_logger

from app.config import settings


class JSONFormatter:
    """Custom JSON formatter for structured logging."""

    def format(self, record):
        """Format log record as JSON."""

        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.get("level").name,
            "message": record.get("message"),
            "logger": record.get("name"),
            "module": record.get("module"),
            "function": record.get("function"),
            "line": record.get("line"),
        }

        # Add extra fields if present
        if record.get("extra"):
            log_data.update(record.get("extra"))

        # Add exception info if present
        if record.get("exception"):
            log_data["exception"] = {
                "type": record["exception"].exc_info[0].__name__,
                "message": str(record["exception"].exc_info[1]),
            }

        return json.dumps(log_data)


def setup_logging(name: str = __name__):
    """
    Configure loguru for the application.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Logger instance configured for SSWingTrade
    """

    # Remove default handler
    loguru_logger.remove()

    # Create logs directory
    log_dir = Path(settings.LOG_FILE_PATH).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    # Console handler (always)
    if settings.APP_ENV == "development":
        loguru_logger.add(
            lambda msg: print(msg, end=""),
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            level=settings.LOG_LEVEL,
            colorize=True,
        )
    else:
        # Production: no color, JSON format
        loguru_logger.add(
            lambda msg: print(msg, end=""),
            format="{message}",
            level=settings.LOG_LEVEL,
            serialize=True,
        )

    # File handler
    log_file = settings.LOG_FILE_PATH

    if settings.LOG_FORMAT == "json":
        # JSON formatted logs
        loguru_logger.add(
            log_file,
            format="{message}",
            level=settings.LOG_LEVEL,
            serialize=True,
            rotation=settings.LOG_ROTATION,
            retention="7 days",
        )
    else:
        # Standard text logs
        loguru_logger.add(
            log_file,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            level=settings.LOG_LEVEL,
            rotation=settings.LOG_ROTATION,
            retention="7 days",
        )

    # Return logger bound to the module name
    return loguru_logger.bind(name=name)


# Global logger instance
logger = setup_logging(__name__)


# ============================================================================
# Logging Utilities
# ============================================================================

def log_event(event_type: str, data: dict, level: str = "INFO"):
    """
    Log a structured event.

    Args:
        event_type: Type of event (trade, error, warning, etc.)
        data: Event data to log
        level: Log level (INFO, WARNING, ERROR, etc.)
    """

    log_data = {
        "event_type": event_type,
        **data,
    }

    if level.upper() == "ERROR":
        logger.error(json.dumps(log_data))
    elif level.upper() == "WARNING":
        logger.warning(json.dumps(log_data))
    else:
        logger.info(json.dumps(log_data))


def log_trade(position_id: int, action: str, details: dict):
    """Log a trade action."""
    log_event("TRADE", {
        "position_id": position_id,
        "action": action,
        **details,
    })


def log_risk_decision(decision: str, reason: str, data: dict):
    """Log a risk engine decision."""
    log_event("RISK_DECISION", {
        "decision": decision,
        "reason": reason,
        **data,
    })


def log_error(error_type: str, message: str, context: dict):
    """Log an error with context."""
    log_event("ERROR", {
        "error_type": error_type,
        "message": message,
        **context,
    }, level="ERROR")


if __name__ == "__main__":
    # Test logging
    logger.info("🟢 Logger initialized successfully")
    logger.debug("This is a debug message")
    logger.info("This is an info message")
    logger.warning("This is a warning")
    logger.error("This is an error")
