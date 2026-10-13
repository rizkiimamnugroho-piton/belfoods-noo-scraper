"""
Bangun data penduduk untuk peta densitas (dijalankan GitHub Actions: workflow "population", manual).
Sumber: WorldPop grid penduduk Indonesia (estimasi per sel ~1 km). Hasil ke sheet:
  Penduduk (DISTRIBUTOR, R, C, PENDUDUK)  Penduduk_Index (DISTRIBUTOR, START_ROW, N)  Penduduk_Meta (KEY, VALUE)
Sel yang disimpan: radius RADIUS sel di sekitar outlet covered tiap distributor.
"""
import os, json, urllib.request
import numpy as np, rasterio, gspread

PATH = "GIS/Population/Global_2000_2020_1km/2020/IDN/idn_ppp_2020_1km_Aggregated.tif"
URLS = [u for u in [os.environ.get("POP_URL"), "https://data.worldpop.org/" + PATH,
                    "https://worldpop-public-data.soton.ac.uk/" + PATH] if u]
RADIUS = 2
CHUNK = 20000


def download(urls, dest):
    last = None
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Belfoods-RTM population builder)"})
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            print("Unduh OK:", u, os.path.getsize(dest), "byte")
            return
        except Exception as e:
            last = e
            print("Gagal unduh", u, "->", e)
    raise RuntimeError("Semua alamat unduhan gagal: " + str(last))


def build_rows(tif, outlets):
    """outlets: list of (distributor, lat, lon) -> (meta, rows [[dist, R, C, pop]], index [[dist, start_row, n]])"""
    with rasterio.open(tif) as src:
        a = src.read(1).astype("float64")
        t = src.transform
    step = t.a
    if abs(abs(t.e) - step) > 1e-6:
        raise RuntimeError("Sel raster tidak persegi")
    lat0, lon0 = t.f + t.e / 2, t.c + t.a / 2          # pusat sel R=0, C=0
    H, W = a.shape
    off = np.array([(i, j) for i in range(-RADIUS, RADIUS + 1) for j in range(-RADIUS, RADIUS + 1)])
    per = {}
    for d, la, lo in outlets:
        per.setdefault(d, []).append((round((lat0 - la) / step), round((lo - lon0) / step)))
    rows, index = [], []
    for d in sorted(per):
        rc = np.unique(np.array(per[d]), axis=0)
        cells = np.unique((rc[:, None, :] + off[None, :, :]).reshape(-1, 2), axis=0)
        cells = cells[(cells[:, 0] >= 0) & (cells[:, 0] < H) & (cells[:, 1] >= 0) & (cells[:, 1] < W)]
        pop = a[cells[:, 0], cells[:, 1]]
        keep = pop >= 1
        cells, pop = cells[keep], pop[keep]
        index.append([d, len(rows) + 2, len(cells)])
        rows += [[d, int(r), int(c), int(round(p))] for (r, c), p in zip(cells, pop)]
    return {"LAT0": lat0, "LON0": lon0, "STEP": step}, rows, index


def put(ss, name, header, data):
    try:
        ws = ss.worksheet(name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(name, 2, len(header))
    ws.resize(rows=max(len(data) + 1, 2), cols=len(header))
    ws.update(range_name="A1", values=[header], value_input_option="RAW")
    for i in range(0, len(data), CHUNK):
        part = data[i:i + CHUNK]
        ws.update(range_name=f"A{i + 2}", values=part, value_input_option="RAW")


def main():
    gc = gspread.service_account_from_dict(json.loads(os.environ["GCP_SA_JSON"]))
    ss = gc.open_by_key(os.environ["SHEET_ID"])
    vals = ss.worksheet("Map_Points").get_all_values(value_render_option="UNFORMATTED_VALUE")
    h = vals[0]
    iT, iD, iLa, iLo = h.index("TYPE"), h.index("DISTRIBUTOR NAME"), h.index("LAT"), h.index("LONG")
    outlets = [(r[iD], float(r[iLa]), float(r[iLo])) for r in vals[1:]
               if r[iT] == "COVERED" and isinstance(r[iLa], (int, float)) and isinstance(r[iLo], (int, float))]
    print("Outlet covered:", len(outlets))
    download(URLS, "/tmp/pop.tif")
    meta, rows, index = build_rows("/tmp/pop.tif", outlets)
    print("Sel:", len(rows), "Distributor:", len(index))
    put(ss, "Penduduk", ["DISTRIBUTOR", "R", "C", "PENDUDUK"], rows)
    put(ss, "Penduduk_Index", ["DISTRIBUTOR", "START_ROW", "N"], index)
    put(ss, "Penduduk_Meta", ["KEY", "VALUE"], [[k, v] for k, v in meta.items()])
    print("Selesai")


if __name__ == "__main__":
    main()
