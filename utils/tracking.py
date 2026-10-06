import logging
import os
import time
from logging.handlers import RotatingFileHandler

# The scheduler and its job children are separate processes. Each file has a
# single writer at a time, so a rotation in one process can never leave the
# other appending to a renamed file.
JOB_LOG = "tenman.log"           # bot and job output


def setup_logging(log_name: str = JOB_LOG, log_dir: str | None = None):
    log_dir = log_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        pass

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = time.gmtime  # the Z suffix promises UTC; outside a UTC container localtime would lie

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    handlers = [console_handler]
    problem = None
    try:
        # Rotate at 5 MB, keep 14 files (~2 weeks of daily runs with some headroom)
        file_handler = RotatingFileHandler(os.path.join(log_dir, log_name), maxBytes=5 * 1024 * 1024,
                                           backupCount=14)
        file_handler.setFormatter(fmt)
        handlers.insert(0, file_handler)
    except OSError as exc:
        # An unwritable logs/ (wrong owner on a bind mount) must not stop the
        # bot: the container engine still captures stdout.
        problem = f"cannot write {log_dir}/{log_name} ({exc.strerror}); logging to stdout only"

    logging.basicConfig(level=logging.INFO, handlers=handlers)
    logger = logging.getLogger("tenman")
    if problem:
        logger.warning(f"{problem}. Fix: the folder must be writable by uid {os.getuid()}")
    return logger
