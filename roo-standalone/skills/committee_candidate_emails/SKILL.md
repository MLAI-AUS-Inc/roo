---
name: committee-candidate-emails
description: List emails for members with 100+ lifetime-earned Roo Points
routing:
  use_when: >
    An admin wants emails for members with 100+ earned Roo Points.
  avoid_when: >
    Personal points, Luma attendee data, or sending event invitations.
  examples:
    - {text: "list members with 100 or more earned points", action: list_eligible_emails}
    - {text: "give me the committee candidate emails", action: list_eligible_emails}
    - {text: "export emails for members with at least 100 earned Roo Points", action: list_eligible_emails}
  negative_examples:
    - {text: "what is my Roo points balance", instead: mlai-points}
    - {text: "show me attendees for this Luma event", instead: luma-events}
actions:
  - name: list_eligible_emails
    description: Privately return the copy-ready eligible email list.
---

# Committee Candidate Emails

- The backend is the source of truth for eligibility and permissions.
- Eligibility is `lifetime_earned >= 100`; purchased and spendable balances do not count.
- Return only the backend-provided email addresses and eligible count.
- Public requests receive only an acknowledgement after private DM delivery.
- The requester is always the verified Slack event actor, never a model parameter.
- Roo does not collect meeting details, create Luma events, or send invitations.
