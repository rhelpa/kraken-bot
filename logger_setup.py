# logger_setup.py
import logging
from logging.handlers import TimedRotatingFileHandler
from config import LOG_PATH


def setup_logger():
    """Configure root logger with file and console handlers."""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    fh = TimedRotatingFileHandler(
        LOG_PATH,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8"
    )
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s",
            "%Y-%m-%d %H:%M:%S"
        )
    )
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s",
            "%H:%M:%S"
        )
    )
    logger.addHandler(ch)

    return logger