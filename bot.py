import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SOURCE = "https://siirt.adalet.gov.tr/Arsiv/duyuru"
TURKEY = timezone(timedelta(hours=3))


class ArchiveParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.item = None
        self.field = None
        self.items = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if "arsivlist" in attrs.get("class", "").split():
                self.depth = 1
            elif self.depth:
                self.depth += 1
                if "baslik" in attrs.get("class", "").split():
                    self.field = "title"
        if self.depth and tag == "a":
            href = attrs.get("href", "")
            # The archive also contains malformed ///rayp... PDF links.
            if href.startswith("///"):
                href = "https://" + href.lstrip("/")
            parts = urlsplit(urljoin(SOURCE, href))
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            if parts.scheme in ("http", "https") and parts.hostname in (
                "siirt.adalet.gov.tr", "rayp.adalet.gov.tr"
            ):
                self.item = {"url": url, "title": "", "date": ""}
        if self.item is not None and tag == "span":
            self.field = "date"

    def handle_data(self, data):
        if self.item is not None and self.field:
            self.item[self.field] += data

    def handle_endtag(self, tag):
        if tag == "span":
            self.field = None
        if tag == "a" and self.item is not None:
            item = {k: " ".join(v.split()) for k, v in self.item.items()}
            if item["title"] and item["date"]:
                self.items.append(item)
            self.item = None
            self.field = None
        if tag == "div" and self.depth:
            self.depth -= 1
            self.field = None


def fetch_items():
    request = Request(SOURCE, headers={"User-Agent": "SiirtDuyuruBot/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=40) as response:
                page = response.read().decode("utf-8-sig")
            parser = ArchiveParser()
            parser.feed(page)
            if not parser.items:
                raise RuntimeError("Duyuru listesi bulunamadi; kayitlar degistirilmedi.")
            return parser.items
        except (URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError("Duyuru sitesine erisilemedi.") from None
            time.sleep(2 ** attempt)


def identity(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def telegram(method, data):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN eksik.")
    request = Request(f"https://api.telegram.org/bot{token}/{method}",
                      data=urlencode(data).encode(), method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (URLError, TimeoutError, ValueError):
        # Do not expose request URLs, which contain the secret token.
        raise RuntimeError("Telegram istegi basarisiz; token, sohbet ve baglantiyi kontrol edin.") from None
    if not result.get("ok"):
        raise RuntimeError("Telegram istegi reddedildi.")
    return result["result"]


def send(text):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID eksik.")
    telegram("sendMessage", {"chat_id": chat, "text": text[:4000],
                             "link_preview_options": json.dumps({"is_disabled": True})})


def check(path, dry_run=False, now=None):
    now = (now or datetime.now(TURKEY)).astimezone(TURKEY)
    if now.hour < 8:
        print("00.00-08.00 sessiz saatleri; kontrol yapilmadi.")
        return
    items = fetch_items()
    if dry_run:
        print(json.dumps(items, ensure_ascii=True, indent=2))
        return
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if state is None:
        send("Siirt duyuru takibi basladi. Mevcut duyurular kaydedildi. "
             "Yeni duyurular 08.00-22.00 arasinda iki saatte bir kontrol edilecek.")
        save_state(path, {"version": 1, "seen": list(dict.fromkeys(map(identity, items)))})
        return
    if state.get("version") != 1 or not isinstance(state.get("seen"), list):
        raise RuntimeError("Durum dosyasi gecersiz; otomatik sifirlanmadi.")
    seen = set(state["seen"])
    for item in reversed(items):
        key = identity(item)
        if key in seen:
            continue
        if datetime.now(TURKEY).hour < 8:
            break
        send(f"Siirt Adliyesi - Yeni veya guncellenen duyuru\n\n{item['title']}\n"
             f"Tarih: {item['date']}\n\n{item['url']}")
        seen.add(key)
        state["seen"].append(key)
        save_state(path, state)
        time.sleep(1)
    print(f"Kontrol tamamlandi: {len(items)} duyuru.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("data/state.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chat-id", action="store_true")
    args = parser.parse_args()
    if args.chat_id:
        for update in telegram("getUpdates", {}):
            chat = update.get("message", {}).get("chat", {})
            if chat:
                print(f"Sohbet: {chat.get('first_name', chat.get('title', ''))}, ID: {chat['id']}")
    else:
        check(args.state, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Hata: {error}" if isinstance(error, RuntimeError) else
              "Hata: kontrol tamamlanamadi; ayarlari ve durum dosyasini kontrol edin.", file=sys.stderr)
        sys.exit(1)
