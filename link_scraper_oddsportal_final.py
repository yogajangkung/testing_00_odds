from selenium import webdriver
from bs4 import BeautifulSoup as soup
import time
import csv
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

options = Options()
# Mode 2: Increased Protection (use DoH but fall back to system DNS)
# Mode 3: Max Protection (DoH only)
options.set_preference("network.trr.mode", 2) 
options.set_preference("network.trr.uri", "https://mozilla.cloudflare-dns.com/dns-query")

base_url = "https://www.oddsportal.com/matches/football/tomorrow/"
# base_url = "https://www.oddsportal.com/matches/football/20260514/"

driver = webdriver.Firefox(options=options)
driver.maximize_window()

driver.get(base_url)

start = input("Start? ")

page_html = driver.page_source
page_soup = soup(page_html, "html.parser")

group_flex = page_soup.findAll("a",{'class':'ml-2 min-h-[32px] w-full hover:cursor-pointer next-m:!mt-0 next-m:flex next-m:items-stretch'})

link_counter = 0

with open('/home/agoy/Documents/Coding/oddsportal_python/links.csv', 'w', newline='') as file:
    writer = csv.writer(file)
    for group in group_flex:
        link_counter += 1
        links = group['href']
        final_link = 'https://oddsportal.com' + links
        writer.writerow([final_link])  # list, bukan string
        # print(final_link)

print(f"Found {link_counter} links")
driver.quit()