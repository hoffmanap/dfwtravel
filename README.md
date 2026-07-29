# El Paso → DFW Metro: Onward Destination Analysis

An interactive breakdown of where El Paso-based travelers go in the first 24 hours after
arriving at a Dallas–Fort Worth area airport, built from device mobility (pathing) data and
rendered in `index.html`.

Open `index.html` in any browser — it's self-contained (data is embedded), but it does load
map tiles and fonts from the internet, so you'll need a connection the first time it renders.

## What's in this analysis

- **7,402** unique El Paso-flagged devices generated **22,210** matched visits across
  **183** DFW-metro municipalities between **2024-12-31** and **2026-07-01**.
- **16,125** visits originated from a **DFW Airport** arrival; **6,085** from **Dallas Love
  Field**. **McKinney National** recorded zero matched arrivals in this dataset.
- **76.0%** of the 9,734 devices flagged as El Paso-based in the visitor-home panel went on
  to generate at least one matched airport-arrival trip during the observation window.
  (Treat this "conversion rate" as a proxy tied to how the upstream panel defines
  "El Paso-based," not a general-population travel rate.)

## Methodology

1. **Home-location flagging** — devices in `visitor_home.tsv` were flagged as El Paso-based
   using their `Common Evening Metro` field.
2. **Join to pathing** — `pathing.tsv` (streamed from the zipped export) was filtered down to
   only pings from those flagged device IDs, joined on device ID.
3. **Airport arrival detection** — pings tagged to any DFW-area airport polygon (DFW's
   multiple terminal polygons were collapsed into a single "DFW Airport" label) were treated
   as arrival events, one per device per airport per calendar date.
4. **24-hour window + spatial join** — every other ping from that device in the 24 hours
   following arrival was matched against DFW Metroplex municipal boundaries
   (`Cities_Region__2025_.geojson`) via point-in-polygon lookup to assign a destination city.
5. **De-duplication** — repeat pings from the same device, in the same destination city, on
   the same local date, were collapsed into a single counted visit.
6. **Aggregation** — visits were rolled up by municipality and by origin airport, producing
   `dfw_destination_summary.csv` and `dfw_destination_detail.csv`, which feed this dashboard.

See `dfw_mobility_analysis.py` for the full, runnable pipeline.

## Reading the dashboard

- **Map** — each DFW-metro municipality is shaded by its share of matched visits (or, when an
  airport filter is applied, its share of that airport's visits specifically). Click a city
  for its full breakdown; click a row in the ranked list to jump to it on the map.
- **Filter chips** — toggle between all airports, DFW Airport only, or Dallas Love Field only.
  The map, ranked list, and percentage shares all update together.
- **Monthly / day-of-week charts** — show volume over time and by weekday, split by origin
  airport, to surface seasonality and weekly travel rhythm.

## Key caveats (also called out in the dashboard itself)

1. **Grapevine dominance is partly geographic, not behavioral.** DFW Airport's terminals,
   hotels, and rental-car facilities sit inside Grapevine's city limits, so a large share of
   Grapevine's 43% share reflects proximity to the airport rather than a distinct onward
   trip. Treat it as a mix of "still near the airport" and genuine visits to Grapevine proper.
2. **A small number of devices travel constantly.** 98 devices (1.3% of all matched devices)
   generated 20+ trips each, together accounting for 15.2% of total visits — most likely
   airline/airport staff, crew, or frequent commuters rather than one-off travelers. They can
   pull city-level totals upward.
3. **McKinney National has no matched data.** No El Paso-flagged device recorded an arrival
   ping there in this dataset, so it's excluded from the airport filter — this may reflect
   true travel patterns, limited McKinney polygon coverage, or a small underlying sample size,
   and is worth checking against the raw pathing data if McKinney volume matters to you.
4. **"El Paso-based" is a panel definition, not a population.** The 76% conversion rate and
   all downstream counts depend entirely on how the visitor-home panel's evening-location
   field identifies El Paso residents — it's a consistent proxy across this analysis, but
   not a validated demographic statistic.

## Files

- `index.html` — the interactive dashboard (map, rankings, trend charts, caveats).
- `README.md` — this file.
- `dfw_mobility_analysis.py` — the pipeline that produced the source CSVs from the raw
  visitor-home and pathing exports.
- `dfw_destination_summary.csv` / `dfw_destination_detail.csv` — source data for this
  dashboard (summary = aggregated counts; detail = one row per deduped device-visit).
