"""
Моніторинг сторінки "Пошук проєктів документів Дніпровської міської ради"
та розсилка нових проєктів у Telegram.

Джерело даних: внутрішній ендпоінт сайту, який використовує сама форма пошуку
на сторінці https://dniprorada.gov.ua/uk/page/poshuk-proektiv-dokumentiv-dniprovskoi-miskoi-radi

    POST https://dniprorada.gov.ua/uk/Widgets/GetCouncilProjectDocuments
    Content-Type: application/x-www-form-urlencoded
    Body: DateRange=dd.MM.yyyy - dd.MM.yyyy&DocHeader=&DocTypeCode=0

Відповідь — фрагмент HTML з таблицею результатів (без пагінації в межах
розумного діапазону дат), що значно легше й дешевше за рендеринг сторінки
в браузері.

Стан (які проєкти вже надіслані) зберігається в JSON-файлі seen_projects.json,
який GitHub Actions коміт(ить) назад у репозиторій після кожного запуску.
"""

import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

SEARCH_URL = "https://dniprorada.gov.ua/uk/Widgets/GetCouncilProjectDocuments"
PAGE_URL = "https://dniprorada.gov.ua/uk/page/poshuk-proektiv-dokumentiv-dniprovskoi-miskoi-radi"
STATE_FILE = os.environ.get("STATE_FILE", "seen_projects.json")

# Скільки днів "назад" від сьогодні захоплювати кожного разу.
# Це буфер на випадок, якщо сайт публікує проєкт із запізненням
# або запуск бота якийсь час не відбувався.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "4"))

# 0 = всі види проєктів; 2 = виконком; 3 = міськрада; 4 = розпорядження голови
DOC_TYPE_CODE = os.environ.get("DOC_TYPE_CODE", "0")

# Токен і chat_id можна або задати тут напряму (простіше), або передати
# через змінні середовища TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (безпечніше,
# якщо репозиторій публічний) — значення з env мають пріоритет.
TELEGRAM_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8562718356:AAFCqZv9o3A8p_QlCRTrQiPZ1Bo3WQRPN9U"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "@dnipro_proekty")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://dniprorada.gov.ua",
    "Referer": PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html, */*; q=0.01",
}


def fetch_html(date_range: str) -> str:
    data = {
        "DateRange": date_range,
        "DocHeader": "",
        "DocTypeCode": DOC_TYPE_CODE,
    }
    resp = requests.post(SEARCH_URL, headers=HEADERS, data=data, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_projects(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    rows = table.find_all("tr")
    projects = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue  # це рядок заголовка або службовий рядок

        published_date = cells[1].get_text(strip=True)
        doc_type = cells[2].get_text(strip=True)
        title = cells[3].get_text(strip=True)

        if not title or not published_date:
            continue

        key = hashlib.sha256(
            f"{published_date}|{doc_type}|{title}".encode("utf-8")
        ).hexdigest()

        try:
            sort_date = datetime.strptime(published_date, "%d.%m.%Y")
        except ValueError:
            sort_date = datetime.min

        projects.append(
            {
                "key": key,
                "date": published_date,
                "sort_date": sort_date.isoformat(),
                "type": doc_type,
                "title": title,
            }
        )
    return projects


def load_seen(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        try:
            return set(json.load(f))
        except json.JSONDecodeError:
            return set()


def save_seen(path: str, seen_keys: set) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_keys), f, ensure_ascii=False, indent=2)


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані у змінних середовища."
        )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    resp.raise_for_status()


def main() -> int:
    today = date.today()
    start = today - timedelta(days=LOOKBACK_DAYS)
    date_range = f"{start.strftime('%d.%m.%Y')} - {today.strftime('%d.%m.%Y')}"

    print(f"Перевіряю проєкти за період: {date_range}")

    html = fetch_html(date_range)
    projects = parse_projects(html)
    print(f"Знайдено {len(projects)} проєктів у відповіді сайту.")

    seen = load_seen(STATE_FILE)
    is_first_run = len(seen) == 0

    new_items = [p for p in projects if p["key"] not in seen]

    if is_first_run:
        print(
            f"Перший запуск: позначаю {len(projects)} існуючих проєктів як "
            "\"вже відомі\" без відправки повідомлень (щоб не заспамити канал)."
        )
    elif new_items:
        new_items.sort(key=lambda p: p["sort_date"])
        for p in new_items:
            text = (
                f"🆕 <b>{p['title']}</b>\n"
                f"Дата публікації: {p['date']}\n"
                f"Вид: {p['type']}"
            )
            send_telegram_message(text)
            print(f"Надіслано: {p['date']} — {p['title'][:70]}")
    else:
        print("Нових проєктів немає.")

    seen.update(p["key"] for p in projects)
    save_seen(STATE_FILE, seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
