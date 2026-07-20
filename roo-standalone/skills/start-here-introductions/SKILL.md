---
name: start-here-introductions
description: Validate each member's single top-level introduction in #_start-here, require information about both the person and their startup, and award 4 Roo points exactly once. Use automatically for new human posts and edits in #_start-here; do not require an @Roo mention.
---

# Start Here Introductions

Process this skill from Slack message events rather than mention routing.

## Workflow

1. Accept only human, top-level messages in `#_start-here`.
2. Reserve the first message as the member's canonical introduction.
3. Require both:
   - a personal introduction, such as name, role, background, interests, or founder journey;
   - a startup, venture, project, or startup idea description, such as what it does, its problem, users, or stage.
4. If either part is missing, ask the member to edit the original post. Do not create another submission.
5. Re-check edits to the canonical post automatically.
6. Award exactly 4 Roo points only after both requirements qualify.
7. Treat duplicate events, later top-level posts, and post-award edits as no-ops for points.

## Safety

- Keep the canonical submission and retry state durable across restarts.
- Use the backend's one-time first-channel-post endpoint as the final award guard.
- Fail closed on ambiguous, malformed, or unavailable classification results.
- Retry transient award failures with the same canonical submission.
- Never log introduction text in operational error messages.
