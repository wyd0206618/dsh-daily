# -*- coding: utf-8 -*-
"""云端现抓 cloud_now.py

为什么有这个文件：
    本地版本需要电脑开机。人出门在外时电脑通常关着，就断了。
    这个版本不依赖任何本地数据文件，全部走公开 HTTP 接口，
    可以放在 GitHub Actions / 任意云函数上定时跑，结果直接发邮件。

依赖：
    标准库 + akshare（仅用于百度经济日历）
    pip install akshare

环境变量（邮件，缺一不可）：
    MAIL_USER   登录账号，如 68239305@qq.com
    MAIL_PASS   SMTP 授权码（不是登录密码）
    MAIL_TO     收件人，多个用逗号分隔；留空则发给 MAIL_USER 自己
    MAIL_HOST   默认 smtp.qq.com

用法：
    python cloud_now.py            # 抓一次并打印，不发邮件
    python cloud_now.py --send     # 抓一次并发送邮件
"""
import json
import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# 品种 → (新浪期货代码, 中文名, 方向符号说明)
FUT = {
    "黄金": ("GC", +1),
    "原油": ("CL", +1),
    "纳指": ("NQ", +1),
    "道指": ("YM", +1),
    "白银": ("SI", +1),
}


def _get(url, timeout=20, decode="utf-8"):
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                               timeout=timeout, context=CTX)
    return r.read().decode(decode, "ignore")


# ---------------- 价格 ----------------
def kline(sym):
    """新浪期货日K，返回 [(date, close), ...]。"""
    u = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/"
         f"GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}")
    b = _get(u)
    m = re.search(r"\((\[.*\])\)", b, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for x in arr:
        try:
            out.append((x["date"], float(x["close"])))
        except Exception:
            continue
    return out


def live(sym):
    """新浪实时价。"""
    b = _get(f"https://hq.sinajs.cn/list=hf_{sym}", decode="gbk")
    v = b.split('"')[1] if '"' in b else ""
    if not v:
        return None
    f = v.split(",")
    try:
        return {"cur": float(f[0]), "high": float(f[4]), "low": float(f[5]),
                "prev": float(f[7])}
    except Exception:
        return None


def btc_kline():
    """比特币日K用新浪外汇接口（hf_BTC 只有实时）。"""
    u = ("https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_x=/"
         "NewForexService.getDayKLine?symbol=fx_sbtcusd")
    try:
        b = _get(u)
        body = b[b.find("(") + 1:b.rfind(")")].strip().strip('"')
        out = []
        for seg in body.split("|"):
            p = seg.split(",")
            if len(p) >= 5 and re.match(r"\d{4}-\d{2}-\d{2}", p[0]):
                try:
                    out.append((p[0], float(p[4])))
                except Exception:
                    pass
        return out
    except Exception:
        return []


def price_block():
    R = ["■ 价格与位置（20 日变化 + 距 20 日均线）"]
    for name, (code, _) in FUT.items():
        k = kline(code)
        L = live(code)
        if not k:
            R.append(f"　{name}：日K 取不到")
            continue
        closes = [c for _, c in k]
        cur = L["cur"] if L else closes[-1]
        chg20 = (cur / closes[-21] - 1) * 100 if len(closes) > 21 else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        dev = (cur / ma20 - 1) * 100 if ma20 else None
        s = f"　{name:<4}现价 {cur:>10,.2f}"
        if chg20 is not None:
            s += f"　20日 {chg20:>+6.1f}%"
        if dev is not None:
            s += f"　距MA20 {dev:>+6.2f}%"
        if L:
            s += f"　日内 {(L['cur']/L['prev']-1)*100:>+5.1f}%"
        R.append(s)
    # 比特币
    k = btc_kline()
    L = live("BTC")
    if k:
        closes = [c for _, c in k]
        cur = L["cur"] if L else closes[-1]
        chg20 = (cur / closes[-21] - 1) * 100 if len(closes) > 21 else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        s = f"　{'比特币':<4}现价 {cur:>10,.0f}"
        if chg20 is not None:
            s += f"　20日 {chg20:>+6.1f}%"
        if ma20:
            s += f"　距MA20 {(cur/ma20-1)*100:>+6.2f}%"
        if L:
            s += f"　日内 {(L['cur']/L['prev']-1)*100:>+5.1f}%"
        R.append(s)
    return R


# ---------------- 日历 ----------------
KEY_EV = ("PCE", "CPI", "PPI", "GDP", "非农", "失业", "初请", "续请", "零售",
          "耐用品", "个人收入", "个人支出", "ISM", "ADP", "工业产出", "职位空缺",
          "EIA", "API", "库存", "库欣", "消费者信心", "PMI", "成屋", "新屋")


def _num(x):
    try:
        if x is None:
            return None
        s = str(x).replace(",", "").replace("%", "").strip()
        if s in ("", "nan", "None", "--"):
            return None
        return float(s)
    except Exception:
        return None


def calendar_block():
    try:
        import akshare as ak
        import contextlib
        import io
        import time
        buf = io.StringIO()
        today = datetime.now().strftime("%Y%m%d")
        df = None
        # 百度会限流，重试 3 次，退避 5/15 秒
        for k in range(3):
            try:
                with contextlib.redirect_stderr(buf):
                    df = ak.news_economic_baidu(today)
                if df is not None and len(df):
                    break
            except Exception:
                df = None
            if k < 2:
                time.sleep(5 * (k + 1))
        if df is None or not len(df):
            return ["■ 今日美国数据：无记录"]
        df = df[(df["地区"] == "美国") & (df["重要性"] <= 2)]
        df = df[df["事件"].astype(str).apply(lambda x: any(k in x for k in KEY_EV))]
        if not len(df):
            return ["■ 今日美国数据：无重要事件"]
        rows = []
        for _, r in df.iterrows():
            a, f_ = _num(r["公布"]), _num(r["预期"])
            if a is None:
                continue
            judge = "—"
            if f_ is not None and f_ != 0:
                judge = "超预期" if a > f_ else ("不及预期" if a < f_ else "符合")
            rows.append((str(r["时间"])[:5], str(r["事件"])[:26], a, f_, judge))
        if not rows:
            return ["■ 今日美国数据：尚未公布"]
        R = [f"■ 今日美国数据（百度日历，已公布 {len(rows)} 条）"]
        for t, ev, a, f_, j in rows[:12]:
            fs = f"{f_:g}" if f_ is not None else "—"
            R.append(f"　{t} {ev:<28}实际 {a:>8.2f}　预期 {fs:>7}　{j}")
        return R
    except Exception as e:
        # 百度限流时的兜底：从新闻里抽当天数据
        R = [f"■ 今日美国数据（百度不可用，从新闻抽取）"]
        try:
            got = _econ_from_news()
        except Exception:
            got = []
        if got:
            R += got
        else:
            R.append("　日历与新闻均未取到，稍后重试")
        return R


def _econ_from_news():
    """从见闻快讯里抽取「指标 实际 X%，预期 Y%」结构。"""
    d = None
    for u in LIVE_APIS:
        try:
            b = _get(u, decode="utf-8")
            d = json.loads(b)
            if (d.get("data") or {}).get("items"):
                break
            d = None
        except Exception:
            d = None
    if not d:
        return []
    items = (d["data"] or {}).get("items") or []
    R = []
    pat = re.compile(r"(.{4,42}?)\s*(-?\d+(?:\.\d+)?)\s*(%|万人|万)?"
                     r"[^\d，,]{0,4}[，,]\s*预期\s*(-?\d+(?:\.\d+)?)")
    for it in items:
        txt = str(it.get("content_text") or "").replace("\n", " ")
        ts = it.get("display_time") or it.get("created_at")
        t_obj = None
        try:
            t_obj = datetime.fromtimestamp(int(ts)) if ts else None
        except Exception:
            t_obj = None
        if not any(k in txt for k in KEY_EV):
            continue
        # 只取近 6 小时
        if t_obj and (datetime.now() - t_obj).total_seconds() > 6 * 3600:
            continue
        m = pat.search(txt)
        if not m:
            continue
        try:
            a = float(m.group(2))
            f_ = float(m.group(4))
        except Exception:
            continue
        j = "超预期" if a > f_ else ("不及预期" if a < f_ else "符合")
        ts = it.get("display_time") or it.get("created_at")
        try:
            t = datetime.fromtimestamp(int(ts)).strftime("%H:%M")
        except Exception:
            t = "--:--"
        R.append(f"　{t} {m.group(1)[:26]:<28}实际 {a:>8.2f}　预期 {f_:>7g}　{j}")
        if len(R) >= 12:
            break
    return R


# ---------------- 新闻 ----------------
LIVE_APIS = [
    "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=100",
    "https://api-one.wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=100",
    "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=100",
]


def news_block(n=8):
    d = None
    for u in LIVE_APIS:
        try:
            b = _get(u, decode="utf-8")
            d = json.loads(b)
            if (d.get("data") or {}).get("items"):
                break
            d = None
        except Exception:
            d = None
    if not d:
        return ["■ 新闻抓取失败（所有域名）"]
    items = (d["data"] or {}).get("items") or []
    R = [f"■ 近 3 小时快讯（共 {len(items)} 条，取重点）"]
    shown = 0
    for it in items:
        txt = str(it.get("content_text") or it.get("title") or "").replace("\n", " ")
        ts = it.get("display_time") or it.get("created_at")
        try:
            t = datetime.fromtimestamp(int(ts))
        except Exception:
            t = None
        if t and (datetime.now() - t).total_seconds() > 3 * 3600:
            continue
        # 只要含方向的实质内容
        if not any(k in txt for k in ("加息", "降息", "通胀", "PCE", "CPI", "GDP",
                                      "黄金", "原油", "比特币", "美元", "收益率",
                                      "美联储", "英央行", "欧洲央行")):
            continue
        R.append(f"　{t:%m-%d %H:%M} {txt[:96]}" if t else f"　{txt[:96]}")
        shown += 1
        if shown >= n:
            break
    if not shown:
        R.append("　（近 3 小时无相关快讯）")
    return R


# ---------------- 六品种偏向（简化版，只用云端可得数据） ----------------
def bias_block():
    """用 20 日变化判断方向。全部来自公开接口，无本地文件。"""
    R = ["■ 六品种偏向（20 日趋势，非预测）"]
    # 美元用新浪 DINIW
    dxy_chg = None
    try:
        b = _get("https://hq.sinajs.cn/list=DINIW", decode="gbk")
        v = b.split('"')[1].split(",")
        dxy_chg = float(v[4]) if len(v) > 4 else None
    except Exception:
        pass
    for name, (code, _) in list(FUT.items()) + [("比特币", ("BTC", 0))]:
        k = btc_kline() if code == "BTC" else kline(code)
        if not k or len(k) < 21:
            R.append(f"　{name}：数据不足")
            continue
        closes = [c for _, c in k]
        chg20 = (closes[-1] / closes[-21] - 1) * 100
        lab = "偏多" if chg20 > 2 else ("偏空" if chg20 < -2 else "中性")
        R.append(f"　{name:<4}20日 {chg20:>+6.1f}%　→ {lab}")
    R.append("　注意：这是「已走出来的趋势」，不是买卖建议")
    R.append("　六链完整版需本地数据，云端只做趋势方向")
    return R


def build():
    now = datetime.now()
    R = [f"北京时间 {now:%Y-%m-%d %H:%M}　云端快照", ""]
    for fn in (calendar_block, price_block, news_block, bias_block):
        try:
            R += fn() + [""]
        except Exception as e:
            R.append(f"■ {fn.__name__} 失败：{type(e).__name__}")
            R.append("")
    R.append("数据源：百度日历 / 新浪期货日K / 新浪实时价 / 见闻快讯")
    return R


# ---------------- 邮件 ----------------
def to_html(lines):
    body = "\n".join(lines).replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>云端快照</title></head>
<body style="margin:0;padding:12px;background:#111;color:#e8e8e8;
font-family:-apple-system,'PingFang SC',sans-serif;font-size:13px;line-height:1.7">
<pre style="white-space:pre-wrap;word-break:break-all;margin:0;font-family:inherit">{body}</pre>
</body></html>"""


def send(lines):
    user = os.environ.get("MAIL_USER")
    pwd = os.environ.get("MAIL_PASS")
    to = os.environ.get("MAIL_TO") or user
    host = os.environ.get("MAIL_HOST", "smtp.qq.com")
    if not (user and pwd):
        print("缺 MAIL_USER / MAIL_PASS，无法发信")
        return False
    import smtplib
    from email.message import EmailMessage
    m = EmailMessage()
    m["Subject"] = f"云端快照 {datetime.now():%m-%d %H:%M}"
    m["From"] = user
    m["To"] = to
    m.set_content("\n".join(lines))
    m.add_alternative(to_html(lines), subtype="html")
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=30, context=CTX) as s:
            s.login(user, pwd)
            s.send_message(m)
        print(f"已发送 → {to}")
        return True
    except Exception as e:
        print(f"发送失败：{type(e).__name__} {str(e)[:80]}")
        return False


if __name__ == "__main__":
    lines = build()
    print("\n".join(lines))
    if "--send" in sys.argv:
        print()
        send(lines)
