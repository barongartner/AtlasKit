"""Plain-English log lines for the crew's log files.

    Tue Oct 20, 5:32:55 PM  Sam (iMessage): Hi, remember to text me...
    Tue Oct 20, 5:34:41 PM  Atlas replied: Locked in. Every bot texts you...
    Tue Oct 20, 5:40:02 PM  PROBLEM: iMessage send failed (...)

12-hour time, no logger names, and the level only shows when something is
actually wrong (Heads up / PROBLEM).
"""

import logging
from datetime import datetime


class ReadableFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        t = datetime.fromtimestamp(record.created)
        stamp = f"{t:%a %b} {t.day}, {t.strftime('%I').lstrip('0')}:{t:%M:%S %p}"
        if record.levelno >= logging.ERROR:
            level = "PROBLEM: "
        elif record.levelno >= logging.WARNING:
            level = "Heads up: "
        else:
            level = ""
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{stamp}  {level}{msg}"


def make_readable() -> None:
    """Swap every root handler's formatter for the readable one."""
    for handler in logging.getLogger().handlers:
        handler.setFormatter(ReadableFormatter())
