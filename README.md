# 黄金 / 白银 ETF 持仓看板

黄金 ETF（SPDR Gold Trust, GLD）与白银 ETF（iShares Silver Trust, SLV）的持仓吨数、本周累计变化、每日变化，
配合 COMEX 金银日线联动显示。数据来自两家官网，每个美股交易日收盘后由 GitHub Actions 自动更新。

- `scripts/update.py`：抓取数据、写入 `data/*.csv`、把数据内嵌进 `index.html`
- 白银吨数 = 流通份额 × 每股含银盎司 ÷ 32150.7466；官网只公布最新一天的真实盎司数，脚本每天记录一条（`data/slv.csv` 的 `ounces` 列）
- 页面：GitHub Pages，分支 main、根目录
