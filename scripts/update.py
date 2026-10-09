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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PAGE = ROOT / "index.html"
UA = "Mozilla/5.0"
OZ_PER_TONNE = 32150.7466
OZ_PER_SHARE = 492910352.50 / 545800000   # 2026-10-06 官网 Ounces in Trust ÷ 当日流通份额
REBUILD_SILVER = "--rebuild-silver" in sys.argv
EMBED_FROM = "2016-01-01"   # 页面内嵌的起始日期（CSV 保留全部历史）

GLD_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en"
# 官网 GLD 页面"Trust Information"用的实时接口：当个交易日的总吨数美东 16:45 前后就更新（北京次日凌晨），
# 比历史档案 xlsx 早很多（xlsx 要等收盘价、成交量等都出齐，约美东 22:30 即北京次日 10:30 之后才有这一天）。
GLD_LIVE_URL = "https://api.spdrgoldshares.com/api/v1/data?product=gld&exchange=NYSE&lang=en"
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


def num(x):
    return float(re.sub(r"[^0-9.\-]", "", x))


def fetch_gld_live():
    """官网页面接口 -> [日期, 收盘价, 每股净值, 总盎司, 总吨数]；日期取总吨数那一项自带的日期。"""
    d = json.loads(get(GLD_LIVE_URL))["data"]
    day = datetime.strptime(d["total_tonnes"]["date"], "%B %d, %Y").date().isoformat()
    return [day, round(num(d["close_usd"]["value"]), 4), round(num(d["nav_share_usd"]["value"]), 4),
            round(num(d["total_ounces"]["value"]), 2), round(num(d["total_tonnes"]["value"]), 2)]


def update_gld():
    rows = fetch_gld()
    try:
        live = fetch_gld_live()
        if live[0] > rows[-1][0]:      # 档案里还没有这一天：先用页面接口的数据补上；档案之后更新了会自动覆盖
            rows.append(live)
            print(f"GLD 页面接口比历史档案新：补入 {live[0]}  {live[4]} 吨")
    except Exception as e:
        print(f"[警告] GLD 页面接口失败（只用历史档案）：{e}", file=sys.stderr)
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
MONTH_CODES = "FGHJKMNQUVXZ"   # 1~12 月的期货月份代码
OVERWRITE_DAYS = 7              # 白银每次只覆盖最近这几天


def yahoo_rows(sym, rng="10y", with_volume=False):
    """Yahoo 日线 -> [[日期, 开, 高, 低, 收(, 量)], ...]；同一天只保留第一条（末尾常多一条盘中/新交易日报价行）。"""
    err = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            res = json.loads(get(f"https://{host}/v8/finance/chart/{sym}?range={rng}&interval=1d"))["chart"]["result"]
            if not res:
                raise ValueError("Yahoo 没有这个代码的数据")
            res = res[0]
            break
        except Exception as e:
            err = e
    else:
        raise RuntimeError(err)
    off, q = res["meta"]["gmtoffset"], res["indicators"]["quote"][0]
    seen = {}
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]): continue
        d = datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()
        row = [d] + [round(q[k][i], 2) for k in ("open", "high", "low", "close")]
        if with_volume: row.append(q["volume"][i] or 0)
        seen.setdefault(d, row)
    return sorted(seen.values())


def yahoo_sessions(sym, rng="7d"):
    """用 Yahoo 小时线合成"交易日"：COMEX 期货每个交易日从前一天美东 18:00 到当天 17:00。
    返回按日期排序的 [(日期, [开, 高, 低, 收], 小时线根数)]。"""
    err = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            res = json.loads(get(f"https://{host}/v8/finance/chart/{sym}?range={rng}&interval=1h"))["chart"]["result"]
            if not res:
                raise ValueError("Yahoo 没有这个代码的小时线")
            res = res[0]
            break
        except Exception as e:
            err = e
    else:
        raise RuntimeError(err)
    off, q = res["meta"]["gmtoffset"], res["indicators"]["quote"][0]
    sess = {}
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]): continue
        et = datetime.fromtimestamp(t + off, timezone.utc)            # 交易所本地时间
        day = (et + timedelta(hours=6)).date()                        # 18:00 起算下一个交易日
        if day.weekday() >= 5: continue                               # 周五 18:00 之后到周日 18:00 休市
        r = sess.get(day.isoformat())
        if r is None:
            sess[day.isoformat()] = [q["open"][i], q["high"][i], q["low"][i], q["close"][i], 1]
        else:
            r[1] = max(r[1], q["high"][i]); r[2] = min(r[2], q["low"][i]); r[3] = q["close"][i]; r[4] += 1
    return [(d, [round(x, 2) for x in v[:4]], v[4]) for d, v in sorted(sess.items())]


def fix_latest_rows(rows, sym):
    """Yahoo 日线有个毛病：美东 18:00 新交易日开盘之后到午夜之前，最后一根日线是"新交易日刚开盘的几个小时"，
    却贴着刚结束那一天的日期，把那天完整的日线顶替掉了（北京时间约 06:00~12:00 抓取就会撞上）。
    这里用小时线合成的交易日来识别并修复：某天的日线若与"下一个交易日"的小时线合成结果一致，就判定被污染，
    改用该日自己的小时线合成值；日线里缺失的完整交易日也用小时线补上。小时线抓不到时原样返回。"""
    try:
        sess = yahoo_sessions(sym)
    except Exception as e:
        print(f"[警告] {sym} 小时线抓取失败，最后一根日线可能不准：{e}", file=sys.stderr)
        return rows
    by = {r[0]: list(r) for r in rows}
    close = lambda a, b: all(abs(x - y) <= 0.011 for x, y in zip(a, b))
    fixed = []
    for i, (d, ohlc, n) in enumerate(sess):
        row = by.get(d)
        nxt = sess[i + 1][1] if i + 1 < len(sess) else None
        corrupted = bool(row and nxt and close(row[1:4], nxt[:3]))
        if (corrupted or row is None) and n >= 20:
            by[d] = [d] + ohlc
            fixed.append(d)
        elif corrupted:
            del by[d]
    if fixed:
        print(f"{sym}: 用小时线修复了 {len(fixed)} 根被污染/缺失的日线 {fixed}")
    return [by[k] for k in sorted(by)]


def update_price(fname, sym):
    p = DATA / fname
    try:
        rows = yahoo_rows(sym)
    except Exception as e:
        print(f"[警告] {sym} 抓取失败，沿用旧文件：{e}", file=sys.stderr); return
    if len(rows) < 500:
        print(f"[警告] {sym} 只取到 {len(rows)} 行，沿用旧文件", file=sys.stderr); return
    rows = fix_latest_rows(rows, sym)
    write_csv(p, ["date", "open", "high", "low", "close"], rows)
    print(f"{sym}: {len(rows)} 天，最新 {rows[-1][0]}")


def pick_silver_contract():
    """在未来 14 个月的白银合约里，挑最近 10 个交易日成交量最大的一个。"""
    today = datetime.now(timezone.utc).date()
    best, best_vol = None, -1
    for k in range(0, 14):
        y, m = divmod(today.year * 12 + today.month - 1 + k, 12)
        sym = f"SI{MONTH_CODES[m]}{str(y)[-2:]}.CMX"
        try:
            rows = yahoo_rows(sym, "1mo", with_volume=True)
        except Exception:
            continue                   # 这个月份没有合约，跳过
        vol = sum(r[5] for r in rows[-10:])
        if vol > best_vol:
            best, best_vol = sym, vol
    if not best or best_vol <= 0:
        raise RuntimeError("没有找到有成交量的白银合约")
    print(f"白银主力合约：{best}（最近 10 日成交量 {best_vol}）")
    return best


def update_silver_price(rebuild=False):
    """白银：Yahoo 的 SI=F（近月连续）在非主力月份有大量 O=H=L=C 的一字线，所以改用成交量最大的具体合约。
    每次只覆盖最近 7 天并补新日期，已存历史不改（主力换月当天新旧合约有一点价差，不做复权）。
    首次迁移用 --rebuild-silver：所选合约"最后一根一字线"之前的日期保留原有 SI=F 行。"""
    p = DATA / "price_silver.csv"
    try:
        sym = pick_silver_contract()
        raw = yahoo_rows(sym, "2y", with_volume=True)
    except Exception as e:
        print(f"[警告] 白银主力合约抓取失败，沿用旧文件：{e}", file=sys.stderr); return
    last_flat = max((i for i, r in enumerate(raw) if r[1] == r[2] == r[3] == r[4]), default=-1)
    new = fix_latest_rows([r[:5] for r in raw[last_flat + 1:]], sym)
    if len(new) < 100:
        print(f"[警告] {sym} 可用数据只有 {len(new)} 行，沿用旧文件", file=sys.stderr); return
    old = [[r["date"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])] for r in read_csv(p)]
    if rebuild or not old:
        merged = [r for r in old if r[0] < new[0][0]] + new
    else:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=OVERWRITE_DAYS)).isoformat()
        have = {r[0]: r for r in old}
        for r in new:
            if r[0] >= cutoff or r[0] not in have:
                have[r[0]] = r
        merged = sorted(have.values())
    write_csv(p, ["date", "open", "high", "low", "close"], merged)
    print(f"{sym}: {len(merged)} 天，最新 {merged[-1][0]}")


# ───────── 内嵌 ─────────
def log_seen():
    """记录官网"首次看到新数据"的时间（用来摸清官网每天几点更新）。"""
    p = DATA / "seen_log.csv"
    rows = read_csv(p)
    last = {}
    for r in rows:
        last[r["source"]] = max(last.get(r["source"], ""), r["data_date"])
    now = datetime.now(timezone.utc)
    bj = now.astimezone(timezone(timedelta(hours=8)))
    for src, f in (("GLD", "gld.csv"), ("SLV", "slv.csv")):
        rs = read_csv(DATA / f)
        if not rs:
            continue
        latest = max(r["date"] for r in rs)
        if latest > last.get(src, ""):
            rows.append({"source": src, "data_date": latest, "first_seen_utc": now.strftime("%Y-%m-%d %H:%M"),
                         "first_seen_beijing": bj.strftime("%Y-%m-%d %H:%M"),
                         "note": "" if last.get(src) else "初始记录（非首次看到时间）"})
            print(f"{src} 新数据 {latest}，首次看到 北京时间 {bj:%Y-%m-%d %H:%M}")
    write_csv(p, ["source", "data_date", "first_seen_utc", "first_seen_beijing", "note"],
              [[r["source"], r["data_date"], r["first_seen_utc"], r["first_seen_beijing"], r.get("note", "")] for r in rows])


def embed():
    def price(f):
        return [[r["date"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])] for r in read_csv(DATA / f) if r["date"] >= EMBED_FROM]
    gld = [[r["date"], float(r["tonnes"])] for r in read_csv(DATA / "gld.csv") if r["date"] >= EMBED_FROM]
    slv = [x for x in slv_tonnes() if x[0] >= EMBED_FROM]
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "gold": {"price": price("price_gold.csv"), "etf": gld},
            "silver": {"price": price("price_silver.csv"), "etf": slv}}
    html_ = PAGE.read_text(encoding="utf-8")
    # 数据没变化时保持原来的"更新于"时间，避免每次定时运行都产生无意义的提交
    m0 = re.search(r'<script id="etf-data">window[.]ETF_DATA=(.*?);</script>', html_, re.S)
    old = json.loads(m0.group(1)) if m0 and m0.group(1) != "null" else None
    strip = lambda d: {k: v for k, v in d.items() if k != "updated"}
    data["updated"] = old["updated"] if old and strip(old) == strip(data) else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    new, n = re.subn(r'(<script id="etf-data">).*?(</script>)', lambda m: m.group(1) + "window.ETF_DATA=" + blob + ";" + m.group(2), html_, count=1, flags=re.S)
    if not n:
        sys.exit("index.html 里没有 etf-data 标签")
    PAGE.write_text(new, encoding="utf-8")
    print(f"已写入 index.html（{len(blob) // 1024} KB）")


def main():
    DATA.mkdir(exist_ok=True)
    for fn, args in ((update_gld, ()), (update_slv, ()), (update_price, ("price_gold.csv", "GC=F")), (update_silver_price, (REBUILD_SILVER,))):
        try:
            fn(*args)
        except Exception as e:
            print(f"[警告] {fn.__name__} 失败，沿用旧数据：{e}", file=sys.stderr)
    log_seen()
    embed()


if __name__ == "__main__":
    main()
