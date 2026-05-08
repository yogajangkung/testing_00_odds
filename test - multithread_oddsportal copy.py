import csv
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
INPUT_CSV  = '/home/agoy/Documents/Coding/oddsportal_python/links.csv'
OUTPUT_CSV = '/home/agoy/Documents/Coding/oddsportal_python/odds_output.csv'
# INPUT_CSV  = 'links.csv'
# OUTPUT_CSV = 'odds_output.csv'
MAX_WORKERS = 10
NAV_TIMEOUT = 30_000   # ms — sama dengan WebDriverWait(driver, 3) di versi Selenium


# ─────────────────────────────────────────────
# Baca links dari CSV
# ─────────────────────────────────────────────
def read_links(csv_path: str) -> list[str]:
    links = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if row and row[0].strip():
                links.append(row[0].strip())
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
# Helper: klik dengan retry refresh
# ─────────────────────────────────────────────
async def click_with_retry(
    page, selector: str, label: str, thread_id: int, max_retry: int = 5
) -> bool:
    for attempt in range(1, max_retry + 1):
        try:
            await page.wait_for_selector(selector, state="visible", timeout=NAV_TIMEOUT)
            await page.click(selector)
            print(f"[Thread-{thread_id}] {label} diklik (percobaan {attempt})")
            return True
        except PlaywrightTimeout:
            if attempt < max_retry:
                print(
                    f"[Thread-{thread_id}] {label} tidak ditemukan "
                    f"(percobaan {attempt}/{max_retry}), refresh..."
                )
                await page.reload()
                await asyncio.sleep(10)
            else:
                print(
                    f"[Thread-{thread_id}] {label} tetap tidak ditemukan "
                    f"setelah {max_retry}x retry → skip link"
                )
    return False


# ─────────────────────────────────────────────
# Scraping per link
# ─────────────────────────────────────────────
async def scrape_link(context, link: str, thread_id: int) -> dict:
    page = await context.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}", lambda r: r.abort())

    def skip(reason: str) -> dict:
        return {"link": link, "match": reason, "home_score": "", "away_score": "", "odds_dict": {}}

    try:
        print(f"[Thread-{thread_id}] Membuka: {link}")
        await page.goto(link, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        # ── More button ──
        if not await click_with_retry(page, "[data-testid='more-button']", "More button", thread_id):
            return skip("SKIP - more_btn not found")

        # ── Tab Correct Score ──
        if not await click_with_retry(page, "text=Correct Score", "Tab Correct Score", thread_id):
            return skip("SKIP - tab_7 not found")

        # ── Klik 0:0 ──
        try:
            await page.wait_for_selector("p:text-is('0:0')", timeout=NAV_TIMEOUT)
            await page.click("p:text-is('0:0')")
            print(f"[Thread-{thread_id}] 0:0 diklik")
        except PlaywrightTimeout:
            print(f"[Thread-{thread_id}] 0:0 tidak ditemukan → skip link")
            return skip("SKIP - 0:0 not found")

        # ── Tunggu odds expanded ──
        try:
            await page.wait_for_selector(
                "[data-testid='over-under-expanded-row']", timeout=NAV_TIMEOUT
            )
        except PlaywrightTimeout:
            print(f"[Thread-{thread_id}] Odds tidak muncul → skip link")
            return skip("SKIP - odds not found")

        await asyncio.sleep(3)

        # ── Nama tim ──
        home_el = page.locator("[data-testid='game-host'] p").first
        away_el = page.locator("[data-testid='game-guest'] p").first
        home_team = (await home_el.inner_text()).strip() if await home_el.count() else "N/A"
        away_team = (await away_el.inner_text()).strip() if await away_el.count() else "N/A"
        match_name = f"{home_team} vs {away_team}"

        # ── Skor: cari sibling div yang isinya digit ──
        # Parent dari game-host/guest biasanya div score wrapper
        home_score = await _extract_score(page, "game-host")
        away_score = await _extract_score(page, "game-guest")

        print(f"[Thread-{thread_id}] Match: {match_name}")
        print(f"[Thread-{thread_id}] Skor: {home_score}-{away_score}")

        # ── Odds per bookmaker ──
        odds_dict = {}
        rows  = page.locator("[data-testid='over-under-expanded-row']")
        count = await rows.count()

        for i in range(count):
            row      = rows.nth(i)
            bm_el    = row.locator("[data-testid='outrights-expanded-bookmaker-name']").first
            odds_el  = row.locator(".odds-text, .odds-link").first   # sesuai versi Selenium: ["a","p"] + class

            if await bm_el.count() and await odds_el.count():
                bm  = (await bm_el.inner_text()).strip()
                odd = (await odds_el.inner_text()).strip()
                odds_dict[bm] = odd
                print(f"[Thread-{thread_id}] {bm}: {odd}")

        print(f"[Thread-{thread_id}] Total bookmaker: {len(odds_dict)}")

        return {
            "link": link,
            "match": match_name,
            "home_score": home_score,
            "away_score": away_score,
            "odds_dict": odds_dict,
        }

    except Exception as e:
        print(f"[Thread-{thread_id}] Error tidak terduga: {e}")
        return {"link": link, "match": "ERROR", "home_score": "", "away_score": "", "odds_dict": {}}
    finally:
        await page.close()


# ─────────────────────────────────────────────
# Ekstrak skor dari sibling div parent game-host/guest
# Replicates extract_score_from_row() di versi Selenium
# ─────────────────────────────────────────────
async def _extract_score(page, testid: str) -> str:
    """
    Ambil semua teks dari sibling div yang satu parent
    dengan [data-testid=testid], lalu cari yang isdigit().
    """
    # evaluate JS: cari parent → loop children → kumpulkan teks sibling
    score = await page.evaluate("""
        (testid) => {
            const el = document.querySelector(`[data-testid="${testid}"]`);
            if (!el || !el.parentElement) return "";
            const siblings = Array.from(el.parentElement.children);
            for (const sib of siblings) {
                if (sib === el) continue;
                const txt = sib.innerText?.trim();
                if (txt && /^\\d+$/.test(txt)) return txt;
            }
            return "";
        }
    """, testid)
    return score or ""


# ─────────────────────────────────────────────
# Tulis semua hasil ke CSV
# ─────────────────────────────────────────────
def write_all_to_csv(output_path: str, results: list[dict]) -> None:
    all_bookmakers, seen = [], set()
    for r in results:
        for bm in r.get("odds_dict", {}):
            if bm not in seen:
                all_bookmakers.append(bm)
                seen.add(bm)

    header = ["link", "match", "home_score", "away_score"] + all_bookmakers

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = {
                "link":       r.get("link", ""),
                "match":      r.get("match", ""),
                "home_score": r.get("home_score", ""),
                "away_score": r.get("away_score", ""),
            }
            for bm in all_bookmakers:
                row[bm] = r.get("odds_dict", {}).get(bm, "")
            writer.writerow(row)

    print(f"\n✓ CSV disimpan: {output_path}")
    print(f"  {len(results)} match | {len(all_bookmakers)} bookmaker unik")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
async def main():
    links = read_links(INPUT_CSV)
    print(f"Total link: {len(links)}")

    all_results = []
    semaphore   = asyncio.Semaphore(MAX_WORKERS)

    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)

        async def run_one(link: str, thread_id: int):
            async with semaphore:
                context = await browser.new_context(java_script_enabled=True)
                try:
                    result = await scrape_link(context, link, thread_id)
                    all_results.append(result)
                    print(f"[Selesai] Thread-{thread_id}: {result['match']}")
                finally:
                    await context.close()

        tasks = [
            asyncio.create_task(run_one(link, i + 1))
            for i, link in enumerate(links)
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            print("\nDihentikan manual...")
        finally:
            await browser.close()
            write_all_to_csv(OUTPUT_CSV, all_results)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCtrl+C diterima.")