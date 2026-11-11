"""每天盤前把漲停寶寶區需要的原始資料抓進倉庫（feed/），讓排程不必在執行時上網。
只存公開資料：us-link、工商時報 RSS、經濟日報證券清單、候選股日線與三線均線。"""
import json, re, time, html, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

SITE = "https://glistening-gumdrop-6e4dc9.netlify.app/.netlify/functions"
TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (pp-us-link feed bot)"}
errors = []

def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(5 * (i + 1))
    errors.append(f"{url}: {last}")
    return None

def nc():
    return str(int(time.time() * 1000))

# 1. us-link
us_link = None
raw = get(f"{SITE}/us-link?nocache={nc()}")
if raw:
    try:
        us_link = json.loads(raw)
    except Exception as e:
        errors.append(f"us-link json: {e}")

# 2. 工商時報 RSS
ctee = []
raw = get("https://www.ctee.com.tw/rss_web/livenews/stock")
if raw:
    try:
        root = ET.fromstring(raw.encode("utf-8"))
        for it in root.iter("item"):
            g = lambda t: (it.findtext(t) or "").strip()
            desc = re.sub(r"<[^>]+>", "", html.unescape(g("description")))
            ctee.append({"title": g("title"), "time": g("pubDate"), "url": g("link"), "desc": desc[:600]})
    except Exception as e:
        errors.append(f"ctee rss: {e}")

# 3. 經濟日報證券分類頁
udn, seen = [], set()
for cate in ("5590", "5710"):
    raw = get(f"https://money.udn.com/money/cate/{cate}")
    if not raw:
        continue
    for m in re.finditer(r'<a[^>]+href="([^"]*?/money/story/\d+/\d+)[^"]*"[^>]*>(.*?)</a>', raw, re.S):
        url, title = m.group(1), re.sub(r"<[^>]+>|\s+", " ", html.unescape(m.group(2))).strip()
        if url.startswith("/"):
            url = "https://money.udn.com" + url
        if len(title) < 8 or url in seen:
            continue
        seen.add(url)
        tm = re.search(re.escape(m.group(0)) + r".{0,400}?(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", raw, re.S)
        udn.append({"cate": cate, "title": title, "url": url, "time": tm.group(1) if tm else ""})

# 4. 上市櫃名稱對照，用來把新聞標題對到代號
names = {}
for url, kc, kn in (("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", "Code", "Name"),
                    ("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes", "SecuritiesCompanyCode", "CompanyName")):
    raw = get(url)
    if raw:
        try:
            for r in json.loads(raw):
                c, n = str(r.get(kc, "")).strip(), str(r.get(kn, "")).strip()
                if re.fullmatch(r"\d{4}", c) and len(n) >= 2:
                    names[n] = c
        except Exception as e:
            errors.append(f"names {url}: {e}")

def codes_in(text):
    found = set(re.findall(r"[（(](\d{4})[）)]", text))
    for n, c in names.items():
        if n in text:
            found.add(c)
        elif n.endswith("-KY") and n[:-3] in text:
            found.add(c)
    return found

for it in ctee:
    it["codes"] = sorted(codes_in(it["title"] + " " + it["desc"]))
for it in udn:
    it["codes"] = sorted(codes_in(it["title"]))

# 5. 候選股＝新聞點名 ∪ us-link 族群台股
cand = set()
for it in ctee + udn:
    cand.update(it["codes"])
for cat in (us_link or {}).get("cats", []):
    for t in cat.get("tw", []):
        if re.fullmatch(r"\d{4}", str(t.get("code", ""))):
            cand.add(str(t["code"]))

row_re = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.,]+)")
stocks = {}
for code in sorted(cand):
    raw = get(f"{SITE}/stock?code={code}&fmt=text&months=2&nocache={nc()}", tries=2)
    if not raw:
        continue
    rows = [m.groups() for m in (row_re.match(l.strip()) for l in raw.splitlines()) if m]
    rows.sort(key=lambda r: r[0], reverse=True)  # 新到舊
    rows = rows[:21]
    if len(rows) < 20:
        stocks[code] = {"error": f"只有 {len(rows)} 列"}
        continue
    closes = [float(r[4]) for r in rows]
    ma = lambda n: round(sum(closes[:n]) / n, 2)
    m5, m10, m20 = ma(5), ma(10), ma(20)
    top, close = max(m5, m10, m20), closes[0]
    line = "站上三線" if close > top else ("待突破" if top <= close * 1.08 else "離三線太遠")
    stocks[code] = {"date": rows[0][0], "close": close, "chg": round((closes[0] / closes[1] - 1) * 100, 2),
                    "vol": rows[0][5], "ma": {"ma5": m5, "ma10": m10, "ma20": m20}, "top": top,
                    "gapPct": round((top / close - 1) * 100, 2), "line": line, "rows": [" ".join(r) for r in rows]}
    time.sleep(0.3)

out = {"generated": datetime.now(TPE).strftime("%Y-%m-%dT%H:%M"),
       "usDate": sorted({u.get("date") for u in (us_link or {}).get("us", []) if u.get("date")})[-1:] or None,
       "us_link": us_link, "ctee": ctee, "udn": udn, "stocks": stocks, "errors": errors}
import os
os.makedirs("feed", exist_ok=True)
json.dump(out, open("feed/zt_input.json", "w"), ensure_ascii=False, indent=1)
print(f"us-link {'ok' if us_link else 'FAIL'} | ctee {len(ctee)} | udn {len(udn)} | stocks {len(stocks)} | errors {len(errors)}")
