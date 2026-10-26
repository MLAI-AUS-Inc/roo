---
name: start-here-introductions
description: Generously validate each member's first top-level post in #_start-here and award 4 Roo points exactly once when it introduces either the person or their startup/project. Use automatically for new human posts and edits in #_start-here; do not require an @Roo mention.
---

# Start Here Introductions

Process this skill from Slack message events rather than mention routing.

## Workflow

1. Accept only human, top-level messages in `#_start-here`, including introductions posted with a file or image.
2. Reserve the first message as the member's canonical introduction.
3. Qualify the post generously when it contains either:
   - even a brief personal introduction, such as a name, role, background, interest, location, or founder journey; or
   - even a brief introduction to a startup, venture, project, or idea, including its name or what the member is building or exploring.
4. Reject only posts with neither signal, such as greeting-only, link-only, generic request, or unrelated posts. Ask the member to edit the original post; do not create another submission.
5. Re-check edits to the canonical post automatically.
6. Award exactly 4 Roo points when either introduction signal qualifies.
7. Treat duplicate events, later top-level posts, and post-award edits as no-ops for points.

## Safety

- Keep the canonical submission and retry state durable across restarts.
- Use the backend's one-time first-channel-post endpoint as the final award guard so later posts cannot earn the award.
- Fail closed on ambiguous, malformed, or unavailable classification results.
- Retry transient award failures with the same canonical submission.
- Never log introduction text in operational error messages.
