"""
=============================================================
  SCRAPER DATA OUTLET DARI GOOGLE MAPS  (v6 - multi-keyword)
  - headless mode (tanpa tampilan browser) = lebih cepat
  - wait for selector, bukan sleep tetap
  - scroll adaptif (berhenti otomatis jika tidak ada item baru)
  - bisa isi 1-5 keyword sekaligus, hasil digabung 1 file Excel
  - nama file output otomatis mengikuti keyword yang diisi
=============================================================
  py scraper_outlet.py
=============================================================
"""

import time
import re
import json
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


def scrape_google_maps(keyword: str, jumlah: int) -> list:
    hasil = []

    with sync_playwright() as p:
        print("[INFO] Membuka browser (headless/tanpa tampilan)...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="id-ID")
        page    = context.new_page()

        url = f"https://www.google.com/maps/search/{keyword.replace(' ', '+')}"
        print(f"[INFO] URL: {url}")
        page.goto(url, wait_until="domcontentloaded")

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
                    print(f"         → Panel belum muncul, coba ulang ({percobaan+1}/2)...")
                    page.goto(url, wait_until="domcontentloaded")
                    time.sleep(1.5)

            if "/maps/place/" in page.url:
                print("         → Hasil cuma 1 outlet, langsung ke halaman detail (tidak ada daftar/feed).")
                hrefs = [page.url]
            elif not panel_ok:
                print("[ERROR] Panel tidak muncul setelah 2x percobaan. Keyword ini dilewati.")
                browser.close()
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

        for i, href in enumerate(hrefs, 1):
            try:
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

                wilayah = None
                if GUNAKAN_REVERSE_GEOCODING:
                    wilayah = ambil_wilayah_dari_koordinat(lat, lon)
                    time.sleep(1.05)  # jaga rate limit Nominatim (maks 1 request/detik)
                if not wilayah:
                    wilayah = parse_wilayah_dari_alamat(detail["Alamat"])

                print(f"[{i}/{total}] {detail['Nama'][:50]}")
                print(f"         {wilayah['Kelurahan']} | {wilayah['Kecamatan']} | {wilayah['Kab/Kota']} | {lat}, {lon}")

                hasil.append({
                    "Nama"      : detail["Nama"],
                    "Alamat"    : detail["Alamat"],
                    "Kelurahan" : wilayah["Kelurahan"],
                    "Kecamatan" : wilayah["Kecamatan"],
                    "Kab/Kota"  : wilayah["Kab/Kota"],
                    "Lat"       : lat,
                    "Long"      : lon,
                    "Keyword_Pencarian": keyword,
                })

            except Exception as e:
                print(f"[{i}/{total}] ✗ Gagal: {e}")

        browser.close()

    return hasil


def simpan_hasil(data: list, nama_file: str):
    if not data:
        print("\n[WARN] Tidak ada data.")
        return

    kolom = ["Nama","Alamat","Kelurahan","Kecamatan","Kab/Kota","Lat","Long","Keyword_Pencarian"]
    df = pd.DataFrame(data, columns=kolom)

    for col in ["Nama","Alamat","Kelurahan","Kecamatan","Kab/Kota"]:
        df[col] = df[col].apply(lambda x: re.sub(r'\s+', ' ', str(x)).strip())

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
    print(df[["Nama","Kelurahan","Kecamatan","Kab/Kota","Lat","Long","Keyword_Pencarian"]].to_string(index=False))


if __name__ == "__main__":
    semua_keyword = [KEYWORD_1, KEYWORD_2, KEYWORD_3, KEYWORD_4, KEYWORD_5]
    keyword_terisi = [k.strip() for k in semua_keyword if k.strip()]

    if not keyword_terisi:
        print("[ERROR] KEYWORD_1 wajib diisi.")
        raise SystemExit(1)

    nama_file_final = NAMA_FILE_OUTPUT_MANUAL.strip() or buat_nama_file_otomatis(keyword_terisi)

    print("=" * 65)
    print("  GOOGLE MAPS OUTLET SCRAPER  v6 (multi-keyword)")
    print("=" * 65)
    print(f"  Jumlah keyword : {len(keyword_terisi)}")
    for i, k in enumerate(keyword_terisi, 1):
        print(f"    {i}. {k}")
    print(f"  Target/keyword : {JUMLAH_OUTLET_PER_KEYWORD} outlet")
    print(f"  File output    : {nama_file_final}.xlsx")
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