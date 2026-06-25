#!/usr/bin/env python3
"""Acquire small, auditable batches of GSI ort_riku10 tiles and build exact-cell mosaics.

Design goals:
- no scraping of the dynamic browser map;
- direct use of the official XYZ image endpoint;
- bounded work: one or two configured grid cells per run;
- raw tiles retained with hashes;
- partial failures are written to ledgers rather than silently discarded;
- no imagery interpretation happens in this script.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "SSM_B001_grid_plan.csv"
DEFAULT_SOURCES = ROOT / "config" / "source_registry.json"

LEDGER_FIELDS = [
    "block_id", "grid_id", "z", "x", "y", "tile_url", "local_relpath",
    "tile_bbox_wgs84", "request_status", "image_sha256", "http_status",
    "attempt_utc", "failure_reason", "source_id", "run_id"
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def parse_bbox(value: str) -> Tuple[float, float, float, float]:
    west, south, east, north = (float(v.strip()) for v in value.split(","))
    if not (west < east and south < north):
        raise ValueError(f"Invalid bbox: {value}")
    return west, south, east, north


def lonlat_to_tile_float(lon: float, lat: float, z: int) -> Tuple[float, float]:
    lat = min(max(lat, -85.05112878), 85.05112878)
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile_to_lonlat(x: float, y: float, z: int) -> Tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def tiles_for_bbox(bbox: Tuple[float, float, float, float], z: int) -> List[Tuple[int, int]]:
    """Return XYZ tiles intersecting bbox; tiny epsilon avoids accidental extra edge tile."""
    west, south, east, north = bbox
    eps = 1e-10
    x0f, y0f = lonlat_to_tile_float(west, north, z)
    x1f, y1f = lonlat_to_tile_float(east - eps, south + eps, z)
    x_min, x_max = math.floor(x0f), math.floor(x1f)
    y_min, y_max = math.floor(y0f), math.floor(y1f)
    return [(x, y) for y in range(y_min, y_max + 1) for x in range(x_min, x_max + 1)]


def tile_bbox(x: int, y: int, z: int) -> str:
    west, north = tile_to_lonlat(x, y, z)
    east, south = tile_to_lonlat(x + 1, y + 1, z)
    return f"{west:.9f},{south:.9f},{east:.9f},{north:.9f}"


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: Iterable[Dict[str, str]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    tmp.replace(path)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_grids(path: Path) -> Dict[str, Dict[str, str]]:
    grids = {row["grid_id"]: row for row in read_csv(path)}
    if not grids:
        raise RuntimeError(f"No grid configuration found at {path}")
    return grids


def is_valid_image(path: Path) -> Tuple[bool, str]:
    try:
        with Image.open(path) as im:
            im.verify()
        return True, ""
    except Exception as exc:  # Pillow throws multiple image-specific error classes
        return False, f"invalid_cached_image: {type(exc).__name__}: {exc}"


def fetch_tile(session: requests.Session, url: str, timeout: int, retries: int) -> Tuple[str, bytes | None, str, str]:
    """Return status, bytes, http_status, failure_reason without raising for expected network failures."""
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            http_status = str(response.status_code)
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
            else:
                blob = response.content
                try:
                    with Image.open(BytesIO(blob)) as im:
                        im.verify()
                    return "downloaded", blob, http_status, ""
                except Exception as exc:
                    last_error = f"non_image_or_corrupt_response: {type(exc).__name__}: {exc}"
        except requests.RequestException as exc:
            http_status = ""
            last_error = f"request_error: {type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(min(2.0, 0.5 * attempt))
    return "not_received", None, http_status if 'http_status' in locals() else "", last_error


def tile_path(out_root: Path, z: int, x: int, y: int) -> Path:
    return out_root / "raw_tiles" / "ort_riku10" / f"z{z}" / f"x{x}_y{y}.png"


def make_mosaic(
    out_root: Path,
    grid: Dict[str, str],
    tile_rows: List[Dict[str, str]],
) -> Dict[str, str]:
    """Build exact bbox mosaic only when every source tile has a valid local image."""
    grid_id = grid["grid_id"]
    z = int(grid["z"])
    bbox = parse_bbox(grid["bbox_wgs84"])
    relevant = [r for r in tile_rows if r["grid_id"] == grid_id]
    required = {(int(r["x"]), int(r["y"])): r for r in relevant}
    if not required:
        return {"grid_id": grid_id, "mosaic_status": "not_planned", "mosaic_relpath": "", "mosaic_sha256": "", "missing_tiles": ""}

    missing = []
    for (x, y), row in required.items():
        path = out_root / row["local_relpath"]
        ok, _ = is_valid_image(path) if path.exists() else (False, "missing")
        if not ok:
            missing.append(f"{x}/{y}")
    if missing:
        return {
            "grid_id": grid_id,
            "mosaic_status": "blocked_missing_source_tiles",
            "mosaic_relpath": "",
            "mosaic_sha256": "",
            "missing_tiles": ";".join(missing),
        }

    west, south, east, north = bbox
    world_px = 256 * (2 ** z)
    def global_px(lon: float, lat: float) -> Tuple[float, float]:
        xf, yf = lonlat_to_tile_float(lon, lat, z)
        return xf * 256, yf * 256

    left_f, top_f = global_px(west, north)
    right_f, bottom_f = global_px(east, south)
    left_i, top_i = math.floor(left_f), math.floor(top_f)
    right_i, bottom_i = math.ceil(right_f), math.ceil(bottom_f)
    width, height = right_i - left_i, bottom_i - top_i
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid pixel bounds for {grid_id}")

    canvas = Image.new("RGB", (width, height), color=(255, 255, 255))
    for (x, y), row in required.items():
        path = out_root / row["local_relpath"]
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            px = x * 256 - left_i
            py = y * 256 - top_i
            canvas.paste(rgb, (px, py))

    rel = Path("mosaics") / "ort_riku10" / f"{grid_id}_z{z}.png"
    target = out_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, format="PNG", optimize=True)
    digest = sha256_bytes(target.read_bytes())
    metadata = {
        "grid_id": grid_id,
        "block_id": grid["block_id"],
        "source_id": grid["source_id"],
        "source_layer": "ort_riku10",
        "bbox_wgs84": [west, south, east, north],
        "zoom": z,
        "pixel_dimensions": [width, height],
        "image_relpath": str(rel).replace("\\", "/"),
        "sha256": digest,
        "generated_utc": utc_now(),
        "scope_status": grid["scope_status"],
        "method": "Exact WGS84 bbox assembled from retained XYZ source tiles; no resampling beyond source tile assembly.",
    }
    write_json(target.with_suffix(".json"), metadata)
    return {
        "grid_id": grid_id,
        "mosaic_status": "complete",
        "mosaic_relpath": str(rel).replace("\\", "/"),
        "mosaic_sha256": digest,
        "missing_tiles": "",
    }


def generate_manifest(out_root: Path, grids: Dict[str, Dict[str, str]], all_rows: List[Dict[str, str]], mosaic_rows: List[Dict[str, str]], source: dict) -> None:
    by_grid: Dict[str, List[Dict[str, str]]] = {}
    for row in all_rows:
        by_grid.setdefault(row["grid_id"], []).append(row)
    mosaic_by_grid = {row["grid_id"]: row for row in mosaic_rows}
    grids_payload = []
    for grid_id in sorted(grids):
        grid = grids[grid_id]
        rows = by_grid.get(grid_id, [])
        ready = sum(1 for r in rows if r["request_status"] in {"downloaded", "cached"})
        missing = sum(1 for r in rows if r["request_status"] not in {"downloaded", "cached"})
        mosaic = mosaic_by_grid.get(grid_id, {"mosaic_status": "not_generated", "mosaic_relpath": "", "mosaic_sha256": "", "missing_tiles": ""})
        grids_payload.append({
            "grid_id": grid_id,
            "block_id": grid["block_id"],
            "bbox_wgs84": [float(x) for x in grid["bbox_wgs84"].split(",")],
            "zoom": int(grid["z"]),
            "scope_status": grid["scope_status"],
            "source_tiles_ready": ready,
            "source_tiles_missing_or_failed": missing,
            "mosaic_status": mosaic["mosaic_status"],
            "mosaic_relpath": mosaic["mosaic_relpath"],
            "mosaic_sha256": mosaic["mosaic_sha256"],
            "missing_tiles": mosaic["missing_tiles"],
            "interpretation_status": "not_interpreted_by_acquisition_workflow",
        })
    manifest = {
        "schema_version": "1.0",
        "generated_utc": utc_now(),
        "block_id": "SSM-B001",
        "purpose": "Auditable acquisition bridge for historical-airphoto candidate extraction. No candidate interpretation is made here.",
        "source": source,
        "scope_warning": "The B001 scope is provisional until its historical-geographic correspondence is separately verified.",
        "grids": grids_payload,
    }
    write_json(out_root / "manifests" / "manifest.json", manifest)


def write_static_index(out_root: Path) -> None:
    index = """<!doctype html>
<html lang=\"ja\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>SSM Historical Airphoto Bridge</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.45} .card{border:1px solid #bbb;padding:1rem;margin:1rem 0} img{max-width:100%;height:auto;border:1px solid #ddd} code{word-break:break-all}</style>
<h1>SSM Historical Airphoto Bridge</h1><p>出典：国土地理院「地理院タイル（空中写真（1936年〜1942年頃））」をもとに本プロジェクトで取得・結合。</p><p>候補抽出・社会的性格付けはこのページでは行いません。</p><div id=\"app\">Loading manifest…</div>
<script>
fetch('manifests/manifest.json').then(r=>r.json()).then(m=>{
 const a=document.getElementById('app'); a.innerHTML='';
 m.grids.forEach(g=>{const d=document.createElement('div');d.className='card';
 const title=document.createElement('h2');title.textContent=g.grid_id+' — '+g.mosaic_status; d.appendChild(title);
 const p=document.createElement('p');p.textContent='bbox: '+g.bbox_wgs84.join(', ')+' / source tiles ready: '+g.source_tiles_ready+' / missing: '+g.source_tiles_missing_or_failed;d.appendChild(p);
 if(g.mosaic_relpath){const img=document.createElement('img'); img.src=g.mosaic_relpath;img.alt=g.grid_id+' historical mosaic';d.appendChild(img);}
 a.appendChild(d);
 });
}).catch(e=>document.getElementById('app').textContent='Manifest load error: '+e);
</script></html>"""
    (out_root / "index.html").write_text(index, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid-ids", required=True, help="Comma-separated configured grid IDs; maximum two per execution.")
    parser.add_argument("--run-id", required=True, help="Auditable run ID, e.g. SSM-T01A.")
    parser.add_argument("--out", default=str(ROOT / "data"), help="Output root, normally data/.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES))
    parser.add_argument("--max-new-tiles", type=int, default=18)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    parser.add_argument("--force-redownload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    requested_ids = [x.strip() for x in args.grid_ids.split(",") if x.strip()]
    if not requested_ids or len(requested_ids) > 2:
        raise SystemExit("One run must name one or two grid IDs only.")
    if not (1 <= args.max_new_tiles <= 24):
        raise SystemExit("--max-new-tiles must be between 1 and 24 for bounded execution.")

    out_root = Path(args.out)
    grids = load_grids(Path(args.config))
    missing_config = [gid for gid in requested_ids if gid not in grids]
    if missing_config:
        raise SystemExit(f"Unknown grid IDs: {', '.join(missing_config)}")
    selected = {gid: grids[gid] for gid in requested_ids}
    with Path(args.sources).open("r", encoding="utf-8") as f:
        sources = json.load(f)
    source = sources["S007"]

    planned: Dict[Tuple[str, int, int, int], Dict[str, str]] = {}
    for gid, grid in grids.items():
        bbox = parse_bbox(grid["bbox_wgs84"])
        z = int(grid["z"])
        for x, y in tiles_for_bbox(bbox, z):
            key = (gid, z, x, y)
            url = source["url_template"].format(z=z, x=x, y=y)
            rel = tile_path(out_root, z, x, y).relative_to(out_root).as_posix()
            planned[key] = {
                "block_id": grid["block_id"], "grid_id": gid, "z": str(z), "x": str(x), "y": str(y),
                "tile_url": url, "local_relpath": rel, "tile_bbox_wgs84": tile_bbox(x, y, z),
                "request_status": "planned_not_acquired", "image_sha256": "", "http_status": "", "attempt_utc": "",
                "failure_reason": "", "source_id": grid["source_id"], "run_id": "",
            }

    ledger_path = out_root / "manifests" / "tile_ledger.csv"
    old_rows = read_csv(ledger_path)
    old_by_key = {(r["grid_id"], int(r["z"]), int(r["x"]), int(r["y"])): r for r in old_rows if r.get("grid_id")}
    for key, row in planned.items():
        if key in old_by_key:
            merged = row.copy(); merged.update({k: v for k, v in old_by_key[key].items() if v not in (None, "")})
            planned[key] = merged

    selected_keys = [key for key in planned if key[0] in selected]
    selected_keys.sort(key=lambda k: (k[0], k[3], k[2]))
    new_attempts = 0
    session = requests.Session()
    session.headers.update({"User-Agent": "SSM-HistoricalAirphoto-Research/1.0 (bounded acquisition; source attribution retained)"})
    run_events = []

    for key in selected_keys:
        row = planned[key]
        path = out_root / row["local_relpath"]
        if path.exists() and not args.force_redownload:
            ok, reason = is_valid_image(path)
            if ok:
                row.update({"request_status": "cached", "image_sha256": sha256_bytes(path.read_bytes()), "http_status": "200_cached", "attempt_utc": utc_now(), "failure_reason": "", "run_id": args.run_id})
                run_events.append({"event": "cache_hit", **row})
                continue
            row.update({"request_status": "invalid_cache", "attempt_utc": utc_now(), "failure_reason": reason, "run_id": args.run_id})
            path.unlink(missing_ok=True)

        if new_attempts >= args.max_new_tiles:
            row.update({"request_status": "deferred_by_tile_cap", "attempt_utc": utc_now(), "failure_reason": f"max_new_tiles={args.max_new_tiles}", "run_id": args.run_id})
            run_events.append({"event": "deferred", **row})
            continue
        new_attempts += 1
        if args.dry_run:
            row.update({"request_status": "dry_run_not_requested", "attempt_utc": utc_now(), "failure_reason": "dry_run", "run_id": args.run_id})
            run_events.append({"event": "dry_run", **row})
            continue

        status, blob, http_status, failure = fetch_tile(session, row["tile_url"], args.timeout, args.retries)
        row.update({"request_status": status, "http_status": http_status, "attempt_utc": utc_now(), "failure_reason": failure, "run_id": args.run_id})
        if status == "downloaded" and blob is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".png.tmp")
            tmp.write_bytes(blob)
            tmp.replace(path)
            row["image_sha256"] = sha256_bytes(blob)
        run_events.append({"event": status, **row})
        time.sleep(args.delay_seconds)

    all_rows = list(planned.values())
    all_rows.sort(key=lambda r: (r["grid_id"], int(r["y"]), int(r["x"])))
    write_csv(ledger_path, all_rows, LEDGER_FIELDS)

    # Need rows only for selected grids when building mosaics.
    all_mosaic_rows = read_csv(out_root / "manifests" / "mosaic_ledger.csv")
    old_mosaic = {r["grid_id"]: r for r in all_mosaic_rows if r.get("grid_id")}
    selected_mosaics = []
    for gid, grid in selected.items():
        result = make_mosaic(out_root, grid, all_rows)
        result["run_id"] = args.run_id
        result["updated_utc"] = utc_now()
        old_mosaic[gid] = result
        selected_mosaics.append(result)
    mosaic_fields = ["grid_id", "mosaic_status", "mosaic_relpath", "mosaic_sha256", "missing_tiles", "run_id", "updated_utc"]
    mosaic_rows = [old_mosaic[k] for k in sorted(old_mosaic)]
    write_csv(out_root / "manifests" / "mosaic_ledger.csv", mosaic_rows, mosaic_fields)

    run_log_fields = ["event"] + LEDGER_FIELDS
    write_csv(out_root / "manifests" / "runs" / f"{args.run_id}.csv", run_events, run_log_fields)
    run_summary = {
        "run_id": args.run_id,
        "generated_utc": utc_now(),
        "grid_ids": requested_ids,
        "max_new_tiles": args.max_new_tiles,
        "new_network_attempts": new_attempts,
        "events": len(run_events),
        "mosaics": selected_mosaics,
        "note": "Acquisition only. This file makes no visual, historical, or social interpretation.",
    }
    write_json(out_root / "manifests" / "runs" / f"{args.run_id}.json", run_summary)
    generate_manifest(out_root, grids, all_rows, mosaic_rows, source)
    write_static_index(out_root)

    summary = {
        "run_id": args.run_id,
        "new_network_attempts": new_attempts,
        "downloaded": sum(1 for e in run_events if e["event"] == "downloaded"),
        "cached": sum(1 for e in run_events if e["event"] == "cache_hit"),
        "failed_or_deferred": sum(1 for e in run_events if e["event"] in {"not_received", "deferred", "dry_run"}),
        "mosaic_statuses": {r["grid_id"]: r["mosaic_status"] for r in selected_mosaics},
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
