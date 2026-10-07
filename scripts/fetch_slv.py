"""iShares Silver Trust (SLV) 持仓数据 -> data/slv.csv

列：date, nav, shares, ounces
- nav / shares：BlackRock 官网"Download"文件的 Historical 表（2006-04-21 起的完整日线）
- ounces：产品页"Ounces in Trust"（信托持银盎司数）。官网只给最新一天，所以从首次运行起每天记录一次；
  更早的日期留空（用 shares × 每股含银量估算，见 README）。
已有的 ounces 记录永远保留；抓取失败只告警、保留旧文件。
"""
import csv, html, re, sys, urllib.request
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "slv.csv"
PAGE = "https://www.ishares.com/us/products/239855/ishares-silver-trust-fund"
DOC = ("https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document"
       "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId=239855"
       "&component=fundDownload&userType=individual")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def iso(s):  # "Oct 06, 2026" -> 2026-10-06
    return datetime.strptime(s.strip(), "%b %d, %Y").date().isoformat()


def historical():
    s = get(DOC)
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
        raise RuntimeError(f"Historical 只有 {len(out)} 行，疑似异常")
    return out


def ounces_today():
    s = get(PAGE)
    v = re.search(r'webqc-datapoint="keyFundFacts-ounces">([\d,\.]+)<', s)
    d = re.search(r'keyFundFacts-ounces-asOf"[^>]*>as of(?:<!-- -->| )+([A-Z][a-z]{2} \d{2}, \d{4})', s)
    if not (v and d):
        raise RuntimeError("页面里找不到 Ounces in Trust")
    return iso(d.group(1)), float(v.group(1).replace(",", ""))


def main():
    old = {}
    if OUT.exists():
        for r in csv.DictReader(OUT.open(encoding="utf-8")):
            old[r["date"]] = r
    try:
        hist = historical()
    except Exception as e:
        print(f"[警告] Historical 抓取失败：{e}", file=sys.stderr)
        hist = {d: (float(r["nav"]), int(r["shares"])) for d, r in old.items() if r["nav"]}
    oz = {d: r["ounces"] for d, r in old.items() if r.get("ounces")}
    try:
        d, v = ounces_today()
        oz[d] = f"{v:.2f}"
        print(f"ounces {d}: {v:,.2f}")
    except Exception as e:
        print(f"[警告] 盎司数抓取失败：{e}", file=sys.stderr)
    if not hist:
        sys.exit("没有任何数据，保留旧文件")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "nav", "shares", "ounces"])
        for d in sorted(set(hist) | set(oz)):
            n, sh = hist.get(d, ("", ""))
            w.writerow([d, n, sh, oz.get(d, "")])
    print(f"written {OUT}: {len(hist)} days, latest {max(hist)}, ounces records {len(oz)}")


if __name__ == "__main__":
    main()
