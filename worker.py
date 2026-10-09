"""
WORKER NOO FINDER  (dijalankan otomatis oleh GitHub Actions)
- Baca antrean Scrape_Request (STATUS = Queued)
- Scrape pakai scraper_outlet.py (script asli, TIDAK diubah)
- Buang hasil yang sudah Covered (<= 50 m dan nama mirip), tulis sisanya ke Map_Points sebagai NOO
CATATAN: log GitHub di repo public bisa dibaca siapa saja -> jangan print data internal.
"""
import os, json, re, time, uuid
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

import numpy as np
import pandas as pd
import gspread
from gspread.utils import rowcol_to_a1

import scraper_outlet as so   # script scraper asli

SHEET_ID = os.environ["SHEET_ID"]
PIN_BASE = os.environ.get("PIN_BASE_URL", "")
RADIUS_M = 50          # jarak maksimal dianggap outlet yang sama
NAME_SIM = 0.6         # kemiripan nama minimal (0-1)
MAX_ATTEMPTS = 2       # percobaan ulang otomatis kalau gagal / 0 hasil
MAX_RUNTIME_MIN = 45   # batas waktu satu run
WIB = timezone(timedelta(hours=7))


def now():
    return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", str(s).upper())).strip()


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a in b or b in a or SequenceMatcher(None, a, b).ratio() >= NAME_SIM


def dist_m(lat, lon, lats, lons):
    R = 6371000.0
    p1, p2 = np.radians(lat), np.radians(lats)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lons - lon) / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def read(ws, unformatted=False):
    vals = ws.get_all_values(value_render_option="UNFORMATTED_VALUE") if unformatted else ws.get_all_values()
    hdr = vals[0] if vals else []
    return hdr, [r + [""] * (len(hdr) - len(r)) for r in vals[1:]]


def upd(ws, hdr, row, **vals):
    ws.batch_update(
        [{"range": rowcol_to_a1(row, hdr.index(k) + 1), "values": [[v]]} for k, v in vals.items()],
        value_input_option="RAW",
    )


def load_points(pts_ws):
    hdr, rows = read(pts_ws, unformatted=True)
    df = pd.DataFrame(rows, columns=hdr)
    df["LAT"] = pd.to_numeric(df["LAT"], errors="coerce")
    df["LONG"] = pd.to_numeric(df["LONG"], errors="coerce")
    cov = df[(df["TYPE"] == "COVERED") & df["LAT"].notna() & df["LONG"].notna()]
    noo = df[(df["TYPE"] == "NOO") & df["LAT"].notna() & df["LONG"].notna()]
    known = {(norm(n), round(a, 4), round(b, 4)) for n, a, b in zip(noo["OUTLET NAME"], noo["LAT"], noo["LONG"])}
    return hdr, cov, known


def process(req, pts_hdr, cov, known):
    kws = [k.strip() for k in str(req["KEYWORD"]).split(",") if k.strip()]
    n = int(float(req.get("MAX_RESULTS") or 20))
    area = " ".join(
        str(req.get(k, "")).strip() for k in ("KELURAHAN", "KECAMATAN", "KAB/KOTA")
        if str(req.get(k, "")).strip().upper() not in ("", "ALL")
    )
    raw = []
    for kw in kws:
        raw += so.scrape_google_maps(f"{kw} {area}".strip(), n)

    cl, cn = cov["LAT"].to_numpy(), cov["LONG"].to_numpy()
    names = cov["OUTLET NAME"].tolist()
    new_rows, n_cov, n_dup = [], 0, 0

    for r in raw:
        try:
            lat, lon = float(r["Lat"]), float(r["Long"])
        except (TypeError, ValueError):
            continue
        key = (norm(r["Nama"]), round(lat, 4), round(lon, 4))
        if key in known:
            n_dup += 1
            continue
        known.add(key)

        nearest = None
        if len(cl):
            d = dist_m(lat, lon, cl, cn)
            nearest = float(d.min())
            if any(similar(r["Nama"], names[i]) for i in np.where(d <= RADIUS_M)[0]):
                n_cov += 1
                continue

        o = {
            "KEY": "NOO-" + uuid.uuid4().hex[:10].upper(), "TYPE": "NOO",
            "OUTLET NAME": r["Nama"], "DISTRIBUTOR NAME": req["DISTRIBUTOR NAME"],
            "OUTLET STATUS": "NOO",
            "KAB/KOTA": req.get("KAB/KOTA", ""), "KECAMATAN": req.get("KECAMATAN", ""),
            "KELURAHAN": r["Kelurahan"], "ALAMAT": r["Alamat"],
            "LATLONG": f"{lat}, {lon}", "LAT": lat, "LONG": lon,
            "PIN_ICON": PIN_BASE + "noo.png", "NOO_STATUS": "New",
            "KEYWORD": r["Keyword_Pencarian"],
            "NEAREST_COVERED_M": round(nearest) if nearest is not None else "",
            "SCRAPED_AT": now(), "USER_EMAIL": req["USER_EMAIL"], "REQUEST_ID": req["REQUEST_ID"],
        }
        new_rows.append([o.get(h, "") for h in pts_hdr])
    return len(raw), n_cov, n_dup, new_rows


def main():
    gc = gspread.service_account_from_dict(json.loads(os.environ["GCP_SA_JSON"]))
    ss = gc.open_by_key(SHEET_ID)
    req_ws, pts_ws = ss.worksheet("Scrape_Request"), ss.worksheet("Map_Points")
    pts_hdr, cov, known = load_points(pts_ws)
    print(f"Covered dengan GPS: {len(cov)}")

    t0 = time.time()
    while time.time() - t0 < MAX_RUNTIME_MIN * 60:
        hdr, rows = read(req_ws)
        ist = hdr.index("STATUS")
        todo = [(i + 2, dict(zip(hdr, r))) for i, r in enumerate(rows) if r[ist] == "Queued"]
        if not todo:
            print("Antrean kosong, selesai.")
            break
        rn, req = todo[0]
        attempts = int(float(req.get("ATTEMPTS") or 0)) + 1
        upd(req_ws, hdr, rn, STATUS="Running", STARTED_AT=now(), ATTEMPTS=attempts, MESSAGE="")
        print(f"Memproses {req['REQUEST_ID']} (percobaan {attempts})")
        try:
            n_raw, n_cov, n_dup, new_rows = process(req, pts_hdr, cov, known)
            if n_raw == 0 and attempts < MAX_ATTEMPTS:
                upd(req_ws, hdr, rn, STATUS="Queued", MESSAGE="0 hasil, mengulang otomatis...")
                time.sleep(20)
                continue
            if new_rows:
                pts_ws.append_rows(new_rows, value_input_option="RAW", table_range="A1")
            msg = (f"{n_raw} ditemukan, {n_cov} sudah Covered, {n_dup} duplikat, {len(new_rows)} NOO baru"
                   if n_raw else "0 hasil dari Google Maps (cek keyword/area, atau kemungkinan diblokir)")
            upd(req_ws, hdr, rn, STATUS="Done", MESSAGE=msg, DONE_AT=now(), RESULT_COUNT=len(new_rows))
            print(f"Selesai: {len(new_rows)} NOO baru")
        except Exception as e:
            status = "Queued" if attempts < MAX_ATTEMPTS else "Failed"
            upd(req_ws, hdr, rn, STATUS=status, MESSAGE=f"Error: {str(e)[:150]}",
                DONE_AT=now() if status == "Failed" else "")
            print(f"Gagal ({status}): {str(e)[:150]}")


if __name__ == "__main__":
    main()
