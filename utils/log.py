"""Shared logger for the DispatchThis plugin.

Every module logs through the same named ``Logger`` so output can be filtered
in the Binary Ninja log window by the "DispatchThis" channel.
"""

from binaryninja import Logger

_logger = Logger(0, "DispatchThis")


def log_info(msg: str):
    _logger.log_info(msg)


def log_warn(msg: str):
    _logger.log_warn(msg)


def log_error(msg: str):
    _logger.log_error(msg)


def log_debug(msg: str):
    _logger.log_debug(msg)
