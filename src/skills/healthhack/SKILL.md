---
name: healthhack
description: Let authorised HealthHack organisers publish announcements from the #healthhack Slack channel to the HealthHack participant app.
priority_channels:
  - healthhack
exclusive_channels:
  - healthhack
routing:
  use_when: >
    In #healthhack only: an organiser asks Roo to make, create, post, or publish
    an announcement to the HealthHack participant app.
  avoid_when: >
    Any other channel. Rewriting announcement copy without publishing it
    (tone-of-voice), general HealthHack questions, or requests to message event
    attendees outside the HealthHack app.
  examples:
    - {text: "announce that doors open at 10:30", action: announce}
    - {text: "post an announcement titled Lunch with body Pizza is in the atrium", action: announce}
    - {text: "publish this to the HealthHack app: judging starts at 5pm", action: announce}
  negative_examples:
    - {text: "rewrite this announcement in our tone of voice", instead: tone-of-voice}
    - {text: "what time does HealthHack start?", instead: respond_in_chat}
    - {text: "email this to everyone going", instead: respond_in_chat}
actions:
  - name: announce
    description: Publish an announcement to the HealthHack participant app (authorised organisers only).
    params:
      title: {type: string}
      body: {type: string}
---

# HealthHack announcements

Use this skill only in `#healthhack` when an organiser asks Roo to publish an
announcement to the HealthHack participant app.

- Extract a clear **title** and **body** from the request.
- If either is missing, ask the organiser to provide it.
- The backend verifies the requesting human's permissions. Roo must not rely on
  a local list of admin Slack IDs.
- Attribute the visible announcement author to Roo while retaining the human
  requester and source Slack message for auditability.
- On success, confirm once in the originating Slack thread.
- This skill publishes only to the HealthHack app. It does not send email, SMS,
  push notifications, Luma blasts, or other attendee communications.

Example:

`@Roo post an announcement titled "Lunch is served" with body "Pizza is in the atrium at 1pm."`
