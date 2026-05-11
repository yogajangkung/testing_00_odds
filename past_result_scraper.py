import csv
import re
import os
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
INPUT_CSV  = 'links copy.csv'
OUTPUT_CSV = 'odds_output.csv'
MAX_WORKERS = 5        # jumlah tab paralel
NAV_TIMEOUT = 30_000   # ms — timeout untuk wait/click

# ─────────────────────────────────────────────
# Tulis satu hasil ke CSV (append mode)
# ─────────────────────────────────────────────
def write_row_to_csv(output_path: str, result: dict, all_fieldnames: list) -> None:
    file_exists = os.path.exists(output_path)
    with open(output_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=all_fieldnames, extrasaction='ignore')
        if not file_exists:
            writer.writeheader()
        row = {"link": result["link"], "match": result["match"]}
        for bm in all_fieldnames:
            row[bm] = result["odds_dict"].get(bm, "")
        writer.writerow(row)

# ─────────────────────────────────────────────
# State CSV (shared antar thread)
# ─────────────────────────────────────────────
csv_lock       = asyncio.Lock()
known_columns  = ["link", "match", "home_avg_scored", "home_avg_conceded",
                  "away_avg_scored", "away_avg_conceded"]
csv_initialized = False

async def append_result_to_csv(output_path: str, result: dict) -> None:
    global csv_initialized, known_columns

    async with csv_lock:
        # Tambah kolom bookmaker baru yang belum dikenal
        for key in result["odds_dict"]:
            if key not in known_columns:
                known_columns.append(key)

        file_exists = os.path.exists(output_path)

        if not csv_initialized or not file_exists:
            # Tulis ulang header (file baru)
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=known_columns, extrasaction='ignore')
                writer.writeheader()
            csv_initialized = True

        with open(output_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=known_columns, extrasaction='ignore')
            row = {"link": result["link"], "match": result["match"]}
            row.update(result["odds_dict"])
            writer.writerow(row)

        print(f"[CSV] Disimpan: {result['match']}")

# ─────────────────────────────────────────────
# Baca links dari CSV
# ─────────────────────────────────────────────
def read_links(csv_path: str) -> list[str]:
    links = []
    with open(csv_path, 'r') as f:
        for row in csv.reader(f):
            if row:
                links.append(row[0])
    return links

# ─────────────────────────────────────────────
# Helper: ambil skor (angka digit) dari sibling
# div di samping container game-host / game-guest
# ─────────────────────────────────────────────
def extract_score_from_siblings(siblings_texts: list[str]) -> str:
    """
    siblings_texts = list teks dari semua sibling div parent game-host/guest.
    Cari teks yang isdigit() — itu skornya.
    """
    for txt in siblings_texts:
        if txt.isdigit():
            return txt
    return ""

# ─────────────────────────────────────────────
# Parse link head2head team
# ─────────────────────────────────────────────

def parse_h2h_to_team_urls(h2h_url: str) -> tuple[str, str]:
    pattern = r'oddsportal\.com/(\w+)/h2h/(.+?)-([A-Za-z0-9]+)/(.+?)-([A-Za-z0-9]+)/'
    match = re.search(pattern, h2h_url)
    if not match:
        return None, None
    sport = match.group(1)
    team1, code1 = match.group(2), match.group(3)
    team2, code2 = match.group(4), match.group(5)
    base = "https://www.oddsportal.com"
    return f"{base}/{sport}/team/{team1}/{code1}/", f"{base}/{sport}/team/{team2}/{code2}/"

# ─────────────────────────────────────────────
# Helper: klik dengan retry refresh
# ─────────────────────────────────────────────
async def click_with_retry(page, selector: str, label: str, thread_id: int, max_retry: int = 2) -> bool:
    for attempt in range(1, max_retry + 1):
        try:
            await page.wait_for_selector(selector, state='visible', timeout=NAV_TIMEOUT)
            await page.click(selector)
            print(f"[Thread-{thread_id}] {label} diklik (percobaan {attempt})")
            return True
        except PlaywrightTimeout:
            if attempt < max_retry:
                print(f"[Thread-{thread_id}] {label} tidak ditemukan "
                      f"(percobaan {attempt}/{max_retry}), refresh...")
                await page.reload()
                await asyncio.sleep(10)
            else:
                print(f"[Thread-{thread_id}] {label} tetap tidak ditemukan "
                      f"setelah {max_retry}x retry → skip link")
    return False

# ─────────────────────────────────────────────
# Scraping per link (jalan di satu Page/tab)
# ─────────────────────────────────────────────
async def scrape_link(context, link: str, thread_id: int) -> dict:
    page = await context.new_page()

    try:
        print(f"[Thread-{thread_id}] Membuka: {link}")
        await page.goto(link, wait_until='domcontentloaded', timeout=50_000)
        await asyncio.sleep(3)

        # # ── Klik more-button ──
        # if not await click_with_retry(page, "[data-testid='more-button']", "More button", thread_id):
        #     return {"link": link, "match": "SKIP - more_btn not found", "odds_dict": {}}

        # ── Klik tab Correct Score ──
        if not await click_with_retry(page, "text=CS", "Correct Score button", thread_id):
            return {"link": link, "match": "SKIP - Correct Score not found", "odds_dict": {}}

        # ── Klik skor 0:0 ──
        try:
            await page.wait_for_selector("p:text-is('0:0')", timeout=NAV_TIMEOUT)
            await page.click("p:text-is('0:0')")
            print(f"[Thread-{thread_id}] 0:0 diklik")
        except PlaywrightTimeout:
            print(f"[Thread-{thread_id}] 0:0 tidak ditemukan → skip link")
            return {"link": link, "match": "SKIP - 0:0 not found", "odds_dict": {}}

        # ── Tunggu odds expanded muncul ──
        try:
            await page.wait_for_selector(
                "[data-testid='over-under-expanded-row']", timeout=NAV_TIMEOUT
            )
        except PlaywrightTimeout:
            print(f"[Thread-{thread_id}] Odds tidak muncul → skip link")
            return {"link": link, "match": "SKIP - odds not found", "odds_dict": {}}

        await asyncio.sleep(3)

        # ── Ambil nama tim ──
        home_el = page.locator("[data-testid='game-host'] a p").first
        away_el = page.locator("[data-testid='game-guest'] a p").first
        home_team  = (await home_el.inner_text()).strip() if await home_el.count() else "N/A"
        away_team  = (await away_el.inner_text()).strip() if await away_el.count() else "N/A"
        match_name = f"{home_team} vs {away_team}"

        # ── Ambil score ──
        host_block  = page.locator("[data-testid='game-host']")
        guest_block = page.locator("[data-testid='game-guest']")

        # Score home = sibling div setelah game-host
        home_score_el = page.locator("[data-testid='game-host'] + div")
        # Score away = div pertama dalam parent yang sama, sebelum game-guest
        away_score_el = guest_block.locator("xpath=preceding-sibling::div[1]")

        home_score = (await home_score_el.inner_text()).strip() if await home_score_el.count() else "N/A"
        away_score = (await away_score_el.inner_text()).strip() if await away_score_el.count() else "N/A"

        score_dict = {
            "home_score_result": home_score,
            "away_score_result": away_score
        }
        # ── Scraping odds per bookmaker ──
        odds_dict = {}
        rows = page.locator("[data-testid='over-under-expanded-row']")
        count = await rows.count()

        for i in range(count):
            row = rows.nth(i)
            bm_el   = row.locator("p[data-testid='outrights-expanded-bookmaker-name']").first
            odds_el = row.locator("p.odds-text").first

            bm_count   = await bm_el.count()
            odds_count = await odds_el.count()

            if bm_count and odds_count:
                bm  = (await bm_el.inner_text()).strip()
                odd = (await odds_el.inner_text()).strip()
                
                # Skip odds yang di-strikethrough (odds tidak aktif)
                is_striked = await odds_el.evaluate("el => el.classList.contains('line-through')")
                if is_striked:
                    continue
                    
                odds_dict[bm] = odd
                
                print(f"[Thread-{thread_id}]   {bm}: {odd}")
        
        link_h2h = parse_h2h_to_team_urls(link)
        # print(link_h2h)

        # ── Home (link pertama) ──
        print(f"[Thread-{thread_id}] Masuk ke link team home: {link_h2h[0]}")
        await page.goto(link_h2h[0], wait_until='domcontentloaded', timeout=50_000)
        try:
            await page.wait_for_selector("span.text-base.font-bold", timeout=15_000)
            await asyncio.sleep(2)  # buffer render JS
            all_spans = page.locator("span.text-base.font-bold")
            home_scored_text   = (await all_spans.nth(3).inner_text()).strip() if await all_spans.count() > 3 else "N/A"
            home_conceded_text = (await all_spans.nth(4).inner_text()).strip() if await all_spans.count() > 4 else "N/A"
        except PlaywrightTimeout:
            print(f"[Thread-{thread_id}] Home team page timeout")
            home_scored_text = home_conceded_text = "N/A"
        print(f"[Thread-{thread_id}] Home - Scored: {home_scored_text}, Conceded: {home_conceded_text}")

        # ── Away (link kedua) ──
        print(f"[Thread-{thread_id}] Masuk ke link team away: {link_h2h[1]}")
        await page.goto(link_h2h[1], wait_until='domcontentloaded', timeout=50_000)
        try:
            await page.wait_for_selector("span.text-base.font-bold", timeout=15_000)
            await asyncio.sleep(2)
            all_spans = page.locator("span.text-base.font-bold")
            away_scored_text   = (await all_spans.nth(3).inner_text()).strip() if await all_spans.count() > 3 else "N/A"
            away_conceded_text = (await all_spans.nth(4).inner_text()).strip() if await all_spans.count() > 4 else "N/A"
        except PlaywrightTimeout:
            print(f"[Thread-{thread_id}] Away team page timeout")
            away_scored_text = away_conceded_text = "N/A"
        print(f"[Thread-{thread_id}] Away - Scored: {away_scored_text}, Conceded: {away_conceded_text}")

        # ── Masukkan ke dict ──
        team_stats = {
            "home_avg_scored":    home_scored_text,
            "home_avg_conceded":  home_conceded_text,
            "away_avg_scored":    away_scored_text,
            "away_avg_conceded":  away_conceded_text,
        }
        odds_dict.update(team_stats)
        odds_dict.update(score_dict)

        print(f"[Thread-{thread_id}] Total bookmaker: {len(odds_dict)}")
        return {"link": link, "match": match_name, "odds_dict": odds_dict}

    except Exception as e:
        print(f"[Thread-{thread_id}] Error tidak terduga: {e}")
        return {"link": link, "match": "ERROR", "odds_dict": {}}
    finally:
        await page.close()


# ─────────────────────────────────────────────
# Tulis semua hasil ke CSV
# ─────────────────────────────────────────────
def write_all_to_csv(output_path: str, results: list[dict]) -> None:
    all_bookmakers, seen = [], set()
    for r in results:
        for bm in r["odds_dict"]:
            if bm not in seen:
                all_bookmakers.append(bm)
                seen.add(bm)

    header = ["link", "match"] + all_bookmakers

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction='ignore')
        writer.writeheader()
        for r in results:
            row = {"link": r["link"], "match": r["match"]}
            for bm in all_bookmakers:
                row[bm] = r["odds_dict"].get(bm, "")
            writer.writerow(row)

    print(f"\n✓ CSV disimpan: {output_path}")
    print(f"  {len(results)} match | {len(all_bookmakers)} bookmaker unik")


# ─────────────────────────────────────────────
# Main async
# Semua tab share 1 browser, tapi pakai Context terpisah
# agar cookie/session tidak bocor antar link
# ─────────────────────────────────────────────
async def main():
    links = read_links(INPUT_CSV)
    print(f"Total link: {len(links)}")
    input("Start? ")

    all_results = []
    semaphore   = asyncio.Semaphore(MAX_WORKERS)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            channel="chrome",
            proxy={
            "server": "http://gw.dataimpulse.com:823",
            "username": "f600d4384f0bafd3cca6__cr.sg",
            "password": "ae076c98bd358226"},
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--blink-settings=imagesEnabled=false',
            ]
        )

        async def run_one(link: str, thread_id: int):
            async with semaphore:
                context = await browser.new_context(
                    java_script_enabled=True,
                    bypass_csp=True,
                    user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                    viewport={"width": 390, "height": 844},
                    device_scale_factor=3,
                    is_mobile=True,
                    has_touch=True,
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
                )
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                """)
                try:
                    result = await scrape_link(context, link, thread_id)
                    all_results.append(result)
                    await append_result_to_csv(OUTPUT_CSV, result)
                    print(f"[Selesai] Thread-{thread_id}: {result['match']}")
                finally:
                    await context.close()

        tasks = [
            asyncio.create_task(run_one(link, i + 1))
            for i, link in enumerate(links)
        ]

        try:
            await asyncio.gather(*tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nDihentikan — membatalkan task yang masih berjalan...")
            for task in tasks:
                task.cancel()
            # tunggu semua task selesai dibatalkan
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            print(f"\nMenyimpan {len(all_results)} hasil yang sudah selesai...")
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCtrl+C diterima.")
