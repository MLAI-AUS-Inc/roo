---
name: mlai-points
description: Manage MLAI points system - check balance, book coworking, claim tasks, redeem rewards
trigger_keywords:
  - points
  - balance
  - coworking
  - book
  - task
  - tasks
  - reward
  - rewards
  - topup
  - top-up
  - top up
  - buy points
  - buy roo points
  - add points
  - add roo points
---

# MLAI Points System Skill

This skill enables Roo to interact with the MLAI Points System via API, allowing members to check their balance, book coworking days, browse open and assigned task queues, claim and submit structured tasks, and redeem rewards.

## Capabilities

### Member Actions
- Check points balance and history
- Request points for yourself in Slack for admin approval
- View task queues and claim open tasks
- Submit completed work for approval
- Unclaim a task before any submission exists
- Book and cancel coworking days
- Points Admins can check another member in for coworking from Slack
- View rewards catalog and request redemptions
- Buy fixed packs of Top-up Roo Points through MLAI checkout

### Full Admin Actions (requires `admin`, `committee`, or `portfolio_lead` role)
- Create new tasks with point values
- Edit supported task fields
- Cancel/archive tasks while preserving history
- Approve or reject task submissions
- Award points manually
- Set coworking capacity overrides

### Structured Task Notes
- Volunteers can refer to tasks by numeric ID or by task code such as `ROO-0042`
- A task can only have one active assignee at a time
- `unclaim` is only allowed before any submission exists
- Approval awards points once, on final acceptance

### Partner Actions (report-only)
- Generate coworking usage reports for date ranges and standard lookback windows

### Super Admin Actions (restricted to Slack user `U05QPB483K9`)
- Promote one tagged user to Points Admin
- Revoke one tagged user's Points Admin access
- Change one tagged Points Admin's weekly allowance

### Admin Weekly Allowance

Each Points Admin has a weekly allowance limiting how many points they can award. The allowance resets every Monday (ISO week).

When an admin attempts to award points:
1. Roo checks if they're a Points Admin
2. Roo checks their remaining weekly allowance
3. If exhausted, Roo informs them immediately (no LLM call needed)
4. If the requested amount exceeds remaining allowance, Roo suggests awarding less

Example responses:
- "You've used your full weekly allowance (100 pts). It resets on Monday. ⏰"
- "You only have 15 pts left this week. Try awarding 15 or less."

## Parameters

- **action**: The action to perform (required) - e.g., "balance", "request_points", "book_coworking", "admin_checkin_coworking", "coworking_report", "claim_task", "submit_task", "award_points", "create_task"
- **pack_id**: Top-up pack ID for `topup_points`; one of `topup_5`, `topup_10`, or `topup_25`
- **task_id**: Task ID number or task code (for example `42` or `ROO-0042`) for task-related actions
- **date**: Date for coworking bookings (YYYY-MM-DD format)
- **start_date**: Start date for coworking reports (YYYY-MM-DD format)
- **end_date**: End date for coworking reports (YYYY-MM-DD format)
- **points**: The number of points to award (integer, positive only)
- **reason**: A short description of why the points are being awarded or requested
- **target_user**: A single Slack User ID (e.g., U012ABC) or mention (e.g., <@U012ABC>) of the person receiving points. For single-user awards.
- **target_users**: A list of Slack User IDs extracted from mentions for multi-user awards. Extract ALL <@U...> patterns from the message. Example: ["U012ABC", "U034DEF"] or ["<@U012ABC>", "<@U034DEF>"]
- **target_slack_id**: (Alias for target_user) A single Slack User ID
- **weekly_allowance**: (Super Admin) Positive integer weekly allowance for one tagged Points Admin
- **submission_text**: Description of work completed for task submissions
- **reward_code**: Code for reward redemption requests
- **task_title**: (Admin) Title for a new task
- **title**: (Admin) Updated task title when editing
- **description**: (Admin) Updated task description when editing
- **portfolio**: (Admin) Portfolio for a new task (tech, marketing, events, general, governance). Defaults to the admin's assigned portfolio if omitted.
- **work_domain**: (Admin) Task domain such as tech, event_ops, or content_comms
- **review_flow**: (Admin) Review flow such as pr_review or deliverable_review
- **reviewer_slack_id**: (Admin) Primary reviewer Slack ID for a task
- **fallback_reviewer_slack_id**: (Admin) Fallback reviewer Slack ID for a task
- **repo**: (Admin) Repo or codebase context for a task
- **estimate_minutes**: (Admin) Estimated task duration in minutes
- **difficulty**: (Admin) Difficulty label such as tiny, small, medium, large, or lead
- **volunteer_ready**: (Admin) Whether the task is ready for the volunteer queue
- **acceptance_criteria**: (Admin) What must be true for the task to be accepted
- **how_to_test**: (Admin) How the reviewer should verify the task
- **definition_of_done**: (Admin) Concise completion criteria
- **blocked_reason**: (Admin) Reason the task is blocked, if any
- **assigned_to_user_id**: (Admin) Optional Slack User ID to assign a new task to (e.g. U012ABC or @alice)

## Command Recognition

Parse user messages to identify the action and parameters:

| Pattern | Action | Example |
|---------|--------|---------|
| `points`, `balance` | balance | "What's my points balance?", "@Roo points" |
| `topup`, `top up Roo Points`, `buy <n> Roo Points`, `add Roo Points`, `I need more points` | topup_points | "@Roo buy 10 Roo Points" |
| `request <n> points for <reason>` | request_points | "Request 5 points for helping at the event" |
| `points earn` | list_tasks | "How do I earn points?", "@Roo points earn" |
| `tasks`, `tasks open` | list_tasks | "What can I claim right now?" |
| `tasks mine` | list_tasks | "Show me my tasks" |
| `tasks review` | list_tasks | "Show me tasks waiting for my review" |
| `tasks all` | list_tasks | "Show me all tasks" |
| `task claim <id/code>` | claim_task | "Task claim 42", "Task claim ROO-0042" |
| `task unclaim <id/code>` | unclaim_task | "Unclaim ROO-0042" |
| `task submit <id/code> <text>` | submit_task | "Task submit 42 fixed the typo" |
| `coworking check <date>` | check_coworking | "Is there space on Dec 20?" |
| `coworking report from <start> to <end>` | coworking_report | "Coworking report from 2026-01-01 to 2026-03-31" |
| `coworking report <start> <end>` | coworking_report | "Coworking report 2026-01-01 2026-03-31" |
| `coworking report this week` | coworking_report | "How many people used the coworking space this week?" |
| `coworking report last week` | coworking_report | "How many people used the coworking space last week?" |
| `coworking compare ...` | coworking_report | "How did coworking usage last week compare to the week prior?" |
| `coworking busiest/quietest ...` | coworking_report | "Which day was busiest for coworking last month?" |
| `coworking trends/recommendations ...` | coworking_report | "Show coworking trends for the last 3 months and recommendations" |
| `coworking report last 3/6 months` | coworking_report | "Coworking report last 3 months" |
| `coworking report last year` | coworking_report | "Coworking report last year" |
| `coworking book <date/today>` | book_coworking | "Book me in", "Book me in for today", "@Roo coworking book" |
| `check/book <@USER> in <date/today>` | admin_checkin_coworking | (Admin) "Check <@U123> in", "Book <@U123> in today" |
| `coworking cancel <date>` | cancel_coworking | "Cancel my booking for Friday", "@Roo coworking cancel" |
| `rewards`, `points rewards` | list_rewards | "What rewards are available?", "@Roo points rewards" |
| `reward request <code>` | request_reward | "I want to get the HOTDESK_DAY reward" |
| `buy a <item>` | request_reward | "Can I buy a sticker?" (LLM infers code) |
| `task create <title> ...` | create_task | (Admin) "Create task called 'Fix docs' with 3 points" |
| `create a task ...` | create_task | (Admin) "Create a task called 'Update README' and assign 5 points" |
| `task edit <id/code> ...` | edit_task | (Admin) "Edit ROO-0042 and set points to 12" |
| `task update <id/code> ...` | edit_task | (Admin) "Update task 42 and change reviewer to <@U123>" |
| `task cancel <id/code>` | cancel_task | (Admin) "Cancel ROO-0042" |
| `delete task <id/code>` | cancel_task | (Admin) "Delete task 42" maps to cancel/archive |
| `task approve <id/code>` | approve_task | (Admin) "Task approve 42", "Task approve ROO-0042" |
| `task reject <id/code>` | reject_task | (Admin) "Task reject ROO-0042 and ask for tests" |
| `points award @user +5 reason` | award_points | (Admin) "Give @sam 5 points for helping out" |
| `reward @user for <activity>` | award_points | (Admin) "Reward @sam for newsletter" (suggests points from rate card) |
| `promote <@USER> to roo points admin` | promote_points_admin | (Super Admin) "Promote <@U123> to roo points admin" |
| `make <@USER> a roo points admin` | promote_points_admin | (Super Admin) "Make <@U123> a roo points admin" |
| `revoke <@USER> as roo points admin` | revoke_points_admin | (Super Admin) "Revoke <@U123> as roo points admin" |
| `remove <@USER> as roo points admin` | revoke_points_admin | (Super Admin) "Remove <@U123> as roo points admin" |
| `set <@USER> weekly points allowance to <n>` | set_points_admin_allowance | (Super Admin) "Set <@U123> weekly points allowance to 150" |
| `change <@USER> weekly points allowance to <n>` | set_points_admin_allowance | (Super Admin) "Change <@U123> weekly points allowance to 150" |
| `increase the number of points <@USER> can give out weekly to <n>` | set_points_admin_allowance | (Super Admin) "Increase the number of points <@U123> can give out weekly to 48" |
| `update how many points <@USER> can give out weekly to <n>` | set_points_admin_allowance | (Super Admin) "Update how many points <@U123> can give out weekly to 60" |

## Workflow

### Step 1: Identify Action
Parse the user's message to determine which action they want:
- Look for action keywords (balance, book, claim, etc.)
- Extract any IDs, dates, or amounts mentioned
- Accept both numeric task ids and `ROO-xxxx` task codes
- Treat `request ... points for ...` as `request_points`, not `award_points`
- For admin actions, verify the Slack mention format
- Treat coworking booking requests without a date as a booking for today
- For admin coworking check-ins, require exactly one tagged Slack user and book that user, not the admin
- For super admin actions, require exactly one tagged Slack user, never fall through into `award_points`, and require a positive numeric allowance when changing allowance

### Step 2: Permission Check
For full admin-only actions (create, edit, cancel, approve, reject, award):
- The API will validate the requester's Slack ID
- Partner admins are not full admins and must be denied for point-mutating actions
- If 403 returned, respond with friendly denial

For admin coworking check-ins:
- Roo must fail fast unless the requester is a full Points Admin
- Partner admins are report-only and cannot check people in
- The target user is the single non-Roo Slack mention in the message
- The booking date defaults to today when omitted
- The coworking booking must be created for the target user's Slack ID so the target user's points are deducted

For super admin actions (promote admin, revoke admin, change allowance):
- Roo must fail fast unless the requester Slack ID is `U05QPB483K9`
- The backend must still validate the requester for defense in depth

For coworking report actions:
- Roo must fail fast unless the requester is a full Points Admin or report-only partner
- Count only active bookings (`status=booked`), not cancelled bookings
- Support exact inclusive date ranges and presets: this week, last week, last 3 months, last 6 months, last year
- Format the response insight-first, with compact detail tables only when useful or requested
- For comparison/trend/recommendation questions, compute report metrics deterministically from backend JSON, then use GPT-5.4 only to explain the bounded numbers
- Label broader suggestions as interpretation or recommendations, never as measured attendance facts
- The backend must still validate the requester for defense in depth

### Step 3: Execute via API
Call the appropriate MLAIBackendClient method with extracted parameters.
- All API calls pass the requester's `slack_user_id` from the Slack event
- Never trust user-provided identity in message text

### Step 4: Format Response
Generate friendly response with relevant information:
- Balance: Show points count and encourage engagement
- Top-up: Show only fixed pack prices or the MLAI checkout link; never show a price-per-point value
- Tasks: Format as a short list with task code, points, and portfolio
- Bookings: Confirm date and show remaining balance
- Errors: Explain what went wrong and suggest alternatives

## Top-up Roo Points

Allowed fixed packs:
- `topup_5`: 5 Top-up Roo Points - A$19.99
- `topup_10`: 10 Top-up Roo Points - A$36.99
- `topup_25`: 25 Top-up Roo Points - A$63.99

Top-up Roo Points are MLAI's internal community reward points. They are not money, have no cash value, cannot be converted to cash, and cannot be sold or transferred. The price of Top-up Roo Points does not represent a monetary value for Roo Points.

Top-up Roo Points are optional and do not count toward lifetime earned contribution.

## Response Style

- Use Australian casual language (mate, no worries, ripper, legend)
- Celebrate achievements with emojis 🎉 🦘 ✨
- Be encouraging about earning points
- Keep responses concise but informative
- Use formatting (bold, lists) for clarity

## Example Responses

### Balance Check
```
G'day mate! Here's your points summary:

💰 **Current Balance:** 15 points
📈 **Lifetime Earned:** 42 points
📉 **Lifetime Spent:** 27 points

Nice work! Check out "@Roo tasks" to earn more 🦘
```

### Task List
```
Here are the open tasks up for grabs:

1. **ROO-0042 - Fix typos in README** (3 pts) 📂 tech
2. **ROO-0043 - Design event banner** (5 pts) 📂 marketing
3. **ROO-0044 - Help with workshop setup** (2 pts) 📂 events

Keen to help? Just say "task claim ROO-0042" to get started!
```

### Coworking Booking Success
```
You beauty! 🎉

Booked you in for **20 Dec 2025** at the coworking space.
Cost: 1 point (Balance remaining: 14 points)

See you there, legend!
```

### Insufficient Balance
```
Ah sorry mate, looks like you're a bit short on points.

You need 1 point for coworking, but you've got 0.

Here are some ways to earn points:
• Check out open tasks with "@Roo tasks"
• Volunteer at upcoming events
• Help out fellow community members

No worries, you'll get there! 💪
```

### Admin Denied
```
Sorry mate, you'll need to be a Points Admin to do that.

If you reckon you should have access, have a chat with the committee. 🤔
```

### Smart Award Suggestion
```
I found a match in the Rate Card: 'Draft newsletter edition' is worth 12 points. Should I award 12 points to @alice?
```
or for distinct matches:
```
That sounds like it could be 'Draft newsletter edition' (12 pts) or 'Newsletter full production' (24 pts). Which one is it?
```

## Error Handling

| Error | Response |
|-------|----------|
| User not found | "Hmm, I can't find your account. Have you linked your Slack? Check with the team." |
| Insufficient balance | Show balance, explain cost, suggest earning opportunities |
| Task not open | "That task isn't available to claim right now (status: {status})" |
| Task cannot be unclaimed | "That task already has submitted work on it, so it can't be released back to the queue." |
| No availability | "No spots left on {date}. Want to check another day?" |
| Network error | "Having trouble reaching the points system. Mind trying again in a tick?" |
| Permission denied | Friendly explanation, point to admins if they need help |
