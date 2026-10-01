"""
Metro Destination Analysis (config-driven)
============================================================
Traces devices whose home location is in a given origin metro (e.g. a home
city or metro area) through arrivals at one or more named airports, then
follows each device for a configurable window (default 24 hours) and
spatially joins its subsequent pings against a set of municipality
boundaries to see where those travelers actually go.

This script itself never changes between runs -- everything that differs
from one analysis to the next (which metro, which airports, which files)
lives in `config.json`. To re-run for a new metro area:

  1. Edit config.json (see the annotated example written alongside this
     script, or below in DEFAULT_CONFIG).
  2. Name your three input files exactly what config.json expects (defaults
     are visitor_home.tsv.gz, pathing.zip, cities.geojson) and place them in
     the same folder as this script.
  3. Run: python mobility_destination_analysis.py

INPUT FILE FLEXIBILITY (no manual unzipping needed)
------------------------------------------------------
- visitor_home file: can be plain .tsv/.csv, or gzip-compressed (.tsv.gz /
  .csv.gz), or a .zip containing exactly one such file. All are read
  directly without you extracting anything.
- pathing file: can be plain .tsv/.csv, gzip-compressed, or a .zip
  containing ANY number of part-files (e.g. report_000.tsv,
  report_001.tsv, ...) -- these are streamed one at a time, in chunks, and
  filtered down to the flagged home-metro device IDs as they're read, so a
  multi-GB pathing export never has to be extracted to disk or fully loaded
  into memory at once.
- cities geojson: plain .geojson/.json (not chunked -- these are small).

CONFIG FIELDS (config.json)
------------------------------
  visitor_home_file      Path to the visitor-home export (see above for
                          accepted formats).
  pathing_file            Path to the pathing export (see above).
  cities_geojson          Path to the municipality boundaries geojson.
  origin_label             Human-readable name for the home metro, used in
                          console output only (e.g. "Miami").
  origin_metro_keywords    List of lowercase substrings matched against the
                          home-metro column to flag "home" devices (e.g.
                          ["miami"] matches "Miami, FL", "Miami-Dade, FL",
                          etc.). Add more entries if your data labels the
                          metro inconsistently.
  home_id_col              Device-id column name in the visitor_home file.
  path_id_col              Device-id column name in the pathing file. (The
                          join uses these two columns even if their names
                          differ across files.)
  home_metro_col           Column in the visitor_home file holding the
                          home/evening metro label.
  path_polygon_col         Column in the pathing file holding the polygon
                          name a ping belongs to.
  path_time_before_col     Column with "time before appearance in polygon".
  path_unix_col            Column with the Unix timestamp of each ping.
  path_lat_col / path_lon_col   Lat/Lon columns in the pathing file.
  path_local_date_col      Local-date column in the pathing file.
  airport_name_map         Dict mapping a lowercase substring of a raw
                          Polygon ID to a normalized airport label. All
                          terminal-specific polygons for one airport should
                          map to the same label (e.g. every terminal
                          polygon at one airport -> the same airport name).
                          Matching is case-insensitive substring matching.
  city_name_field          Property name in the geojson holding each
                          municipality's display name.
  window_hours             Hours after airport arrival to look for
                          destination pings (default 24).
  min_dwell_hours          Minimum time span (in hours) a device must be
                          observed within a destination city -- measured as
                          the gap between its first and last matched ping
                          there -- for that visit to count as a genuine
                          destination stay rather than pass-through traffic
                          (e.g. still at/near the airport itself). Default
                          2. A device with only ONE ping in a city has no
                          measurable span (dwell = 0) and will always be
                          dropped by any nonzero threshold -- this trades
                          away some real-but-under-sampled visits in
                          exchange for excluding unverifiable single-ping
                          ones. Set to 0 to disable and keep every matched
                          ping exactly as before.
  pathing_zip_member       Optional: exact filename to read if a zip has
                          more than one candidate file and auto-detection
                          isn't reliable. Usually leave as null.

DEPENDENCIES
------------
    pip install pandas shapely

USAGE
-----
    python mobility_destination_analysis.py
    python mobility_destination_analysis.py --config miami_config.json
    python mobility_destination_analysis.py --airport "<one of your airport_name_map values>"
    python mobility_destination_analysis.py --window-hours 24

Output: <origin_label>_destination_summary.csv, <origin_label>_destination_detail.csv
"""

import argparse
import json
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

# ---------------------------------------------------------------------------
# DEFAULT CONFIG -- used only if config.json doesn't exist yet. Running the
# script once with no config.json will write this out for you to edit.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "visitor_home_file": "visitor_home.tsv.gz",
    "pathing_file": "pathing.zip",
    "cities_geojson": "cities.geojson",
    "origin_label": "REPLACE_WITH_ORIGIN_METRO_NAME",
    "origin_metro_keywords": ["replace with lowercase metro keyword(s)"],
    "home_id_col": "Hashed Ubermedia Id",
    "path_id_col": "Hashed Device ID",
    "home_metro_col": "Common Evening Metro",
    "path_polygon_col": "Polygon ID",
    "path_time_before_col": "Time before appearance in polygon",
    "path_unix_col": "Unix Timestamp of Observation Point",
    "path_lat_col": "Lat of Observation Point",
    "path_lon_col": "Lon of Observation Point",
    "path_local_date_col": "Local Date",
    "airport_name_map": {
        "REPLACE_WITH_RAW_POLYGON_ID_FRAGMENT_1": "REPLACE_WITH_NORMALIZED_AIRPORT_NAME_1",
        "REPLACE_WITH_RAW_POLYGON_ID_FRAGMENT_2": "REPLACE_WITH_NORMALIZED_AIRPORT_NAME_2"
    },
    "city_name_field": "CITY",
    "window_hours": 24,
    "min_dwell_hours": 2,
    "pathing_zip_member": None,
}

CONFIG_PATH = "config.json"

# ---------------------------------------------------------------------------


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"[config] {path} not found -- writing a starter config template "
              f"with placeholder values. Fill in your metro, airports, and file "
              f"names, then re-run.")
        with open(p, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        raise SystemExit(f"Wrote {path}. Edit it, add your input files, and re-run.")

    with open(p) as f:
        cfg = json.load(f)
    merged = {**DEFAULT_CONFIG, **cfg}
    return merged


def normalize_airport(raw_polygon_id, airport_name_map: dict):
    if not isinstance(raw_polygon_id, str):
        return None
    low = raw_polygon_id.lower()
    for fragment, label in airport_name_map.items():
        if fragment.lower() in low:
            return label
    return None


def _zip_members(zf: zipfile.ZipFile, forced_member: Optional[str]):
    members = [n for n in zf.namelist() if not n.endswith("/")]
    if forced_member:
        if forced_member not in members:
            raise ValueError(
                f"pathing_zip_member='{forced_member}' not found in archive. "
                f"Members present: {members}"
            )
        return [forced_member]
    return sorted(members)


def _read_any(path: str, forced_zip_member: Optional[str] = None, chunksize: Optional[int] = None):
    """Read a plain file, a .gz file, or a .zip (single or multi-member)
    directly -- pandas infers gzip compression from the extension
    automatically; zips are handled explicitly since they may contain
    multiple part-files."""
    lower = str(path).lower()
    if lower.endswith(".zip"):
        zf = zipfile.ZipFile(path)
        members = _zip_members(zf, forced_zip_member)
        if chunksize:
            def gen():
                for member_name in members:
                    print(f"  ...reading archive member: {member_name}")
                    fh = zf.open(member_name, "r")
                    for chunk in pd.read_csv(fh, sep="\t", dtype=str, chunksize=chunksize):
                        yield chunk
            return gen()
        else:
            frames = []
            for member_name in members:
                fh = zf.open(member_name, "r")
                frames.append(pd.read_csv(fh, sep="\t", dtype=str))
            return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    else:
        # plain .tsv/.csv or .gz/.bz2 -- pandas infers compression from suffix
        if chunksize:
            return pd.read_csv(path, sep="\t", dtype=str, chunksize=chunksize, compression="infer")
        return pd.read_csv(path, sep="\t", dtype=str, compression="infer")


def load_origin_device_ids(cfg: dict) -> set:
    df = _read_any(cfg["visitor_home_file"])
    home_id_col = cfg["home_id_col"]
    home_metro_col = cfg["home_metro_col"]
    keywords = [k.lower() for k in cfg["origin_metro_keywords"]]

    if home_id_col not in df.columns:
        raise ValueError(
            f"Column '{home_id_col}' not found in {cfg['visitor_home_file']}. "
            f"Available columns: {list(df.columns)}"
        )

    metro_lower = df.get(home_metro_col, pd.Series([""] * len(df))).fillna("").str.lower()
    mask = metro_lower.apply(lambda m: any(kw in m for kw in keywords))
    origin_ids = set(df.loc[mask, home_id_col].dropna().unique())
    print(f"[visitor_home] {len(df):,} rows loaded; "
          f"{len(origin_ids):,} unique {cfg['origin_label']}-based device IDs flagged.")
    return origin_ids


def load_pathing(cfg: dict, device_ids: set, chunksize: int = 1_000_000) -> pd.DataFrame:
    path_id_col = cfg["path_id_col"]
    path_unix_col = cfg["path_unix_col"]
    path_lat_col = cfg["path_lat_col"]
    path_lon_col = cfg["path_lon_col"]
    path_time_before_col = cfg["path_time_before_col"]
    path_poly_col = cfg["path_polygon_col"]

    kept_chunks = []
    total_rows = 0
    reader = _read_any(cfg["pathing_file"], forced_zip_member=cfg.get("pathing_zip_member"), chunksize=chunksize)

    for i, chunk in enumerate(reader):
        if i == 0 and path_id_col not in chunk.columns:
            raise ValueError(
                f"Column '{path_id_col}' not found in {cfg['pathing_file']}. "
                f"Available columns: {list(chunk.columns)}"
            )
        total_rows += len(chunk)
        filtered = chunk[chunk[path_id_col].isin(device_ids)]
        if not filtered.empty:
            kept_chunks.append(filtered)
        if (i + 1) % 10 == 0:
            kept_so_far = sum(len(c) for c in kept_chunks)
            print(f"  ...scanned {total_rows:,} pathing rows so far, {kept_so_far:,} kept.")

    df = pd.concat(kept_chunks, ignore_index=True) if kept_chunks else pd.DataFrame(columns=[path_id_col])
    print(f"[pathing] {total_rows:,} rows scanned; {len(df):,} rows remain after "
          f"restricting to {cfg['origin_label']}-based device IDs.")

    if df.empty:
        return df

    df[path_unix_col] = pd.to_numeric(df[path_unix_col], errors="coerce")
    df[path_lat_col] = pd.to_numeric(df[path_lat_col], errors="coerce")
    df[path_lon_col] = pd.to_numeric(df[path_lon_col], errors="coerce")
    df[path_time_before_col] = pd.to_numeric(df[path_time_before_col], errors="coerce")
    df = df.dropna(subset=[path_unix_col, path_lat_col, path_lon_col])

    df["airport_label"] = df[path_poly_col].apply(lambda x: normalize_airport(x, cfg["airport_name_map"]))
    return df


def load_city_polygons(path: str, city_name_field: str):
    with open(path, "r") as f:
        gj = json.load(f)
    geoms, names = [], []
    for feat in gj["features"]:
        try:
            geom = shape(feat["geometry"])
        except Exception:
            continue
        name = feat["properties"].get(city_name_field, "Unknown")
        geoms.append(geom)
        names.append(name)
    tree = STRtree(geoms)
    print(f"[geojson] {len(geoms)} municipality polygons loaded from {path}.")
    return tree, geoms, names


def find_municipality(tree, geoms, names, lat, lon):
    pt = Point(lon, lat)
    for idx in tree.query(pt):
        geom = geoms[int(idx)]
        if geom.contains(pt):
            return names[int(idx)]
    return None


def find_airport_arrivals(path_df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    path_id_col = cfg["path_id_col"]
    path_unix_col = cfg["path_unix_col"]
    path_time_before_col = cfg["path_time_before_col"]

    arrivals = path_df[path_df["airport_label"].notna()].copy()
    if arrivals.empty:
        return arrivals

    arrivals["abs_time_before"] = arrivals[path_time_before_col].abs()
    arrivals = arrivals.sort_values([path_id_col, "airport_label", "abs_time_before"])
    arrivals["arrival_date"] = pd.to_datetime(arrivals[path_unix_col], unit="s", utc=True).dt.date

    dedup = arrivals.drop_duplicates(subset=[path_id_col, "airport_label", "arrival_date"], keep="first")
    return dedup[[path_id_col, "airport_label", path_unix_col, "arrival_date"]].rename(
        columns={path_unix_col: "arrival_unix_ts"}
    )


def build_destination_detail(path_df, arrivals, tree, geoms, names, cfg, window_hours):
    path_id_col = cfg["path_id_col"]
    path_unix_col = cfg["path_unix_col"]
    path_lat_col = cfg["path_lat_col"]
    path_lon_col = cfg["path_lon_col"]
    path_local_date_col = cfg["path_local_date_col"]

    window_secs = window_hours * 3600
    results = []
    pings_by_device = {dev: g for dev, g in path_df.groupby(path_id_col)}

    for _, arr in arrivals.iterrows():
        dev = arr[path_id_col]
        airport = arr["airport_label"]
        arr_ts = arr["arrival_unix_ts"]
        window_df = pings_by_device.get(dev)
        if window_df is None:
            continue
        in_window = window_df[
            (window_df[path_unix_col] > arr_ts) & (window_df[path_unix_col] <= arr_ts + window_secs)
        ]
        for _, ping in in_window.iterrows():
            city = find_municipality(tree, geoms, names, ping[path_lat_col], ping[path_lon_col])
            if city is None:
                continue
            results.append({
                path_id_col: dev,
                "origin_airport": airport,
                "arrival_unix_ts": arr_ts,
                "destination_city": city,
                "ping_unix_ts": ping[path_unix_col],
                "local_date": ping.get(path_local_date_col),
            })

    detail = pd.DataFrame(results)
    if detail.empty:
        return detail

    dedup_cols = [path_id_col, "origin_airport", "destination_city", "local_date"]

    # Compute dwell (span between first and last observed ping) per
    # device/airport/city/date group BEFORE collapsing to one row -- this is
    # what lets us tell a genuine stay apart from a single momentary ping
    # while just passing through (e.g. still at/near the airport itself).
    # Note: a group with only one ping has dwell_seconds == 0 by construction,
    # since there's no second ping to measure a span against -- these are
    # exactly the rows a nonzero min_dwell_hours threshold will drop.
    dwell_stats = (
        detail.groupby(dedup_cols)["ping_unix_ts"]
        .agg(dwell_first_ping_ts="min", dwell_last_ping_ts="max", ping_count="count")
        .reset_index()
    )
    dwell_stats["dwell_seconds"] = dwell_stats["dwell_last_ping_ts"] - dwell_stats["dwell_first_ping_ts"]

    detail = detail.sort_values("ping_unix_ts").drop_duplicates(subset=dedup_cols, keep="first")
    detail = detail.merge(dwell_stats, on=dedup_cols, how="left")

    min_dwell_hours = cfg.get("min_dwell_hours", 0) or 0
    min_dwell_seconds = min_dwell_hours * 3600
    if min_dwell_seconds > 0:
        before = len(detail)
        detail = detail[detail["dwell_seconds"] >= min_dwell_seconds].copy()
        after = len(detail)
        print(f"[dwell filter] min_dwell_hours={min_dwell_hours}: dropped {before - after:,} of "
              f"{before:,} visit rows with dwell under {min_dwell_hours}h (likely pass-through "
              f"pings, e.g. still at/near the airport itself, rather than a genuine destination stay).")

    return detail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=CONFIG_PATH, help="Path to config.json")
    parser.add_argument("--airport", default="All", help="Restrict to a single origin airport label from your airport_name_map (default: All).")
    parser.add_argument("--window-hours", type=float, default=None, help="Override config's window_hours.")
    parser.add_argument("--chunksize", type=int, default=1_000_000, help="Rows per chunk when streaming pathing data.")
    args = parser.parse_args()

    cfg = load_config(args.config)

    placeholder_markers = ("REPLACE_WITH",)
    def has_placeholder(v):
        if isinstance(v, str):
            return any(m in v for m in placeholder_markers)
        if isinstance(v, list):
            return any(has_placeholder(x) for x in v)
        if isinstance(v, dict):
            return any(has_placeholder(k) or has_placeholder(x) for k, x in v.items())
        return False

    if has_placeholder(cfg.get("origin_label")) or has_placeholder(cfg.get("origin_metro_keywords")) \
            or has_placeholder(cfg.get("airport_name_map")):
        raise SystemExit(
            f"{args.config} still has placeholder values (origin_label, "
            f"origin_metro_keywords, and/or airport_name_map). Edit it with your "
            f"actual metro name, home-metro keyword(s), and airport polygon "
            f"mappings, then re-run."
        )

    window_hours = args.window_hours if args.window_hours is not None else cfg["window_hours"]

    for key in ["visitor_home_file", "pathing_file", "cities_geojson"]:
        if not Path(cfg[key]).exists():
            raise SystemExit(f"Required input file not found: {cfg[key]} (config key: {key})")

    origin_ids = load_origin_device_ids(cfg)
    if not origin_ids:
        raise SystemExit(f"No {cfg['origin_label']}-based device IDs found -- check "
                          f"home_metro_col / origin_metro_keywords in {args.config}.")

    path_df = load_pathing(cfg, origin_ids, chunksize=args.chunksize)
    tree, geoms, names = load_city_polygons(cfg["cities_geojson"], cfg["city_name_field"])

    arrivals = find_airport_arrivals(path_df, cfg)
    print(f"[arrivals] {len(arrivals):,} distinct device/airport/date arrival events found.")
    if arrivals.empty:
        raise SystemExit("No airport arrival events matched airport_name_map -- "
                          "check that map against your actual Polygon ID values.")

    if args.airport != "All":
        arrivals = arrivals[arrivals["airport_label"] == args.airport]
        print(f"[filter] {len(arrivals):,} arrival events remain for airport = {args.airport}")

    slug = cfg["origin_label"].lower().replace(" ", "_")

    # Written BEFORE the destination-detail step, and independent of whether a
    # given arrival went on to produce a matched neighborhood ping -- this is
    # the correct denominator for normalizing against ground-truth passenger
    # counts (see normalize_visits.py), since some arrivals never generate a
    # captured destination ping at all.
    arrivals_path = f"{slug}_airport_arrivals.csv"
    arrivals.to_csv(arrivals_path, index=False)
    print(f"Wrote {arrivals_path} ({len(arrivals):,} rows)")

    detail = build_destination_detail(path_df, arrivals, tree, geoms, names, cfg, window_hours)

    if detail.empty:
        print("No destination pings found within the window for the selected airport(s).")
        return

    detail_path = f"{slug}_destination_detail.csv"
    detail.to_csv(detail_path, index=False)

    summary = (
        detail.groupby(["origin_airport", "destination_city"])
        .size()
        .reset_index(name="unique_device_visits")
        .sort_values(["origin_airport", "unique_device_visits"], ascending=[True, False])
    )
    summary_path = f"{slug}_destination_summary.csv"
    summary.to_csv(summary_path, index=False)

    print(f"\nWrote {detail_path} ({len(detail):,} rows)")
    print(f"Wrote {summary_path} ({len(summary):,} rows)\n")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
