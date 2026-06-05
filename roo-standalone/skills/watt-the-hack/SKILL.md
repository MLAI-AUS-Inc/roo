---
name: watt-the-hack
description: Post announcements to the Watt The Hack hackathon website and answer questions about the event. Use for any request to make, post, or create an announcement in the Watt The Hack channel.
trigger_keywords:
  - watt the hack
  - watt
  - announcement
  - announce
  - post announcement
  - create announcement
priority_channels:
  - watt-the-hack
exclusive_channels:
  - watt-the-hack
---

# Watt The Hack Skill

This skill lets organisers publish announcements to the Watt The Hack participant
website straight from Slack. Published announcements appear in the Announcements
panel on the participant dashboard.

## Making an announcement

When someone tags Roo in #watt-the-hack and asks to make / post / create an
announcement, extract a clear **title** and a **body** from their message and
publish it to the Watt The Hack site.

- Only MLAI superusers can post announcements. Authorisation is verified by the
  backend against the requester's account — if the person asking is not a
  superuser, the backend refuses and Roo relays a polite "only MLAI superusers
  can post announcements" message.
- Once published, confirm back in the thread with the announcement title.
- If the message is missing a clear title or body, ask the organiser to provide
  both.

Example: "@roo post an announcement titled 'Lunch is served' with body 'Pizza is
in the atrium at 1pm.'"

## Parameters

- **query**: The organiser's announcement request, or a question about the Watt
  The Hack event (required)
