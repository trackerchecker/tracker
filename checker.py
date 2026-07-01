""" Price Checker — uruchamiany przez GitHub Actions co godzinę.
Czyta oferty z Firestore, sprawdza ceny przez  API,
zapisuje historię i wysyła maila przy zmianie.
"""

import json
import logging
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import firebase_admin
from firebase_admin import credentials, firestore

try:
    from curl_cffi import requests as http
    CURL_CFFI = True
except ImportError:
    import requests as http
    CURL_CFFI = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Firebase init ─────────────────────────────────────────────────────────────
creds_json = os.environ["FIREBASE_CREDENTIALS"]
cred = credentials.Certificate(json.loads(creds_json))
firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_offers():
    return [{"id": d.id, **d.to_dict()} for d in db.collection("offers").stream()]

def get_prices(offer_id):
    doc = db.collection("prices").document(offer_id).get()
    return doc.to_dict().get("history", []) if doc.exists else []

def save_prices(offer_id, history):
    db.collection("prices").document(offer_id).set({"history": history})

def get_config():
    doc = db.collection("config").document("main").get()
    return doc.to_dict() if doc.exists else {}

def save_status(last_run, log_entries):
    db.collection("config").document("status").set({
        "last_run": last_run,
        "running":  False,
        "log":      log_entries[:50],
    })

# ── TUI API ───────────────────────────────────────────────────────────────────

def extract_offer_code(url):
    m = re.search(r"/OfferCodeWS/([A-Z0-9]+)", url)
    if m:
        return m.group(1)
    try:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        if "offerCode" in qs:
            return qs["offerCode"][0]
    except Exception:
        pass
    return None

def fetch_price(url):
    offer_code = extract_offer_code(url)
    if not offer_code:
        log.error("Nie udało się wyciągnąć offerCode z: %s", url)
        return None

    api_url = (
        "https://www.tui.pl/api/services/tui-search/api/search/offers/price"
        f"?offerCode={offer_code}&details=true&analytics=true"
        "&alternativeOffers=true&mode=REALTIME"
    )
    headers = {
        "User-Agent":         "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept":             "*/*",
        "Accept-Language":    "pl-PL,pl;q=0.9",
        "Referer":            url,
        "Tui-Api-Key":        "www",
        "X-App-Id":           "50a81304-78a0-4886-8274-f9a90a7af5ab",
        "X-Market":           "pl",
        "X-Market-Currency":  "PLN",
        "X-Market-Language":  "pl",
        "Sec-Fetch-Dest":     "empty",
        "Sec-Fetch-Mode":     "cors",
        "Sec-Fetch-Site":     "same-origin",
    }
    try:
        if CURL_CFFI:
            resp = http.get(api_url, headers=headers, impersonate="chrome131", timeout=15)
        else:
            resp = http.get(api_url, headers=headers, timeout=15)

        log.info("API %d dla %s", resp.status_code, offer_code)
        if resp.status_code != 200:
            return None

        data  = resp.json()
        price = float(data["priceDetails"]["totalPrice"])
        log.info("Cena: %.0f zł", price)
        return price
    except Exception as e:
        log.error("Błąd fetch_price: %s", e)
        return None

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(cfg, subject, body_html):
    gmail_user = os.environ.get("GMAIL_USER") or cfg.get("gmail_user", "")
    gmail_pass = os.environ.get("GMAIL_PASSWORD") or cfg.get("gmail_password", "")
    notify     = cfg.get("notify_email") or gmail_user

    if not gmail_user or not gmail_pass:
        log.warning("Gmail nie skonfigurowany")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = notify
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail_user, gmail_pass)
            s.sendmail(gmail_user, notify, msg.as_string())
        log.info("Email wysłany: %s", subject)
    except Exception as e:
        log.error("Błąd emaila: %s", e)

def build_email_html(changes, cfg):
    rows = ""
    for c in changes:
        direction = "🔴 wzrosła" if c["diff"] > 0 else "🟢 spadła"
        diff_str  = f"+{c['diff']:.0f}" if c["diff"] > 0 else f"{c['diff']:.0f}"
        rows += f"""
        <tr>
          <td style="padding:10px;border-bottom:1px solid #eee">{c['name']}</td>
          <td style="padding:10px;border-bottom:1px solid #eee">{c['old']:.0f} zł</td>
          <td style="padding:10px;border-bottom:1px solid #eee;font-weight:700">{c['new']:.0f} zł</td>
          <td style="padding:10px;border-bottom:1px solid #eee">{direction} ({diff_str} zł)</td>
          <td style="padding:10px;border-bottom:1px solid #eee"><a href="{c['url']}">Sprawdź</a></td>
        </tr>"""
    return f"""
    <html><body style="font-family:Arial,sans-serif;color:#222;max-width:700px;margin:auto">
      <h2 style="color:#1B115C">🏖️ Zmiana ceny</h2>
      <p>{datetime.now().strftime('%d.%m.%Y %H:%M')} · {len(changes)} zmian</p>
      <table style="border-collapse:collapse;width:100%;font-size:14px">
        <thead><tr style="background:#1B115C;color:#fff">
          <th style="padding:10px;text-align:left">Oferta</th>
          <th style="padding:10px;text-align:left">Poprzednia</th>
          <th style="padding:10px;text-align:left">Aktualna</th>
          <th style="padding:10px;text-align:left">Zmiana</th>
          <th style="padding:10px;text-align:left">Link</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </body></html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg     = get_config()
    offers  = get_offers()
    changes = []
    log_entries = []
    now     = datetime.now().isoformat(timespec="minutes")

    # Retencja logów
    retention_days = int(cfg.get("log_retention_days", 7))
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(timespec="minutes")

    for offer in offers:
        if not offer.get("active", True):
            continue

        name = offer["name"]
        url  = offer["url"]
        log.info("Sprawdzam: %s", name)

        new_price = fetch_price(url)
        entry = {"time": now, "name": name, "price": new_price, "ok": new_price is not None}
        log_entries.append(entry)

        if new_price is None:
            continue

        history   = get_prices(offer["id"])
        # Rotacja — usuń stare wpisy
        history   = [h for h in history if h.get("time","") >= cutoff]
        old_price = history[-1]["price"] if history else None
        history.append({"time": now, "price": new_price})
        save_prices(offer["id"], history)

        if old_price is not None and abs(new_price - old_price) >= 1:
            changes.append({
                "name": name, "url": url,
                "old":  old_price, "new": new_price,
                "diff": new_price - old_price,
            })

    save_status(now, log_entries)

    if changes and cfg.get("notifications_enabled", True):
        send_email(cfg, f"TUI — zmiana ceny ({len(changes)} ofert)", build_email_html(changes, cfg))
    elif changes:
        log.info("Zmiany wykryte ale powiadomienia wyłączone")

    log.info("Gotowe. Sprawdzono %d ofert, %d zmian.", len(offers), len(changes))

if __name__ == "__main__":
    main()
