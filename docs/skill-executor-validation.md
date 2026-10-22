# Skill executor refactor validation — 23 October 2026

All 20 current skill packages were inventoried. Nineteen chat capabilities now
have registered executor classes; introductions has an event-only executor.
The central `SkillExecutor` performs dispatch and response normalization.

## Skill coverage

Paths below are relative to `src/roo/skills/handlers/`; test names are relative
to `tests/roo/` and have the `test_` prefix and `.py` suffix.

| Skill | Executor | Behavioral suites |
| --- | --- | --- |
| admin-brain | `admin_brain.AdminBrainHandler` | `admin_brain`, `admin_brain_interactions`, `unified_admin_routing` |
| admin-actions | `admin_actions.AdminActionsHandler` | `admin_actions`, `admin_worker` |
| committee-candidate-emails | `committee_candidate_emails.CommitteeCandidateEmailsHandler` | `committee_candidate_emails` |
| connect-users | `connect_users.ConnectUsersHandler` | `skill_execution_behaviors` |
| content-factory | `content_factory.ContentFactoryHandler` | `content_factory_article_flow` |
| github-integration | `github_integration.GithubIntegrationHandler` | `skill_execution_behaviors`, shared publishing coverage in `content_factory_article_flow` |
| healthhack | `healthhack.HealthhackHandler` | `healthhack_announcements` |
| linear-channel-issues | `linear_channel_issues.LinearChannelIssuesHandler` | `linear_channel_issues` |
| linear-meeting-actions | `linear_meeting_actions.LinearMeetingActionsHandler` | `linear_meeting_actions`, `linear_effort_sizing`, `linear_meeting_sources`, `linear_inference` |
| luma-events | `luma_events.LumaEventsHandler` | `luma_events` |
| meeting-room-booking | `meeting_room_booking.MeetingRoomBookingHandler` | `meeting_room_booking`, `meeting_room_availability`, `meeting_room_actions`, `meeting_room_clarifications` |
| mlai-data-query | `mlai_data_query.MlaiDataQueryHandler` | `mlai_data_query` |
| mlai-points | `mlai_points.MlaiPointsHandler` | `points_requests`, `points_flex`, `private_points_delivery`, `event_discounts`, `founder_account_link`, `rate_card`, coworking suites |
| reconciliation-report | `reconciliation_report.ReconciliationReportHandler` | `reconciliation_report` |
| start-here-introductions | `roo.start_here_introductions.StartHereIntroductionsHandler` | `start_here_introductions`, `skill_executor_classes` |
| studio-client-balance | `studio_client_balance.StudioClientBalanceHandler` | `studio_client_balance_routing` |
| studio-hours | `studio_hours.StudioHoursHandler` | `studio_report_routing` |
| studio-invoices | `studio_invoices.StudioInvoicesHandler` | `invoice_ingress`, `skill_execution_behaviors` |
| tone-of-voice | `tone_of_voice.ToneOfVoiceHandler` | `skill_loading_contract`, `skill_executor_classes` |
| watt-the-hack | `watt_the_hack.WattTheHackHandler` | `skill_execution_behaviors` |

Every registered class also has construction/ownership coverage. Existing
registry tests check argument forwarding and rich response preservation.
Added prompt tests check instructions, history, provider options and failure
handling. Event-only skills remain unavailable through chat dispatch.

## Before and after

- Initial baseline: 1,171 passed, five content-factory failures across 37 suites.
  Those five failures were caused by that suite's obsolete `frontmatter` stub:
  it returned no parsed skills. With the real parser preloaded, the saved
  original executor passed all 72 content-factory tests. The obsolete stub was
  removed explicitly; no content-factory action gates were changed.
- Eight additional behavior tests for connect-users, Watt-the-hack, GitHub and
  invoice help pass against both the saved original executor and the new code.
- Final validation: **1,553 passed** across 52 separately run suites. The
  capability-honesty suite also reported three existing expected failures and
  three skipped opt-in live model tests. There were no unexpected failures.
- All 299 original domain methods are accounted for. AST comparison, normalized
  for relocated imports and the points class reference, found 297 unchanged;
  tone-of-voice and generic prompt execution intentionally use the new shared
  prompt executor. Dispatch adapters preserve their original argument mapping.
- All 20 packages pass the metadata linter. Syntax and diff whitespace checks
  pass. The new behavior and class tests are included in CI.

Tests ran under Python 3.11 in disposable containers, with networking disabled,
read-only application mounts and fake credentials. Booking migrations ran only
against disposable test databases with explicit user approval. The main database
was untouched. No model API, Slack/backend write, deployment or server startup
was performed. These are regression checks, not exhaustive branch coverage or
live integration validation.

## Compatibility and remaining scope

The coordinator retains legacy helper forwarding for existing Slack callbacks
and test callers. Handlers never depend on or call back into the coordinator.
Old tests patch dependencies at their new import sites using
`skill_test_support.patch_skill_dependency`; their behavioral assertions remain
intact. New tests use individual handlers and injected model clients directly.

Shared publishing and Linear reading helpers are kept in focused support
classes. Points and Linear meeting actions remain large domains; this extraction
does not redesign their workflows or change their permission model.
