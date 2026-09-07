"""Office Manager pilot boundary; expanding it requires a reviewed rollout."""

# Slack channel IDs are stable across renames. Never use a channel display name.
OFFICE_MANAGER_TEST_CHANNEL_ID = "C0BRM181EDV"


def is_office_manager_channel_allowed(channel_id: object) -> bool:
    return channel_id == OFFICE_MANAGER_TEST_CHANNEL_ID


class OfficeManagerChannelRestrictedError(RuntimeError):
    """Keep blocked historical work pending without contacting other channels."""
