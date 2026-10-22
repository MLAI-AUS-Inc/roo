"""Explicit skill class bindings; importing this catalogue does not load handlers."""
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .contracts import SkillHandler

@dataclass(frozen=True)
class HandlerBinding:
    module: str
    class_name: str

    def create(self) -> "SkillHandler":
        return getattr(import_module(self.module, __package__), self.class_name)()

HANDLERS = {
    'admin-brain': HandlerBinding(".handlers.admin_brain", "AdminBrainHandler"),
    'admin-actions': HandlerBinding(".handlers.admin_actions", "AdminActionsHandler"),
    'content-factory': HandlerBinding(".handlers.content_factory", "ContentFactoryHandler"),
    'connect-users': HandlerBinding(".handlers.connect_users", "ConnectUsersHandler"),
    'mlai-points': HandlerBinding(".handlers.mlai_points", "MlaiPointsHandler"),
    'committee-candidate-emails': HandlerBinding(".handlers.committee_candidate_emails", "CommitteeCandidateEmailsHandler"),
    'meeting-room-booking': HandlerBinding(".handlers.meeting_room_booking", "MeetingRoomBookingHandler"),
    'studio-invoices': HandlerBinding(".handlers.studio_invoices", "StudioInvoicesHandler"),
    'studio-hours': HandlerBinding(".handlers.studio_hours", "StudioHoursHandler"),
    'studio-client-balance': HandlerBinding(".handlers.studio_client_balance", "StudioClientBalanceHandler"),
    'mlai-data-query': HandlerBinding(".handlers.mlai_data_query", "MlaiDataQueryHandler"),
    'linear-channel-issues': HandlerBinding(".handlers.linear_channel_issues", "LinearChannelIssuesHandler"),
    'github-integration': HandlerBinding(".handlers.github_integration", "GithubIntegrationHandler"),
    'linear-meeting-actions': HandlerBinding(".handlers.linear_meeting_actions", "LinearMeetingActionsHandler"),
    'tone-of-voice': HandlerBinding(".handlers.tone_of_voice", "ToneOfVoiceHandler"),
    'healthhack': HandlerBinding(".handlers.healthhack", "HealthhackHandler"),
    'watt-the-hack': HandlerBinding(".handlers.watt_the_hack", "WattTheHackHandler"),
    'luma-events': HandlerBinding(".handlers.luma_events", "LumaEventsHandler"),
    'reconciliation-report': HandlerBinding(".handlers.reconciliation_report", "ReconciliationReportHandler"),
}

EVENT_HANDLERS = {
    "start-here-introductions": HandlerBinding("roo.start_here_introductions", "StartHereIntroductionsHandler"),
}
EVENT_ONLY_SKILLS = frozenset(EVENT_HANDLERS)
REQUIRED_CLIENTS = {"linear-meeting-actions": "LinearMeetingActionsClient"}
