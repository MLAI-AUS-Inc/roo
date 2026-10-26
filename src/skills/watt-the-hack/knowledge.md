# Watt The Hack — Event Knowledge Base

Roo answers participant questions in #watt-the-hack using ONLY the facts below.
Source: watt-the-hack.com (incl. the official FAQ), the Speakers & Mentors pack, and
the in-app participant docs (mlai.au/watt-the-hack/docs).

## At a glance
- **Event:** Watt The Hack — a live AI and energy hackathon where teams automate homes and run the grid that powers them. Presented by MLAI.
- **The idea:** what happens in the home affects the grid, and what happens in the grid affects the home. One track runs households; another runs the city energy system; a third pitches solutions to a real Australian energy problem.
- **Dates:** Friday 5 June 2026 (team formation, trivia, dinner, networking) and Saturday 6 June 2026 (the main hack day, demos, judging and awards).
- **Venue:** **Stone & Chalk Melbourne, 121 King Street, Melbourne VIC 3000** (a Melbourne startup hub). ~6 min walk from Southern Cross station.
- **Who it's for:** AI builders, developers, data scientists, students, early-career builders, energy/climate/infrastructure people, designers, product thinkers, researchers, founders and tinkerers. You do NOT need to be an energy expert — just curious and willing to build.
- **Team size:** Minimum 2, maximum 6. You can come alone and team up at the event.
- **Register:** On Luma (the "Register Now" / "Get Tickets" button on watt-the-hack.com). Early-bird tickets are limited.
- **Food:** Provided (dinner Friday; lunch, snacks and dinner Saturday).
- **What to bring:** Your laptop, charger, and anything you like building with. No finished idea or energy expertise needed.

## What it is
Watt The Hack is a practical AI + energy hackathon — people build, test and improve working systems during the program, making energy trade-offs visible and hands-on: energy, money, comfort and reliability. Teams also uncover a real Australian energy-sector problem by talking to speakers and mentors, and hack together a solution to win the main prize.

**About MLAI:** a not-for-profit AI community bringing together students, builders, founders, engineers, designers, researchers and domain experts who want to learn, build and ship — running everything from monthly talks to large-scale hackathons and startup programs.

## Tracks / Challenges
You choose ONE challenge to be judged on. (A team may enter multiple challenges if they want, but it's not required, and each team is eligible for only one prize.) Beginners are welcome — Pitching and Smart Home have beginner-friendly pathways; Grid Guardian is the advanced coding track.

### Track 1 — Amber Electric Smart Home Sprint (beginner-friendly)
- **What it is:** Teams manage an individual simulated home — do chores, respond to the family living there, buy upgrades, and write simple automation rules / AI logic to balance **energy, cost, carbon and house mood (comfort)**. It includes coding, but it's not only coding.
- **You won't build the simulation from scratch** — the starter setup / coding environment is provided at the event. The focus is on writing automation logic that makes smarter household energy decisions, shifting usage to cheaper, greener times.
- **How it works in-app:** a SENSE → THINK → ACT pipeline built with drag-and-drop blocks — pick Inputs (Smart Meter, Temperature Sensor, Weather Forecast), a Schedule (Time of Day, Day of Week, Price Signal), a Brain (ChatGPT, Claude or Gemini), Actions (Shift Load, Reduce Usage, Charge Battery) and Outputs (Smart Plugs, Battery System, EV Charger), then Deploy and watch the house react. Capabilities unlock over a multi-day campaign; bank savings and spend them in an in-app upgrade shop. (One of the simulated families is "The Quokkas".)
- **Start:** you must be in a valid team (2–6) and click "Start your house" before you can deploy.

### Track 2 — City of Melbourne Grid Guardian (advanced)
- **What it is:** Teams manage the wider city energy system — running a simulated Australian city grid. You balance supply and demand, manage storage and infrastructure, and respond to weather, demand spikes and grid stress while trading off cost, carbon and reliability.
- **How it works:** an advanced **Python** track. You write a controller that operates the grid each timestep (one step = 15 minutes). It controls battery charge/discharge, emergency diesel, solar curtailment, and (when enabled) FCAS standby reserve.
- **Scoring:** your score is the total cost accumulated over the run — **lower is better, and negative means you turned a profit.** The provided starter template scores 0; beat the naive baseline. Avoid blackouts (unmet demand) and overvoltage (over-export) — both carry severe penalties; the biggest single import spike is billed as a one-off demand charge, so peak-shaving matters.
- **Submit / test:** through the in-app Submission Portal (paste your code, no CLI). An `OPENAI_API_KEY` is injected into your container; only call an LLM in `plan`/`replan`, never each step; the whole run must finish within ~14 minutes. Playtest locally first: `pip install "watt-the-hack[playtest]"`.

### Track 3 — Base44 Pitching Track (beginner & advanced)
- **What it is:** Teams choose a real Australian energy problem, talk to mentors and industry professionals, build or mock up a solution (moving fast with Base44 and the AI tools on hand), and pitch it to the judges on Saturday evening. Great for teams into product ideas, demos, storytelling and real-world impact. This track plays for the main prize.
- **It's less about how much code you write** and more about insight, teamwork and a compelling story. Your solution can be anything — a consumer energy app, a smarter way to manage a household or neighbourhood's power, a grid or market mechanism, a hardware concept — as long as it addresses a genuine user need and meets the judging criteria.
- There is a "How to Pitch Your Idea" guide linked from the in-app docs.

## Team rules
- Minimum 2, maximum 6 people per team.
- One team can enter multiple challenges.
- Teams cannot split or form sub-teams.
- One participant can only be part of one team.
- Each team is only eligible for one prize.
- No team? No problem — a fun trivia exercise (with extra prizes) at the start mixes people into teams with the right blend of skills.

## Prizes
Prizes are in **OpenAI API credits**, thanks to OpenAI:
- 🥇 1st place — **$10,000 USD** in OpenAI API credits
- 🥈 2nd place — **$5,000 USD** in OpenAI API credits
- 🥉 3rd place — **$1,000 USD** in OpenAI API credits
- **PLUS the top 5 teams get ChatGPT Pro for a year.**

## Judging
Judges look for projects that are **useful, relevant to the energy challenge, technically strong, clearly explained, and supported by a working demo or convincing prototype.** The best projects show a real problem, a clever solution and a believable path forward.

**Pitching (Base44) rubric — 6 categories, 60 points total:** Innovation (/5), Usefulness (/10), Viability (/10), Technical Feasibility (/10), Business Readiness (/10), Sustainability (/5). Scores between semifinal judges are normalised for fairness.

On Saturday: pitching-track submissions open at 5:00 PM (semifinals), finalist teams pitch to the judges at 7:45 PM, and winners are announced at 8:30 PM. The Grid Guardian track is scored automatically by total cost; the Smart Home track on energy, carbon and comfort.

**Judging panel:** David Gilmore (MLAI), Mat Brennan (Amber Electric), Doron Bahar (Base44), Julian Featherston (HAL Systems).

## Schedule
**Friday 5 June 2026 — team formation, trivia and networking**
- 5:30 PM — Doors open & registration
- 6:00 PM — Welcome from MC and sponsor notes
- 6:15 PM — Keynote & welcome (John Allsop — AIE, Web Directions)
- 6:25 PM — Event overview (Dr Sam Donegan)
- 6:40 PM — First round of trivia
- 7:00 PM — Break for dinner & drinks
- 7:25 PM — Second round of trivia
- 7:45 PM — Third round of trivia
- 8:15 PM — Networking
- 9:30 PM — Event close

**Saturday 6 June 2026 — hack day, pitches and awards**
- 10:30 AM — Doors open & registration
- 11:00 AM — Welcome from MC and sponsor notes
- 11:25 AM — Keynote speakers on Australia's energy/grid challenges (Bill Lilley — RACE for 2030; Julian Featherston — HAL Systems)
- 11:45 AM — Event/track overview (MC and Monash DeepNeuron)
- 12:00 PM — Hacking begins; lunch arrives; mentors available
- 1:00 PM / 2:00 PM / 3:00 PM — Speaker sessions (Boardroom, Level 1): Hao Wang (Monash), Mat Brennan (Amber Electric), David Gilmore (MLAI)
- 4:30 PM — Snacks and coffee
- 5:00 PM — Submissions for the pitching track (semifinals)
- 6:30 PM — Dinner
- 7:30 PM — Closing ceremony (Dr Sam Donegan)
- 7:45 PM — Finalist teams pitch to the judges
- 8:20 PM — Final scores calculated (all streams)
- 8:30 PM — Winners announced; networking follows
- 9:30 PM — Event closes

## Speakers & mentors
- Dr Sam Donegan — Medical Doctor, Full-Stack/AI Engineer and Entrepreneur (speaker)
- David Gilmore — Cyber Security Analyst, AI Security Researcher & Teacher; MLAI (speaker; judge)
- Julian Featherston — Co-Founder & Co-CEO at HAL Systems (speaker; judge)
- Hao Wang — ARC DECRA Fellow, Senior Lecturer in Data Science & AI at Monash University (speaker)
- Doron Bahar — AI Product Manager @ Eftsure; AI consultant & community leader; Base44 (speaker; judge)
- Mat Brennan — Software Engineer at Amber Electric (speaker; judge)
- Dylan Lynton — Machine Learning Engineer at Amber Electric (mentor)
- Dr Bill Lilley — CEO, RACE for 2030 CRC (speaker)
- James Kahn, PhD — Principal Software Engineer at HAL Systems (mentor)
- Scott Falkner — OpenAI (speaker)
- John Allsop — AI Engineer Melbourne / Web Directions (keynote)
- Jeremy Kelaher — mentor

Mentors come from AI, startups, software, energy and climate, and are there to help teams shape, build and pitch their ideas.

## Sponsors & partners
Codec, City of Melbourne, Base44, Amber Electric, RACE for 2030, AI Engineer Melbourne, Stone & Chalk, Web Directions, HAL Systems, ACASE. OpenAI provides the prize credits. Venue partners: Stone & Chalk Melbourne and Melbourne AI Lab.

## Getting there
- **Address:** Stone & Chalk, 121 King Street, Melbourne VIC 3000.
- **Public transport:** ~6 min walk from Southern Cross station.
- **Parking:** multiple all-day car parks nearby — 522 Flinders Lane, 588 Lt Bourke St, 542 Little Bourke St. Pre-booking is recommended to secure a spot.

## FAQ
- **Who is this hackathon for?** AI builders, developers, students, energy nerds, designers, founders, tinkerers — people who like chaotic weekend projects with real-world stakes. You don't need to be an energy expert; just curious and willing to build.
- **Do I need to know how the energy grid works?** No — the important bits are explained during the event. The point is to bring AI people and energy people together to learn from each other.
- **Do I need to be a strong coder?** Not necessarily. Coding helps, but great teams also need people who design, think through user problems, explain ideas, research and pitch.
- **What will we be building?** AI-powered ideas, tools, agents, simulations or products that tackle real energy problems — around carbon, cost, convenience, grid behaviour, smart homes and energy decision-making.
- **What are the prizes?** $10,000 / $5,000 / $1,000 USD in OpenAI API credits for 1st/2nd/3rd, plus ChatGPT Pro for a year for the top 5 teams.
- **How big can teams be?** 2 to 6. One team can enter multiple challenges; teams can't split or form sub-teams; one participant is on only one team; each team is eligible for one prize.
- **Can I come alone?** Yes — a trivia exercise at the start (with extra prizes) mixes people into balanced teams.
- **Do I need to bring an idea?** No. You can, but challenge prompts, examples and problem spaces are shared at the event.
- **What should I prepare?** Bring your laptop, charger and anything you build with. No finished idea or energy expertise required.
- **Will we complete everything during the hackathon?** Yes — the main build, testing, demo prep and pitching all happen during the event. You can brainstorm beforehand but don't need a finished project.
- **What is the competition format?** Teams of 2–6 choose one challenge (Smart Home, Grid Guardian or Pitching). Friday is kickoff, food, team formation and intros; Saturday is the main hack day, then demos, judging and awards.
- **Will there be mentors?** Yes — from AI, startups, software, energy and climate.
- **What are the judging criteria?** Projects that are useful, relevant to the energy challenge, technically strong, clearly explained, and backed by a working demo or convincing prototype.
- **Can beginners actually participate?** Yes — beginner-friendly pathways, clear prompts and plenty of support.
- **Is this only for students?** No — open to builders, professionals, founders, researchers and anyone interested in AI and energy.
- **Is Smart Home mainly coding?** It includes coding but isn't only coding — you manage a simulated household, respond to family behaviour, buy upgrades, and write simple automation rules / AI logic to balance energy, cost, carbon and mood.
- **Where will we write the Smart Home automation code?** The starter setup / coding environment is provided at the event — you won't build the simulation from scratch; the focus is the automation logic.
- **What does Grid Guardian mean by "managing the energy system"?** Running a simulated city energy system — balancing supply and demand, managing storage and infrastructure, and responding to weather, demand spikes and grid stress while balancing cost, carbon and reliability.
- **Do we have to do both Smart Home and Grid Guardian?** No — choose one challenge. Teams may enter multiple if they want, but each participant is on only one team and each team wins only one prize.
- **Is there a Pitching Challenge?** Yes — choose a real energy problem, talk to mentors, build or mock up a solution, and pitch to judges. Good for product ideas, demos, storytelling and real-world impact.
- **When will the final structure be available?** The high-level structure is already on the website; final challenge briefs, technical setup and judging details are shared closer to the event and explained at kickoff.
- **Do we keep the IP for what we build?** Yes, unless otherwise stated — your team owns what you create. MLAI may ask to showcase photos, demos or summaries from the event.
- **Will food be provided?** Yes — more details closer to the event.
- **Where is it happening?** Stone & Chalk Melbourne, 121 King Street, Melbourne VIC 3000.
- **Why energy and AI?** Energy is one of Australia's biggest, messiest and most important systems, and AI gives new ways to model, automate, forecast, explain and optimise parts of it. (Also, watching developers try to solve the grid in a weekend is objectively funny.)

## Contact & links
- Website: watt-the-hack.com · Register on Luma.
- MLAI: mlai.au · email hi@mlai.au · Instagram @mlai_aus.
