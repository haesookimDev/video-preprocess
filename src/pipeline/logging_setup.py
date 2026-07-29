"""로깅 설정: 콘솔(INFO, 간결) + 파일(DEBUG, 상세) 이중 출력."""

import logging
from pathlib import Path

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] "
    "(%(module)s:%(lineno)d) %(message)s"
)
DATE_FORMAT = "%H:%M:%S"


def setup_logging(log_file: Path) -> logging.Logger:
    """루트 'pipeline' 로거를 구성하고 반환한다.

    - 콘솔: INFO 이상, 진행 상황 파악용
    - 파일: DEBUG 이상, 세그먼트/프레임 단위 상세 기록
    """
    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
    logger.addHandler(file_handler)

    logger.debug("로그 파일: %s", log_file)
    return logger


def stage_logger(stage_name: str) -> logging.Logger:
    return logging.getLogger(f"pipeline.{stage_name}")
