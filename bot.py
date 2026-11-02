"""TRY Прайсер — бот для перепродажи карт PSN Турция.
Запускается GitHub Actions по расписанию: забирает цены FunPay и Ростелеком Игр,
считает цену для лота и пишет в Telegram, когда рекомендация меняется.
"""
import json, math, os, re, sys, html, time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
STATE_PATH = os.path.join(ROOT, "state.json")
TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT_ID", "")
MSK = timezone(timedelta(hours=3))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
DENOMS = [int(d) for d in CFG["denominations"]]


def num(s):
    s = re.sub(r"[\s  ]", "", str(s)).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {"last": {}, "tg_offset": 0, "last_summary": "", "rt_cost": {}}


def save_state(st):
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def tg(text):
    if not TOKEN or not CHAT:
        print("[no telegram]\n" + text)
        return
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": CHAT, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
        print("tg", r.status_code)


def denom_from(text):
    found = [num(m) for m in re.findall(r"(\d[\d\s ]{0,6}\d|\d)\s*(?:TRY|TL|лир)", text, re.I)]
    found = [int(x) for x in found if x and int(x) in DENOMS]
    return found[-1] if found else None


# ---------- FunPay ----------
def parse_funpay(page):
    soup = BeautifulSoup(page, "html.parser")
    lots = {}
    items = soup.select("a.tc-item") or soup.select(".tc-item")
    for it in items:
        desc = it.select_one(".tc-desc-text") or it.select_one(".tc-desc") or it
        d = denom_from(desc.get_text(" ", strip=True)) or denom_from(it.get_text(" ", strip=True))
        if not d:
            continue
        price_el = it.select_one(".tc-price")
        price = None
        if price_el is not None:
            price = num(price_el.get("data-s")) if price_el.get("data-s") else None
            if price is None:
                m = re.search(r"(\d[\d\s ]*(?:[.,]\d{1,2})?)", price_el.get_text(" ", strip=True))
                price = num(m.group(1)) if m else None
        if not price or price < 50:
            continue
        seller_el = it.select_one(".media-user-name") or it.select_one(".tc-user")
        seller = seller_el.get_text(" ", strip=True).split()[0] if seller_el else "?"
        amount_el = it.select_one(".tc-amount")
        stock = num(amount_el.get_text("", strip=True)) if amount_el else 0
        lots.setdefault(d, []).append({"p": price, "s": seller, "q": stock or 0})
    for d in lots:
        lots[d].sort(key=lambda x: x["p"])
    return lots, len(items)


def fetch_funpay():
    url = CFG["funpay_url"]
    if not url.startswith("http"):
        raise RuntimeError("В config.json не указана ссылка funpay_url")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_funpay(r.text)


# ---------- Ростелеком Игры ----------
def parse_rt_price(page):
    soup = BeautifulSoup(page, "html.parser")
    # 1) структурированные данные (JSON-LD / meta)
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if "price" in x and num(x["price"]):
                    return num(x["price"])
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    meta = soup.find("meta", attrs={"itemprop": "price"}) or soup.find("meta", attrs={"property": "product:price:amount"})
    if meta and num(meta.get("content")):
        return num(meta.get("content"))
    # 2) первая цена в ₽ после названия карты
    text = soup.get_text(" ", strip=True)
    m = re.search(r"play\s*station\s*store\s*[\d\s ]{3,7}\s*TRY.{0,200}?(\d[\d\s ]{1,8})\s*₽", text, re.I | re.S)
    if m:
        return num(m.group(1))
    return None


def fetch_rt(st):
    costs, notes = {}, []
    for d in DENOMS:
        url = CFG["rt_urls"].get(str(d), "")
        price = None
        if url.startswith("http"):
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                price = parse_rt_price(r.text)
            except Exception as e:
                notes.append(f"{d} TRY: РИ не открылась ({type(e).__name__})")
        if price:
            costs[d] = price
            st["rt_cost"][str(d)] = price
        else:
            costs[d] = st["rt_cost"].get(str(d)) or CFG["rt_fallback_cost"].get(str(d))
            if url.startswith("http"):
                notes.append(f"{d} TRY: цену РИ не нашёл, беру {costs[d]} ₽")
    return costs, notes


# ---------- расчёт ----------
def calc(d, cost, lots):
    fee = CFG["funpay_fee_pct"] / 100
    wd = CFG["withdraw_pct"] / 100
    tax = CFG["tax_pct"] / 100
    nick = CFG["my_nick"].lower()
    rivals = [x for x in lots.get(d, []) if x["s"].lower() != nick]
    if CFG["skip_bots"]:
        live = [x for x in rivals if x["q"] < CFG["bot_stock_threshold"]]
        rivals = live or rivals
    mine = [x for x in lots.get(d, []) if x["s"].lower() == nick]
    if not rivals or not cost:
        return None
    ref = rivals[0]
    buyer = ref["p"] - CFG["undercut_rub"]
    lot = round(buyer / (1 + fee))
    min_lot = math.ceil(cost * (1 + CFG["min_margin_pct"] / 100) / ((1 - wd) * (1 - tax)))
    status = "✅ Ставь" if lot >= min_lot else ("🟡 Тонко" if lot * (1 - wd) * (1 - tax) > cost else "⛔️ Не закупай")
    rec = lot if lot * (1 - wd) * (1 - tax) > cost else min_lot
    profit = rec * (1 - wd) * (1 - tax) - cost
    return {"d": d, "cost": cost, "ref": ref, "lot": rec, "buyer": round(rec * (1 + fee), 2), "profit": profit,
            "margin": profit / cost, "min_lot": min_lot, "status": status, "mine": mine[0]["p"] if mine else None}


def rub(v):
    return f"{v:,.0f}".replace(",", " ") + " ₽"


def fmt(r):
    me = ""
    if r["mine"]:
        my_lot = round(r["mine"] / (1 + CFG["funpay_fee_pct"] / 100))
        me = f"\n   сейчас у тебя {rub(my_lot)}" + (" — поменяй" if my_lot != r["lot"] else " — ок")
    return (f"<b>{r['d']} TRY</b> {r['status']}\n"
            f"   ставь <b>{rub(r['lot'])}</b> → покупатель {r['buyer']:.2f} ₽\n"
            f"   прибыль {r['profit']:+.0f} ₽ ({r['margin']*100:.1f}%), не ниже {rub(r['min_lot'])}\n"
            f"   ориентир: {html.escape(r['ref']['s'])} {r['ref']['p']} ₽{me}")


def summary(res, notes):
    now = datetime.now(MSK).strftime("%d.%m %H:%M")
    body = "\n\n".join(fmt(r) for r in res)
    tail = ("\n\n⚠️ " + "\n⚠️ ".join(notes)) if notes else ""
    return f"🇹🇷 <b>PSN Турция — цены</b> · {now} МСК\n\n{body}{tail}"


def check_commands(st):
    """Читает новые сообщения боту. /prices или «цены» — прислать полную сводку."""
    if not TOKEN:
        return False
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params={"offset": st.get("tg_offset", 0), "timeout": 0}, timeout=20).json()
    except Exception:
        return False
    want = False
    for u in r.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").lower()
        if str(msg.get("chat", {}).get("id")) == str(CHAT) and any(k in text for k in ("/prices", "/start", "цены", "цена")):
            want = True
    return want


def main():
    st = load_state()
    st.setdefault("rt_cost", {})
    asked = check_commands(st)
    notes = []
    try:
        lots, n_items = fetch_funpay()
    except Exception as e:
        tg(f"⚠️ Не смог открыть FunPay: {html.escape(str(e))[:300]}")
        save_state(st)
        return
    if not lots:
        tg(f"⚠️ FunPay открылся, но лоты не распознаны (элементов на странице: {n_items}). Пришли этот текст Claude — поправит парсер.")
        save_state(st)
        return
    costs, rt_notes = fetch_rt(st)
    notes += rt_notes
    res = [r for r in (calc(d, costs[d], lots) for d in DENOMS) if r]

    changes = []
    for r in res:
        prev = st["last"].get(str(r["d"]))
        if prev is None or abs(prev["lot"] - r["lot"]) >= CFG["notify_on_change_rub"] or prev["status"] != r["status"]:
            if prev is not None:
                changes.append(f"<b>{r['d']} TRY</b>: {rub(prev['lot'])} → <b>{rub(r['lot'])}</b> {r['status']}\n"
                               f"   ориентир {html.escape(r['ref']['s'])} {r['ref']['p']} ₽, прибыль {r['profit']:+.0f} ₽")
        st["last"][str(r["d"])] = {"lot": r["lot"], "status": r["status"]}

    now = datetime.now(MSK)
    day_key = now.strftime("%Y-%m-%d")
    daily = now.hour >= CFG["daily_summary_hour_msk"] and st.get("last_summary") != day_key
    if asked or daily or not st.get("last_summary"):
        tg(summary(res, notes))
        if daily or not st.get("last_summary"):
            st["last_summary"] = day_key
    elif changes:
        tg("🔔 <b>Пора поменять цену</b>\n\n" + "\n\n".join(changes))
    save_state(st)


if __name__ == "__main__":
    main()
