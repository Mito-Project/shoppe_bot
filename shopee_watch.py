"""
Theo dõi giá sản phẩm Shopee và báo về Telegram khi giá <= mức mong muốn.
Chạy được trên GitHub Actions (dùng cookie) hoặc trên máy tính cá nhân.

  python shopee_watch.py            # kiểm tra giá
  python shopee_watch.py --report   # kiểm tra + gửi giá hiện tại về Telegram (để chạy thử)
  python shopee_watch.py --test     # chỉ gửi tin nhắn thử về Telegram
  python shopee_watch.py --headed   # hiện cửa sổ trình duyệt
  python shopee_watch.py --login    # (máy cá nhân) đăng nhập Shopee 1 lần, không cần cookie
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"
COOKIE_FILE = BASE / "cookies.txt"      # tùy chọn khi chạy ở máy cá nhân
PROFILE_DIR = BASE / "shopee_profile"

PRICE_RE = re.compile(r"₫\s?(\d{1,3}(?:\.\d{3})+|\d+)")
SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]
CONTEXT_OPTS = dict(
    locale="vi-VN",
    timezone_id="Asia/Ho_Chi_Minh",
    viewport={"width": 1280, "height": 900},
    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
)


# ---------- tiện ích ----------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt_vnd(n):
    return f"{n:,.0f}".replace(",", ".") + "đ"


def load_config():
    cfg = load_json(CONFIG_FILE, None)
    if not cfg:
        sys.exit(f"Không đọc được {CONFIG_FILE}.")
    cfg["telegram_token"] = os.getenv("TELEGRAM_TOKEN") or cfg.get("telegram_token", "")
    cfg["chat_id"] = os.getenv("TELEGRAM_CHAT_ID") or str(cfg.get("chat_id", ""))
    if not cfg["telegram_token"] or not cfg["chat_id"] or "DIEN_" in cfg["telegram_token"] + cfg["chat_id"]:
        sys.exit("Thiếu TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (kiểm tra Secrets hoặc config.json).")
    return cfg


def send_telegram(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": cfg["chat_id"], "text": text,
                                     "disable_web_page_preview": True}, timeout=20)
    except requests.RequestException as e:
        print("Lỗi kết nối Telegram:", e)
        return False
    if not r.ok:
        print("Lỗi gửi Telegram:", r.text)
    return r.ok


# ---------- cookie ----------
def parse_cookies(raw):
    """Nhận cookie dạng JSON (Cookie-Editor) hoặc dạng cookies.txt (Netscape)."""
    raw = raw.strip()
    cookies = []
    if raw.startswith("[") or raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cookies", [])
        for c in data:
            ck = {"name": c["name"], "value": c["value"],
                  "domain": c.get("domain") or ".shopee.vn", "path": c.get("path") or "/",
                  "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure"))}
            exp = c.get("expirationDate") or c.get("expires")
            if isinstance(exp, (int, float)) and exp > 0:
                ck["expires"] = int(exp)
            ss = SAMESITE.get(str(c.get("sameSite", "")).lower())
            if ss and not (ss == "None" and not ck["secure"]):
                ck["sameSite"] = ss
            cookies.append(ck)
    else:
        for line in raw.splitlines():
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            ck = {"name": name, "value": value, "domain": domain, "path": path or "/",
                  "httpOnly": http_only, "secure": secure.upper() == "TRUE"}
            if expires.isdigit() and int(expires) > 0:
                ck["expires"] = int(expires)
            cookies.append(ck)
    return [c for c in cookies if "shopee" in c["domain"]]


def get_raw_cookies():
    raw = os.getenv("SHOPEE_COOKIES", "").strip()
    if not raw and COOKIE_FILE.exists():
        raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
    return raw


def open_browser(p, headless):
    """Trả về (context, hàm đóng, đang_dùng_cookie)."""
    raw = get_raw_cookies()
    if raw:
        cookies = parse_cookies(raw)
        print(f"Đã nạp {len(cookies)} cookie Shopee.")
        browser = p.chromium.launch(headless=headless, args=BROWSER_ARGS)
        ctx = browser.new_context(**CONTEXT_OPTS)
        ctx.add_cookies(cookies)
        return ctx, browser.close, True
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=headless, args=BROWSER_ARGS, **CONTEXT_OPTS)
    return ctx, ctx.close, False


# ---------- đọc giá ----------
def price_from_api(data):
    """API nội bộ Shopee trả giá dạng số nguyên nhân 100000."""
    d = data.get("data") or {}
    item = d.get("item") if isinstance(d.get("item"), dict) else d
    for key in ("price_min", "price"):
        v = item.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return v / 100000
    return None


def price_from_ldjson(page):
    for txt in page.locator('script[type="application/ld+json"]').all_text_contents():
        try:
            obj = json.loads(txt)
        except json.JSONDecodeError:
            continue
        for o in (obj if isinstance(obj, list) else [obj]):
            offers = o.get("offers") if isinstance(o, dict) else None
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                for key in ("lowPrice", "price"):
                    try:
                        v = float(offers.get(key))
                        if v > 0:
                            return v
                    except (TypeError, ValueError):
                        pass
    return None


def price_from_text(page):
    try:
        text = page.inner_text("main", timeout=5000)
    except Exception:
        text = page.inner_text("body")
    m = PRICE_RE.search(text)
    return float(m.group(1).replace(".", "")) if m else None


def get_price(ctx, url, idx):
    page = ctx.new_page()
    responses = []
    page.on("response", lambda r: responses.append(r)
            if ("/api/v4/pdp/get_pc" in r.url or "/api/v4/item/get" in r.url) else None)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)
        if "/buyer/login" in page.url or "/verify/" in page.url:
            page.screenshot(path=str(BASE / f"debug_{idx}_login.png"))
            return None, "need_login"
        price = None
        for r in responses:
            try:
                price = price_from_api(r.json())
                if price:
                    break
            except Exception:
                pass
        price = price or price_from_ldjson(page) or price_from_text(page)
        if price is None:
            page.screenshot(path=str(BASE / f"debug_{idx}.png"))
        return price, None
    finally:
        page.close()


# ---------- các chế độ chạy ----------
def do_login(p):
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=False, args=BROWSER_ARGS, **CONTEXT_OPTS)
    page = ctx.new_page()
    page.goto("https://shopee.vn/buyer/login")
    input("Đăng nhập Shopee trong cửa sổ trình duyệt. Xong thì quay lại đây nhấn Enter... ")
    ctx.close()
    print("Đã lưu phiên đăng nhập.")


def do_check(p, cfg, headless, report):
    state = load_json(STATE_FILE, {})
    meta = state.setdefault("_meta", {})
    ctx, close, using_cookies = open_browser(p, headless)
    products = [x for x in cfg["products"] if "DAN_LINK" not in x["url"]]
    if not products:
        sys.exit("Chưa có link sản phẩm trong config.json.")
    failed = 0
    try:
        for idx, prod in enumerate(products):
            name, url, target = prod["name"], prod["url"], prod["target_price"]
            st = state.setdefault(url, {})
            price, err = get_price(ctx, url, idx)

            if err == "need_login":
                print("Shopee yêu cầu đăng nhập.")
                if not meta.get("login_warned") or report:
                    hint = ("Hãy xuất lại cookie và cập nhật secret SHOPEE_COOKIES trên GitHub."
                            if using_cookies else "Chạy lệnh: python shopee_watch.py --login")
                    send_telegram(cfg, f"⚠️ Bot Shopee: cần đăng nhập lại.\n{hint}")
                    meta["login_warned"] = True
                failed = len(products)
                break
            meta["login_warned"] = False

            if price is None:
                print(f"[{name}] Không đọc được giá.")
                failed += 1
                continue

            print(f"[{name}] Giá hiện tại: {fmt_vnd(price)} (mục tiêu ≤ {fmt_vnd(target)})")
            st.update(last_price=price, last_check=time.strftime("%Y-%m-%d %H:%M:%S"))

            sent = False
            notified = st.get("notified_price")
            if price <= target:
                if notified is None or price < notified:
                    msg = (f"🔔 {name} đang giảm giá!\n"
                           f"Giá hiện tại: {fmt_vnd(price)}\n"
                           f"Mục tiêu của bạn: ≤ {fmt_vnd(target)}\n{url}")
                    if send_telegram(cfg, msg):
                        st["notified_price"] = price
                        sent = True
            else:
                st["notified_price"] = None
            if report and not sent:
                send_telegram(cfg, f"📊 {name}: {fmt_vnd(price)} (mục tiêu ≤ {fmt_vnd(target)})")
            time.sleep(3)
    finally:
        close()

    # Báo khi bot liên tục không đọc được giá (có thể bị Shopee chặn)
    if failed == len(products):
        meta["fail_runs"] = meta.get("fail_runs", 0) + 1
        if meta["fail_runs"] == 3 or (report and failed):
            send_telegram(cfg, "⚠️ Bot Shopee không đọc được giá. Có thể Shopee đang chặn "
                               "hoặc trang đã thay đổi. Xem ảnh chụp lỗi trong tab Actions.")
    else:
        meta["fail_runs"] = 0
    save_json(STATE_FILE, state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if args.login:
        with sync_playwright() as p:
            do_login(p)
        return

    cfg = load_config()
    if args.test:
        ok = send_telegram(cfg, "✅ Bot theo dõi giá Shopee đã kết nối thành công!")
        print("Đã gửi tin nhắn thử." if ok else "Gửi thất bại, kiểm tra token/chat_id.")
        return

    with sync_playwright() as p:
        do_check(p, cfg, headless=not args.headed, report=args.report)


if __name__ == "__main__":
    main()
