"""Tests for the logging configuration."""

import logging

from fleet_usage.logging_setup import LOG_FILE_NAME, configure_logging


def test_handlers_and_file(tmp_path):
    logger = configure_logging(tmp_path / 'logs')
    try:
        assert len(logger.handlers) == 2
        levels = sorted(handler.level for handler in logger.handlers)
        assert levels == [logging.INFO, logging.WARNING]
        logger.info('hello file')
        for handler in logger.handlers:
            handler.flush()
        content = (tmp_path / 'logs' / LOG_FILE_NAME).read_text(
            encoding='utf-8'
        )
        assert 'hello file' in content
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_repeated_configuration_does_not_stack(tmp_path):
    configure_logging(tmp_path / 'logs')
    logger = configure_logging(tmp_path / 'logs')
    try:
        assert len(logger.handlers) == 2
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
