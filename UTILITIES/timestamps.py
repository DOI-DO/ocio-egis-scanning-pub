"""Shared timestamp helper."""
import datetime


def build_timestamp():
    """Return the shared YYYYMMDD_HHMM timestamp string used everywhere."""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M")