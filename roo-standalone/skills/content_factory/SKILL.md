---
name: content-factory
description: Generate SEO-optimized blog articles using the Content Factory pipeline
trigger_keywords:
  - write me an article
  - write an article
  - write article
  - research the best article
  - content factory
  - auto write
  - write content
  - generate article
  - target keyword
  - seo keyword
  - scaffold
  - create articles
  - articles directory
  - articles page
  - set up articles
  - set up blog
  - create blog page
  - add blog
  - scan codebase
  - scan my codebase
  - scan repo
  - scan repository
  - scan the repo
  - scan the domain
  - scan domain
  - analyze repo
  - analyse repo
  - analyze domain
  - analyse domain
routing:
  use_when: >
    The user wants NEW website content produced or planned via the Content Factory
    pipeline: write an SEO article/blog post, research what to write next (topics,
    keywords), scan a connected repo/domain to set it up, scaffold the blog/articles
    section, or publish a finished article bundle as a pull request.
  avoid_when: >
    Summarising, reviewing, or giving opinions on EXISTING content someone shared.
    Research that is not aimed at producing site content. GitHub auth problems
    (github-integration). Vocabulary alone ("article", "blog") is not intent.
  examples:
    - {text: "write me an article about how to build an ai agent harness", action: write}
    - {text: "scan the repo for the domain mlai.au", action: scan}
    - {text: "what should I write about next for woofya.com.au?", action: research}
    - {text: "set up a blog section on my site", action: scaffold}
    - {text: "publish this article as a PR", action: publish_pr}
  negative_examples:
    - {text: "can you summarise this article for me?", instead: respond_in_chat}
    - {text: "what's the topic for this week's meetup?", instead: respond_in_chat}
    - {text: "research the best time to post on linkedin", instead: respond_in_chat}
    - {text: "reconnect github for mlai.au", instead: github-integration}
actions:
  - name: scan
    description: Scan/analyse a connected repo or domain so Content Factory can work with it.
    params:
      domain: {type: string, description: "Domain like woofya.com.au if mentioned."}
  - name: scaffold
    description: Create the articles/blog infrastructure (directory, listing page) in the repo.
    params:
      domain: {type: string}
  - name: research
    description: Research what to write — topic discovery, keyword research, next-article recommendations.
    params:
      domain: {type: string}
      topic: {type: string, description: "Seed topic if the user gave one."}
  - name: write
    description: Write a new SEO-optimised article/blog post.
    params:
      domain: {type: string}
      topic: {type: string, description: "What the article should be about."}
  - name: publish_pr
    description: Publish a finished article bundle as a GitHub pull request (usually a thread follow-up).
    params:
      domain: {type: string}
---

# Content Factory Skill

This skill enables Roo to manage the content generation workflow in Slack, acting as the liaison between users and the Content Factory pipeline.

## Role: Content Factory Liaison

You are an agent responsible for managing the content generation workflow in Slack.

### Trigger
You receive a `topic_confirmation_request` event from the backend containing:
- Selected keyword/topic
- Reasoning/metrics (volume, difficulty, tier)
- Potential alternatives

### Responsibilities
1. **Present Options**: Format the research results into a clear, interactive Slack message using Block Kit.
2. **Explain Reasoning**: Briefly summarize why the main topic was chosen (e.g., "High opportunity score of 85.2 with low competition").
3. **Handle Confirmation**:
   - If user clicks "Approve": Trigger the article generation for the selected topic.
   - If user clicks "Pick Alternative": Present a menu or simple way to select one of the `top_alternatives`.
   - If user clicks "Cancel": Abort the job.

### Tone
Professional, concise, and helpful. Focus on the SEO metrics to justify the recommendation.

## Parameters

- **action**: The user's intent (optional) - one of "scan", "scaffold", or "write". Use "scan" for requests about scanning/analysing/connecting a repo. Use "scaffold" for requests about creating an articles directory, articles page, blog page, or setting up the blog structure. Use "write" for requests about writing or generating articles/blog posts. If unclear, omit.
- **domain**: The user's website domain (required) - e.g., "mlai.au"
- **topic**: The article topic or title (optional) - e.g., "How to Find a Technical Co-Founder". If omitted, triggers "Auto Mode" (research & write).
- **target_keyword**: SEO target keyword (optional) - e.g., "find technical cofounder"
- **competitors**: List of competitor domains for discovery mode (optional)
- **confirmed**: Boolean flag indicating user has accepted the disclaimer (optional - internal use)

## Workflow

### Step 1: Extract Parameters
Parse the user's request to identify:
- Their domain (from context or ask if not provided)
- The topic they want to write about (optional)
- Any specific keywords they mentioned

If the user is vague (e.g., "write me some content" or "auto write"), assume they want **Auto Mode**.
If the user provides a specific idea (e.g. "write about X"), extract it as **topic**.

### Step 2: Confirm Before Starting
Before starting generation, confirm the details with the user:

**If Topic Provided:**
```
I'll generate an article for {domain} about "{topic}" targeting "{target_keyword}".
Sound good? 👍
```

**If Auto Mode (No Topic):**
```
I'll research your competitors and automatically write the best article for {domain}.
Ready to go? 🚀
```

### Step 3: Start Generation
Use the `generate_article` function from `client.py` to start the job.

The function returns a `job_id` which is used to track progress.

For repo-backed actions, treat GitHub auth as a preflight gate:
- Run scan/scaffold work only after GitHub access is confirmed for the target domain.
- If the user chooses `publish_code`, reconnect GitHub before queueing the article run when auth is missing or expired.
- If the user chooses `content_only`, continue without GitHub auth.

### Step 4: Topic Confirmation (Auto Mode)
When research is complete, the backend sends a `topic_confirmation_request` callback.
Roo displays an interactive card with:
- Recommended topic with SEO metrics
- Alternative topics in a dropdown
- Approve/Cancel buttons

### Step 5: Monitor Progress
Poll the job status and update the user with progress milestones:
- 🔍 **Researching** (0-20%) - Analyzing competitors and gathering data
- 📋 **Strategizing** (20-40%) - Creating content brief and outline
- ✍️ **Writing** (40-80%) - Drafting the article content
- ✨ **Optimizing** (80-90%) - SEO optimization and polish
- 🚀 **Publishing** (90-100%) - Creating PR and preview

Only send updates when progress changes significantly (every 20% or major step change).

### Step 6: Report Success
When complete, provide the user with:
- 👀 **Preview URL** - Cloudflare preview link
- 💻 **PR URL** - GitHub Pull Request for review
- Summary of what was created

Example success message:
```
✅ Article Published!

Topic: ai startup accelerator
URL: https://mlai.au/articles/ai-startup-accelerator
PR: https://github.com/drsamdonegan/mlai-au/pull/205
```

## Response Style

- Use encouraging language during progress updates
- Celebrate completion with enthusiasm
- Provide clear links and actionable next steps
- Use emojis appropriately to convey status
- Keep the Australian casual tone (mate, no worries, ripper, etc.)

## Error Handling

If generation fails:
1. Apologize briefly
2. Explain what went wrong (if known)
3. Suggest trying again or reaching out for help

If the failure is an auth blocker with `error_code: AUTH_REQUIRED`:
1. Do not describe it as a generic pipeline failure
2. Start the GitHub reconnect flow for the active domain
3. Keep the pending action so the user can retry after reconnecting

Example:
```
Sorry mate, ran into a snag with that article generation.
The AI writer seems to be having a moment. Mind trying again in a few? 🤔
```
