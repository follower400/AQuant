"""临时脚本：验证新浪个股日线 + 行业成分股接口（用完即删）"""
import time

import akshare as ak

print("=== 1) 新浪个股日线 stock_zh_a_daily(symbol='sz000001') ===")
try:
    k = ak.stock_zh_a_daily(symbol="sz000001", start_date="20260801",
                            end_date="20260821", adjust="qfq")
    print("columns:", list(k.columns), "| rows:", len(k))
    print(k.tail(3).to_string())
except Exception as e:
    print("FAILED:", type(e).__name__, str(e)[:100])

time.sleep(2)

print("=== 2) 新浪行业成分股 stock_sector_detail(sector='hangye_ZA01') ===")
try:
    detail = ak.stock_sector_detail(sector="hangye_ZA01")
    print("columns:", list(detail.columns), "| rows:", len(detail))
    print(detail.head(5).to_string())
except Exception as e:
    print("FAILED:", type(e).__name__, str(e)[:100])

time.sleep(2)

print("=== 3) 新浪行业列表中含电子/计算机/通信的板块 ===")
try:
    sectors = ak.stock_sector_spot(indicator="行业")
    for _, row in sectors.iterrows():
        name = str(row["板块"])
        if any(k in name for k in ("电子", "计算机", "通信", "电力", "传媒")):
            print(row["label"], name)
except Exception as e:
    print("FAILED:", type(e).__name__, str(e)[:100])
