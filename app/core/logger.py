"""
Structured logging -- readable console output for local dev, JSON lines
to a file for later ingestion into a real log system once deployed.

Every scraper logs at each meaningful stage (launch, navigate, fill,
submit, response received, block detected, retry, success/failure) so
a run's logs read like a timeline, and an IP block or selector break is
diagnosable from logs alone.
"""

import logging
import os
from pythonjsonlogger import jsonlogger

from app.config.settings import settings

os.makedirs(settings.log_dir, exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured -- avoid duplicate handlers

    logger.setLevel(settings.log_level)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )

    file_handler = logging.FileHandler(os.path.join(settings.log_dir, "tracking_app.log"))
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        jsonlogger.JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger
