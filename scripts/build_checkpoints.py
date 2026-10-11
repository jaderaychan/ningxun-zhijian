"""Build the D02 checkpoint list from the official Jiangsu police flood list.

Source (citable, and the only reason this file may state these names):
    江苏省公安厅交警总队《江苏警方公布易积水路段》，2015-07-20
    https://gat.jiangsu.gov.cn/art/2015/7/20/art_6373_6620598.html
    其中南京 32 处。

The list gives place names, not coordinates, so each name is geocoded by matching
it against OpenStreetMap road names *inside the study area* rather than by calling
a geocoding service: local name matching is reproducible, needs no extra service,
and fails loudly (an unmatched name is reported, never silently dropped).

Vintage matters and is reported: this list is from 2015, so it records places that
were flood-prone then. A point that no longer floods is a false negative for the
model that is worth exactly as much as a model false positive — both are findings,
not scoring noise.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

SOURCE_URL = "https://gat.jiangsu.gov.cn/art/2015/7/20/art_6373_6620598.html"
SOURCE_TITLE = "江苏省公安厅交警总队：江苏警方公布易积水路段（南京 32 处），2015-07-20"

# Nanjing entries, verbatim from the source page.
NANJING_2015 = [
    "启迪大街绕城涵洞口",
    "聚宝山庄岔路口",
    "后标营路",
    "老宁栖路与玄武大道交界处",
    "龙蟠路锁金北村路口",
    "明陵路",
    "植物园路",
    "岗子村往龙脖子方向路口",
    "双麒路涵洞",
    "滨江花园高架隔音棚内",
    "幕府西路幕府南路路口",
    "七乡河大道铁路涵洞",
    "下梅墓铁路涵洞",
    "炼西路",
    "栖霞大道十月村路口",
    "仙林大道文澜路学海路之间",
    "仙隐北路仙鹤山庄路口",
    "文澜路天秤路",
    "九乡河西路文城东路",
    "仙林大道南大门口",
    "仙林大道海陵药业路口",
    "经天路液晶谷路口",
    "仙林大道经天路",
    "寅春路栖霞大桥南口北口匝道",
    "宁杭公路五棵松",
    "马群转盘",
    "环陵路沪宁高速连接线涵洞",
    "仙尧路栖霞区委党校门口",
    "新尧路海门中学",
    "312国道毕升路路口",
    "312国道仙林高铁站路口",
    "太龙路三元祠",
]

# Tokens that carry no locating power on their own; a match on one of these alone
# would put a checkpoint in the wrong district.
GENERIC = ("路口", "涵洞", "涵洞口", "门口", "之间", "交界处", "转盘", "匝道", "方向", "内", "段")

# A matched OSM name should look like a road. Without this, the substring rule
# happily maps "仙尧路栖霞区委党校门口" onto an OSM way literally named "校门口",
# which would seed the hit-rate check with a point in the wrong place.
ROAD_SUFFIXES = ("路", "街", "大道", "公路", "巷", "桥", "隧道", "线", "道", "村", "庄", "祠")


def normalise(text: str) -> str:
    return re.sub(r"[\s（）()·\-—]", "", text or "")


def clean_osm_name(name: str) -> str:
    """Drop the disambiguating suffix OSM sometimes appends, e.g. 岗子村#3."""
    return re.sub(r"#\d+$", "", normalise(name))


def candidate_tokens(name: str) -> list[str]:
    """Split a list entry into the road names it mentions, longest first."""
    raw = re.split(r"(?:与|往|及|到)", name)
    tokens = []
    for part in raw:
        part = normalise(part)
        # strip a trailing generic suffix to expose the road name itself
        for suffix in GENERIC:
            if part.endswith(suffix) and len(part) > len(suffix):
                part = part[: -len(suffix)]
        # strip leading route numbers such as 312国道 / 宁杭公路
        part = re.sub(r"^\d+", "", part)
        if part and part not in GENERIC and len(part) >= 2:
            tokens.append(part)
    # also keep the full entry for exact-name hits
    tokens.append(normalise(name))
    seen = set()
    ordered = []
    for tok in sorted(tokens, key=len, reverse=True):
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return ordered


def _way_points(ways: list[dict]) -> list[tuple[float, float]]:
    return [(pt["lon"], pt["lat"]) for w in ways for pt in (w.get("geometry") or []) if "lon" in pt]


def _closest_pair(a: list[tuple[float, float]], b: list[tuple[float, float]]):
    """Closest lon/lat pair between two polylines, compared in metres.

    Comparing raw degrees would let longitude dominate at this latitude, so the
    search runs in a local metric approximation before the winning pair is used.
    """
    if not a or not b:
        return None
    import math

    lat0 = sum(p[1] for p in a) / len(a)
    scale_x = 111320.0 * math.cos(math.radians(lat0))
    scale_y = 110540.0
    best = None
    for lon_a, lat_a in a:
        ax, ay = lon_a * scale_x, lat_a * scale_y
        for lon_b, lat_b in b:
            dx = ax - lon_b * scale_x
            dy = ay - lat_b * scale_y
            distance = dx * dx + dy * dy
            if best is None or distance < best[0]:
                best = (distance, (lon_a, lat_a), (lon_b, lat_b))
    if best is None:
        return None
    _, pa, pb = best
    return ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0, best[0] ** 0.5)


def segment_entry(name: str, index: dict) -> list[tuple[str, list]]:
    """Split an entry into the road names it mentions, using the road vocabulary.

    Entries are written for humans, not parsers: "文澜路天秤路" is two roads with
    no separator, and "仙林大道文澜路学海路之间" is three. Splitting on
    punctuation alone therefore finds nothing; instead every known road name that
    occurs in the entry is located and the longest non-overlapping matches are
    taken in order.
    """
    entry = normalise(name)
    candidates = []
    for key, ways in index.items():
        if len(key) < 3 or not key.endswith(ROAD_SUFFIXES):
            continue
        position = entry.find(key)
        if position >= 0:
            candidates.append((position, len(key), key, ways))
    # longest first at each position, then sweep left to right without overlap
    candidates.sort(key=lambda c: (c[0], -c[1]))
    chosen = []
    cursor = -1
    for position, length, key, ways in sorted(candidates, key=lambda c: (-c[1], c[0])):
        if position + length <= cursor or position <= cursor - 1:
            continue
        if any(position < end and position + length > start for start, end, _, _ in chosen):
            continue
        chosen.append((position, position + length, key, ways))
    chosen.sort(key=lambda c: c[0])
    return [(key, ways) for _, _, key, ways in chosen]


def locate_entry(name: str, index: dict) -> dict:
    """Place an entry on the map, preferring a road intersection over a centroid.

    "A与B交界处" and "A路B路之间" both mean the place is where roads meet, not the
    middle of either road. A single road's centroid can be kilometres off, which
    would make the validation result depend on our geocoding instead of the model.
    """
    located = segment_entry(name, index)
    if len(located) >= 2:
        points = [(key, _way_points(ways)) for key, ways in located]
        best = None
        for i in range(len(points)):
            for j in range(i + 1, len(points)):
                pair = _closest_pair(points[i][1], points[j][1])
                if pair and (best is None or pair[2] < best[2]):
                    best = pair
        if best and best[2] < 3000.0:
            return {
                "matched": True,
                "method": "intersection",
                "osm_name": "+".join(key for key, _ in points),
                "lon": best[0],
                "lat": best[1],
                "separation_m": round(best[2], 1),
            }

    # fall back to a single road, stripping generic suffixes as before
    tokens = candidate_tokens(name)
    for token in tokens:
        hits = [
            (key, ways)
            for key, ways in index.items()
            if len(key) >= 3 and key.endswith(ROAD_SUFFIXES) and (token in key or key in token)
        ]
        if hits:
            key, ways = max(hits, key=lambda kv: len(kv[0]))
            pts = _way_points(ways)
            if pts:
                return {
                    "matched": True,
                    "method": "road_name",
                    "osm_name": key,
                    "lon": sum(p[0] for p in pts) / len(pts),
                    "lat": sum(p[1] for p in pts) / len(pts),
                    "separation_m": None,
                }
    return {"matched": False, "method": "none", "osm_name": "", "lon": None, "lat": None, "separation_m": None}


def match_roads(names: list[str], elements: list[dict]) -> dict[str, dict]:
    """Match each entry to OSM roads and locate it."""
    index: dict[str, list[dict]] = {}
    for element in elements:
        tags = element.get("tags") or {}
        road_name = tags.get("name") or tags.get("name:zh")
        if not road_name:
            continue
        key = clean_osm_name(road_name)
        geometry = element.get("geometry") or []
        if len(geometry) < 1 or len(key) < 2:
            continue
        index.setdefault(key, []).append(element)
    return {name: locate_entry(name, index) for name in names}


def main() -> int:
    from floodkit.exposure import fetch_osm, load_features

    bounds = (118.72, 32.00, 119.00, 32.16)
    cache = os.path.join(ROOT, "data", "osm")
    print("bounds:", bounds)
    roads_path = fetch_osm(bounds, cache, "roads")
    elements = load_features(roads_path)
    print("OSM ways in enlarged study area:", len(elements))

    matches = match_roads(NANJING_2015, elements)
    out_dir = os.path.join(ROOT, "data", "checkpoints")
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    for name in NANJING_2015:
        info = matches[name]
        rows.append(
            {
                "name": name,
                "matched": info["matched"],
                "osm_name": info.get("osm_name", ""),
                "method": info.get("method", ""),
                "lon": info.get("lon", ""),
                "lat": info.get("lat", ""),
                "ways": info.get("ways", 0),
            }
        )
    csv_path = os.path.join(out_dir, "nanjing_official_2015.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["name", "matched", "method", "osm_name", "lon", "lat", "ways"])
        writer.writeheader()
        writer.writerows(rows)

    meta = {
        "source_title": SOURCE_TITLE,
        "source_url": SOURCE_URL,
        "study_bounds_wgs84": list(bounds),
        "total": len(NANJING_2015),
        "matched": sum(1 for r in rows if r["matched"]),
        "unmatched": [r["name"] for r in rows if not r["matched"]],
        "note": "2015 list; matched by road-name matching against OSM inside the study area.",
    }
    with open(os.path.join(out_dir, "nanjing_official_2015.meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    print("matched %d / %d" % (meta["matched"], meta["total"]))
    print("csv:", csv_path)
    for r in rows:
        if r["matched"]:
            print("   OK   %-22s -> %-16s (%.5f, %.5f)" % (r["name"][:22], r["osm_name"][:16], r["lon"], r["lat"]))
    if meta["unmatched"]:
        print("unmatched (reported, not dropped):")
        for name in meta["unmatched"]:
            print("   MISS %s" % name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
