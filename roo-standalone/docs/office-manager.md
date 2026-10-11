# Office Manager retirement

Retired on 12 October 2026. Roo no longer routes these requests, accepts
Office Manager volunteer clicks, retries their outbox, or calls the Victor
application reporting API. Deployment no longer requires these feature flags
or the Victor signing credential, and readiness no longer depends on Office
Manager contracts or workers.

Coworking bookings, meeting-room bookings, shared points authorization,
Studio reports, client hours, and Admin Brain remain supported. Historical
zero-cost Office Manager booking receipts remain valid.

No database migration or stored-data deletion accompanies this retirement.
Existing SQLite receipts/attempts and backend records are retained; the
retired Office Manager action queue no longer processes accepted attempts.
Archive review and any data retention decision are separate from this change.
The public founder-games Slack channel and its history are separate from
Victor applicant reporting.
