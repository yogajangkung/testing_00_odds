"""
OddsPortal Link Scraper - Playwright Chromium Headless
Target  : oddsportal.com/matches/football/tomorrow/
Output  : links.csv (match links only)
"""

import asyncio
import csv
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.oddsportal.com/matches/football/tomorrow/"
OUTPUT_PATH = Path("/home/agoy/Documents/Coding/oddsportal_python/links.csv")

PAGE_LOAD_TIMEOUT    = 60_000   # ms
SELECTOR_TIMEOUT     = 30_000   # ms
SCROLL_PAUSE         = 1.2      # detik antar scroll
MAX_SCROLL_ATTEMPTS  = 30       # batas atas scroll loop
# ───────────────────────────────────────────────────────────────────────────────


CSS_SELECTOR = "a.ml-2.min-h-\\[32px\\].w-full.hover\\:cursor-pointer"


async def scroll_to_last_match(page) -> None:
    """
    Scroll ke elemen MATCH_CLASS paling bawah yang sudah ter-render,
    tunggu elemen baru muncul, ulangi sampai jumlah tidak bertambah.
    """
    print("[*] Scrolling ke match terakhir yang terlihat...")
    prev_count = 0
    no_change_count = 0

    for attempt in range(MAX_SCROLL_ATTEMPTS):
        # Ambil semua elemen match yang sudah ada
        elements = await page.query_selector_all(CSS_SELECTOR)
        curr_count = len(elements)

        if curr_count == 0:
            await asyncio.sleep(SCROLL_PAUSE)
            continue

        # Scroll elemen terakhir ke dalam viewport
        last_el = elements[-1]
        await last_el.scroll_into_view_if_needed()
        await asyncio.sleep(SCROLL_PAUSE)

        print(f"    [{attempt + 1}] Match terlihat: {curr_count}")

        if curr_count == prev_count:
            no_change_count += 1
            if no_change_count >= 3:
                print(f"[*] Scroll selesai — total {curr_count} match elements.")
                break
        else:
            no_change_count = 0
            prev_count = curr_count


async def extract_links(page) -> list[str]:
    """Ambil semua href dari link match (pakai class selector asli)."""
    elements = await page.query_selector_all(CSS_SELECTOR)

    seen = set()
    final_links = []

    for el in elements:
        href = await el.get_attribute("href")
        if not href:
            continue
        # Pastikan ini link match (bukan league/overview)
        # Format match: /football/league-name/match-slug/
        parts = [p for p in href.split("/") if p]
        if len(parts) < 3:
            continue
        if href in seen:
            continue
        seen.add(href)
        full = "https://www.oddsportal.com" + href if href.startswith("/") else href
        final_links.append(full)

    return final_links


async def scrape() -> list[str]:
    async with async_playwright() as p:
        # ── Launch Chromium headless ──
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        # ── Sembunyikan webdriver flag (anti-bot) ──
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        # ── Block resource tidak perlu (lebih cepat) ──
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,ico}",
            lambda route: route.abort(),
        )
        await page.route(
            "**/ads/**",
            lambda route: route.abort(),
        )

        # ── Buka halaman ──
        print(f"[*] Membuka: {BASE_URL}")
        try:
            await page.goto(
                BASE_URL,
                timeout=PAGE_LOAD_TIMEOUT,
                wait_until="domcontentloaded",
            )
        except PlaywrightTimeout:
            print("[!] Timeout saat membuka halaman.")
            await browser.close()
            return []
        
        try:
            # Gunakan selector yang fleksibel
            await page.click("button:has-text('I Accept')", timeout=5000)
        except:
            pass # Abaikan jika tidak ada

        # ── Tunggu element match pertama muncul ──
        print("[*] Menunggu konten match...")
        try:
            await page.wait_for_selector(
                "a.ml-2.min-h-\\[32px\\].w-full",
                timeout=SELECTOR_TIMEOUT,
            )
        except PlaywrightTimeout:
            print("[!] Selector tidak ditemukan — struktur halaman mungkin berubah.")
            await browser.close()
            return []

        # ── Scroll ke match terakhir sampai tidak ada yang baru ──
        await scroll_to_last_match(page)

        # ── Ekstrak links ──
        links = await extract_links(page)

        await browser.close()
        return links


def save_csv(links: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for link in links:
            writer.writerow([link])
    print(f"[+] CSV tersimpan: {path}")


async def main():
    print("=" * 52)
    print("  OddsPortal Link Scraper — Playwright Chromium")
    print(f"  URL    : {BASE_URL}")
    print(f"  Output : {OUTPUT_PATH}")
    print("=" * 52)

    links = await scrape()

    if not links:
        print("[!] Tidak ada link ditemukan.")
        return

    print(f"\n[+] Total ditemukan: {len(links)} links")
    for i, l in enumerate(links[:5], 1):
        print(f"    {i}. {l}")
    if len(links) > 5:
        print(f"    ... dan {len(links) - 5} lainnya")

    save_csv(links, OUTPUT_PATH)
    print(f"\n[✓] Done — {len(links)} match links disimpan.")


if __name__ == "__main__":
    asyncio.run(main())