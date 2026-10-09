"""Shared run-timing helpers: wall-clock start/end and elapsed formatting."""
import datetime
import time


# Human-readable wall-clock format used in email bodies, e.g.
# "2026-08-15 13:30:07".
WALLCLOCK_FMT = "%Y-%m-%d %H:%M:%S"


def now_wallclock():
    """Return the current local wall-clock time formatted for display."""
    return datetime.datetime.now().strftime(WALLCLOCK_FMT)


def start_timer():
    """
    Begin timing a run. Returns (start_wallclock_str, start_perf) where
    start_perf is a high-resolution counter to pair with elapsed_since().
    """
    return now_wallclock(), time.perf_counter()


def format_elapsed(seconds):
    """
    Convert a number of seconds into a friendly H:MM:SS.s string,
    e.g. 207.4 -> "0:03:27.4".
    """
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{int(hrs)}:{int(mins):02d}:{secs:04.1f}"


def elapsed_since(start_perf):
    """Return seconds elapsed since a start_perf from start_timer()."""
    return time.perf_counter() - start_perf


def timing_block(start_wallclock, start_perf):
    """
    Build the shared three-line timing block for an email body:

        Start time    : 2026-08-15 13:30:07
        End time      : 2026-08-15 13:33:34
        Elapsed       : 0:03:27.4

    Captures the end time and elapsed at the moment it is called, so call it
    right before composing the email.
    """
    end_wallclock = now_wallclock()
    elapsed = format_elapsed(elapsed_since(start_perf))
    return (
        f"Start time    : {start_wallclock}\n"
        f"End time      : {end_wallclock}\n"
        f"Elapsed       : {elapsed}\n"
    )