"""
WORKER NOO FINDER  (dijalankan otomatis oleh GitHub Actions)
- Baca antrean Scrape_Request (STATUS = Queued)
- Scrape pakai scraper_outlet.py (v6.1: perilaku CLI lama tetap; worker memakai strict=True supaya blokir terdeteksi)
- Buang hasil yang sudah Covered (<= 50 m dan nama mirip), tulis sisanya ke Map_Points sebagai NOO
CATATAN: log GitHub di repo public bisa dibaca siapa saja -> jangan print data internal.

TAMBAHAN v6.2 (no HP + foto + log):
- scraper_outlet.py v6.2 mengembalikan No_HP, No_WA, Foto_Link dan SELALU mencatat tiap pencarian ke Sheet penampung (butuh env LOG_URL dan LOG_TOKEN
  di workflow: env: LOG_URL: ${{ secrets.LOG_URL }} / LOG_TOKEN: ${{ secrets.LOG_TOKEN }}). Pencatat = email pengguna (hanya masuk Sheet, tidak dicetak ke log).
- NOO baru ditulis dengan kolom PHONE (format 08xx) dan FOTO LINK di Map_Points (keduanya dibawa oleh build Code.gs).
- Hasil yang cocok dengan outlet Covered tidak jadi NOO, tetapi nomor HP/foto-nya mengisi kolom PHONE / FOTO LINK outlet Covered itu bila masih kosong,
  dan dicatat juga di tab Phone_Scrape (KEY, OUTLET NAME, PHONE, FOTO LINK, UPDATED) supaya tidak hilang saat build ulang Map_Points dari MDS.

TAMBAHAN (anti-blokir Google Maps) - logika lama di atas tidak diubah:
- 1 keyword per pencarian (MAX_KEYWORD), sisanya dibuang
- Jeda acak antar pencarian, batas per run / per jam / per 24 jam
- Circuit breaker: error koneksi/captcha beruntun atau "0 hasil" beruntun -> cooldown otomatis (naik bertahap),
  antrean TIDAK hilang, hanya ditunda dan diberi pesan jelas di kolom MESSAGE
- Status bersama dengan Apps Script di tab Scraper_State (kickScraper tidak memanggil GitHub saat cooldown,
  dan mengirim email ke admin saat ada ALERT)
- Pesan error ramah (bukan stack trace Playwright), koordinat pengguna disamarkan di log dan pesan
"""
import os, json, re, time, uuid, io, math, contextlib, random, urllib.request, urllib.error
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
IDLE_MAX_S = 600       # setelah antrean kosong, worker menunggu 10 menit (request berikutnya langsung diproses)
WIB = timezone(timedelta(hours=7))

# ---------- GUARD ANTI-BLOKIR (batas sebelum overload) ----------
MAX_KEYWORD = 1                 # keyword per pencarian (sisanya dibuang)
GAP_MIN_S, GAP_MAX_S = 30, 60   # jeda acak antar pencarian (detik)
RETRY_GAP_S = 90                # jeda sebelum mengulang pencarian yang error / 0 hasil
MAX_REQ_PER_RUN = 12            # maksimal pencarian per sekali run GitHub
MAX_REQ_PER_HOUR = 20           # maksimal pencarian dimulai dalam 60 menit terakhir (semua pengguna)
MAX_REQ_PER_DAY = 120           # maksimal pencarian dimulai dalam 24 jam terakhir
NET_TRIP = 2                    # error koneksi / captcha / timeout beruntun -> cooldown
ZERO_TRIP = 3                   # "0 hasil" beruntun (pencarian berbeda) -> cooldown pendek
COOLDOWN_MIN = 30               # cooldown awal (menit); naik 2x tiap pelanggaran beruntun
COOLDOWN_MAX_MIN = 240
ZERO_COOLDOWN_MIN = 15
EXPIRE_HOURS = 24               # request yang menunggu lebih dari ini dibatalkan dengan pesan jelas
STATE_SHEET = "Scraper_State"   # status bersama dengan Apps Script (cooldown, alert)
ISI_HP_COVERED = True           # no HP / foto hasil scrape yang cocok dengan outlet Covered -> isi PHONE / FOTO LINK outlet itu bila masih kosong
PHONE_SCRAPE_SHEET = "Phone_Scrape"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

NET_PAT = re.compile(r"ERR_CONNECTION|ERR_TIMED_OUT|ERR_NETWORK|ERR_PROXY|ERR_SSL|ERR_TUNNEL|ERR_EMPTY_RESPONSE|"
                     r"ERR_NAME|ERR_INTERNET|ERR_ADDRESS|net::ERR_|ECONNRESET|Connection (?:reset|closed|refused|aborted)|"
                     r"Remote end closed|RemoteDisconnected|EOF occurred|SSL", re.I)
CAPTCHA_PAT = re.compile(r"MapsBlocked|captcha|unusual traffic|/sorry/|recaptcha|HTTP Error (?:403|429)|Too Many Requests", re.I)
TIMEOUT_PAT = re.compile(r"Timeout \d+\s*ms exceeded|TimeoutError|timed out", re.I)


def now():
    return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")


def fmt(dt):
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s):
    """Baca 'yyyy-MM-dd HH:mm:ss' (WIB). Format lain -> None (tidak dianggap)."""
    try:
        return datetime.strptime(str(s).strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB)
    except (ValueError, TypeError):
        return None


def hhmm(dt):
    return dt.astimezone(WIB).strftime("%H:%M")


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


def zoom_for(radius_m, lat):
    """Zoom Google Maps agar lebar peta kira-kira 2 x radius (peta efektif ~880 px)."""
    mpp = 2 * radius_m / 880.0
    z = math.log2(156543.03 * math.cos(math.radians(lat)) / mpp)
    return max(12, min(18, round(z)))


def to_float(x):
    return float(str(x).replace(",", "."))


def norm_kab(s):
    s = re.sub(r"\s+", " ", str(s).upper().replace(".", " ")).strip()
    if s in ("", "-"):
        return ""
    s = re.sub(r"^KABUPATEN ", "KAB. ", s)
    s = re.sub(r"^KAB ", "KAB. ", s)
    return re.sub(r"^KOTA ADMINISTRASI ", "KOTA ", s)


def norm_kec(s):
    s = re.sub(r"\s+", " ", str(s).upper().replace(".", " ")).strip()
    return "" if s in ("", "-") else re.sub(r"^(KECAMATAN|KEC) ", "", s)


def mask(s):
    """Sembunyikan koordinat lokasi pengguna dari teks yang dicetak ke log GitHub (log repo public bisa dibaca siapa saja)."""
    return re.sub(r"@-?\d+\.\d+,-?\d+\.\d+", "@LOKASI", str(s))


def call(fn, *a, **kw):
    """Panggil Google Sheets API; ulangi dengan jeda bila kena batas kuota (429) atau error server sementara."""
    for i in range(4):
        try:
            return fn(*a, **kw)
        except gspread.exceptions.APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code in (429, 500, 502, 503, 504) and i < 3:
                time.sleep(15 * (i + 1))
                continue
            raise


def read(ws, unformatted=False):
    vals = call(ws.get_all_values, value_render_option="UNFORMATTED_VALUE") if unformatted else call(ws.get_all_values)
    hdr = vals[0] if vals else []
    return hdr, [r + [""] * (len(hdr) - len(r)) for r in vals[1:]]


def upd(ws, hdr, row, **vals):
    call(ws.batch_update,
         [{"range": rowcol_to_a1(row, hdr.index(k) + 1), "values": [[v]]} for k, v in vals.items()],
         value_input_option="RAW")


def get_log_ws(ss):
    try:
        return ss.worksheet("Run_Log")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet("Run_Log", 1000, 4)
        ws.append_row(["TIME", "SOURCE", "EVENT", "DETAIL"])
        return ws


def log(ws, event, detail=""):
    try:
        ws.append_row([now(), "GitHub", event, mask(detail)[:300]], value_input_option="RAW")
    except Exception:
        pass


# ---------- STATUS BERSAMA (tab Scraper_State) ----------
class State:
    """Penyimpanan key-value kecil di sheet, dibaca juga oleh Apps Script (kickScraper, aplikasi, email alert)."""

    def __init__(self, ss):
        try:
            self.ws = ss.worksheet(STATE_SHEET)
        except gspread.WorksheetNotFound:
            self.ws = ss.add_worksheet(STATE_SHEET, 60, 3)
            self.ws.append_row(["KEY", "VALUE", "UPDATED"])
        self.d, self.row = {}, {}
        self.reload()

    def reload(self):
        vals = call(self.ws.get_all_values)
        self.d, self.row = {}, {}
        for i, r in enumerate(vals[1:], start=2):
            if r and r[0]:
                self.d[r[0]] = r[1] if len(r) > 1 else ""
                self.row[r[0]] = i

    def get(self, k, default=""):
        v = self.d.get(k, "")
        return v if v != "" else default

    def geti(self, k):
        try:
            return int(float(self.get(k, 0)))
        except ValueError:
            return 0

    def set(self, **kv):
        ups, new = [], []
        for k, v in kv.items():
            v = "" if v is None else str(v)
            self.d[k] = v
            if k in self.row:
                ups.append({"range": f"B{self.row[k]}:C{self.row[k]}", "values": [[v, now()]]})
            else:
                new.append([k, v, now()])
        if ups:
            call(self.ws.batch_update, ups, value_input_option="RAW")
        if new:
            call(self.ws.append_rows, new, value_input_option="RAW")
            self.reload()


def cooldown_info(st):
    u = parse_ts(st.get("COOLDOWN_UNTIL"))
    if u and u > datetime.now(WIB):
        return u, st.get("COOLDOWN_KIND", "BLOCK"), st.get("COOLDOWN_REASON", "")
    return None


def set_hold_msg(req_ws, until, reason):
    """Beri tahu semua pencarian yang masih antre bahwa scraper ditunda (supaya pengguna tahu, bukan hanya menunggu)."""
    msg = f"Ditunda otomatis sampai {hhmm(until)} WIB: {reason}. Antrean aman dan diproses lanjut sendiri."
    hdr, rows = read(req_ws)
    ist, im = hdr.index("STATUS"), hdr.index("MESSAGE")
    ups = [{"range": rowcol_to_a1(i + 2, im + 1), "values": [[msg]]}
           for i, r in enumerate(rows) if r[ist] == "Queued" and r[im] != msg]
    if ups:
        call(req_ws.batch_update, ups, value_input_option="RAW")
    return sum(1 for r in rows if r[ist] == "Queued")


def start_hold(st, log_ws, req_ws, kind, reason, minutes=None, until=None, alert=True):
    """Pasang cooldown. kind: BLOCK (diblokir, naik bertahap) | ZERO (0 hasil beruntun) | LIMIT (batas per jam/hari)."""
    level = st.geti("TRIP_LEVEL")
    if until is None:
        if minutes is None:
            minutes = min(COOLDOWN_MAX_MIN, COOLDOWN_MIN * (2 ** level))
        until = datetime.now(WIB) + timedelta(minutes=minutes)
    vals = dict(COOLDOWN_UNTIL=fmt(until), COOLDOWN_KIND=kind, COOLDOWN_REASON=reason[:200],
                NET_STREAK=0, ZERO_STREAK=0)
    if kind == "BLOCK":
        vals["TRIP_LEVEL"] = level + 1
    queued = set_hold_msg(req_ws, until, reason)
    if alert and kind in ("BLOCK", "ZERO"):
        judul = ("Scraper Cari NOO DITUNDA (kemungkinan diblokir Google)" if kind == "BLOCK"
                 else "Scraper Cari NOO: hasil 0 berulang, ditunda sementara")
        body = (f"Scraper ditunda otomatis sampai {fmt(until)} WIB.\n"
                f"Penyebab: {reason}\n"
                f"Pencarian yang menunggu di antrean: {queued} (aman, diproses lanjut setelah cooldown).\n\n"
                "Yang bisa dilakukan:\n"
                "1. Tunggu. Sistem mencoba lagi otomatis; bila gagal lagi, jeda dinaikkan bertahap (maks 4 jam).\n"
                "2. Bila berulang: IP server GitHub kemungkinan diblokir Google. Jalankan scraper dari IP biasa "
                "(self-hosted runner di laptop/PC kantor) lalu isi Variable RUNNER_LABEL di GitHub.\n"
                "3. Cek log run terakhir di tab Actions GitHub.\n"
                "4. Setelah masalah beres, jalankan scraperResetCooldown() di Apps Script untuk membuka tahan.")
        vals.update(ALERT_ID=uuid.uuid4().hex[:8], ALERT_SUBJECT=judul, ALERT_BODY=body)
    st.set(**vals)
    log(log_ws, "COOLDOWN", f"{kind} sampai {fmt(until)}: {reason}")
    print(f"Scraper ditunda ({kind}) sampai {fmt(until)}: {mask(reason)}")
    return until


# ---------- KLASIFIKASI ERROR + PESAN RAMAH ----------
def classify(msg):
    m = str(msg)
    if CAPTCHA_PAT.search(m):
        return "CAPTCHA"
    if NET_PAT.search(m):
        return "NET"
    if TIMEOUT_PAT.search(m):
        return "TIMEOUT"
    return "OTHER"


def friendly(kind, msg):
    if kind == "NET":
        return "Koneksi ke Google Maps ditolak (kemungkinan IP server scraper diblokir Google)"
    if kind == "CAPTCHA":
        return "Google membatasi akses scraper (captcha / halaman tidak dikenali, server terdeteksi sebagai bot)"
    if kind == "TIMEOUT":
        return "Google Maps tidak merespons tepat waktu"
    return "Scraper error: " + mask(msg)[:110]


def preflight():
    """Cek ringan sebelum membuka browser. Return (ok, kind, detail). Hanya sinyal awal, bukan vonis."""
    try:
        rq = urllib.request.Request("https://www.google.com/maps?hl=id",
                                    headers={"User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9"})
        with urllib.request.urlopen(rq, timeout=25) as r:
            url, body = r.geturl(), r.read(60000).decode("utf-8", "ignore")
        if "/sorry/" in url or CAPTCHA_PAT.search(body[:20000]):
            return False, "CAPTCHA", "halaman verifikasi Google"
        return True, "", ""
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            return False, "CAPTCHA", f"HTTP {e.code}"
        return True, "", f"HTTP {e.code}"
    except Exception as e:
        return False, classify(e) if classify(e) != "OTHER" else "NET", str(e)[:120]


def check_limits(hdr, rows):
    """Batas pencarian per jam / per 24 jam (dihitung dari STARTED_AT). Return (until, alasan) atau None."""
    iS = hdr.index("STARTED_AT")
    n0 = datetime.now(WIB)
    h, d = [], []
    for r in rows:
        t = parse_ts(r[iS])
        if t:
            if n0 - t <= timedelta(hours=24):
                d.append(t)
            if n0 - t <= timedelta(hours=1):
                h.append(t)
    if len(d) >= MAX_REQ_PER_DAY:
        return min(d) + timedelta(hours=24), f"batas {MAX_REQ_PER_DAY} pencarian per 24 jam tercapai"
    if len(h) >= MAX_REQ_PER_HOUR:
        return min(h) + timedelta(hours=1), f"batas {MAX_REQ_PER_HOUR} pencarian per jam tercapai (melindungi dari blokir Google)"
    return None


def load_points(pts_ws):
    hdr, rows = read(pts_ws, unformatted=True)
    df = pd.DataFrame(rows, columns=hdr)
    df["LAT"] = pd.to_numeric(df["LAT"], errors="coerce")
    df["LONG"] = pd.to_numeric(df["LONG"], errors="coerce")
    cov = df[(df["TYPE"] == "COVERED") & df["LAT"].notna() & df["LONG"].notna()]
    noo = df[(df["TYPE"] == "NOO") & df["LAT"].notna() & df["LONG"].notna()]
    known = {(norm(n), round(a, 4), round(b, 4)) for n, a, b in zip(noo["OUTLET NAME"], noo["LAT"], noo["LONG"])}
    return hdr, cov, known


def split_kw(raw):
    return [k.strip() for k in str(raw).split(",") if k.strip()]


def process(req, pts_hdr, cov, known):
    kws = split_kw(req["KEYWORD"])[:MAX_KEYWORD]       # 1 keyword per pencarian
    n = int(float(req.get("MAX_RESULTS") or 20))
    nearby = str(req.get("MODE", "")).strip().upper() == "NEARBY"
    raw, extra = [], ""
    if nearby:
        # Mode "dekat lokasi saya": cari di sekitar titik (LAT, LONG) dengan RADIUS_M, urut dari yang terdekat.
        la0, lo0, rad = to_float(req["LAT"]), to_float(req["LONG"]), int(float(req["RADIUS_M"]))
        z = zoom_for(rad, la0)
        extra = f" dalam radius {rad} m"
        for kw in kws:
            q = f"{kw}/@{la0:.6f},{lo0:.6f},{z}z"
            with contextlib.redirect_stdout(io.StringIO()):      # log scraper memuat URL berisi lokasi pengguna, jangan dicetak
                res = so.scrape_google_maps(q, min(n * 2, 60), strict=True,
                                            meta={"by": req.get("USER_EMAIL", ""), "src": "WEBAPP",
                                                  "keyword": f"{kw} (dekat lokasi, radius {rad} m)"})
            keep = []
            for r in res:
                try:
                    lat, lon = float(r["Lat"]), float(r["Long"])
                except (TypeError, ValueError):
                    continue
                dm = float(dist_m(la0, lo0, np.array([lat]), np.array([lon]))[0])
                if dm <= rad * 1.05:
                    r["Keyword_Pencarian"] = kw
                    r["_jarak"] = dm
                    keep.append(r)
            keep.sort(key=lambda r: r["_jarak"])
            raw += keep[:n]
    else:
        area = " ".join(
            str(req.get(k, "")).strip() for k in ("KELURAHAN", "KECAMATAN", "KAB/KOTA")
            if str(req.get(k, "")).strip().upper() not in ("", "ALL")
        )
        for kw in kws:
            raw += so.scrape_google_maps(f"{kw} {area}".strip(), n, strict=True,
                                         meta={"by": req.get("USER_EMAIL", ""), "src": "WEBAPP", "keyword": f"{kw} {area}".strip()})

    cl, cn = cov["LAT"].to_numpy(), cov["LONG"].to_numpy()
    names = cov["OUTLET NAME"].tolist()
    new_rows, n_cov, n_dup = [], 0, 0
    cov_up, cov_seen = [], set()      # outlet Covered yang PHONE / FOTO LINK-nya masih kosong dan kini ada dari scraping
    hp_col = "PHONE" if "PHONE" in cov.columns else None
    ft_col = "FOTO LINK" if "FOTO LINK" in cov.columns else None
    kosong = lambda v: str(v).strip() in ("", "0", "nan", "None")

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
            hit = [i for i in np.where(d <= RADIUS_M)[0] if similar(r["Nama"], names[i])]
            if hit:
                n_cov += 1
                if ISI_HP_COVERED and (r.get("No_HP") or r.get("Foto_Link")):
                    idx = cov.index[hit[0]]
                    isi_hp = bool(hp_col and r.get("No_HP") and kosong(cov.at[idx, hp_col]))
                    isi_ft = bool(ft_col and r.get("Foto_Link") and kosong(cov.at[idx, ft_col]))
                    if (isi_hp or isi_ft) and idx not in cov_seen:
                        cov_seen.add(idx)
                        cov_up.append({"row": int(idx) + 2, "key": str(cov.at[idx, "KEY"]), "nama": str(cov.at[idx, "OUTLET NAME"]),
                                       "hp": r.get("No_HP", "") if isi_hp else "", "foto": r.get("Foto_Link", "") if isi_ft else ""})
                continue

        kab, kec = req.get("KAB/KOTA", ""), req.get("KECAMATAN", "")
        if nearby:                                  # wilayah diambil dari hasil reverse-geocoding titiknya
            kab, kec = norm_kab(r["Kab/Kota"]), norm_kec(r["Kecamatan"])
        o = {
            "KEY": "NOO-" + uuid.uuid4().hex[:10].upper(), "TYPE": "NOO",
            "OUTLET NAME": r["Nama"], "DISTRIBUTOR NAME": req["DISTRIBUTOR NAME"],
            "OUTLET STATUS": "NOO",
            "KAB/KOTA": kab, "KECAMATAN": kec,
            "KELURAHAN": r["Kelurahan"], "ALAMAT": r["Alamat"],
            "KODEPOS": (re.search(r"\b(\d{5})\b", str(r["Alamat"])) or [0, ""])[1],
            "LATLONG": f"{lat}, {lon}", "LAT": lat, "LONG": lon,
            "PIN_ICON": PIN_BASE + "noo.png", "NOO_STATUS": "New",
            "KEYWORD": r["Keyword_Pencarian"],
            "PHONE": r.get("No_HP", ""), "FOTO LINK": r.get("Foto_Link", ""),
            "NEAREST_COVERED_M": round(nearest) if nearest is not None else "",
            "SCRAPED_AT": now(), "USER_EMAIL": req["USER_EMAIL"], "REQUEST_ID": req["REQUEST_ID"],
        }
        new_rows.append([o.get(h, "") for h in pts_hdr])
    return len(raw), n_cov, n_dup, new_rows, extra, cov_up


def simpan_phone_scrape(ss, cov_up):
    """Catat nomor/foto outlet Covered hasil scraping di tab Phone_Scrape. Build Map_Points (Code.gs) memakainya bila PHONE di MDS kosong."""
    try:
        ws = ss.worksheet(PHONE_SCRAPE_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(PHONE_SCRAPE_SHEET, rows=2000, cols=5)
        call(ws.update, "A1:E1", [["KEY", "OUTLET NAME", "PHONE", "FOTO LINK", "UPDATED"]], value_input_option="RAW")
    ada = {str(v).strip(): i + 2 for i, v in enumerate(call(ws.col_values, 1)[1:])}
    now = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M")
    baru, ups = [], []
    for u in cov_up:
        if u["key"] in ada:
            if u["hp"]:
                ups.append({"range": f"C{ada[u['key']]}", "values": [[u["hp"]]]})
            if u["foto"]:
                ups.append({"range": f"D{ada[u['key']]}", "values": [[u["foto"]]]})
        else:
            baru.append([u["key"], u["nama"], u["hp"], u["foto"], now])
    if baru:
        call(ws.append_rows, baru, value_input_option="RAW")
    if ups:
        call(ws.batch_update, ups, value_input_option="RAW")


def main():
    gc = gspread.service_account_from_dict(json.loads(os.environ["GCP_SA_JSON"]))
    ss = gc.open_by_key(SHEET_ID)
    req_ws, pts_ws = ss.worksheet("Scrape_Request"), ss.worksheet("Map_Points")
    log_ws = get_log_ws(ss)
    st = State(ss)

    # --- sedang cooldown? (mis. dipanggil manual) -> jangan menyentuh Google, keluar ---
    cd = cooldown_info(st)
    if cd:
        until, kind, reason = cd
        set_hold_msg(req_ws, until, reason)
        print(f"Scraper masih ditahan ({kind}) sampai {fmt(until)}, keluar.")
        log(log_ws, "HOLD", f"Run dibatalkan: ditahan ({kind}) sampai {fmt(until)}")
        return

    pts_hdr, cov, known = load_points(pts_ws)
    print(f"Covered dengan GPS: {len(cov)}")
    log(log_ws, "START", f"Covered dengan GPS: {len(cov)}")

    t0 = time.time()
    idle_since = None
    preflighted = False
    done_run, next_ok = 0, 0.0
    while time.time() - t0 < MAX_RUNTIME_MIN * 60:
        hdr, rows = read(req_ws)
        ist, icr = hdr.index("STATUS"), hdr.index("CREATED_AT")
        todo = []
        for i, r in enumerate(rows):
            if r[ist] != "Queued":
                continue
            c = parse_ts(r[icr])
            if c and datetime.now(WIB) - c > timedelta(hours=EXPIRE_HOURS):     # kedaluwarsa: beri kabar, jangan menggantung
                upd(req_ws, hdr, i + 2, STATUS="Failed", DONE_AT=now(),
                    MESSAGE=f"Kedaluwarsa: tidak sempat diproses dalam {EXPIRE_HOURS} jam (scraper sempat ditunda). Silakan kirim ulang.")
                log(log_ws, "EXPIRED", f"{r[hdr.index('REQUEST_ID')]} menunggu lebih dari {EXPIRE_HOURS} jam")
                continue
            todo.append((i + 2, dict(zip(hdr, r))))
        if not todo:
            if idle_since is None:
                idle_since = time.time()
            if time.time() - idle_since > IDLE_MAX_S:
                print("Antrean kosong, selesai.")
                log(log_ws, "IDLE", "Antrean kosong, worker berhenti")
                break
            time.sleep(15)
            continue
        idle_since = None

        # --- batas sebelum overload ---
        if done_run >= MAX_REQ_PER_RUN:
            print(f"Batas {MAX_REQ_PER_RUN} pencarian per run tercapai, run berikutnya melanjutkan.")
            log(log_ws, "RUN_LIMIT", f"Batas {MAX_REQ_PER_RUN} per run tercapai, sisa antrean {len(todo)}")
            break
        lim = check_limits(hdr, rows)
        if lim:
            until, why = lim
            start_hold(st, log_ws, req_ws, "LIMIT", why, until=until, alert=False)
            break

        # --- cek ringan sekali per run: kalau gagal, error koneksi pertama langsung memicu cooldown ---
        if not preflighted:
            preflighted = True
            ok, pk, pd_ = preflight()
            if not ok:
                print(f"Pre-flight gagal ({pk}): {mask(pd_)}")
                log(log_ws, "PREFLIGHT", f"gagal ({pk}): {pd_}")
                st.set(NET_STREAK=max(st.geti("NET_STREAK"), NET_TRIP - 1))

        # --- jeda acak antar pencarian ---
        wait = next_ok - time.time()
        if wait > 0:
            time.sleep(wait)

        rn, req = todo[0]
        rid = req["REQUEST_ID"]
        attempts = int(float(req.get("ATTEMPTS") or 0)) + 1
        multi = len(split_kw(req.get("KEYWORD", ""))) > MAX_KEYWORD
        upd(req_ws, hdr, rn, STATUS="Running", STARTED_AT=now(), ATTEMPTS=attempts, MESSAGE="")
        print(f"Memproses {rid} (percobaan {attempts})")
        done_run += 1
        try:
            n_raw, n_cov, n_dup, new_rows, extra, cov_up = process(req, pts_hdr, cov, known)
            if n_raw == 0 and attempts < MAX_ATTEMPTS:
                upd(req_ws, hdr, rn, STATUS="Queued", MESSAGE="0 hasil, mengulang otomatis...")
                log(log_ws, "RETRY", f"{rid}: 0 hasil, percobaan {attempts}")
                next_ok = time.time() + RETRY_GAP_S
                continue
            if new_rows:
                call(pts_ws.append_rows, new_rows, value_input_option="RAW", table_range="A1")
            if cov_up:      # isi PHONE / FOTO LINK outlet Covered yang masih kosong + catat di Phone_Scrape
                ups = []
                for u in cov_up:
                    if u["hp"] and "PHONE" in pts_hdr:
                        ups.append({"range": rowcol_to_a1(u["row"], pts_hdr.index("PHONE") + 1), "values": [[u["hp"]]]})
                    if u["foto"] and "FOTO LINK" in pts_hdr:
                        ups.append({"range": rowcol_to_a1(u["row"], pts_hdr.index("FOTO LINK") + 1), "values": [[u["foto"]]]})
                if ups:
                    call(pts_ws.batch_update, ups, value_input_option="RAW")
                try:
                    simpan_phone_scrape(ss, cov_up)
                except Exception as e:
                    print(f"[WARN] Phone_Scrape gagal: {str(e)[:120]}")
            pre = f"Hanya keyword pertama ('{split_kw(req['KEYWORD'])[0]}') yang dicari (1 keyword per pencarian). " if multi else ""
            msg = pre + (f"{n_raw} ditemukan{extra}, {n_cov} sudah Covered, {n_dup} duplikat, {len(new_rows)} NOO baru"
                         + (f", HP {len(cov_up)} outlet existing terisi" if cov_up else "")
                         if n_raw else "0 hasil" + (extra + " (coba radius lebih besar atau keyword lain)" if extra
                                                    else " dari Google Maps. Keyword/area ini kemungkinan memang tidak ada hasil. "
                                                         "Bila terjadi berulang pada pencarian lain, scraper bisa jadi sedang dibatasi Google"))
            upd(req_ws, hdr, rn, STATUS="Done", MESSAGE=msg[:300], DONE_AT=now(), RESULT_COUNT=len(new_rows))
            print(f"Selesai: {len(new_rows)} NOO baru")
            log(log_ws, "DONE", f"{rid}: {msg}")
            next_ok = time.time() + random.uniform(GAP_MIN_S, GAP_MAX_S)
            if n_raw:      # sukses nyata: reset penghitung, turunkan tingkat cooldown bertahap
                st.set(NET_STREAK=0, ZERO_STREAK=0, LAST_OK_AT=now(), TRIP_LEVEL=max(0, st.geti("TRIP_LEVEL") - 1))
            else:
                z = st.geti("ZERO_STREAK") + 1
                st.set(ZERO_STREAK=z)
                if z >= ZERO_TRIP:
                    start_hold(st, log_ws, req_ws, "ZERO", f"{z} pencarian beruntun 0 hasil (kemungkinan Google membatasi scraper)",
                               minutes=ZERO_COOLDOWN_MIN)
                    break
        except Exception as e:
            emsg = mask(str(e))
            kind = classify(emsg)
            fr = friendly(kind, emsg)
            print(f"Gagal [{kind}]: {emsg[:150]}")
            log(log_ws, "ERROR", f"{rid} [{kind}] percobaan {attempts}: {emsg[:200]}")
            if kind in ("NET", "CAPTCHA", "TIMEOUT"):
                streak = st.geti("NET_STREAK") + 1
                st.set(NET_STREAK=streak, LAST_ERROR=f"{now()} [{kind}] {emsg[:150]}")
                if kind == "TIMEOUT" and attempts >= MAX_ATTEMPTS:
                    upd(req_ws, hdr, rn, STATUS="Failed", MESSAGE=f"Gagal: {fr}. Coba lagi nanti atau ganti keyword/area.", DONE_AT=now())
                else:   # blokir koneksi tidak menghabiskan jatah percobaan: ditunda, bukan dibuang
                    upd(req_ws, hdr, rn, STATUS="Queued", ATTEMPTS=(attempts if kind == "TIMEOUT" else max(0, attempts - 1)),
                        MESSAGE=f"{fr}. Dicoba lagi otomatis.", DONE_AT="")
                if streak >= NET_TRIP:
                    start_hold(st, log_ws, req_ws, "BLOCK", f"{fr} ({streak}x beruntun)")
                    break
                next_ok = time.time() + RETRY_GAP_S
            else:
                final = attempts >= MAX_ATTEMPTS
                upd(req_ws, hdr, rn, STATUS="Failed" if final else "Queued",
                    MESSAGE=(f"Gagal: {fr}" if final else f"{fr}. Mengulang otomatis..."),
                    DONE_AT=now() if final else "")
                next_ok = time.time() + RETRY_GAP_S

    log(log_ws, "END", f"Run selesai: {done_run} pencarian diproses")


if __name__ == "__main__":
    main()
