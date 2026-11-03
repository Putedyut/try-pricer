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
    "undercut_rub": 1,
    "min_margin_pct": 2,
    "skip_bots": False,
    "bot_stock_threshold": 10000,
    "notify_on_change_rub": 1,
    "daily_summary_hour_msk": 10,
    "market_check_min": 5,
    "rt_report_min": 30,
    "run_minutes": 340,
    "payout_hold_days": 2,
    "low_stock": 2,
    "try_rub_rate": 1.62,
}


KEYBOARD = {"keyboard": [[{"text": "📊 Рынок"}, {"text": "🛒 РТК"}], [{"text": "💰 Мои цены"}, {"text": "📒 Бухгалтерия"}], [{"text": "❓ Помощь"}]],
            "resize_keyboard": True, "is_persistent": True}


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
    if v is None:
        return "—"
    if abs(v - round(v)) >= 0.005:
        return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
    return f"{v:,.0f}".replace(",", " ") + " ₽"


# ---------------- Telegram ----------------
def tg_api(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "description": r.text[:200]}
    return r.status_code, data


def read_messages(st, wait=0):
    """Новые сообщения боту (ждёт до wait секунд). Запоминает твой chat id. Возвращает список текстов."""
    if not TOKEN:
        return []
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                          json={"offset": st["tg_offset"], "timeout": wait}, timeout=wait + 15)
        data = r.json()
    except Exception as e:
        log("getUpdates: Telegram недоступен:", e)
        time.sleep(3)
        return []
    if r.status_code != 200:
        log(f"getUpdates ошибка {r.status_code}: {data.get('description')}")
        time.sleep(3)
        return []
    texts = []
    for u in data.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            continue
        cid = str(chat.get("id", ""))
        allowed = {c for c in (ENV_CHAT, st.get("chat_id", "")) if c}
        if allowed and cid not in allowed and st.get("chat_locked"):
            continue  # чужие люди, нашедшие бота, игнорируются
        if cid and st.get("chat_id") != cid:
            log(f"Твой chat id: {cid}")
            st["chat_id"] = cid
        st["chat_locked"] = True
        texts.append(msg.get("text") or "")
    return texts


def send(st, text, keyboard=True):
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
                code, data = tg_api("sendMessage", chat_id=chat, text=ch, parse_mode="HTML", disable_web_page_preview=True, **({"reply_markup": KEYBOARD} if keyboard else {}))
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
    rate = float(cfg.get("try_rub_rate", 1.62))
    for d in list(lots):
        lo, hi = d * rate * 0.8, d * rate * 2.0
        bad = [x for x in lots[d] if not (lo <= x["p"] <= hi)]
        if bad:
            log(f"FunPay {d} TRY: отбросил {len(bad)} лотов с ценой не за карту (например {bad[0]['s']} {bad[0]['p']} ₽)")
        lots[d] = [x for x in lots[d] if lo <= x["p"] <= hi]
        if not lots[d]:
            del lots[d]
            continue
        seen, uniq = set(), []
        for x in sorted(lots[d], key=lambda x: x["p"]):
            key = (x["s"], x["p"])
            if key not in seen:
                seen.add(key)
                uniq.append(x)
        lots[d] = uniq
    return lots, total


# ---------------- Ростелеком Игры ----------------
def rt_candidates(page):
    """Все цены в рублях со страницы РИ: из структурированных данных и из текста."""
    soup = BeautifulSoup(page, "html.parser")
    cands = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for k in ("price", "lowPrice"):
                    if k in x and num(x[k]):
                        cands.append(num(x[k]))
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    for attrs in ({"itemprop": "price"}, {"property": "product:price:amount"}):
        for meta in soup.find_all(attrs=attrs):
            if num(meta.get("content")):
                cands.append(num(meta.get("content")))
    text = soup.get_text(" ", strip=True).replace("\u00a0", " ").replace("\u202f", " ")
    for m in re.finditer(r"(\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d{1,2})?\s*₽", text):
        v = num(m.group(1))
        if v:
            cands.append(v)
    return cands


def pick_rt_price(cands, d, rate):
    """Цена карты d TRY на РИ — около d × курс лиры. Старую цену «без скидки» (~1.8×) и цены других товаров отбрасываем.
    Из подходящих берём ближайшую к номиналу по курсу."""
    target = d * rate
    ok = [v for v in cands if target * 0.85 <= v <= target * 1.15]
    return min(ok, key=lambda v: abs(v - target)) if ok else None


def fetch_rt(cfg, st):
    costs, notes = {}, []
    rate = float(cfg.get("try_rub_rate", 1.62))
    for d in [int(x) for x in cfg["denominations"]]:
        urls = as_list(cfg.get("rt_urls", {}).get(str(d)))
        found, err = [], None
        for url in urls:
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                cands = rt_candidates(r.text)
                p = pick_rt_price(cands, d, rate)
                log(f"РИ {d} TRY → HTTP {r.status_code}, цены на странице: {sorted(set(cands))[:15]}, выбрал: {p}")
                if p:
                    found.append(p)
            except Exception as e:
                err = type(e).__name__
                log(f"РИ {d} TRY → ошибка {err}")
        if found:
            costs[d] = min(found)
            st["rt_cost"][str(d)] = costs[d]
        else:
            saved = st["rt_cost"].get(str(d))
            if not (saved and pick_rt_price([saved], d, rate)):
                saved = None
            costs[d] = saved or cfg["rt_fallback_cost"].get(str(d))
            if urls:
                why = f"сайт не открылся ({err})" if err else "цену на странице не нашёл"
                notes.append(f"{d} TRY: РИ — {why}, считаю по {rub(costs[d])}")
    return costs, notes


# ---------------- расчёт ----------------
def econ(cfg, cost, lot):
    """Что получится, если в лоте стоит lot ₽: цена для покупателя, тебе на руки, прибыль, маржа."""
    fee = cfg["funpay_fee_pct"] / 100
    keep = (1 - cfg["withdraw_pct"] / 100) * (1 - cfg["tax_pct"] / 100)
    net = lot * keep
    profit = net - cost if cost else None
    return {"lot": lot, "buyer": lot * (1 + fee), "net": net, "profit": profit,
            "margin": (profit / cost) if cost else None}


def lot_for_buyer(cfg, buyer, floor=False):
    v = buyer / (1 + cfg["funpay_fee_pct"] / 100)
    if floor:
        return math.floor(v)
    half = round(v * 2) / 2  # FunPay показывает цену покупателя с округлением — восстанавливаем цену лота (428,5 и т.п.)
    return half if abs(v - half) < 0.08 else round(v, 2)


def market_view(cfg, st, d, lots):
    """Самый дешёвый конкурент, моя позиция и расчёт для «на 1 ₽ дешевле самого дешёвого»."""
    nick = str(cfg.get("my_nick", "")).strip().lower()
    arr = lots.get(d, [])
    mine = [x for x in arr if nick and x["s"].lower() == nick]
    rivals = [x for x in arr if not (nick and x["s"].lower() == nick)]
    if cfg.get("skip_bots"):
        rivals = [x for x in rivals if x["q"] < cfg["bot_stock_threshold"]] or rivals
    cost = st["rt_cost"].get(str(d)) or cfg["rt_fallback_cost"].get(str(d))
    v = {"d": d, "cost": cost, "rival": rivals[0] if rivals else None, "second": rivals[1] if len(rivals) > 1 else None}
    # моя цена: с FunPay (по нику), иначе — то, что ты вписал боту
    if mine:
        v["my_lot"] = lot_for_buyer(cfg, mine[0]["p"])
        v["my_src"] = "FunPay"
    elif st["my_lot"].get(str(d)):
        v["my_lot"] = st["my_lot"][str(d)]
        v["my_src"] = "вписал ты"
    else:
        v["my_lot"] = None
    v["mine"] = econ(cfg, cost, v["my_lot"]) if v["my_lot"] else None
    if v["rival"]:
        target_buyer = v["rival"]["p"] - cfg["undercut_rub"]
        v["cheapest"] = econ(cfg, cost, lot_for_buyer(cfg, target_buyer, floor=True))
    else:
        v["cheapest"] = None
    return v


def money(e):
    if not e or e["profit"] is None:
        return "—"
    warn = " ⚠️ в ноль/минус" if e["profit"] <= 0 else ""
    return f"{e['profit']:+.0f} ₽ ({e['margin'] * 100:.1f}%){warn}"


def bot_mark(cfg, x):
    return " 🤖" if x and x["q"] >= cfg["bot_stock_threshold"] else ""


def fmt_market(cfg, v):
    lines = [f"<b>{v['d']} TRY</b> · закупка РТК {rub(v['cost'])}"]
    if v["rival"]:
        lines.append(f"   дешевле всех: {html.escape(v['rival']['s'])}{bot_mark(cfg, v['rival'])} — {v['rival']['p']:g} ₽")
    else:
        lines.append("   конкурентов на FunPay нет")
    if v["mine"]:
        m = v["mine"]
        pos = ""
        if v["rival"]:
            pos = " 🥇 ты дешевле всех" if m["buyer"] <= v["rival"]["p"] else f" 🔻 дороже рынка на {m['buyer'] - v['rival']['p']:.2f} ₽"
        lines.append(f"   у тебя: {rub(m['lot'])} в лоте → покупатель {m['buyer']:.2f} ₽ ({v['my_src']}){pos}")
        lines.append(f"   твоя прибыль с карты: {money(m)}")
    if v["cheapest"]:
        c = v["cheapest"]
        lines.append(f"   на 1 ₽ дешевле рынка: {rub(c['lot'])} в лоте → прибыль {money(c)}")
    return "\n".join(lines)


def report_market(cfg, st, lots, notes):
    now = datetime.now(MSK).strftime("%d.%m %H:%M")
    views = [market_view(cfg, st, d, lots) for d in DENOMS(cfg)]
    body = "\n\n".join(fmt_market(cfg, v) for v in views)
    tail = ("\n\n⚠️ " + "\n⚠️ ".join(html.escape(n) for n in notes)) if notes else ""
    return f"📊 <b>FunPay · PSN Турция</b> · {now}\n\n{body}{tail}"


def report_rt(cfg, st, prev, notes):
    now = datetime.now(MSK).strftime("%d.%m %H:%M")
    lines = []
    for d in DENOMS(cfg):
        c = st["rt_cost"].get(str(d)) or cfg["rt_fallback_cost"].get(str(d))
        p = prev.get(str(d))
        if p and not pick_rt_price([p], d, float(cfg.get("try_rub_rate", 1.62))):
            p = None  # старое значение было ошибочным — не сравниваем с ним
        ch = ""
        if p and c and abs(p - c) >= 1:
            ch = f"  ({'↑' if c > p else '↓'} было {rub(p)})"
        lines.append(f"{d} TRY — <b>{rub(c)}</b>{ch}")
    tail = ("\n\n⚠️ " + "\n⚠️ ".join(html.escape(n) for n in notes)) if notes else ""
    return f"🛒 <b>Цены Ростелеком Игр</b> · {now}\n\n" + "\n".join(lines) + tail


def report_mine(cfg, st, lots):
    out = []
    for d in DENOMS(cfg):
        v = market_view(cfg, st, d, lots)
        if v["mine"]:
            m = v["mine"]
            out.append(f"<b>{d} TRY</b>: {rub(m['lot'])} в лоте ({v['my_src']}) → покупатель {m['buyer']:.2f} ₽\n"
                       f"   на руки {m['net']:.0f} ₽ − закупка {rub(v['cost'])} = <b>{money(m)}</b>")
        else:
            ex = v["cheapest"]["lot"] if v["cheapest"] else round(d * 1.7)
            out.append(f"<b>{d} TRY</b>: цена не известна — впиши, например: <code>{d} {ex}</code>")
    return "💰 <b>Мои цены и заработок с одной карты</b>\n\n" + "\n\n".join(out)



# ---------------- бухгалтерия ----------------
# Журнал операций хранится зашифрованным в файле ledger.enc в репозитории (ключ — секрет LEDGER_KEY),
# поэтому никто, кроме тебя, его не прочитает, а данные не пропадут вместе с кэшем GitHub.
LEDGER_PATH = os.path.join(ROOT, "ledger.enc")
LEDGER_KEY = os.environ.get("LEDGER_KEY", "").strip()


def _fernet():
    import base64, hashlib
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(LEDGER_KEY.encode()).digest()))


def empty_ledger():
    return {"sales": [], "buys": [], "withdrawals": [], "seq": 0}


def load_ledger(st):
    if LEDGER_KEY and os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH, "rb") as f:
                return json.loads(_fernet().decrypt(f.read()).decode())
        except Exception as e:
            raise RuntimeError(f"не смог расшифровать ledger.enc — проверь секрет LEDGER_KEY ({type(e).__name__})")
    return st.get("ledger") or empty_ledger()


def save_ledger(st, led):
    if LEDGER_KEY:
        with open(LEDGER_PATH, "wb") as f:
            f.write(_fernet().encrypt(json.dumps(led, ensure_ascii=False).encode()))
    st["ledger"] = led  # копия в памяти бота на случай, если секрета нет
    push_ledger()


def push_ledger():
    """Сразу сохраняет журнал в репозиторий, чтобы бухгалтерия не потерялась, даже если запуск оборвётся."""
    if not (LEDGER_KEY and os.environ.get("GITHUB_ACTIONS") == "true"):
        return
    import subprocess
    cmds = [["git", "config", "user.name", "try-pricer"],
            ["git", "config", "user.email", "bot@users.noreply.github.com"],
            ["git", "add", "ledger.enc"],
            ["git", "commit", "-m", "ledger", "--quiet"]]
    try:
        for c in cmds:
            subprocess.run(c, cwd=ROOT, check=False, capture_output=True, timeout=30)
        for _ in range(3):
            subprocess.run(["git", "pull", "--rebase", "--quiet", "origin", "main"], cwd=ROOT, capture_output=True, timeout=60)
            r = subprocess.run(["git", "push", "--quiet", "origin", "HEAD:main"], cwd=ROOT, capture_output=True, timeout=60)
            if r.returncode == 0:
                log("Журнал бухгалтерии сохранён в репозиторий")
                return
            time.sleep(3)
        log("Не смог сохранить журнал:", r.stderr.decode()[-300:])
    except Exception as e:
        log("Не смог сохранить журнал:", e)


def now_ts():
    return int(time.time())


def ts_str(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M")


def avg_buy_cost(led, d):
    buys = [b for b in led["buys"] if b["d"] == d]
    q = sum(b["qty"] for b in buys)
    return sum(b["qty"] * b["price"] for b in buys) / q if q else None


def sale_profit(cfg, s):
    keep = (1 - cfg["withdraw_pct"] / 100) * (1 - cfg["tax_pct"] / 100)
    return s["qty"] * (s["price"] * keep - s["cost"])


def acc_period(cfg, led, since):
    sales = [s for s in led["sales"] if s["ts"] >= since]
    by = {}
    for s in sales:
        b = by.setdefault(s["d"], {"qty": 0, "rev": 0.0, "cost": 0.0, "profit": 0.0})
        b["qty"] += s["qty"]
        b["rev"] += s["qty"] * s["price"]
        b["cost"] += s["qty"] * s["cost"]
        b["profit"] += sale_profit(cfg, s)
    tot = {k: sum(b[k] for b in by.values()) for k in ("qty", "rev", "cost", "profit")}
    return by, tot


def report_accounting(cfg, st, led):
    now = datetime.now(MSK)
    day0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    week0 = int((now - timedelta(days=7)).timestamp())
    month0 = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    lines = ["📒 <b>Бухгалтерия</b> · " + now.strftime("%d.%m %H:%M")]
    for title, since in (("Сегодня", day0), ("7 дней", week0), ("С начала месяца", month0), ("Всё время", 0)):
        _, t = acc_period(cfg, led, since)
        lines.append(f"\n<b>{title}:</b> {t['qty']} шт · выручка {rub(t['rev'])} · прибыль <b>{rub(t['profit'])}</b>")
    by, t = acc_period(cfg, led, month0)
    if by:
        lines.append("\n<b>Продажи за месяц по картам:</b>")
        for d in sorted(by):
            b = by[d]
            lines.append(f"{d} TRY — {b['qty']} шт, ср. цена {rub(b['rev'] / b['qty'])}, прибыль {rub(b['profit'])}")
    # баланс FunPay
    sold = sum(s["qty"] * s["price"] for s in led["sales"])
    withdrawn = sum(w["amount"] for w in led["withdrawals"])
    received = sum(w["received"] for w in led["withdrawals"])
    hold_from = now_ts() - int(float(cfg.get("payout_hold_days", 2)) * 86400)
    on_hold = sum(s["qty"] * s["price"] for s in led["sales"] if s["ts"] >= hold_from)
    balance = sold - withdrawn
    lines.append("\n<b>Баланс FunPay:</b>")
    lines.append(f"на балансе {rub(balance)} · из них в холде {rub(min(on_hold, balance))} · можно вывести {rub(max(0, balance - on_hold))}")
    lines.append(f"выведено всего {rub(withdrawn)} → пришло на карту {rub(received)}")
    # остатки кодов
    if led["buys"]:
        lines.append("\n<b>Остатки кодов:</b>")
        for d in DENOMS(cfg):
            bought = sum(b["qty"] for b in led["buys"] if b["d"] == d)
            sold_q = sum(s["qty"] for s in led["sales"] if s["d"] == d)
            if bought:
                left = bought - sold_q
                lines.append(f"{d} TRY — {left} шт" + (" ⚠️ заканчиваются" if 0 < left <= int(cfg.get("low_stock", 2)) else (" ❗️нет в наличии" if left <= 0 else "")))
        spent = sum(b["qty"] * b["price"] for b in led["buys"])
        lines.append(f"потрачено на закупку {rub(spent)}")
    if not led["sales"] and not led["buys"] and not led["withdrawals"]:
        lines.append("\nПока пусто. Записывай операции:\n<code>продал 250 x2</code>\n<code>купил 2500 x5 по 4022</code>\n<code>вывел 5000</code>")
    if not LEDGER_KEY:
        lines.append("\n⚠️ Секрет LEDGER_KEY не добавлен — журнал хранится только в памяти бота и может пропасть.")
    return "\n".join(lines)


def handle_accounting(cfg, st, text, lots):
    """Команды бухгалтерии. Возвращает ответ или None, если это не команда бухгалтерии."""
    t = re.sub(r"(?<=\d)\s*[хx×*]\s*(?=\d)", " x", text.strip().lower())
    t = re.sub(r"(?<=\s)[хx×*]\s*(?=\d)", "x", t)
    try:
        led = load_ledger(st)
    except RuntimeError as e:
        return "⚠️ " + html.escape(str(e))

    m = re.fullmatch(r"(продал[аи]?|продажа|sold)\s+(\d{3,4})\s*(?:try|лир)?\s*(?:x\s*(\d+)|(\d+)\s*шт\.?)?\s*(?:(?:по|@|за)\s*(\d+(?:[.,]\d+)?))?\s*(?:₽|р|руб\.?)?", t)
    if m:
        d = int(m.group(2))
        if d not in DENOMS(cfg):
            return f"Нет такого номинала: {d}. Есть: {', '.join(map(str, DENOMS(cfg)))}"
        qty = int(m.group(3) or m.group(4) or 1)
        price = num(m.group(5)) if m.group(5) else None
        if price is None:
            v = market_view(cfg, st, d, lots)
            price = v["my_lot"]
        if not price:
            return f"Не знаю, по какой цене продал. Напиши с ценой из лота: <code>продал {d} x{qty} по 428</code>"
        cost = avg_buy_cost(led, d) or st["rt_cost"].get(str(d)) or cfg["rt_fallback_cost"].get(str(d))
        led["seq"] += 1
        sale = {"id": led["seq"], "ts": now_ts(), "d": d, "qty": qty, "price": float(price), "cost": float(cost)}
        led["sales"].append(sale)
        save_ledger(st, led)
        p = sale_profit(cfg, sale)
        return (f"✅ Записал продажу #{sale['id']}: {d} TRY × {qty} по {rub(price)}\n"
                f"   выручка {rub(qty * price)} · закупка {rub(qty * cost)} · прибыль <b>{rub(p)}</b>\n"
                f"   отменить: <code>отмена</code>")

    m = re.fullmatch(r"(купил[аи]?|закупка|закупил[аи]?)\s+(\d{3,4})\s*(?:try|лир)?\s*(?:x\s*(\d+)|(\d+)\s*шт\.?)?\s*(?:(?:по|@|за)\s*(\d+(?:[.,]\d+)?))?\s*(?:₽|р|руб\.?)?", t)
    if m:
        d = int(m.group(2))
        if d not in DENOMS(cfg):
            return f"Нет такого номинала: {d}."
        qty = int(m.group(3) or m.group(4) or 1)
        price = num(m.group(5)) if m.group(5) else (st["rt_cost"].get(str(d)) or cfg["rt_fallback_cost"].get(str(d)))
        led["seq"] += 1
        led["buys"].append({"id": led["seq"], "ts": now_ts(), "d": d, "qty": qty, "price": float(price)})
        save_ledger(st, led)
        return f"📦 Записал закупку #{led['seq']}: {d} TRY × {qty} по {rub(price)} = {rub(qty * price)}\n   отменить: <code>отмена</code>"

    m = re.fullmatch(r"(вывел[аи]?|вывод)\s+(\d+(?:[.,]\d+)?)\s*(?:₽|р|руб\.?)?", t)
    if m:
        amount = num(m.group(2))
        fee = amount * cfg["withdraw_pct"] / 100
        led["seq"] += 1
        led["withdrawals"].append({"id": led["seq"], "ts": now_ts(), "amount": amount, "received": amount - fee})
        save_ledger(st, led)
        return f"💸 Записал вывод #{led['seq']}: {rub(amount)} с баланса → на карту {rub(amount - fee)} (комиссия {rub(fee)})\n   отменить: <code>отмена</code>"

    if t in ("отмена", "отменить", "undo", "отмени"):
        all_ops = [("sales", x) for x in led["sales"]] + [("buys", x) for x in led["buys"]] + [("withdrawals", x) for x in led["withdrawals"]]
        if not all_ops:
            return "Отменять нечего."
        kind, op = max(all_ops, key=lambda k: k[1]["id"])
        led[kind] = [x for x in led[kind] if x["id"] != op["id"]]
        save_ledger(st, led)
        name = {"sales": "продажу", "buys": "закупку", "withdrawals": "вывод"}[kind]
        return f"↩️ Отменил {name} #{op['id']} от {ts_str(op['ts'])}."

    if any(k in t for k in ("бухгалт", "/acc", "отчёт", "отчет", "баланс")):
        return report_accounting(cfg, st, led)

    if any(k in t for k in ("журнал", "история", "/log")):
        ops = sorted([("продажа", x) for x in led["sales"]] + [("закупка", x) for x in led["buys"]] + [("вывод", x) for x in led["withdrawals"]],
                     key=lambda k: -k[1]["id"])[:20]
        if not ops:
            return "Журнал пуст."
        rows = []
        for kind, x in ops:
            if kind == "вывод":
                rows.append(f"#{x['id']} {ts_str(x['ts'])} · вывод {rub(x['amount'])}")
            else:
                rows.append(f"#{x['id']} {ts_str(x['ts'])} · {kind} {x['d']} TRY × {x['qty']} по {rub(x['price'])}")
        return "🧾 <b>Последние операции</b>\n" + "\n".join(rows) + "\n\nУдалить конкретную: <code>удалить 12</code>"

    m = re.fullmatch(r"удали(?:ть)?\s+#?(\d+)", t)
    if m:
        i = int(m.group(1))
        for kind in ("sales", "buys", "withdrawals"):
            if any(x["id"] == i for x in led[kind]):
                led[kind] = [x for x in led[kind] if x["id"] != i]
                save_ledger(st, led)
                return f"🗑 Удалил операцию #{i}."
        return f"Операции #{i} нет."

    if any(k in t for k in ("выгруз", "csv", "/export")):
        return ("__CSV__", ledger_csv(led))
    return None


def ledger_csv(led):
    rows = ["id;дата;тип;номинал;кол-во;цена;закупка за шт;сумма;на карту"]
    for x in led["sales"]:
        rows.append(f"{x['id']};{ts_str(x['ts'])};продажа;{x['d']};{x['qty']};{x['price']:.2f};{x['cost']:.2f};{x['qty'] * x['price']:.2f};")
    for x in led["buys"]:
        rows.append(f"{x['id']};{ts_str(x['ts'])};закупка;{x['d']};{x['qty']};{x['price']:.2f};;{x['qty'] * x['price']:.2f};")
    for x in led["withdrawals"]:
        rows.append(f"{x['id']};{ts_str(x['ts'])};вывод;;;;;{x['amount']:.2f};{x['received']:.2f}")
    return "\n".join(rows).encode("utf-8-sig")


def send_file(st, name, data, caption=""):
    chat = ENV_CHAT or st.get("chat_id")
    if not (TOKEN and chat):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                          data={"chat_id": chat, "caption": caption}, files={"document": (name, data)}, timeout=30)
        log("sendDocument", r.status_code)
        return r.status_code == 200
    except Exception as e:
        log("sendDocument ошибка", e)
        return False


HELP = """❓ <b>Что умеет бот</b>

📊 <b>Рынок</b> — самая дешёвая цена на FunPay по каждой карте, твоя цена, твоя прибыль и прибыль, если встать на 1 ₽ дешевле рынка.
🛒 <b>РТК</b> — текущие цены закупки в Ростелеком Играх (сами приходят раз в 30 минут).
💰 <b>Мои цены</b> — твои цены и заработок с одной карты.

<b>Вписать свою цену:</b> номинал и цена из поля «Цена за 1 шт.» на FunPay, например
<code>250 425</code>
<code>2500 4288</code>
Бот сразу посчитает маржу и заработок. Если твой лот виден на FunPay по нику, бот берёт цену оттуда сам.
<code>250 сброс</code> — забыть вписанную цену.

<b>Сам пишет, когда:</b>
🚨 кто-то поставил цену дешевле твоей;
🥇 ты стал самым дешёвым или ниже рынка;
🛒 раз в 30 минут — цены РТК.

📒 <b>Бухгалтерия</b> — продажи, прибыль, баланс FunPay, выводы, остатки кодов.
<code>продал 250 x3</code> — по твоей текущей цене (или <code>продал 250 x3 по 428</code>)
<code>купил 2500 x5 по 4022</code> — закупка в РТК (без цены — по текущей цене РТК)
<code>вывел 5000</code> — вывод с FunPay (бот вычтет 3%)
<code>отмена</code> — отменить последнюю операцию
<code>журнал</code> — последние операции, <code>удалить 12</code> — удалить конкретную
<code>выгрузка</code> — прислать всю бухгалтерию файлом для Excel"""


def DENOMS(cfg):
    return [int(x) for x in cfg["denominations"]]


def handle_text(cfg, st, text, lots, notes):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if len(lines) > 1:
        answers = []
        for l in lines:
            ans = handle_one(cfg, st, l, lots, notes)
            if isinstance(ans, tuple):
                return ans
            if ans is HELP:
                ans = f"Не понял строку «{html.escape(l)}». Пиши номинал и цену: <code>250 428.5</code>"
            answers.append(ans)
        return "\n\n".join(answers)
    return handle_one(cfg, st, text, lots, notes)


def handle_one(cfg, st, text, lots, notes):
    acc = handle_accounting(cfg, st, text or "", lots)
    if acc is not None:
        return acc
    t = (text or "").strip().lower()
    m = re.fullmatch(r"(\d{3,4})\s*[:=\-–—]?\s*(\d+(?:[.,]\d+)?|сброс|reset)\s*(?:₽|р|руб)?\.?", t)
    if m and int(m.group(1)) in DENOMS(cfg):
        d = int(m.group(1))
        if m.group(2) in ("сброс", "reset"):
            st["my_lot"].pop(str(d), None)
            return f"Забыл твою цену для {d} TRY."
        lot = round(num(m.group(2).replace(",", ".")), 2)
        st["my_lot"][str(d)] = lot
        v = market_view(cfg, st, d, lots)
        e = econ(cfg, v["cost"], lot)
        lines = [f"✍️ <b>{d} TRY</b>: записал {rub(lot)} в лоте",
                 f"   покупатель увидит {e['buyer']:.2f} ₽",
                 f"   на руки после вывода {e['net']:.0f} ₽ − закупка РТК {rub(v['cost'])}",
                 f"   <b>заработок с карты: {money(e)}</b>"]
        if v["rival"]:
            if e["buyer"] > v["rival"]["p"]:
                lines.append(f"   🔻 дороже рынка: {html.escape(v['rival']['s'])} — {v['rival']['p']:g} ₽")
            else:
                lines.append(f"   🥇 дешевле всех (следующий {html.escape(v['rival']['s'])} — {v['rival']['p']:g} ₽)")
        if v["cheapest"]:
            lines.append(f"   на 1 ₽ дешевле рынка было бы {rub(v['cheapest']['lot'])} → {money(v['cheapest'])}")
        if v.get("my_src") == "FunPay" and v["my_lot"] != lot:
            lines.append(f"   ℹ️ на FunPay сейчас вижу у тебя {rub(v['my_lot'])}: бот считает по FunPay, пока лот виден там")
        return "\n".join(lines)
    if any(k in t for k in ("мои", "моя", "/my", "маржа", "заработ")):
        return report_mine(cfg, st, lots)
    if any(k in t for k in ("ртк", "ростел", "/rt", "закуп")):
        return report_rt(cfg, st, {}, notes)
    if any(k in t for k in ("рынок", "/market", "/prices", "цены", "цена")):
        return report_market(cfg, st, lots, notes) if lots else "FunPay пока не загрузился, попробуй через минуту."
    return HELP


def alerts(cfg, st, lots):
    """Тревоги по рынку: тебя подрезали / ты дешевле всех. Каждая — один раз на изменение."""
    out = []
    for d in DENOMS(cfg):
        v = market_view(cfg, st, d, lots)
        if not v["mine"] or not v["rival"]:
            continue
        me, rv = v["mine"], v["rival"]
        if me["buyer"] > rv["p"] + 0.005:
            key = f"under:{rv['s']}:{rv['p']}:{me['lot']}"
            if st["alert"].get(str(d)) != key:
                st["alert"][str(d)] = key
                c = v["cheapest"]
                out.append(f"🚨 <b>{d} TRY — у тебя не самая низкая цена</b>\n"
                           f"   {html.escape(rv['s'])}{bot_mark(cfg, rv)} — {rv['p']:g} ₽, у тебя {me['buyer']:.2f} ₽\n"
                           f"   чтобы встать на 1 ₽ дешевле: {rub(c['lot'])} в лоте → заработок {money(c)}")
        else:
            gap = rv["p"] - me["buyer"]
            key = f"top:{rv['s']}:{rv['p']}:{me['lot']}"
            if st["alert"].get(str(d)) != key:
                was_under = str(st["alert"].get(str(d), "")).startswith("under")
                st["alert"][str(d)] = key
                if was_under or gap > cfg["undercut_rub"] + 0.5:
                    extra = ""
                    if gap > cfg["undercut_rub"] + 0.5:
                        c = v["cheapest"]
                        extra = (f"\n   ты ниже рынка на {gap:.2f} ₽: можно поднять до {rub(c['lot'])} "
                                 f"(на 1 ₽ дешевле {html.escape(rv['s'])}) → заработок {money(c)} вместо {money(me)}")
                    out.append(f"🥇 <b>{d} TRY — ты самый дешёвый</b>\n"
                               f"   у тебя {me['buyer']:.2f} ₽, следующий {html.escape(rv['s'])} — {rv['p']:g} ₽{extra}")
    return out


def reply(st, ans):
    if isinstance(ans, tuple) and ans[0] == "__CSV__":
        name = "buhgalteriya_" + datetime.now(MSK).strftime("%Y-%m-%d") + ".csv"
        if not send_file(st, name, ans[1], "📒 Вся бухгалтерия — открывается в Excel / Google Таблицах"):
            send(st, "Не смог отправить файл.")
    else:
        send(st, ans)


# ---------------- главный цикл ----------------
def refresh_market(cfg, st):
    try:
        lots, n = fetch_funpay(cfg)
        if not lots:
            return None, [f"FunPay открылся, но лоты карт не найдены (элементов: {n})"]
        return lots, []
    except Exception as e:
        return None, [f"FunPay: {str(e)[:200]}"]


def main():
    st = load_state()
    for k in ("my_lot", "alert"):
        st.setdefault(k, {})
    st.setdefault("last_rt_report", 0)
    save_state(st)
    cfg, cfg_err = load_cfg()
    if cfg_err:
        send(st, "⚠️ " + html.escape(cfg_err))
        save_state(st)
        sys.exit(0)

    started = time.time()
    budget = float(cfg.get("run_minutes", 340)) * 60
    lots, notes, rt_notes = None, [], []
    next_market = 0
    first_loop = True
    pending = read_messages(st, wait=0)  # сначала узнаём, куда писать, и что ты уже спросил
    try:
        while True:
            now = time.time()
            if now >= next_market:
                new_lots, notes = refresh_market(cfg, st)
                if new_lots:
                    lots = new_lots
                    for a in alerts(cfg, st, lots):
                        send(st, a)
                elif notes and st.get("last_err") != notes[0]:
                    st["last_err"] = notes[0]
                    send(st, "⚠️ " + html.escape(notes[0]))
                next_market = now + float(cfg["market_check_min"]) * 60
            if now - st["last_rt_report"] >= float(cfg["rt_report_min"]) * 60 - 60:
                prev = dict(st["rt_cost"])
                _, rt_notes = fetch_rt(cfg, st)
                if send(st, report_rt(cfg, st, prev, rt_notes)):
                    st["last_rt_report"] = now
            if first_loop and MANUAL and lots:
                send(st, report_market(cfg, st, lots, notes))
            first_loop = False
            for text in pending:
                log("Сообщение:", text)
                reply(st, handle_text(cfg, st, text, lots or {}, notes))
            pending = []
            save_state(st)
            left = budget - (time.time() - started)
            if left <= 5:
                break
            wait = int(min(50, left - 3, max(1, next_market - time.time())))
            pending = read_messages(st, wait=max(1, wait))
            save_state(st)
        for text in pending:
            reply(st, handle_text(cfg, st, text, lots or {}, notes))
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
