"""Runtime skill executors, imported normally and checked by registry tests."""
from .contracts import SkillExecutorInterface, EventSkillExecutorInterface
from .executors.admin_brain import AdminBrainExecutor
from .executors.admin_actions import AdminActionsExecutor
from .executors.content_factory import ContentFactoryExecutor
from .executors.connect_users import ConnectUsersExecutor
from .executors.mlai_points import MlaiPointsExecutor
from .executors.committee_candidate_emails import CommitteeCandidateEmailsExecutor
from .executors.meeting_room_booking import MeetingRoomBookingExecutor
from .executors.studio_invoices import StudioInvoicesExecutor
from .executors.studio_hours import StudioHoursExecutor
from .executors.studio_client_balance import StudioClientBalanceExecutor
from .executors.mlai_data_query import MlaiDataQueryExecutor
from .executors.linear_channel_issues import LinearChannelIssuesExecutor
from .executors.github_integration import GithubIntegrationExecutor
from .executors.linear_meeting_actions import LinearMeetingActionsExecutor
from .executors.tone_of_voice import ToneOfVoiceExecutor
from .executors.healthhack import HealthhackExecutor
from .executors.watt_the_hack import WattTheHackExecutor
from .executors.luma_events import LumaEventsExecutor
from .executors.reconciliation_report import ReconciliationReportExecutor
from roo.start_here_introductions import StartHereIntroductionsExecutor

EXECUTORS: dict[str, type[SkillExecutorInterface]] = {
    'admin-brain': AdminBrainExecutor,
    'admin-actions': AdminActionsExecutor,
    'content-factory': ContentFactoryExecutor,
    'connect-users': ConnectUsersExecutor,
    'mlai-points': MlaiPointsExecutor,
    'committee-candidate-emails': CommitteeCandidateEmailsExecutor,
    'meeting-room-booking': MeetingRoomBookingExecutor,
    'studio-invoices': StudioInvoicesExecutor,
    'studio-hours': StudioHoursExecutor,
    'studio-client-balance': StudioClientBalanceExecutor,
    'mlai-data-query': MlaiDataQueryExecutor,
    'linear-channel-issues': LinearChannelIssuesExecutor,
    'github-integration': GithubIntegrationExecutor,
    'linear-meeting-actions': LinearMeetingActionsExecutor,
    'tone-of-voice': ToneOfVoiceExecutor,
    'healthhack': HealthhackExecutor,
    'watt-the-hack': WattTheHackExecutor,
    'luma-events': LumaEventsExecutor,
    'reconciliation-report': ReconciliationReportExecutor,
}

EVENT_EXECUTORS: dict[str, type[EventSkillExecutorInterface]] = {
    'start-here-introductions': StartHereIntroductionsExecutor,
}

EVENT_ONLY_SKILLS = frozenset(EVENT_EXECUTORS)
