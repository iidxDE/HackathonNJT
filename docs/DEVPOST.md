# Crossing — Devpost draft

**Tagline (one line):** The fastest train on paper isn't always the best way to make your 9:00 at Penn. Crossing ranks NJ → NY Penn commutes by what they really cost: fare, parking, tolls, fuel, your time, and the chance you're late.

---

## Inspiration

Every weekday, thousands of New Jersey commuters drive to a station and take a train into NY Penn. The choice looks simple: pick the station and the train that gets you in on time. But the timetable lies by omission. It doesn't tell you that one train is reliably 10 minutes late, that one station's lot is resident-only, or that one option pays two fares while another pays tolls. And this month, the [Portal North Bridge](https://www.njtransit.com/press-releases/portal-north-bridge-enters-final-phase-construction-work-begins-put-final-track) cutover sent every weekday Midtown Direct train to Hoboken instead of Penn. The map changed overnight.

I wanted a tool that answers the question commuters actually have: *which option is cheapest once you price in the risk of being late?*

## What it does

For an origin, a deadline at NY Penn and a few personal settings (value of time, how bad being late is, car mpg), Crossing compares options across six access stations (Chatham, Summit, Short Hills, Secaucus, Newark Penn and Hoboken via PATH) plus driving all the way. For each one it shows:

- **Total daily cost**, itemized and sourced: NJT fare, PATH fare (including the Portal cutover's free cross-honoring), parking, tolls and fuel.
- **Reliability from real data**: P(on time), p50/p90 arrival and which train you'll actually end up on, from a Monte Carlo over observed NJ Transit delays.
- **A risk-adjusted cost** = money + value of time + expected cost of lateness, and a ranking by it.
- **Which fragile infrastructure each option depends on**: Portal bridges, the 1910 North River tunnels, NY Penn and the PATH tunnels.

**The core claim, demonstrated:** ranking by risk-adjusted cost picks a different option than ranking by schedule. For the demo trip (Tue 10-13, arrive by 09:00), the timetable plan from Newark Penn is train 3926, leaving home at 07:56, which is on time only **30 of 100 mornings**. Crossing recommends train 3224, leaving at 07:40: **99 of 100 mornings**, same $30.06/day, risk-adjusted $70.63. Across a 12-setting sweep of lateness penalty × safety margin, the top pick changes in **9 of 12**.

**Disruptions.** A "Penn suspended" scenario removes every train into or out of NY Penn, and the router finds what's left. Newark and Secaucus direct trains grey out, Chatham and Summit via Hoboken + PATH move to the top, and the table shows Δ rank and Δ cost for every row. A real archived NJ Transit alert can be replayed through the same pipeline and is correctly judged *not affecting your trip*.

## How I built it

Everything heavy happens offline; the app reads only local files and runs with no network.

- **Delay data:** my own archive of NJ Transit's real-time rail feed, polled every ~30 s since Thu 10-08 (~98% of polls captured), cleaned of stale and partial snapshots and joined to GTFS (≥ 99% join rate after a zero-padding fix on train numbers). Delays are pooled by line × direction × time period and sampled as **whole historical runs**, so delays stay correlated along a train's trip.
- **Routing:** my own **range RAPTOR** over NJ Transit rail + PATH GTFS (1,723 trips, 176 stops), run once per access station, using a network-agnostic loader I can point at other GTFS feeds. Cost and reliability are deliberately *not* RAPTOR criteria; they're scored on its Pareto set.
- **Exclusion runs:** pure schedule-Pareto never surfaces slower routes that avoid fragile assets. So each station also gets a run with NY Penn banned, which found a Penn-free route: Secaucus → lower level → Pascack Valley train to Hoboken → PATH. The same mechanism powers the disruption scenario.
- **Scoring:** a vectorized Monte Carlo (2,000 draws, fixed seed) with common random numbers across options. Drive time comes from Google Routes API traffic models (cached at build time); the rider catches the first catchable train, and lateness is capped.
- **Alerts:** archived NJ Transit station messages are parsed by **Gemini** into structured JSON (kind, lines, trains, cause, delay, direction, Penn disruption) with a JSON schema at temperature 0, cached, and replayed offline. I hand-labeled a sample and verified each message's train against GTFS (25 of 34 verified).
- **UI:** Streamlit, with itemized costs, delay-pool evidence and assumptions for every row.
- **Discipline:** 172 tests, golden regression snapshots (the router reproduces the hand-coded options, every difference explained, and exclusion runs are checked to change nothing when off), and every eval report stamped with the git commit that produced it.

## Challenges I ran into

- **The map changed mid-hackathon.** The Portal cutover took effect Monday 10-12, so the delay history comes from the old timetable while routing uses the new one. Pools are keyed by line and period rather than train number, so they carry over.
- **Train numbers don't join naively.** GTFS zero-pads them ("0400") and the feed doesn't ("400"); a plain string join silently dropped most Gladstone and Morris & Essex trains.
- **The feed's Penn time isn't an arrival time.** On some runs it's arrival plus a 13-minute terminal dwell; I confirmed this against NJ Transit's own station schedule before trusting the delay measure.
- **Schedule-optimal routing hides the interesting options**, which led to the exclusion runs.
- **Matching alerts to trains** needed care: train numbers repeat across days and timetables, and "12:08 AM" trains belong to the previous service day.
- **Free-tier Gemini quota** (20 requests/day per model) meant each message got exactly one extraction call.

## Accomplishments that I'm proud of

- The core claim holds on real data, not a toy: the risk-aware pick differs from the timetable pick in 9 of 12 settings, and the reason is visible (on time 30/100 vs 99/100).
- A from-scratch RAPTOR that reproduces the hand-built options (every difference explained) and finds a Penn-free route nobody hand-coded.
- Gemini extraction scored against hand-reviewed labels, not assumed: on 20 reviewed messages, 100% on alert kind, cause, train numbers and delay minutes, and 95% on line names.
- Honest uncertainty: every price has a source and date, every estimate is labeled, and options with an incomplete cost are shown but never ranked.

## What I learned

Reliability is a property of *runs*, not stops: sampling whole historical runs instead of independent stop delays changes the answer. A good router should find options the schedule hides, not only the fastest ones. And a model's accuracy is something you measure on labeled data, not something you assume.

## What's next for Crossing

- Apply more alert types to the ranking: delay shifts, cancelled trains, line suspensions (the router already accepts any exclusion).
- A live "this train is already running late" nowcast from the real-time feed.
- More history (weeks, not days) and a post-cutover evaluation of predicted vs. actual on-time rates.
- PATH reliability from real data, and weekly/monthly pass pricing.
- Other networks: the routing core is network-agnostic and was designed alongside a Tokyo version.

## Known limitations (stated, not hidden)

About 1.5 weekdays of delay history, pre-cutover. PATH delays are assumed, not measured. Drive-time spread comes from Google's traffic models. Transfer times are estimates. The Secaucus → Hoboken fare isn't sourced yet, so that route is shown but unranked. Alerts beyond "Penn suspended" are display-only for now. Full list in the README.

## Built with

Python · pandas · NumPy · PyArrow · Streamlit · pytest · Google Maps Routes API · Google Gemini API · NJ Transit GTFS and real-time rail data · PATH GTFS · AWS (EC2 data collection)
