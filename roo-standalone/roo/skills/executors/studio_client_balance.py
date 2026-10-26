"""Fixed client-balance report capability."""
from .studio_hours import StudioHoursExecutor

class StudioClientBalanceExecutor(StudioHoursExecutor):
    report_kind = "client_balance"
