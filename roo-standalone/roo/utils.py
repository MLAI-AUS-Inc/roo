from datetime import datetime
from zoneinfo import ZoneInfo
from .config import get_settings

def get_current_date():
    """Get the current date in the configured timezone."""
    settings = get_settings()
    tz = ZoneInfo(settings.TIMEZONE)
    return datetime.now(tz).date()

def get_current_datetime():
    """Get the current datetime in the configured timezone."""
    settings = get_settings()
    tz = ZoneInfo(settings.TIMEZONE)
    return datetime.now(tz)
