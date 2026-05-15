import csv
import re
import os
import asyncio
import math
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
INPUT_CSV  = '/home/agoy/Documents/Coding/oddsportal_python/links.csv'
OUTPUT_CSV = '/home/agoy/Documents/Coding/oddsportal_python/odds_output.csv'
MAX_WORKERS = 20        # jumlah tab paralel
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

def poisson_prob(lam, k):
    return (math.exp(-lam) * lam**k) / math.factorial(k)

def calc_over_prob(lam, line):
    """line = 1, 2, atau 3 (untuk over 1.5, 2.5, 3.5)"""
    return 1 - sum(poisson_prob(lam, k) for k in range(line + 1))

def calc_ev(prob, decimal_odds):
    try:
        odds = float(decimal_odds)
        return round((prob * odds) - 1, 4)
    except (ValueError, TypeError):
        return "N/A"

# ─────────────────────────────────────────────
# Scraping per link (jalan di satu Page/tab)
# ─────────────────────────────────────────────
async def scrape_link(context, link: str, thread_id: int) -> dict:
    page = await context.new_page()
    odds_dict = {}
    try:
        print(f"[Thread-{thread_id}] Membuka: {link}")
        await page.goto(link, wait_until='domcontentloaded', timeout=50_000)
        await asyncio.sleep(3)

        # ── Klik tab Over/Under ──
        if not await click_with_retry(page, "text=Over/Under", "Over/Under button", thread_id):
            return {"link": link, "match": "SKIP - Over/Under not found", "odds_dict": {}}

        rows_over_under = page.locator('[data-testid="over-under-collapsed-row"]')
        await rows_over_under.first.wait_for()
        count_rows_over_under = await rows_over_under.count()

        for i in range(count_rows_over_under):
            row = rows_over_under.nth(i)

            # ambil text dari:
            # <p class="breadcrumbs-m:!hidden">O/U +1.5</p>
            line_text = await row.locator(
                'p.breadcrumbs-m\\:\\!hidden'
            ).inner_text()

            line_text = line_text.strip()

            # filter line yang diinginkan
            line_map = {
                "O/U +1.5": "odds_over_15",
                "O/U +2.5": "odds_over_25",
                "O/U +3.5": "odds_over_35",
            }

            if line_text in line_map:
                odds_loc = row.locator('p[data-testid="odd-container-default"]')
                over_odd = await odds_loc.nth(0).inner_text()
                var_name = line_map[line_text]
                odds_dict[var_name] = over_odd

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
        print(f"[Thread-{thread_id}] Match: {match_name}")


        # ── Scraping odds per bookmaker ──
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
        
        # ── Kalkulasi spread & min odds dari bookmaker murni ──
        NON_BM_KEYS = {
            "odds_over_15", "odds_over_25", "odds_over_35",
            "home_avg_scored", "home_avg_conceded",
            "away_avg_scored", "away_avg_conceded",
            "lambda_goals", "average_goals",
            "prob_over_15", "prob_over_25", "prob_over_35",
            "ev_over_15", "ev_over_25", "ev_over_35",
        }
        bm_odds_values = []
        for key, val in odds_dict.items():
            if key in NON_BM_KEYS:
                continue
            try:
                bm_odds_values.append(float(val))
            except (ValueError, TypeError):
                continue

        if bm_odds_values:
            odds_max    = max(bm_odds_values)
            odds_min    = min(bm_odds_values)
            odds_spread = round(odds_max - odds_min, 4)
        else:
            odds_max    = "N/A"
            odds_min    = "N/A"
            odds_spread = "N/A"

        odds_dict["spread"]   = odds_spread
        odds_dict["min_odds"] = odds_min

        link_h2h = parse_h2h_to_team_urls(link)

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

        try:
            hs = float(home_scored_text)
            hc = float(home_conceded_text)
            as_ = float(away_scored_text)
            ac = float(away_conceded_text)

            lambda_goals = ((hs + ac) / 2) + ((as_ + hc) / 2)
            avg_goals = (hs + hc + as_ + ac) / 4

            prob_over_15 = calc_over_prob(lambda_goals, 1)
            prob_over_25 = calc_over_prob(lambda_goals, 2)
            prob_over_35 = calc_over_prob(lambda_goals, 3)

            ev_15 = calc_ev(prob_over_15, odds_dict.get("odds_over_15", "N/A"))
            ev_25 = calc_ev(prob_over_25, odds_dict.get("odds_over_25", "N/A"))
            ev_35 = calc_ev(prob_over_35, odds_dict.get("odds_over_35", "N/A"))

        except (ValueError, TypeError):
            lambda_goals = prob_over_15 = prob_over_25 = prob_over_35 = "N/A"
            ev_15 = ev_25 = ev_35 = "N/A"

        team_stats = {
            "home_avg_scored":   home_scored_text,
            "home_avg_conceded": home_conceded_text,
            "away_avg_scored":   away_scored_text,
            "away_avg_conceded": away_conceded_text,
            "lambda_goals":      lambda_goals,
            "average_goals":     avg_goals,
            "prob_over_15":      round(prob_over_15, 4) if prob_over_15 != "N/A" else "N/A",
            "prob_over_25":      round(prob_over_25, 4) if prob_over_25 != "N/A" else "N/A",
            "prob_over_35":      round(prob_over_35, 4) if prob_over_35 != "N/A" else "N/A",
            "ev_over_15":        ev_15,
            "ev_over_25":        ev_25,
            "ev_over_35":        ev_35,
        }
        odds_dict.update(team_stats)

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