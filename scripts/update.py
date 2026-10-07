"""更新黄金/白银 ETF 持仓与金银价格，并把数据内嵌进 index.html。

数据源
- GLD（SPDR Gold Trust）：官网历史档案 xlsx，含每日总吨数。
- SLV（iShares Silver Trust）：BlackRock 官网 Download 文件的"流通份额"。吨数 = 流通份额 × 固定的每股含银盎司 ÷ 32150.7466，
  每股含银盎司固定为 OZ_PER_SHARE（取 2026-10-06 官网公布的 492,910,352.50 盎司 ÷ 545,800,000 份）。
  这样每日变化只反映份额的申购/赎回，不掺入管理费造成的缓慢损耗；吨数绝对值有极小偏差。
- 价格：Yahoo Finance COMEX 黄金 GC=F、白银 SI=F 日线。

任何一个数据源失败都只告警并沿用旧文件。
"""
import csv, html, json, re, subprocess, sys, urllib.request, zipfile, io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PAGE = ROOT / "index.html"
UA = "Mozilla/5.0"
OZ_PER_TONNE = 32150.7466
OZ_PER_SHARE = 492910352.50 / 545800000   # 2026-10-06 官网 Ounces in Trust ÷ 当日流通份额
EMBED_FROM = "2016-01-01"   # 页面内嵌的起始日期（CSV 保留全部历史）

GLD_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en"
SLV_PAGE = "https://www.ishares.com/us/products/239855/ishares-silver-trust-fund"
SLV_DOC = ("https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
           "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId=239855"
           "&component=fundDownload&userType=individual")
YAHOO = "https://{host}/v8/finance/chart/{sym}?range=10y&interval=1d"


def get(url, binary=False, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
            return b if binary else b.decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            try:   # 兜底用 curl（有些网络环境下 urllib 被拦）
                b = subprocess.run(["curl", "-sL", "-m", "60", "-A", UA, url], capture_output=True, check=True).stdout
                if b:
                    return b if binary else b.decode("utf-8", errors="replace")
            except Exception as e2:
                last = e2
    raise RuntimeError(last)


def read_csv(p):
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []


def write_csv(p, head, rows):
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(head); w.writerows(rows)


# ───────── GLD ─────────
def col_idx(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def fetch_gld():
    z = zipfile.ZipFile(io.BytesIO(get(GLD_URL, binary=True)))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    q = "{%s}" % ns["m"]
    ss = [("".join(t.text or "" for t in si.iter(q + "t"))) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
    out = []
    for r in ET.fromstring(z.read("xl/worksheets/sheet2.xml")).iter(q + "row"):
        cells = {}
        for c in r.findall("m:c", ns):
            v = c.find("m:v", ns)
            if v is not None:
                cells[col_idx(c.get("r"))] = ss[int(v.text)] if c.get("t") == "s" else v.text
        try:
            d = datetime.strptime(cells[0], "%d-%b-%Y").date().isoformat()
            out.append([d, round(float(cells[1]), 4), round(float(cells[3]), 4), round(float(cells[8]), 2), round(float(cells[9]), 2)])
        except (KeyError, ValueError):
            continue    # 表头、US Holiday 等
    if len(out) < 4000:
        raise RuntimeError(f"GLD 只解析到 {len(out)} 行，疑似异常")
    return sorted(out)


def update_gld():
    rows = fetch_gld()
    write_csv(DATA / "gld.csv", ["date", "close", "nav", "ounces", "tonnes"], rows)
    print(f"GLD: {len(rows)} 天，最新 {rows[-1][0]}  {rows[-1][4]} 吨")


# ───────── SLV ─────────
def iso(s):
    return datetime.strptime(s.strip(), "%b %d, %Y").date().isoformat()


def fetch_slv_hist():
    s = get(SLV_DOC)
    m = re.search(r'<ss:Worksheet ss:Name="Historical">(.*?)</ss:Worksheet>', s, re.S)
    if not m:
        raise RuntimeError("找不到 Historical 工作表")
    out = {}
    for r in re.findall(r"<ss:Row[^>]*>(.*?)</ss:Row>", m.group(1), re.S):
        c = [html.unescape(re.sub(r"<[^>]+>", "", x)) for x in re.findall(r"<ss:Data[^>]*>.*?</ss:Data>", r, re.S)]
        if len(c) < 4 or c[0] == "As Of":
            continue
        try:
            out[iso(c[0])] = (float(c[1]), int(float(c[3])))
        except ValueError:
            continue
    if len(out) < 1000:
        raise RuntimeError(f"SLV Historical 只有 {len(out)} 行")
    return out


def update_slv():
    p = DATA / "slv.csv"
    try:
        hist = fetch_slv_hist()
    except Exception as e:
        print(f"[警告] SLV Historical 失败，沿用旧文件：{e}", file=sys.stderr); return
    write_csv(p, ["date", "nav", "shares"], [[d, hist[d][0], hist[d][1]] for d in sorted(hist)])
    print(f"SLV: {len(hist)} 天，最新 {max(hist)}")


def slv_tonnes():
    return [[r["date"], round(int(r["shares"]) * OZ_PER_SHARE / OZ_PER_TONNE, 2)] for r in read_csv(DATA / "slv.csv") if r["shares"]]


# ───────── 价格 ─────────
def update_price(fname, sym):
    p = DATA / fname
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            res = json.loads(get(YAHOO.format(host=host, sym=sym)))["chart"]["result"][0]
            break
        except Exception as e:
            err = e
    else:
        print(f"[警告] {sym} 抓取失败，沿用旧文件：{err}", file=sys.stderr); return
    off, q = res["meta"]["gmtoffset"], res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]): continue
        d = datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()
        rows.append([d] + [round(q[k][i], 2) for k in ("open", "high", "low", "close")])
    if len(rows) < 500:
        print(f"[警告] {sym} 只取到 {len(rows)} 行，沿用旧文件", file=sys.stderr); return
    seen = {}
    for r in rows:   # Yahoo 末尾常多出一条同日的盘中/新交易日报价行：同一天只保留第一条（完整日线）
        seen.setdefault(r[0], r)
    rows = list(seen.values())
    write_csv(p, ["date", "open", "high", "low", "close"], sorted(rows))
    print(f"{sym}: {len(rows)} 天，最新 {rows[-1][0]}")


# ───────── 内嵌 ─────────
def embed():
    def price(f):
        return [[r["date"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])] for r in read_csv(DATA / f) if r["date"] >= EMBED_FROM]
    gld = [[r["date"], float(r["tonnes"])] for r in read_csv(DATA / "gld.csv") if r["date"] >= EMBED_FROM]
    slv = [x for x in slv_tonnes() if x[0] >= EMBED_FROM]
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "gold": {"price": price("price_gold.csv"), "etf": gld},
            "silver": {"price": price("price_silver.csv"), "etf": slv}}
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html_ = PAGE.read_text(encoding="utf-8")
    new, n = re.subn(r'(<script id="etf-data">).*?(</script>)', lambda m: m.group(1) + "window.ETF_DATA=" + blob + ";" + m.group(2), html_, count=1, flags=re.S)
    if not n:
        sys.exit("index.html 里没有 etf-data 标签")
    PAGE.write_text(new, encoding="utf-8")
    print(f"已写入 index.html（{len(blob) // 1024} KB）")


def main():
    DATA.mkdir(exist_ok=True)
    for fn, args in ((update_gld, ()), (update_slv, ()), (update_price, ("price_gold.csv", "GC=F")), (update_price, ("price_silver.csv", "SI=F"))):
        try:
            fn(*args)
        except Exception as e:
            print(f"[警告] {fn.__name__} 失败，沿用旧数据：{e}", file=sys.stderr)
    embed()


if __name__ == "__main__":
    main()
