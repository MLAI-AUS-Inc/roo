"""Fixed client-balance report capability."""
from .studio_hours import StudioHoursHandler

class StudioClientBalanceHandler(StudioHoursHandler):
    report_kind = "client_balance"
