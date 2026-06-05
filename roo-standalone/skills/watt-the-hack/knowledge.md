# Watt The Hack — Event Knowledge Base

Roo answers participant questions in #watt-the-hack using ONLY the facts below.
Source: watt-the-hack.com and the in-app participant docs (mlai.au/watt-the-hack/docs).

## At a glance
- **Event:** Watt The Hack — an AI and energy hackathon where teams automate homes and run the grid that powers them.
- **Dates:** Friday 5 June 2026 and Saturday 6 June 2026 (two days).
- **Location:** Melbourne. (Exact venue/address is shared with registered participants.)
- **Who it's for:** Students and early professionals. Beginners are welcome — there are beginner-friendly tracks.
- **Team size:** Minimum 2, maximum 6. You can come alone and team up at the event.
- **Register:** On Luma (the "Register Now" / "Get Tickets" button on watt-the-hack.com). Early-bird tickets are limited.
- **Cost to attend:** Ticketed via Luma; food (lunch, dinner, snacks) is provided.

## What it is
Watt The Hack is about building AI + energy solutions with real trade-offs — energy, cost, comfort and reliability. You pick ONE challenge to focus on. The challenges are: the **Pitching Challenge** (Base44), the **Grid Guardian Challenge** (City of Melbourne, advanced), and the **Smart Home Challenge** (Amber Electric). Each challenge aligns with one of the prizes. You do NOT have to do more than one track.

## Challenges / Tracks
You choose one track. Beginners are welcome in the Pitching and Smart Home tracks; Grid Guardian is for advanced coders.

### Pitching Challenge — Base44 (beginners & advanced)
- **What it is:** Less about how much code you write, more about insight, teamwork and a compelling story. There's a big prize at the end.
- **What you do:** Interview mentors and industry experts, attend the talks, choose a real problem in the Australian energy grid, hack together a demo or MVP (moving fast with Base44 and the AI tools on hand), then pitch your solution to a panel of judges on Saturday evening.
- **Your solution can be anything** that tackles a real problem in Australia's energy system — a consumer energy app, a smarter way to manage a household or neighbourhood's power, a grid or market mechanism, a hardware concept, etc. As long as it addresses a genuine user need and meets the judging criteria, you'll score well.
- **How to do well (Double Diamond):** interview mentors → identify and define a specific problem → ideate and prototype (an MVP/mockup built with Base44 is enough) → test and iterate with mentors/users → deliver a concise, compelling pitch (problem, who benefits, how it works, why it's better).
- There is a "How to Pitch Your Idea" guide linked from the in-app docs.

### Grid Guardian Challenge — City of Melbourne (advanced)
- **What it is:** An advanced **Python coding** track. You write a controller that operates a simulated Australian city power grid (single node), balancing generation and demand at each timestep and protecting the grid from challenges (some common, some uncommon).
- **Goal / scoring:** Your score is the **total cost accumulated over the run — lower is better, and a negative score means you turned a profit.** Submitting the provided starter template scores 0; you must beat the naive baseline. The closer to the optimal solution, the closer to 100 points.
- **Timestep:** one step = 15 minutes (0.25 h). MW = power/rate; MWh = energy (10 MW for one step = 2.5 MWh).
- **What your controller controls each step (an `action` dict):** `battery_flow_mw` (positive = discharge to grid, negative = charge), `emergency_generator` (MW of diesel, expensive backup), `curtail_solar` (MW of solar to disconnect to avoid overvoltage), and — when the scenario enables it — `fcas_reserve_mw` (MW of inverter capacity kept on standby for frequency control). Omitted keys default to 0; out-of-range values are clamped.
- **Avoid:** blackouts (unmet demand) and overvoltage (exporting beyond the cap) — both carry severe penalties. The biggest single import spike is billed as a one-off demand charge, so peak-shaving matters.
- **Submit:** through the in-app Submission Portal — paste your code into the editor (no zipping/CLI). Your code is either a `controller(state)` function or a Strategy class with a `step(self, state)` method.
- **LLM use:** an `OPENAI_API_KEY` is injected into your container; only call an LLM in `plan`/`replan`, never in `step`; use fast models; the whole evaluation must finish within ~14 minutes or it times out (a timeout is a free retry).
- **Attempts:** 3 submissions per scenario; the final "Gauntlet" allows 1 — so playtest locally first (`pip install "watt-the-hack[playtest]"`, then `python -m watt_the_hack.playtest strategy.py --scenario t1_welcome --open-report`).

### Smart Home Challenge — Amber Electric (beginners & advanced)
- **What it is:** The beginner-friendly, **no-code** track sponsored by Amber Electric. You train an AI to run a simulated smart home — watch an Australian family go about their day, then automate the house to balance **energy, cost and house mood** (comfort), shifting usage to cheaper, greener times.
- **What you build:** a SENSE → THINK → ACT automation pipeline using **drag-and-drop blocks**. Pick Inputs (Smart Meter, Temperature Sensor, Weather Forecast), a Schedule (Time of Day, Day of Week, Price Signal), a Brain (ChatGPT, Claude or Gemini), Actions (Shift Load, Reduce Usage, Charge Battery) and Outputs (Smart Plugs, Battery System, EV Charger), then hit **Deploy** and watch the house react live.
- **Campaign:** capabilities unlock over a multi-day campaign on a shared clock — start with a simple light switch, then add automations, schedules, and the full board. Bank savings and spend them in the in-app Upgrade Shop on permanent upgrades.
- **Scoring:** you manage Energy (kWh), Carbon (kg) and Comfort/Mood %, which drive your score; Money is a resource you manage. The best runs shift heavy loads into cheap, low-carbon windows rather than just switching things off.
- **To start:** you must be in a valid team (2–6) and click "Start your house" before you can deploy.

## Prizes
Prizes are in **OpenAI API credits** and align with the challenges:
- 🥇 1st place — **$10,000 USD** in OpenAI API credits
- 🥈 2nd place — **$5,000 USD** in OpenAI API credits
- 🥉 3rd place — **$1,000 USD** in OpenAI API credits
- **PLUS the top 5 teams get ChatGPT Pro for a year.**

## Judging
**Pitching (Base44) rubric — 6 categories, 60 points total:**
- Innovation — /5
- Usefulness (addresses real user needs / a meaningful problem) — /10
- Viability (scalable, cost-effective, realistic to adopt) — /10
- Technical Feasibility — /10
- Business Readiness (clear value proposition; can be implemented/funded/commercialised) — /10
- Sustainability (minimises long-term environmental impact) — /5

Scores between semifinal judges are normalised for fairness. On Saturday: pitching-track submissions open at 5:00 PM (semifinals), finalist teams pitch to the judges at 7:45 PM, and winners are announced at 8:30 PM.

**Judging panel:** David Gilmore (MLAI), Mat Brennan (Amber Electric), Doron Bahar (Base44), Julian Featherston (HAL Systems).

The **Grid Guardian** track is scored automatically by total accumulated cost (lower is better). The **Smart Home** track is scored on energy, carbon and comfort.

## Schedule
**Friday 5 June 2026 — welcome, trivia and networking**
- 5:30 PM — Doors open & registration
- 6:00 PM — Welcome from MC and sponsor notes
- 6:15 PM — Keynote & welcome
- 6:25 PM — Event overview from Dr Sam Donegan
- 6:40 PM — First round of trivia
- 7:00 PM — Break for dinner & drinks
- 7:25 PM — Second round of trivia
- 7:45 PM — Third round of trivia
- 8:15 PM — Networking
- 9:30 PM — Event close

**Saturday 6 June 2026 — hack day, pitches and awards**
- 10:30 AM — Doors open & registration
- 11:00 AM — Welcome from MC and sponsor notes
- 11:25 AM — Keynote speakers on Australia's energy/grid challenges
- 11:40 AM — Event/track overview from the MC
- 12:00 PM — Hacking begins; lunch arrives; mentors available
- 1:00 PM / 2:00 PM / 3:00 PM — Speaker sessions (Boardroom, Level 1)
- 4:30 PM — Snacks and coffee
- 5:00 PM — Submissions for the pitching track (semifinals)
- 6:30 PM — Dinner
- 7:30 PM — Closing ceremony
- 7:45 PM — Finalist teams pitch to the judges
- 8:20 PM — Final scores calculated
- 8:30 PM — Winners announced; networking follows
- 9:30 PM — Event closes

## Speakers & mentors
- Dr Sam Donegan — Medical Doctor, Full-Stack/AI Engineer and Entrepreneur (speaker)
- David Gilmore — Cyber Security Analyst, AI Security Researcher & Teacher (speaker; judge)
- Julian Featherston — Co-Founder & Co-CEO at HAL Systems (speaker; judge)
- Hao Wang — ARC DECRA Fellow, Senior Lecturer in Data Science & AI at Monash University (speaker)
- Doron Bahar — AI Product Manager @ Eftsure; AI consultant & community leader (speaker; judge)
- Mat Brennan — Software Engineer at Amber Electric (speaker; judge)
- Dylan Lynton — Machine Learning Engineer at Amber Electric (mentor)
- Dr Bill Lilley — CEO, RACE for 2030 CRC (speaker)
- James Kahn, PhD — Principal Software Engineer at HAL Systems (mentor)
- Scott Falkner — OpenAI (speaker)
- Jeremy Kelaher — mentor

## Sponsors & partners
Codec, City of Melbourne, Base44, Amber, RACE for 2030, AI Engineer Melbourne, Stone & Chalk, Web Directions, HAL Systems, ACASE. (OpenAI provides the prize credits.)

## Code of conduct (summary)
Be respectful of teammates, mentors, organisers and other teams. Contribute honestly, use equipment safely, and follow organiser instructions. Treat other teams' ideas and strategies as learning opportunities, not targets for mockery.

## FAQ
- **Who is it for?** Students and early professionals — beginners through to advanced.
- **Do I need to know how the energy grid works?** No. Talks and mentors will get you up to speed.
- **Do I need to be a strong coder?** No. The Smart Home track is no-code (drag-and-drop) and the Pitching track is about ideas and storytelling. Grid Guardian is the advanced Python track.
- **What will we build?** Depends on your track — a pitch/demo (Base44), a Python grid controller (Grid Guardian), or a smart-home automation (Smart Home).
- **What are the prizes?** $10,000 / $5,000 / $1,000 USD in OpenAI API credits for 1st/2nd/3rd, plus ChatGPT Pro for a year for the top 5 teams.
- **How big can teams be?** 2 to 6 people.
- **Can I come alone?** Yes — you can form or join a team at the event (teams must be at least 2).
- **Do I need to bring an idea?** No.
- **Do I have to do both Smart Home and Grid Guardian?** No — pick one track.
- **Can beginners participate?** Yes — the Pitching and Smart Home tracks are beginner-friendly.
- **Is it only for students?** No — students and early professionals.
- **Will food be provided?** Yes — dinner on Friday, and lunch, snacks and dinner on Saturday.
- **Where is it?** Melbourne (exact venue shared with registrants).
- For anything not covered here, point people to watt-the-hack.com or an organiser.

## Contact & links
- Website: watt-the-hack.com · Register on Luma.
- MLAI: mlai.au · email hi@mlai.au · Instagram @mlai_aus.
