"""Logging configuration shared by every command.

Two sinks are installed: a rotating file in the platform log directory
that keeps the full ``INFO`` trail of unattended scheduled runs, and the
standard error stream at ``WARNING`` so that an interactive user only
sees what actually needs attention.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ['LOG_FILE_NAME', 'configure_logging']

LOG_FILE_NAME = 'fleet-usage.log'
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
FILE_FORMAT = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'
STDERR_FORMAT = '%(levelname)s: %(message)s'


def configure_logging(
    log_dir: Path,
    *,
    level: int = logging.INFO,
    stderr_level: int = logging.WARNING,
) -> logging.Logger:
    """Install the file and stderr handlers on the package logger.

    Calling this more than once replaces the previously installed
    handlers, which keeps repeated invocations inside tests harmless.

    Parameters
    ----------
    log_dir : pathlib.Path
        Directory for the rotating log file. Created if necessary.
    level : int, optional
        Level of the file handler.
    stderr_level : int, optional
        Level of the stderr handler.

    Returns
    -------
    logging.Logger
        The configured ``fleet_usage`` logger.
    """
    logger = logging.getLogger('fleet_usage')
    logger.setLevel(min(level, stderr_level))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(stderr_level)
    stderr_handler.setFormatter(logging.Formatter(STDERR_FORMAT))
    logger.addHandler(stderr_handler)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / LOG_FILE_NAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding='utf-8',
        )
    except OSError:
        logger.warning('log directory %s is not writable', log_dir)
        return logger
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    logger.addHandler(file_handler)
    return logger
