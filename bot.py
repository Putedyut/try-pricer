"""TRY Прайсер — бот для перепродажи карт PSN Турция.

GitHub Actions запускает его раз в 10 минут. Бот:
  1) читает новые сообщения боту (команда «цены» / /prices, и запоминает твой chat id);
  2) забирает цены конкурентов с FunPay и цену закупки с Ростелеком Игр;
  3) считает, какую цену ставить в лоте;
  4) пишет в Telegram: сводку при ручном запуске, по команде и раз в день,
     а в остальное время — только когда цену пора менять.
Бот никогда не падает с ошибкой: любая проблема приходит тебе сообщением и печатается в лог.
"""
import html
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
MSK = timezone(timedelta(hours=3))
TOKEN = os.environ.get("TG_TOKEN", "").strip()
ENV_CHAT = os.environ.get("TG_CHAT_ID", "").strip()
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}
FUNPAY_COOKIES = {"cy": "rub", "locale": "ru"}

DEFAULT_CFG = {
    "funpay_urls": [],
    "rt_urls": {},
    "rt_fallback_cost": {"250": 403, "500": 804, "1000": 1609, "1500": 2414, "2000": 3218, "2500": 4022},
    "denominations": [250, 500, 1000, 1500, 2000, 2500],
    "my_nick": "",
    "funpay_fee_pct": 13.03,
    "withdraw_pct": 3,
    "tax_pct": 0,
    "undercut_rub": 2,
    "min_margin_pct": 2,
    "skip_bots": True,
    "bot_stock_threshold": 10000,
    "notify_on_change_rub": 1,
    "daily_summary_hour_msk": 10,
}


def log(*a):
    print(*a, flush=True)


# ---------------- настройки и память ----------------
def load_cfg():
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            user = json.load(f)
    except Exception as e:
        return dict(DEFAULT_CFG), f"config.json не читается ({e}). Проверь кавычки и запятые."
    cfg = dict(DEFAULT_CFG)
    cfg.update(user)
    if "funpay_url" in user and not user.get("funpay_urls"):
        cfg["funpay_urls"] = user["funpay_url"]
    return cfg, None


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("last", {})
    st.setdefault("tg_offset", 0)
    st.setdefault("last_summary", "")
    st.setdefault("rt_cost", {})
    st.setdefault("chat_id", "")
    return st


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def as_list(v):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip().startswith("http")]


def num(s):
    if s is None:
        return None
    s = re.sub(r"[\s  ]", "", str(s)).replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def rub(v):
    return f"{v:,.0f}".replace(",", " ") + " ₽"


# ---------------- Telegram ----------------
def tg_api(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "description": r.text[:200]}
    return r.status_code, data


def read_messages(st):
    """Новые сообщения боту. Запоминает chat id того, кто пишет боту, и ловит команду «цены»."""
    if not TOKEN:
        return False
    try:
        code, data = tg_api("getUpdates", offset=st["tg_offset"], timeout=0)
    except Exception as e:
        log("getUpdates: Telegram недоступен:", e)
        return False
    if code != 200:
        log(f"getUpdates ошибка {code}: {data.get('description')}")
        return False
    asked = False
    for u in data.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            continue
        cid = str(chat.get("id", ""))
        if cid:
            if st.get("chat_id") != cid:
                log(f"Нашёл твой chat id по сообщению боту: {cid}")
            st["chat_id"] = cid
        text = (msg.get("text") or "").lower()
        if any(k in text for k in ("/prices", "/start", "цен", "price")):
            asked = True
    return asked


def send(st, text):
    """Отправляет сообщение. Сначала в TG_CHAT_ID, при ошибке — в чат, из которого тебе писали боту."""
    if not TOKEN:
        log("НЕТ TG_TOKEN: добавь секрет TG_TOKEN в Settings → Secrets and variables → Actions → Secrets.")
        log(text)
        return False
    targets = []
    for c in (ENV_CHAT, st.get("chat_id", "")):
        if c and c not in targets:
            targets.append(c)
    if not targets:
        log("НЕ ЗНАЮ, КУДА ПИСАТЬ: напиши своему боту в Telegram слово «цены» и запусти Run workflow ещё раз.")
        return False
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
    for chat in targets:
        ok = True
        for ch in chunks:
            try:
                code, data = tg_api("sendMessage", chat_id=chat, text=ch, parse_mode="HTML", disable_web_page_preview=True)
                if code == 400 and "parse" in str(data.get("description", "")).lower():
                    plain = re.sub(r"<[^>]+>", "", ch)
                    code, data = tg_api("sendMessage", chat_id=chat, text=html.unescape(plain), disable_web_page_preview=True)
            except Exception as e:
                log("Telegram недоступен:", e)
                return False
            if code != 200:
                ok = False
                desc = str(data.get("description", ""))
                log(f"Telegram ОШИБКА {code} для чата {chat}: {desc}")
                if code == 401:
                    log("→ Неверный TG_TOKEN. Скопируй токен заново у @BotFather и обнови секрет.")
                    return False
                if "bots" in desc:
                    log("→ В TG_CHAT_ID стоит id бота. Нужен ТВОЙ id из @userinfobot (или просто удали секрет TG_CHAT_ID и напиши боту «цены»).")
                elif code == 403:
                    log("→ Открой своего бота в Telegram, нажми «Старт» и напиши «цены».")
                elif code == 400:
                    log("→ Неверный TG_CHAT_ID. Напиши боту «цены» — бот сам узнает твой чат.")
                break
        if ok:
            log(f"Telegram: сообщение отправлено в чат {chat} ✅")
            if chat != ENV_CHAT:
                st["chat_id"] = chat
            return True
    return False


# ---------------- FunPay ----------------
def denom_from(text, denoms):
    vals = []
    for m in re.findall(r"(\d[\d\s ]{0,6}\d|\d)\s*(?:TRY|TL|₺|лир)", text, re.I):
        v = num(m)
        if v and int(v) in denoms:
            vals.append(int(v))
    return vals[-1] if vals else None


def parse_funpay_html(page, denoms):
    soup = BeautifulSoup(page, "html.parser")
    lots = {}
    items = soup.select("a.tc-item") or soup.select(".tc-item")
    for it in items:
        full = it.get_text(" ", strip=True)
        desc_el = it.select_one(".tc-desc-text") or it.select_one(".tc-desc")
        d = (denom_from(desc_el.get_text(" ", strip=True), denoms) if desc_el else None) or denom_from(full, denoms)
        if not d:
            continue
        price = None
        pe = it.select_one(".tc-price")
        if pe is not None:
            price = num(pe.get("data-s")) or num(pe.get_text(" ", strip=True))
        if not price:
            continue
        se = it.select_one(".media-user-name") or it.select_one(".tc-user")
        seller = se.get_text(" ", strip=True).split()[0] if se and se.get_text(strip=True) else "?"
        ae = it.select_one(".tc-amount")
        stock = num(ae.get_text("", strip=True)) if ae else 0
        lots.setdefault(d, []).append({"p": price, "s": seller, "q": stock or 0})
    return lots, len(items)


def parse_funpay_text(page, denoms):
    """Запасной разбор: по тексту страницы, если разметка FunPay поменялась."""
    text = BeautifulSoup(page, "html.parser").get_text("\n", strip=True).replace(" ", " ")
    lots, last, n = {}, 0, 0
    for m in re.finditer(r"(\d+(?:[.,]\d{1,2})?)\s*₽", text):
        chunk = text[last:m.start()]
        last = m.end()
        d = denom_from(chunk, denoms)
        if not d:
            continue
        price = num(m.group(1))
        qm = re.search(r"(\d{1,3}(?: \d{3})+|\d+)\s*$", chunk)
        stock = num(qm.group(1)) if qm else 0
        lines = [x.strip() for x in chunk.split("\n") if x.strip()]
        seller = "?"
        for i, l in enumerate(lines):
            if "на сайте" in l.lower():
                for k in range(i - 1, -1, -1):
                    if re.fullmatch(r"[\w.\-]{2,30}", lines[k]) and not lines[k].isdigit():
                        seller = lines[k]
                        break
                break
        lots.setdefault(d, []).append({"p": price, "s": seller, "q": stock or 0})
        n += 1
    return lots, n


def fetch_funpay(cfg):
    urls = as_list(cfg.get("funpay_urls"))
    if not urls:
        raise RuntimeError("в config.json не вписана ссылка на FunPay (funpay_urls)")
    denoms = [int(x) for x in cfg["denominations"]]
    lots, total = {}, 0
    for url in urls:
        r = requests.get(url, headers=HEADERS, cookies=FUNPAY_COOKIES, timeout=30)
        log(f"FunPay {url} → HTTP {r.status_code}, {len(r.text)} символов")
        r.raise_for_status()
        part, n = parse_funpay_html(r.text, denoms)
        if not part:
            part, n = parse_funpay_text(r.text, denoms)
            log(f"FunPay: разметка не узнана, разобрал по тексту: {n} лотов")
        total += n
        for d, arr in part.items():
            lots.setdefault(d, []).extend(arr)
        time.sleep(1)
    for d in list(lots):
        seen, uniq = set(), []
        for x in sorted(lots[d], key=lambda x: x["p"]):
            key = (x["s"], x["p"])
            if key not in seen:
                seen.add(key)
                uniq.append(x)
        lots[d] = uniq
    return lots, total


# ---------------- Ростелеком Игры ----------------
def parse_rt_price(page):
    soup = BeautifulSoup(page, "html.parser")
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
    for attrs in ({"itemprop": "price"}, {"property": "product:price:amount"}):
        meta = soup.find(attrs=attrs)
        if meta is not None and num(meta.get("content")):
            return num(meta.get("content"))
    text = soup.get_text(" ", strip=True).replace(" ", " ").replace(" ", " ")
    m = re.search(r"play\s*station\s*store\s*[\d ]{3,7}\s*TRY.{0,250}?(\d[\d ]{1,8})\s*₽", text, re.I | re.S)
    return num(m.group(1)) if m else None


def fetch_rt(cfg, st):
    costs, notes = {}, []
    for d in [int(x) for x in cfg["denominations"]]:
        urls = as_list(cfg.get("rt_urls", {}).get(str(d)))
        found, err = [], None
        for url in urls:
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                log(f"РИ {d} TRY → HTTP {r.status_code}")
                r.raise_for_status()
                p = parse_rt_price(r.text)
                if p and p > 50:
                    found.append(p)
            except Exception as e:
                err = type(e).__name__
        if found:
            costs[d] = min(found)
            st["rt_cost"][str(d)] = costs[d]
        else:
            costs[d] = st["rt_cost"].get(str(d)) or cfg["rt_fallback_cost"].get(str(d))
            if urls:
                why = f"сайт не открылся ({err})" if err else "цену на странице не нашёл"
                notes.append(f"{d} TRY: РИ — {why}, считаю по {rub(costs[d])}")
    return costs, notes


# ---------------- расчёт ----------------
def calc(cfg, d, cost, lots):
    if not cost:
        return None
    fee = cfg["funpay_fee_pct"] / 100
    wd = cfg["withdraw_pct"] / 100
    tax = cfg["tax_pct"] / 100
    nick = str(cfg.get("my_nick", "")).strip().lower()
    all_lots = lots.get(d, [])
    mine = [x for x in all_lots if nick and x["s"].lower() == nick]
    rivals = [x for x in all_lots if not (nick and x["s"].lower() == nick)]
    if cfg.get("skip_bots"):
        live = [x for x in rivals if x["q"] < cfg["bot_stock_threshold"]]
        rivals = live or rivals
    if not rivals:
        return None
    ref = rivals[0]
    keep = (1 - wd) * (1 - tax)
    lot = round((ref["p"] - cfg["undercut_rub"]) / (1 + fee))
    min_lot = math.ceil(cost * (1 + cfg["min_margin_pct"] / 100) / keep)
    if lot >= min_lot:
        status, rec = "✅ Ставь", lot
    elif lot * keep > cost:
        status, rec = "🟡 Тонко", lot
    else:
        status, rec = "⛔️ Не закупай", min_lot
    profit = rec * keep - cost
    return {"d": d, "cost": cost, "ref": ref, "lot": rec, "buyer": rec * (1 + fee), "profit": profit,
            "margin": profit / cost, "min_lot": min_lot, "status": status,
            "mine": round(mine[0]["p"] / (1 + fee)) if mine else None}


def fmt(r):
    me = ""
    if r["mine"] is not None:
        me = f"\n   у тебя сейчас {rub(r['mine'])}" + (" — ок" if r["mine"] == r["lot"] else " — <b>поменяй</b>")
    return (f"<b>{r['d']} TRY</b> · {r['status']}\n"
            f"   ставь <b>{rub(r['lot'])}</b> (покупатель увидит {r['buyer']:.2f} ₽)\n"
            f"   прибыль {r['profit']:+.0f} ₽ · {r['margin'] * 100:.1f}% · не ниже {rub(r['min_lot'])}\n"
            f"   ориентир: {html.escape(r['ref']['s'])} — {r['ref']['p']:g} ₽{me}")


def summary(res, notes):
    now = datetime.now(MSK).strftime("%d.%m %H:%M")
    body = "\n\n".join(fmt(r) for r in res) if res else "Нет данных ни по одному номиналу."
    tail = ("\n\n⚠️ " + "\n⚠️ ".join(html.escape(n) for n in notes)) if notes else ""
    return f"🇹🇷 <b>PSN Турция — цены</b> · {now} МСК\n\n{body}{tail}"


# ---------------- главный цикл ----------------
def run(st):
    cfg, cfg_err = load_cfg()
    asked = read_messages(st)
    save_state(st)
    if cfg_err:
        send(st, "⚠️ " + html.escape(cfg_err))
        return
    denoms = [int(x) for x in cfg["denominations"]]

    try:
        lots, n_items = fetch_funpay(cfg)
    except Exception as e:
        send(st, f"⚠️ FunPay: {html.escape(str(e))[:300]}")
        return
    if not lots:
        send(st, f"⚠️ FunPay открылся, но лоты с номиналами {', '.join(map(str, denoms))} TRY не найдены "
                 f"(элементов на странице: {n_items}). Проверь ссылку или пришли лог Claude.")
        return

    costs, notes = fetch_rt(cfg, st)
    res, missing = [], []
    for d in denoms:
        r = calc(cfg, d, costs.get(d), lots)
        (res.append(r) if r else missing.append(str(d)))
    if missing:
        notes.append(f"нет лотов конкурентов на FunPay для: {', '.join(missing)} TRY")
    for r in res:
        log(f"{r['d']} TRY: ставь {r['lot']} ₽, прибыль {r['profit']:+.0f} ₽, ориентир {r['ref']['s']} {r['ref']['p']}")

    changes = []
    for r in res:
        prev = st["last"].get(str(r["d"]))
        if prev and (abs(prev["lot"] - r["lot"]) >= cfg["notify_on_change_rub"] or prev["status"] != r["status"]):
            changes.append(f"<b>{r['d']} TRY</b>: {rub(prev['lot'])} → <b>{rub(r['lot'])}</b> · {r['status']}\n"
                           f"   ориентир {html.escape(r['ref']['s'])} — {r['ref']['p']:g} ₽, прибыль {r['profit']:+.0f} ₽")

    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    daily_due = now.hour >= cfg["daily_summary_hour_msk"] and st.get("last_summary") != today
    first = not st.get("last_summary")
    if MANUAL or asked or daily_due or first:
        sent = send(st, summary(res, notes))
    elif changes:
        sent = send(st, "🔔 <b>Пора поменять цену</b>\n\n" + "\n\n".join(changes))
    else:
        log("Цены не изменились — сообщение не нужно.")
        sent = True
    if sent:
        if MANUAL or asked or daily_due or first:
            st["last_summary"] = today
        for r in res:
            st["last"][str(r["d"])] = {"lot": r["lot"], "status": r["status"]}


def main():
    st = load_state()
    save_state(st)
    try:
        run(st)
    except Exception as e:
        log(traceback.format_exc())
        try:
            send(st, f"⚠️ Бот споткнулся: {html.escape(type(e).__name__)}: {html.escape(str(e))[:300]}")
        except Exception:
            pass
    finally:
        save_state(st)
    sys.exit(0)


if __name__ == "__main__":
    main()
