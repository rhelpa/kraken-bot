# logger_setup.py
import logging
from logging.handlers import TimedRotatingFileHandler
from config import LOG_PATH


def setup_logger(*, log_path: str, level=logging.DEBUG):
    root = logging.getLogger()
    root.setLevel(level)

    # rotating file handler (with full timestamp in the log line)
    fh = TimedRotatingFileHandler(
        log_path,
        when="midnight", interval=1, backupCount=7,
        encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    root.addHandler(fh)

    # console handler (just show time, level and message)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    root.addHandler(ch)

    return root
