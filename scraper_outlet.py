"""
=============================================================
  SCRAPER DATA OUTLET DARI GOOGLE MAPS  (v6 - multi-keyword)
  + v6.1 anti-blokir: UA tidak 'HeadlessChrome', consent otomatis, retry koneksi,
    deteksi captcha/halaman asing (strict=True dipakai worker: raise MapsBlocked, bukan diam-diam 0 hasil)
  - headless mode (tanpa tampilan browser) = lebih cepat
  - wait for selector, bukan sleep tetap
  - scroll adaptif (berhenti otomatis jika tidak ada item baru)
  - bisa isi 1-5 keyword sekaligus, hasil digabung 1 file Excel
  - nama file output otomatis mengikuti keyword yang diisi
  v6.2: + No HP / No WA / foto outlet (diunggah ke Drive, di Sheet hanya link)
        + SETIAP scraping (siapa pun yang menjalankan: laptop, GitHub Actions, worker)
          otomatis dicatat ke Google Sheet penampung (tab SCRAPE_LOG + SCRAPE_RUN)
=============================================================
  py scraper_outlet.py
=============================================================
"""

import os
import time
import re
import json
import glob
import uuid
import base64
import hashlib
import getpass
import socket
import random
from datetime import datetime
import urllib.request
import urllib.parse
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─────────────────────────────────────────────────────────────
#  KONFIGURASI
#  Isi KEYWORD_1 wajib. KEYWORD_2 s/d KEYWORD_5 opsional —
#  kosongkan ("") kalau tidak dipakai. Semua keyword yang diisi
#  akan di-scrape lalu digabung jadi SATU file Excel.
#  Nama file output otomatis dibuat dari keyword yang diisi.
# ─────────────────────────────────────────────────────────────
KEYWORD_1 = "SRC PRINGSEWU LAMPUNG"
KEYWORD_2 = "SRC KOTABUMI LAMPUNG"
KEYWORD_3 = "SRC BANDARLAMPUNG"
KEYWORD_4 = "SRC KRUI LAMPUNG"
KEYWORD_5 = "SRC LIWA LAMPUNG"

JUMLAH_OUTLET_PER_KEYWORD = 10   # target outlet PER keyword

# Kosongkan ("") kalau mau nama file auto dari keyword.
# Isi manual kalau mau nama file custom, contoh: "DATA_OUTLET_LAMPUNG"
NAMA_FILE_OUTPUT_MANUAL = ""

# True  = Kelurahan/Kecamatan/Kab-Kota diambil dari reverse-geocoding
#         koordinat (OpenStreetMap Nominatim) → jauh lebih akurat,
#         tapi nambah ±1 detik proses per outlet.
# False = pakai cara lama (tebak dari teks alamat Google Maps saja,
#         lebih cepat tapi kadang salah tangkap nama wilayah).
GUNAKAN_REVERSE_GEOCODING = True

# ── v6.2 ─────────────────────────────────────────────────────
AMBIL_NO_HP = True        # ambil nomor telepon dari halaman outlet
AMBIL_FOTO  = os.environ.get("AMBIL_FOTO", "1") != "0"   # ambil foto utama outlet -> Drive (butuh LOG aktif); isi env AMBIL_FOTO=0 untuk mematikan
FOTO_LEBAR  = 800         # lebar foto (px); tinggi otomatis 3:4
LOG_KE_SHEET = True       # catat tiap scraping ke Google Sheet penampung

# URL web app + token TIDAK ditulis di file ini (repo publik!). Isi lewat:
#   (1) environment variable LOG_URL dan LOG_TOKEN  (GitHub Actions: Secrets), atau
#   (2) file log_config.json di folder yang sama:  {"url": "https://script.google.com/macros/s/.../exec", "token": "..."}
#       (masukkan log_config.json ke .gitignore)
# Opsional: RUN_BY = nama pemakai yang ditulis di kolom "DIJALANKAN OLEH" (default: user@komputer)
#           RUN_SOURCE = label sumber (default: LAPTOP / GITHUB ACTIONS); worker mengirim meta sendiri (email pengguna + WEBAPP)
# ─────────────────────────────────────────────────────────────


def buat_nama_file_otomatis(keywords: list) -> str:
    """Bangun nama file dari daftar keyword, dibersihkan dari karakter aneh."""
    def bersihkan(k):
        k = re.sub(r'[^A-Za-z0-9]+', '_', k.strip())
        return k.strip('_').upper()

    bagian = [bersihkan(k) for k in keywords if k.strip()]
    if not bagian:
        return "DATA_OUTLET_HASIL"

    nama = "DATA_OUTLET_" + "_".join(bagian)
    # Batasi panjang nama file biar tidak kepanjangan kalau keyword banyak
    if len(nama) > 150:
        nama = nama[:150]
    return nama


def ambil_teks(page, selectors: list, attr: str = None) -> str:
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                val = el.get_attribute(attr) if attr else el.inner_text()
                if val and val.strip():
                    return val.strip()
        except Exception:
            pass
    return "-"


def parse_latlong_dari_url(url: str):
    match = re.search(r'/@(-?\d+\.\d+),(-?\d+\.\d+)', url)
    if match:
        return float(match.group(1)), float(match.group(2))
    match2 = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
    if match2:
        return float(match2.group(1)), float(match2.group(2))
    return "-", "-"


def parse_wilayah_dari_alamat(alamat: str) -> dict:
    hasil = {"Kelurahan": "-", "Kecamatan": "-", "Kab/Kota": "-"}
    if alamat == "-":
        return hasil

    parts = [p.strip() for p in alamat.split(",")]

    for p in parts:
        if re.match(r'^(Kota|Kabupaten|Kab\.)\s+', p, re.IGNORECASE):
            hasil["Kab/Kota"] = re.sub(r'\s+\d{5}.*$', '', p).strip()
            break
    if hasil["Kab/Kota"] == "-":
        for p in reversed(parts):
            p_clean = re.sub(r'\s+\d{5}.*$', '', p).strip()
            if p_clean and not re.match(r'^(Jawa|Jl\.|RT|RW|\d)', p_clean, re.IGNORECASE):
                hasil["Kab/Kota"] = p_clean
                break

    for p in parts:
        m = re.match(r'^Kec(?:amatan)?\.?\s+(.+)$', p, re.IGNORECASE)
        if m:
            hasil["Kecamatan"] = m.group(1).strip()
            break

    kec_index = None
    for i, p in enumerate(parts):
        if re.match(r'^Kec(?:amatan)?\.?\s+', p, re.IGNORECASE):
            kec_index = i
            break

    if kec_index is not None and kec_index >= 2:
        kandidat = parts[kec_index - 1]
        if not re.match(r'^(RT|RW|\d)', kandidat, re.IGNORECASE):
            hasil["Kelurahan"] = kandidat
    elif kec_index is None and len(parts) >= 2:
        kandidat = parts[1]
        if not re.match(r'^(RT|RW|\d|Kota|Kab)', kandidat, re.IGNORECASE):
            hasil["Kelurahan"] = kandidat

    return hasil


def ambil_wilayah_dari_koordinat(lat, lon):
    """
    Reverse-geocode koordinat ke Kelurahan/Kecamatan/Kab-Kota pakai
    OpenStreetMap Nominatim (gratis, tanpa API key).

    Ini JAUH lebih akurat dibanding parse_wilayah_dari_alamat() yang cuma
    nebak dari teks alamat mentah Google Maps — karena berbasis koordinat
    persis, bukan tebak-tebakan pola teks yang formatnya suka tidak
    konsisten (contoh kasus: nama kelurahan "Kota Gapura" salah kebaca
    sebagai nama kabupaten/kota karena diawali kata "Kota").

    Return None kalau gagal (tidak ada internet ke Nominatim, koordinat
    kosong, dsb) — caller sebaiknya fallback ke parse_wilayah_dari_alamat().
    """
    if lat in ("-", None) or lon in ("-", None):
        return None
    try:
        params = urllib.parse.urlencode({
            "format": "jsonv2",
            "lat": lat,
            "lon": lon,
            "zoom": 18,
            "addressdetails": 1,
            "accept-language": "id",
        })
        url = f"https://nominatim.openstreetmap.org/reverse?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "RTM-Belfoods-OutletScraper/1.0 (internal tool, non-komersial)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        addr = data.get("address", {})
        if not addr:
            return None

        kelurahan = (addr.get("village") or addr.get("hamlet")
                     or addr.get("neighbourhood") or addr.get("suburb") or "-")
        kecamatan = (addr.get("city_district") or addr.get("suburb")
                     or addr.get("municipality") or addr.get("subdistrict") or "-")
        kab_kota  = (addr.get("county") or addr.get("city")
                     or addr.get("state_district") or addr.get("regency") or "-")

        return {"Kelurahan": kelurahan, "Kecamatan": kecamatan, "Kab/Kota": kab_kota}

    except Exception:
        return None


def scrape_detail(page) -> dict:
    nama = ambil_teks(page, [
        'h1.DUwDvf',
        'h1[class*="fontHeadlineLarge"]',
        'h1',
    ])
    alamat = ambil_teks(page, [
        'button[data-item-id="address"] div.fontBodyMedium',
        'button[data-item-id="address"]',
        '[data-tooltip="Salin alamat"] div.fontBodyMedium',
        'button[aria-label*="Alamat"]',
    ])
    return {"Nama": nama, "Alamat": alamat}


# ═════════════════════════════════════════════════════════════
#  v6.2 — NO HP, FOTO, LOG KE GOOGLE SHEET
# ═════════════════════════════════════════════════════════════
def normalisasi_hp(raw) -> tuple:
    """
    Return (hp_lokal, wa). hp_lokal = format 08xx / 0721xxx; wa = 628xx... (kosong kalau bukan nomor seluler,
    karena telepon rumah/kantor tidak punya WhatsApp -> tombol cukup 'Telepon').
    """
    if not raw or str(raw).strip() in ("", "-"):
        return "", ""
    d = re.sub(r"\D", "", str(raw))
    if not d:
        return "", ""
    if d.startswith("62"):
        intl = d
    elif d.startswith("0"):
        intl = "62" + d[1:]
    elif d.startswith("8"):
        intl = "62" + d
    else:
        return "", ""
    if not (9 <= len(intl) <= 15):
        return "", ""
    lokal = "0" + intl[2:]
    wa = intl if intl.startswith("628") else ""
    return lokal, wa


def ambil_no_hp(page) -> tuple:
    """Baca nomor telepon dari panel info outlet. Return (hp_lokal, wa)."""
    raw = ""
    try:
        el = page.query_selector('button[data-item-id^="phone:tel:"]')
        if el:
            raw = (el.get_attribute("data-item-id") or "").split("phone:tel:", 1)[-1]
            if not re.search(r"\d{6,}", raw):
                raw = el.get_attribute("aria-label") or el.inner_text() or ""
        if not raw:
            el = page.query_selector('button[aria-label^="Telepon"], button[aria-label^="Phone"]')
            if el:
                raw = el.get_attribute("aria-label") or el.inner_text() or ""
    except Exception:
        pass
    return normalisasi_hp(raw)


def ambil_url_foto(page) -> str:
    """URL foto utama outlet (diperbesar ke FOTO_LEBAR). Kosong kalau outlet tidak punya foto."""
    try:
        page.wait_for_selector('img[src*="googleusercontent.com"]', timeout=2500)
    except Exception:
        pass
    cands = []
    for sel in ['button[jsaction*="heroHeaderImage"] img', 'button.aoRNLd img',
                'div[role="main"] img[src*="googleusercontent.com/p/"]',
                'img[src*="googleusercontent.com/p/"]', 'img[src*="gps-cs-s"]']:
        try:
            for el in page.query_selector_all(sel)[:3]:
                cands.append(el.get_attribute("src") or "")
        except Exception:
            pass
    try:
        el = page.query_selector('meta[property="og:image"]')
        if el:
            cands.append(el.get_attribute("content") or "")
    except Exception:
        pass
    for u in cands:
        if not u.startswith("http"):
            continue
        low = u.lower()
        if "staticmap" in low or "/maps/vt" in low or "streetviewpixels" in low or "favicon" in low:
            continue
        if "googleusercontent" not in low and "ggpht" not in low:
            continue
        tinggi = int(FOTO_LEBAR * 0.75)
        if re.search(r"=[wsh]\d+", u):
            u = re.sub(r"=[wsh]\d+[^/?]*$", f"=w{FOTO_LEBAR}-h{tinggi}-k-no", u)
        return u
    return ""


# ---------- koneksi ke web app penampung (LogScrape.gs) ----------
def _muat_log_config():
    url = os.environ.get("LOG_URL", "").strip()
    token = os.environ.get("LOG_TOKEN", "").strip()
    if not (url and token):
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_config.json")
        if os.path.exists(f):
            try:
                c = json.load(open(f, encoding="utf-8"))
                url = url or str(c.get("url", "")).strip()
                token = token or str(c.get("token", "")).strip()
            except Exception:
                pass
    return url, token


_LOG_URL, _LOG_TOKEN = _muat_log_config()
_LOG_AKTIF = bool(LOG_KE_SHEET and _LOG_URL and _LOG_TOKEN)


def _meta_default() -> dict:
    try:
        by = os.environ.get("RUN_BY") or f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        by = os.environ.get("RUN_BY") or "tidak diketahui"
    src = os.environ.get("RUN_SOURCE") or ("GITHUB ACTIONS" if os.environ.get("GITHUB_ACTIONS") else "LAPTOP")
    return {"by": by, "src": src}


def _post_json(url: str, payload: dict, timeout: int = 90, coba: int = 3) -> dict:
    """POST JSON ke web app Apps Script (302 -> GET hasil diikuti otomatis oleh urllib). Retry 3x."""
    payload = dict(payload, token=_LOG_TOKEN)
    data = json.dumps(payload).encode("utf-8")
    err = None
    for t in range(coba):
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "text/plain;charset=utf-8"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "respon tidak OK"))
            return res
        except Exception as e:
            err = e
            time.sleep(2 * (t + 1))
    raise RuntimeError(f"gagal kirim ke penampung: {str(err)[:120]}")


def proses_foto(page, nama: str, lat, lon) -> str:
    """Unduh foto outlet dari Google Maps lalu unggah ke Drive lewat web app. Return link Drive ('' kalau gagal / tidak ada)."""
    if not (AMBIL_FOTO and _LOG_AKTIF):
        return ""
    try:
        src = ambil_url_foto(page)
        if not src:
            return ""
        resp = page.context.request.get(src, timeout=15000)
        if not resp.ok:
            return ""
        body = resp.body()
        mime = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        if not body or len(body) > 4_500_000 or not mime.startswith("image/"):
            return ""
        kunci = hashlib.sha1(f"{nama}|{lat}|{lon}".encode("utf-8")).hexdigest()[:10]
        slug = re.sub(r"[^A-Za-z0-9]+", "_", nama).strip("_")[:40] or "OUTLET"
        res = _post_json(_LOG_URL, {"action": "photo", "name": f"{slug}_{kunci}", "mime": mime,
                                    "b64": base64.b64encode(body).decode("ascii")}, timeout=90, coba=2)
        return res.get("url", "")
    except Exception as e:
        print(f"         ⚠ foto dilewati: {str(e)[:80]}")
        return ""


def _kirim_paket(paket: dict) -> bool:
    try:
        _post_json(_LOG_URL, dict(paket, action="rows"))
        return True
    except Exception as e:
        print(f"[WARN] {e}")
        return False


def _kirim_pending():
    """Kirim ulang paket log yang dulu gagal terkirim (file log_pending_*.json)."""
    for f in glob.glob("log_pending_*.json"):
        try:
            if _kirim_paket(json.load(open(f, encoding="utf-8"))):
                os.remove(f)
                print(f"[INFO] log tertunda terkirim: {f}")
        except Exception:
            pass


def kirim_log(rows: list, keyword: str, target: int, status: str, pesan: str, meta, durasi: float):
    """Catat 1 kali scraping (1 keyword) ke Sheet penampung. Tidak pernah membuat scraping gagal."""
    if not _LOG_AKTIF:
        return
    m = _meta_default()
    m.update(meta or {})
    run_id = datetime.now().strftime("%y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    data = [{
        "nama": r.get("Nama", ""), "alamat": r.get("Alamat", ""), "kel": r.get("Kelurahan", ""),
        "kec": r.get("Kecamatan", ""), "kab": r.get("Kab/Kota", ""), "lat": r.get("Lat", ""), "lon": r.get("Long", ""),
        "hp": r.get("No_HP", ""), "wa": r.get("No_WA", ""), "foto": r.get("Foto_Link", ""), "maps": r.get("Link_Maps", ""),
    } for r in rows]
    ket = "OK" if (rows or status != "OK") else "KOSONG"
    base = {"meta": {"by": m["by"], "src": m["src"], "runId": run_id, "keyword": m.get("keyword") or keyword},
            "run": {"target": target, "found": len(rows), "hp": sum(1 for r in rows if r.get("No_HP")),
                    "foto": sum(1 for r in rows if r.get("Foto_Link")), "durasi": round(durasi),
                    "status": status if status != "OK" else ket, "pesan": pesan}}
    _kirim_pending()
    pecah = [data[i:i + 100] for i in range(0, len(data), 100)] or [[]]
    semua_ok = True
    for i, chunk in enumerate(pecah):
        paket = dict(base, rows=chunk)
        if i > 0:
            paket.pop("run")
        if not _kirim_paket(paket):
            semua_ok = False
            fn = f"log_pending_{run_id}_{i}.json"
            json.dump(paket, open(fn, "w", encoding="utf-8"))
            print(f"[WARN] log disimpan lokal ({fn}), akan dikirim ulang pada scraping berikutnya.")
    if semua_ok:
        print(f"[INFO] Log tercatat di Sheet penampung: {len(rows)} outlet (run {run_id})")


class MapsBlocked(Exception):
    """Google Maps menolak / membatasi akses (captcha, consent tak bisa dilewati, halaman asing, koneksi ditutup)."""


def _goto_dengan_retry(page, url):
    """goto halaman pencarian; error koneksi (net::ERR_*) dicoba ulang 1x setelah jeda, lalu dilempar apa adanya."""
    for t in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception as e:
            if t == 0 and "net::ERR" in str(e):
                print("         → koneksi ditolak, coba ulang sekali setelah jeda...")
                time.sleep(random.uniform(8, 14))
                continue
            raise


def _tangani_consent(page, url):
    """Kalau diarahkan ke halaman consent Google, klik setujui lalu buka ulang URL pencarian."""
    if "consent.google" not in page.url and "consent.youtube" not in page.url:
        return
    print("         → halaman consent Google, mencoba menyetujui...")
    try:
        page.locator("button:has-text('Terima semua'), button:has-text('Accept all'), button:has-text('Setuju')").first.click(timeout=6000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    if "consent." in page.url:
        _goto_dengan_retry(page, url)


def _jenis_halaman(page) -> str:
    """SORRY (captcha/unusual traffic) | CONSENT | NOHASIL (Google bilang memang tidak ada hasil) | ASING (tidak dikenali)"""
    u = page.url
    if "/sorry/" in u or "recaptcha" in u:
        return "SORRY"
    if "consent." in u:
        return "CONSENT"
    try:
        t = page.inner_text("body", timeout=3000).lower()
    except Exception:
        t = ""
    if "unusual traffic" in t or "lalu lintas yang tidak biasa" in t or "captcha" in t:
        return "SORRY"
    if "tidak dapat menemukan" in t or "can't find" in t or "cannot find" in t:
        return "NOHASIL"
    return "ASING"


def _scrape_inti(keyword: str, jumlah: int, strict: bool, hasil: list) -> list:
    """
    strict=False (default, perilaku lama CLI): panel tidak muncul -> print error, return [].
    strict=True  (dipakai worker): bedakan "memang tidak ada hasil" (return []) dari "diblokir / halaman asing"
                 (raise MapsBlocked), supaya worker bisa menunda scraper, bukan mencatat 0 hasil palsu.
    """
    # 'hasil' diisi bertahap (dari pembungkus scrape_google_maps) supaya hasil parsial tetap
    # tercatat di Sheet walau scraping terputus di tengah jalan.
    with sync_playwright() as p:
        print("[INFO] Membuka browser (headless/tanpa tampilan)...")
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        try:
            # UA headless bawaan memuat "HeadlessChrome" (tanda bot paling jelas) -> ganti dengan UA Chrome biasa
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  f"Chrome/{browser.version} Safari/537.36")
            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, locale="id-ID", timezone_id="Asia/Jakarta",
                user_agent=ua, extra_http_headers={"Accept-Language": "id-ID,id;q=0.9,en;q=0.8"},
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            try:
                context.add_cookies([{"name": "CONSENT", "value": "YES+", "domain": ".google.com", "path": "/"}])
            except Exception:
                pass
            page = context.new_page()

            url = f"https://www.google.com/maps/search/{keyword.replace(' ', '+')}"
            print(f"[INFO] URL: {url}")
            _goto_dengan_retry(page, url)
            _tangani_consent(page, url)

            panel_selector = 'div[role="feed"]'
            print(f"[INFO] Scroll untuk kumpulkan {jumlah} outlet...")

            # Kadang Google Maps langsung redirect ke halaman detail outlet
            # (bukan ke daftar/feed) kalau hasil pencariannya cuma 1.
            # Cek dulu URL saat ini sebelum nunggu panel feed.
            time.sleep(1.5)
            if "/maps/place/" in page.url:
                print("         → Hasil cuma 1 outlet, langsung ke halaman detail (tidak ada daftar/feed).")
                hrefs = [page.url]
            else:
                panel_ok = False
                for percobaan in range(2):  # coba 2x sebelum menyerah
                    try:
                        page.wait_for_selector(panel_selector, timeout=20000)
                        panel_ok = True
                        break
                    except PWTimeout:
                        if "/maps/place/" in page.url:
                            # redirect terjadi belakangan (lambat)
                            panel_ok = False
                            break
                        if percobaan == 0 and _jenis_halaman(page) in ("SORRY", "NOHASIL"):
                            break   # captcha / memang tidak ada hasil: mengulang tidak ada gunanya
                        print(f"         → Panel belum muncul, coba ulang ({percobaan+1}/2)...")
                        _goto_dengan_retry(page, url)
                        _tangani_consent(page, url)
                        time.sleep(1.5)

                if "/maps/place/" in page.url:
                    print("         → Hasil cuma 1 outlet, langsung ke halaman detail (tidak ada daftar/feed).")
                    hrefs = [page.url]
                elif not panel_ok:
                    jenis = _jenis_halaman(page)
                    print(f"[ERROR] Panel tidak muncul ({jenis}). Keyword ini dilewati.")
                    if strict and jenis == "SORRY":
                        raise MapsBlocked("MapsBlocked: Google meminta verifikasi (captcha / unusual traffic)")
                    if strict and jenis in ("ASING", "CONSENT"):
                        raise MapsBlocked("MapsBlocked: panel hasil tidak muncul dan halaman tidak dikenali "
                                          "(kemungkinan akses dibatasi Google atau tampilan Maps berubah)")
                    return []
                else:
                    # Scroll adaptif — berhenti otomatis jika 5x scroll tidak ada item baru
                    stagnant = 0
                    prev_count = 0
                    hrefs = []
                    for i in range(60):
                        hrefs = page.eval_on_selector_all(
                            'a[href*="/maps/place/"]',
                            "els => els.map(e => e.href)"
                        )
                        hrefs = list(dict.fromkeys(hrefs))
                        print(f"         → {len(hrefs)} item (scroll ke-{i+1})")
                        if len(hrefs) >= jumlah:
                            break
                        if len(hrefs) == prev_count:
                            stagnant += 1
                            if stagnant >= 5:
                                print("         → tidak ada item baru, lanjut scraping...")
                                break
                        else:
                            stagnant = 0
                        prev_count = len(hrefs)
                        page.evaluate(f"document.querySelector('{panel_selector}').scrollBy(0, 3000)")
                        time.sleep(0.8)  # lebih cepat dari sebelumnya (was 1.5s)

            hrefs = hrefs[:jumlah]
            total = len(hrefs)
            print(f"[INFO] Total outlet: {total}\n")

            gagal_net = 0   # error koneksi beruntun saat membuka halaman outlet
            for i, href in enumerate(hrefs, 1):
                try:
                    time.sleep(random.uniform(0.3, 0.9))   # jeda kecil acak antar halaman outlet
                    page.goto(href, wait_until="domcontentloaded")

                    # Tunggu h1 muncul (lebih cepat dari sleep tetap)
                    try:
                        page.wait_for_selector('h1', timeout=6000)
                    except PWTimeout:
                        pass

                    # Tunggu URL punya koordinat (max 4 detik)
                    for _ in range(8):
                        current_url = page.url
                        if re.search(r'/@-?\d+\.\d+,-?\d+\.\d+', current_url):
                            break
                        time.sleep(0.5)

                    current_url = page.url
                    lat, lon    = parse_latlong_dari_url(current_url)
                    detail      = scrape_detail(page)

                    hp, wa, foto_link = "", "", ""
                    if AMBIL_NO_HP:
                        try:   # tunggu panel info (alamat/telepon) tampil
                            page.wait_for_selector('button[data-item-id="address"]', timeout=3000)
                        except PWTimeout:
                            pass
                        hp, wa = ambil_no_hp(page)
                    foto_link = proses_foto(page, detail["Nama"], lat, lon)

                    wilayah = None
                    if GUNAKAN_REVERSE_GEOCODING:
                        wilayah = ambil_wilayah_dari_koordinat(lat, lon)
                        time.sleep(1.05)  # jaga rate limit Nominatim (maks 1 request/detik)
                    if not wilayah:
                        wilayah = parse_wilayah_dari_alamat(detail["Alamat"])

                    print(f"[{i}/{total}] {detail['Nama'][:50]}")
                    print(f"         {wilayah['Kelurahan']} | {wilayah['Kecamatan']} | {wilayah['Kab/Kota']} | {lat}, {lon}")
                    print(f"         HP: {'ada' if hp else '-'} | WA: {'ada' if wa else '-'} | Foto: {'ada' if foto_link else '-'}")   # nomor tidak dicetak (log GitHub publik)

                    hasil.append({
                        "Nama"      : detail["Nama"],
                        "Alamat"    : detail["Alamat"],
                        "Kelurahan" : wilayah["Kelurahan"],
                        "Kecamatan" : wilayah["Kecamatan"],
                        "Kab/Kota"  : wilayah["Kab/Kota"],
                        "Lat"       : lat,
                        "Long"      : lon,
                        "Keyword_Pencarian": keyword,
                        "No_HP"     : hp,
                        "No_WA"     : wa,
                        "Link_WA"   : f"https://wa.me/{wa}" if wa else "",
                        "Foto_Link" : foto_link,
                        "Link_Maps" : current_url,
                    })
                    gagal_net = 0

                except Exception as e:
                    print(f"[{i}/{total}] ✗ Gagal: {e}")
                    gagal_net = gagal_net + 1 if "net::ERR" in str(e) else 0
                    if gagal_net >= 3:     # koneksi ditutup berulang di tengah jalan: berhenti, jangan lanjut menekan Google
                        print("[ERROR] 3 kegagalan koneksi beruntun, scraping dihentikan.")
                        if strict and not hasil:
                            raise MapsBlocked("MapsBlocked: koneksi ke Google Maps ditutup berulang saat membuka halaman outlet "
                                              "(net::ERR_CONNECTION)")
                        break
        finally:
            try:
                browser.close()
            except Exception:
                pass

    return hasil


def scrape_google_maps(keyword: str, jumlah: int, strict: bool = False, meta: dict = None) -> list:
    """
    Pembungkus: menjalankan scraping 1 keyword lalu SELALU mencatat hasilnya (termasuk yang 0 hasil / diblokir)
    ke Sheet penampung bila LOG_URL & LOG_TOKEN terisi.
    meta (opsional): {"by": "nama pemakai", "src": "WEBAPP"} -> kolom DIJALANKAN OLEH / SUMBER.
    Perilaku strict dan nilai kembalian sama seperti sebelumnya.
    """
    kumpul, status, pesan, t0 = [], "OK", "", time.time()
    try:
        return _scrape_inti(keyword, jumlah, strict, kumpul)
    except MapsBlocked as e:
        status, pesan = "BLOCKED", str(e)[:200]
        raise
    except Exception as e:
        status, pesan = "ERROR", str(e)[:200]
        raise
    finally:
        try:
            kirim_log(kumpul, keyword, jumlah, status, pesan, meta, time.time() - t0)
        except Exception as e:
            print(f"[WARN] log gagal: {e}")


def simpan_hasil(data: list, nama_file: str):
    if not data:
        print("\n[WARN] Tidak ada data.")
        return

    kolom = ["Nama","Alamat","Kelurahan","Kecamatan","Kab/Kota","Lat","Long","Keyword_Pencarian",
             "No_HP","No_WA","Link_WA","Foto_Link","Link_Maps"]
    df = pd.DataFrame(data, columns=kolom)

    for col in ["Nama","Alamat","Kelurahan","Kecamatan","Kab/Kota"]:
        df[col] = df[col].apply(lambda x: re.sub(r'\s+', ' ', str(x)).strip())
    for col in ["No_HP","No_WA","Link_WA","Foto_Link","Link_Maps"]:
        df[col] = df[col].fillna("").astype(str)

    # Hapus duplikat kalau outlet yang sama muncul di lebih dari satu keyword
    sebelum = len(df)
    df = df.drop_duplicates(subset=["Nama","Alamat"], keep="first").reset_index(drop=True)
    dihapus = sebelum - len(df)

    file_xlsx = f"{nama_file}.xlsx"
    df.to_excel(file_xlsx, index=False, engine="openpyxl")
    print(f"\n[OK] Excel : {file_xlsx}")
    if dihapus > 0:
        print(f"[INFO] {dihapus} outlet duplikat (muncul di >1 keyword) sudah dihapus.")

    print(f"\n{'─'*70}")
    print(f"  Total: {len(df)} outlet")
    print(f"{'─'*70}")
    pd.set_option('display.max_colwidth', 28)
    print(df[["Nama","Kelurahan","Kecamatan","Kab/Kota","No_HP","Keyword_Pencarian"]].to_string(index=False))


if __name__ == "__main__":
    semua_keyword = [KEYWORD_1, KEYWORD_2, KEYWORD_3, KEYWORD_4, KEYWORD_5]
    keyword_terisi = [k.strip() for k in semua_keyword if k.strip()]

    if not keyword_terisi:
        print("[ERROR] KEYWORD_1 wajib diisi.")
        raise SystemExit(1)

    nama_file_final = NAMA_FILE_OUTPUT_MANUAL.strip() or buat_nama_file_otomatis(keyword_terisi)

    print("=" * 65)
    print("  GOOGLE MAPS OUTLET SCRAPER  v6.2 (multi-keyword + HP + foto + log)")
    print("=" * 65)
    print(f"  Jumlah keyword : {len(keyword_terisi)}")
    for i, k in enumerate(keyword_terisi, 1):
        print(f"    {i}. {k}")
    print(f"  Target/keyword : {JUMLAH_OUTLET_PER_KEYWORD} outlet")
    print(f"  File output    : {nama_file_final}.xlsx")
    print(f"  Log ke Sheet   : {'AKTIF' if _LOG_AKTIF else 'NONAKTIF (LOG_URL / LOG_TOKEN belum diisi)'}")
    print(f"  Foto -> Drive  : {'ya' if (AMBIL_FOTO and _LOG_AKTIF) else 'tidak'}")
    print("=" * 65 + "\n")

    data_gabungan = []
    for idx, kw in enumerate(keyword_terisi, 1):
        print(f"\n{'='*65}")
        print(f"  KEYWORD {idx}/{len(keyword_terisi)} : {kw}")
        print(f"{'='*65}")
        hasil_kw = scrape_google_maps(kw, JUMLAH_OUTLET_PER_KEYWORD)
        data_gabungan.extend(hasil_kw)

    print(f"\n{'='*65}")
    print("  MENGGABUNGKAN & MENYIMPAN HASIL")
    print(f"{'='*65}")
    simpan_hasil(data_gabungan, nama_file_final)
