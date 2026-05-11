import csv
import re
import os
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# Config — sesuaikan path
# ─────────────────────────────────────────────
INPUT_CSV   = '/home/agoy/testing_00_odds/links_copy.csv'
OUTPUT_CSV  = '/home/agoy/testing_00_odds/odds_output.csv'
MAX_WORKERS = 3       # mulai dari 3, naikkan ke 5 kalau RAM cukup
NAV_TIMEOUT = 30_000  # 30 detik per elemen

# ─────────────────────────────────────────────
# State CSV (shared antar thread, thread-safe)
# ─────────────────────────────────────────────
csv_lock        = asyncio.Lock()
known_columns   = [
    "link", "match", "home_score", "away_score",
    "home_avg_scored", "home_avg_conceded",
    "away_avg_scored", "away_avg_conceded",
]
csv_initialized = False

async def append_result_to_csv(output_path: str, result: dict) -> None:
    global csv_initialized, known_columns
    async with csv_lock:
        for key in result.get("odds_dict", {}):
            if key not in known_columns:
                known_columns.append(key)
        file_exists = os.path.exists(output_path)
        if not csv_initialized or not file_exists:
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                csv.DictWriter(f, fieldnames=known_columns, extrasaction='ignore').writeheader()
            csv_initialized = True
        with open(output_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=known_columns, extrasaction='ignore')
            row = {
                "link":       result.get("link", ""),
                "match":      result.get("match", ""),
                "home_score": result.get("home_score", ""),
                "away_score": result.get("away_score", ""),
            }
            row.update(result.get("odds_dict", {}))
            writer.writerow(row)
        print(f"[CSV] Disimpan: {result.get('match', '?')}")

# ─────────────────────────────────────────────
# Baca links dari CSV
# ─────────────────────────────────────────────
def read_links(csv_path: str) -> list:
    links = []
    with open(csv_path, 'r') as f:
        for row in csv.reader(f):
            if row and row[0].strip():
                links.append(row[0].strip())
    return links

# ─────────────────────────────────────────────
# Parse H2H URL → dua URL team
# ─────────────────────────────────────────────
def parse_h2h_to_team_urls(h2h_url: str):
    pattern = r'oddsportal\.com/(\w+)/h2h/(.+?)-([A-Za-z0-9]+)/(.+?)-([A-Za-z0-9]+)/'
    m = re.search(pattern, h2h_url)
    if not m:
        return None, None
    sport = m.group(1)
    base  = "https://www.oddsportal.com"
    return (
        f"{base}/{sport}/team/{m.group(2)}/{m.group(3)}/",
        f"{base}/{sport}/team/{m.group(4)}/{m.group(5)}/",
    )

# ─────────────────────────────────────────────
# Helper: klik dengan retry
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
                print(f"[Thread-{thread_id}] {label} timeout, refresh...")
                await page.reload()
                await asyncio.sleep(5)
            else:
                print(f"[Thread-{thread_id}] {label} tidak ditemukan → skip")
    return False

# ─────────────────────────────────────────────
# Ekstrak skor dari sibling div (via JS)
# ─────────────────────────────────────────────
async def _extract_score(page, testid: str) -> str:
    score = await page.evaluate("""
        (testid) => {
            const el = document.querySelector(`[data-testid="${testid}"]`);
            if (!el || !el.parentElement) return "";
            for (const sib of el.parentElement.children) {
                if (sib === el) continue;
                const txt = sib.innerText?.trim();
                if (txt && /^\\d+$/.test(txt)) return txt;
            }
            return "";
        }
    """, testid)
    return score or ""

# ─────────────────────────────────────────────
# Scraping per link
# ─────────────────────────────────────────────
async def scrape_link(context, link: str, thread_id: int) -> dict:
    page = await context.new_page()

    # Blokir resource tidak perlu
    async def block_resources(route, request):
        if request.resource_type in {"image", "media", "font", "stylesheet"}:
            await route.abort()
        else:
            await route.continue_()
    await page.route("**/*", block_resources)

    def skip(reason):
        print(f"[Thread-{thread_id}] {reason}")
        return {"link": link, "match": reason, "home_score": "", "away_score": "", "odds_dict": {}}

    try:
        print(f"[Thread-{thread_id}] Membuka: {link}")
        await page.goto(link, wait_until='domcontentloaded', timeout=50_000)
        await asyncio.sleep(2)

        # ── Tab CS ──
        if not await click_with_retry(page, "text=CS", "Tab CS", thread_id):
            return skip("SKIP - CS not found")

        # ── Klik 0:0 ──
        try:
            await page.wait_for_selector("p:text-is('0:0')", timeout=NAV_TIMEOUT)
            await page.click("p:text-is('0:0')")
            print(f"[Thread-{thread_id}] 0:0 diklik")
        except PlaywrightTimeout:
            return skip("SKIP - 0:0 not found")

        # ── Tunggu odds expanded ──
        try:
            await page.wait_for_selector("[data-testid='over-under-expanded-row']", timeout=NAV_TIMEOUT)
        except PlaywrightTimeout:
            return skip("SKIP - odds not found")

        await asyncio.sleep(2)

        # ── Nama tim ──
        home_el   = page.locator("[data-testid='game-host'] a p").first
        away_el   = page.locator("[data-testid='game-guest'] a p").first
        home_team = (await home_el.inner_text()).strip() if await home_el.count() else "N/A"
        away_team = (await away_el.inner_text()).strip() if await away_el.count() else "N/A"
        match_name = f"{home_team} vs {away_team}"

        # ── Skor ──
        home_score = await _extract_score(page, "game-host")
        away_score = await _extract_score(page, "game-guest")
        print(f"[Thread-{thread_id}] {match_name} | Skor: {home_score}-{away_score}")

        # ── Odds per bookmaker ──
        odds_dict = {}
        rows  = page.locator("[data-testid='over-under-expanded-row']")
        count = await rows.count()
        for i in range(count):
            row     = rows.nth(i)
            bm_el   = row.locator("p[data-testid='outrights-expanded-bookmaker-name']").first
            odds_el = row.locator("p.odds-text").first
            if await bm_el.count() and await odds_el.count():
                is_striked = await odds_el.evaluate("el => el.classList.contains('line-through')")
                if is_striked:
                    continue
                bm  = (await bm_el.inner_text()).strip()
                odd = (await odds_el.inner_text()).strip()
                odds_dict[bm] = odd
        print(f"[Thread-{thread_id}] Bookmaker: {len(odds_dict)}")

        # ── Team stats: avg scored/conceded ──
        home_url, away_url = parse_h2h_to_team_urls(link)
        home_scored = home_conceded = away_scored = away_conceded = "N/A"

        for url, prefix in [(home_url, "home"), (away_url, "away")]:
            if not url:
                continue
            try:
                print(f"[Thread-{thread_id}] Team {prefix}: {url}")
                await page.goto(url, wait_until='domcontentloaded', timeout=50_000)
                await page.wait_for_selector("span.text-base.font-bold", timeout=15_000)
                await asyncio.sleep(2)
                spans = page.locator("span.text-base.font-bold")
                sc  = (await spans.nth(3).inner_text()).strip() if await spans.count() > 3 else "N/A"
                con = (await spans.nth(4).inner_text()).strip() if await spans.count() > 4 else "N/A"
                if prefix == "home":
                    home_scored, home_conceded = sc, con
                else:
                    away_scored, away_conceded = sc, con
                print(f"[Thread-{thread_id}] {prefix} scored={sc} conceded={con}")
            except PlaywrightTimeout:
                print(f"[Thread-{thread_id}] Team {prefix} timeout → N/A")
            except Exception as e:
                print(f"[Thread-{thread_id}] Team {prefix} error: {e}")

        odds_dict.update({
            "home_avg_scored":   home_scored,
            "home_avg_conceded": home_conceded,
            "away_avg_scored":   away_scored,
            "away_avg_conceded": away_conceded,
        })

        return {
            "link":       link,
            "match":      match_name,
            "home_score": home_score,
            "away_score": away_score,
            "odds_dict":  odds_dict,
        }

    except Exception as e:
        print(f"[Thread-{thread_id}] Error tidak terduga: {e}")
        return {"link": link, "match": "ERROR", "home_score": "", "away_score": "", "odds_dict": {}}
    finally:
        await page.close()

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
async def main():
    if not os.path.exists(INPUT_CSV):
        print(f"ERROR: File input tidak ditemukan: {INPUT_CSV}")
        return

    links = read_links(INPUT_CSV)
    if not links:
        print("ERROR: Tidak ada link di CSV")
        return

    print(f"Total link: {len(links)}")
    print(f"Output: {OUTPUT_CSV}")
    print(f"Workers: {MAX_WORKERS}")
    print("Memulai scraping...")

    all_results = []
    semaphore   = asyncio.Semaphore(MAX_WORKERS)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--blink-settings=imagesEnabled=false',
            ]
        )
        print(f"Browser launched: Chromium headless")

        async def run_one(link: str, thread_id: int):
            async with semaphore:
                try:
                    context = await browser.new_context(
                        java_script_enabled=True,
                        bypass_csp=True,
                        user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                        viewport={"width": 390, "height": 844},
                        device_scale_factor=2,
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
                except Exception as e:
                    print(f"[Thread-{thread_id}] Context error: {e} → skip: {link}")

        tasks = [
            asyncio.create_task(run_one(link, i + 1))
            for i, link in enumerate(links)
        ]

        try:
            await asyncio.gather(*tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nDihentikan — cancel semua task...")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            print(f"\nTotal selesai: {len(all_results)} dari {len(links)} link")
            await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCtrl+C diterima.")
