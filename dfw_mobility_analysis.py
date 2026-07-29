"""
DFW Metro Destination Analysis for El Paso-Based Travelers
============================================================

WHAT THIS SCRIPT DOES
----------------------
1. Loads the "visitor home" panel (`visitor_home.tsv`) and flags devices whose
   common-evening (home) location is El Paso, TX.
2. Loads the "pathing" panel (`pathing.tsv`) and keeps only records belonging
   to those El Paso-based devices (joined on device id).
3. Within the pathing data, finds each device's arrival event at one of the
   three DFW-area airports:
       - DFW International Airport (all terminal-specific polygons combined)
       - Dallas Love Field
       - McKinney National Airport
4. For each such arrival, looks at every other pathing ping from that same
   device in the following 24 hours and spatially joins it against the DFW
   metro municipality boundaries (Cities_Region__2025_.geojson) to figure out
   which city/town the traveler visited.
5. De-duplicates so a device visiting the same municipality multiple times on
   the same local date only counts once (prevents a device that pings 10x in
   one afternoon in Frisco from being counted as 10 "visits").
6. Aggregates counts of unique device-visits by municipality, with an
   optional filter on which origin airport to include.

REQUIRED INPUT FILES (place these next to this script)
--------------------------------------------------------
1. visitor_home.tsv   -- rename your visitor-home export to this exact name.
   Expected columns (tab-separated), based on the sample you shared:
     Hashed Ubermedia Id, Polygon Name, Visit Timestamp,
     Common Evening Lat, Common Evening Long, Common Evening Country,
     Common Evening State, Common Evening Postal, Common Evening Census,
     Common Evening Metro, Date, Time, Day of Week, Time Zone

2. pathing.tsv (or a .zip containing it) -- your pathing export. This can be
   left zipped: the script streams rows directly out of the archive in
   chunks, filtering down to El Paso-based device IDs as it goes, so the
   full multi-GB file is never fully extracted to disk or loaded into memory
   at once. If your zip has more than one file inside it, set
   PATHING_ZIP_MEMBER (near the top of the script) to the exact filename to
   read. Expected columns (tab-separated), based on the sample you shared:
     Polygon ID, Hashed Device ID, Lat of Observation Point,
     Lon of Observation Point, Time before appearance in polygon,
     Unix Timestamp of Observation Point, Local Date, Local Time of Day,
     Local Day of Week, Local Timezone of Observation Point

3. Cities_Region__2025_.geojson -- the DFW metro municipalities polygon file
   you uploaded. Keep the filename as-is, or change CITIES_GEOJSON below.

NOTE ON THE DEVICE ID JOIN
---------------------------
Your visitor-home file uses the column "Hashed Ubermedia Id" while the
pathing file uses "Hashed Device ID". The script treats these as the same
identifier and joins on it -- if your actual field names differ, edit the
HOME_ID_COL / PATH_ID_COL constants below.

NOTE ON AIRPORT POLYGON NAMES
-------------------------------
You mentioned DFW Airport was split into several polygons by terminal. Edit
the AIRPORT_NAME_MAP dictionary below so every raw Polygon ID string used in
your pathing file for a DFW terminal maps to "DFW Airport". Do the same if
Love Field or McKinney National were split into multiple polygons. Matching
is substring-based and case-insensitive, so partial names work.

DEPENDENCIES
------------
Only pandas and shapely are required (no geopandas needed):
    pip install pandas shapely

USAGE
-----
    python dfw_mobility_analysis.py
    python dfw_mobility_analysis.py --airport "DFW Airport"
    python dfw_mobility_analysis.py --airport "Dallas Love Field"
    python dfw_mobility_analysis.py --airport "McKinney National"
    python dfw_mobility_analysis.py --window-hours 24

Output: dfw_destination_summary.csv (aggregated counts by municipality),
        dfw_destination_detail.csv (one row per deduped device-visit).
"""

import argparse
import json
from pathlib import Path

import pandas as pd
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

# ---------------------------------------------------------------------------
# CONFIG -- edit these to match your actual field names / file locations
# ---------------------------------------------------------------------------

VISITOR_HOME_FILE = "visitor_home.tsv"
PATHING_FILE = "10144294_Dallas_Airports_pathing_x_report.zip"
# If your zip file's internal member is not a single .tsv (or is named
# something unexpected), set this to the exact filename inside the zip.
# Leave as None to auto-detect -- this works both when the zip has exactly
# one member, AND when it has multiple part-files (e.g. report_000.tsv,
# report_001.tsv, ...), in which case they'll all be read in sorted order.
PATHING_ZIP_MEMBER = None
CITIES_GEOJSON = "Cities_Region__2025_.geojson"

HOME_ID_COL = "Hashed Ubermedia Id"      # device id column in visitor_home.tsv
PATH_ID_COL = "Hashed Device ID"          # device id column in pathing.tsv

HOME_METRO_COL = "Common Evening Metro"   # e.g. "El Paso, TX"
HOME_STATE_COL = "Common Evening State"   # e.g. "TX"

# Strings that identify "home = El Paso" in the visitor-home file. Matching is
# case-insensitive substring matching against HOME_METRO_COL (falls back to
# checking postal/state if metro is blank). Add variants if your metro field
# is labeled differently (e.g. "El Paso-Las Cruces").
EL_PASO_METRO_KEYWORDS = ["el paso"]

PATH_POLY_COL = "Polygon ID"
PATH_TIME_BEFORE_COL = "Time before appearance in polygon"
PATH_UNIX_COL = "Unix Timestamp of Observation Point"
PATH_LAT_COL = "Lat of Observation Point"
PATH_LON_COL = "Lon of Observation Point"
PATH_LOCAL_DATE_COL = "Local Date"

# Map raw Polygon ID strings (as they appear in pathing.tsv) to a normalized
# airport label. Matching is case-insensitive substring matching, so you only
# need to supply a distinguishing fragment of each polygon's name -- e.g. if
# your terminal polygons are named "DFW Airport - Terminal A", "DFW Airport -
# Terminal B", etc., the single entry "dfw" below will catch all of them.
AIRPORT_NAME_MAP = {
    "dfw": "DFW Airport",
    "dallas/fort worth": "DFW Airport",
    "dallas fort worth": "DFW Airport",
    "love field": "Dallas Love Field",
    "dal ": "Dallas Love Field",
    "mckinney": "McKinney National",
    "tki": "McKinney National",
}

CITY_NAME_FIELD = "CITY"  # property name in the geojson holding city name

DEFAULT_WINDOW_HOURS = 24

# ---------------------------------------------------------------------------


def normalize_airport(raw_polygon_id: str):
    """Return the normalized airport label for a raw Polygon ID, or None."""
    if not isinstance(raw_polygon_id, str):
        return None
    low = raw_polygon_id.lower()
    for fragment, label in AIRPORT_NAME_MAP.items():
        if fragment in low:
            return label
    return None


def load_el_paso_device_ids(path: str) -> set:
    df = pd.read_csv(path, sep="\t", dtype=str, low_memory=False)
    if HOME_ID_COL not in df.columns:
        raise ValueError(
            f"Column '{HOME_ID_COL}' not found in {path}. "
            f"Available columns: {list(df.columns)}"
        )

    def is_el_paso(row) -> bool:
        metro = str(row.get(HOME_METRO_COL, "") or "").lower()
        if any(kw in metro for kw in EL_PASO_METRO_KEYWORDS):
            return True
        return False

    mask = df.apply(is_el_paso, axis=1)
    el_paso_ids = set(df.loc[mask, HOME_ID_COL].dropna().unique())
    print(f"[visitor_home] {len(df)} rows loaded; "
          f"{len(el_paso_ids)} unique El Paso-based device IDs flagged.")
    return el_paso_ids


def _open_pathing_stream(path: str, chunksize: int):
    """Return an iterator of DataFrame chunks over the pathing file, reading
    directly out of a .zip archive (without extracting it to disk) if `path`
    ends in .zip, or from a plain .tsv/.csv otherwise. Zips containing
    multiple part-files (e.g. report_000.tsv, report_001.tsv, ...) are
    streamed one member at a time, in order."""
    if str(path).lower().endswith(".zip"):
        import zipfile

        zf = zipfile.ZipFile(path)
        members = [
            n for n in zf.namelist() if not n.endswith("/")  # skip directory entries
        ]
        if PATHING_ZIP_MEMBER:
            if PATHING_ZIP_MEMBER not in members:
                raise ValueError(
                    f"PATHING_ZIP_MEMBER='{PATHING_ZIP_MEMBER}' not found in {path}. "
                    f"Members present: {members}"
                )
            members_to_read = [PATHING_ZIP_MEMBER]
        else:
            members_to_read = sorted(members)

        def gen():
            header_cols = None
            for member_name in members_to_read:
                print(f"  ...reading archive member: {member_name}")
                fh = zf.open(member_name, "r")
                reader = pd.read_csv(fh, sep="\t", dtype=str, chunksize=chunksize)
                for chunk in reader:
                    if header_cols is None:
                        header_cols = list(chunk.columns)
                    yield chunk

        return gen()
    else:
        return pd.read_csv(path, sep="\t", dtype=str, chunksize=chunksize)


def load_pathing(path: str, device_ids: set, chunksize: int = 1_000_000) -> pd.DataFrame:
    """Stream the (potentially huge / zipped) pathing file in chunks, keeping
    only rows for El Paso-based device IDs, so the full multi-GB file never
    has to sit in memory or get extracted to disk at once."""
    kept_chunks = []
    total_rows = 0
    reader = _open_pathing_stream(path, chunksize)

    for i, chunk in enumerate(reader):
        if i == 0 and PATH_ID_COL not in chunk.columns:
            raise ValueError(
                f"Column '{PATH_ID_COL}' not found in {path}. "
                f"Available columns: {list(chunk.columns)}"
            )
        total_rows += len(chunk)
        filtered = chunk[chunk[PATH_ID_COL].isin(device_ids)]
        if not filtered.empty:
            kept_chunks.append(filtered)
        if (i + 1) % 10 == 0:
            kept_so_far = sum(len(c) for c in kept_chunks)
            print(f"  ...scanned {total_rows:,} pathing rows so far, "
                  f"{kept_so_far:,} kept.")

    df = (
        pd.concat(kept_chunks, ignore_index=True)
        if kept_chunks
        else pd.DataFrame(columns=[PATH_ID_COL])
    )
    print(f"[pathing] {total_rows:,} rows scanned; {len(df):,} rows remain after "
          f"restricting to El Paso-based device IDs.")

    if df.empty:
        return df

    # numeric coercion
    df[PATH_UNIX_COL] = pd.to_numeric(df[PATH_UNIX_COL], errors="coerce")
    df[PATH_LAT_COL] = pd.to_numeric(df[PATH_LAT_COL], errors="coerce")
    df[PATH_LON_COL] = pd.to_numeric(df[PATH_LON_COL], errors="coerce")
    df[PATH_TIME_BEFORE_COL] = pd.to_numeric(df[PATH_TIME_BEFORE_COL], errors="coerce")
    df = df.dropna(subset=[PATH_UNIX_COL, PATH_LAT_COL, PATH_LON_COL])

    df["airport_label"] = df[PATH_POLY_COL].apply(normalize_airport)
    return df


def load_city_polygons(path: str):
    with open(path, "r") as f:
        gj = json.load(f)
    geoms, names = [], []
    for feat in gj["features"]:
        try:
            geom = shape(feat["geometry"])
        except Exception:
            continue
        name = feat["properties"].get(CITY_NAME_FIELD, "Unknown")
        geoms.append(geom)
        names.append(name)
    tree = STRtree(geoms)
    print(f"[geojson] {len(geoms)} municipality polygons loaded from {path}.")
    return tree, geoms, names


def find_municipality(tree, geoms, names, lat, lon):
    """Point-in-polygon lookup. Works with shapely >=2.0, where
    STRtree.query() returns integer indices into the array passed to the
    tree's constructor."""
    pt = Point(lon, lat)  # shapely uses (x=lon, y=lat)
    candidate_idx = tree.query(pt)
    for idx in candidate_idx:
        geom = geoms[int(idx)]
        if geom.contains(pt):
            return names[int(idx)]
    return None


def find_airport_arrivals(path_df: pd.DataFrame) -> pd.DataFrame:
    """One row per device per airport arrival event (the ping marking arrival,
    i.e. Time before appearance in polygon == 0, or the min abs value as a
    fallback if an exact 0 isn't present for that visit)."""
    arrivals = path_df[path_df["airport_label"].notna()].copy()
    if arrivals.empty:
        return arrivals

    arrivals["abs_time_before"] = arrivals[PATH_TIME_BEFORE_COL].abs()
    arrivals = arrivals.sort_values(
        [PATH_ID_COL, "airport_label", "abs_time_before"]
    )
    # keep the ping closest to 0 (the actual arrival moment) per
    # device + airport + "trip" -- we approximate one trip per calendar date
    # of arrival to allow repeat trips over the dataset's time range.
    arrivals["arrival_date"] = pd.to_datetime(
        arrivals[PATH_UNIX_COL], unit="s", utc=True
    ).dt.date

    dedup = arrivals.drop_duplicates(
        subset=[PATH_ID_COL, "airport_label", "arrival_date"], keep="first"
    )
    return dedup[[PATH_ID_COL, "airport_label", PATH_UNIX_COL, "arrival_date"]].rename(
        columns={PATH_UNIX_COL: "arrival_unix_ts"}
    )


def build_destination_detail(
    path_df: pd.DataFrame, arrivals: pd.DataFrame, tree, geoms, names, window_hours: float
) -> pd.DataFrame:
    window_secs = window_hours * 3600
    results = []

    # group pathing pings by device for fast lookup
    pings_by_device = {
        dev: g for dev, g in path_df.groupby(PATH_ID_COL)
    }

    for _, arr in arrivals.iterrows():
        dev = arr[PATH_ID_COL]
        airport = arr["airport_label"]
        arr_ts = arr["arrival_unix_ts"]
        window_df = pings_by_device.get(dev)
        if window_df is None:
            continue
        in_window = window_df[
            (window_df[PATH_UNIX_COL] > arr_ts)
            & (window_df[PATH_UNIX_COL] <= arr_ts + window_secs)
        ]
        for _, ping in in_window.iterrows():
            city = find_municipality(
                tree, geoms, names, ping[PATH_LAT_COL], ping[PATH_LON_COL]
            )
            if city is None:
                continue
            results.append(
                {
                    PATH_ID_COL: dev,
                    "origin_airport": airport,
                    "arrival_unix_ts": arr_ts,
                    "destination_city": city,
                    "ping_unix_ts": ping[PATH_UNIX_COL],
                    "local_date": ping.get(PATH_LOCAL_DATE_COL),
                }
            )

    detail = pd.DataFrame(results)
    if detail.empty:
        return detail

    # de-duplicate: same device + same destination city + same local date
    # (or same arrival event if local_date missing) counts once
    dedup_cols = [PATH_ID_COL, "origin_airport", "destination_city", "local_date"]
    detail = detail.sort_values("ping_unix_ts").drop_duplicates(
        subset=dedup_cols, keep="first"
    )
    return detail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--airport",
        choices=["DFW Airport", "Dallas Love Field", "McKinney National", "All"],
        default="All",
        help="Restrict analysis to a single origin airport (default: All).",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help="Hours after airport arrival to look for destination pings (default: 24).",
    )
    parser.add_argument(
        "--visitor-home", default=VISITOR_HOME_FILE, help="Path to visitor_home.tsv"
    )
    parser.add_argument("--pathing", default=PATHING_FILE,
                         help="Path to the pathing file (.tsv, .csv, or .zip containing one such file)")
    parser.add_argument(
        "--chunksize", type=int, default=1_000_000,
        help="Rows per chunk when streaming the pathing file (default: 1,000,000). "
             "Lower this if you run low on memory.",
    )
    parser.add_argument(
        "--cities", default=CITIES_GEOJSON, help="Path to the DFW cities geojson"
    )
    args = parser.parse_args()

    for f in [args.visitor_home, args.pathing, args.cities]:
        if not Path(f).exists():
            raise SystemExit(f"Required input file not found: {f}")

    el_paso_ids = load_el_paso_device_ids(args.visitor_home)
    if not el_paso_ids:
        raise SystemExit("No El Paso-based device IDs found -- check "
                          "HOME_METRO_COL / EL_PASO_METRO_KEYWORDS in the config.")

    path_df = load_pathing(args.pathing, el_paso_ids, chunksize=args.chunksize)
    tree, geoms, names = load_city_polygons(args.cities)

    arrivals = find_airport_arrivals(path_df)
    print(f"[arrivals] {len(arrivals)} distinct device/airport/date arrival events found.")
    if arrivals.empty:
        raise SystemExit("No airport arrival events matched AIRPORT_NAME_MAP -- "
                          "check that map against your actual Polygon ID values.")

    if args.airport != "All":
        arrivals = arrivals[arrivals["airport_label"] == args.airport]
        print(f"[filter] {len(arrivals)} arrival events remain for airport = {args.airport}")

    detail = build_destination_detail(
        path_df, arrivals, tree, geoms, names, args.window_hours
    )

    if detail.empty:
        print("No destination pings found within the window for the selected airport(s).")
        return

    detail_path = "dfw_destination_detail.csv"
    detail.to_csv(detail_path, index=False)

    summary = (
        detail.groupby(["origin_airport", "destination_city"])
        .size()
        .reset_index(name="unique_device_visits")
        .sort_values(["origin_airport", "unique_device_visits"], ascending=[True, False])
    )
    summary_path = "dfw_destination_summary.csv"
    summary.to_csv(summary_path, index=False)

    print(f"\nWrote {detail_path} ({len(detail)} rows)")
    print(f"Wrote {summary_path} ({len(summary)} rows)\n")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()