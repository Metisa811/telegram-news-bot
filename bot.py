import calendar
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# بله (اختیاری): اگر این دو تنظیم شوند، خبرها در بله هم ارسال می‌شوند
BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
BALE_CHAT_ID = os.environ.get("BALE_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GROQ_API_KEY:
    print("❌ خطا: متغیرهای Secret تنظیم نشده‌اند.")
    sys.exit(1)

# اول مدل قوی‌تر، اگر سهمیه‌اش تمام شد مدل بعدی
MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

CHANNEL_TAG = "@newsberoza"   # آیدی کانال؛ ته هر پیام با یک خط فاصله
DB_FILE = "sent_articles.json"       # شناسه‌ی خبرهای ارسال‌شده (هرگز پاک نمی‌شود)
ARCHIVE_FILE = "news_archive.jsonl"  # بایگانی کامل: منبع + متن اصلی + متن منتشرشده
GLOSSARY_FILE = "glossary.json"      # واژه‌نامه، قوانین نگارش و نمونه‌ها (RAG)
DB_MAX = 200000
INTERVAL_SECONDS = 120   # فاصله بین هر دور
MAX_SEND_PER_CYCLE = 1   # هر دور حداکثر چند خبر
MAX_TRIES_PER_CYCLE = 4  # هر دور حداکثر چند خبر را به هوش مصنوعی بده
GIT_SYNC_EVERY = 15      # هر چند دور، داده‌ها در گیت ذخیره شود (~۳۰ دقیقه)
DEDUP_HOURS = 24          # خبرهای تکراری را در این بازه بررسی کن
SIM_PRE = 0.6             # شباهت متن اصلی (قبل از هوش مصنوعی)
SIM_POST = 0.5            # شباهت متن بازنویسی‌شده (بعد از هوش مصنوعی)
MAX_FAILS_PER_ITEM = 3    # بعد از این تعداد شکست، خبر رد می‌شود
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "330"))

TELEGRAM_CHANNELS = [
    "idfinfarsi",
    "withyashar",
    "IranintlTV",
    "BazChiShod",
]


def gnews(query, lang="en-US", country="US"):
    ceid = f"{country}:{lang.split('-')[0]}"
    return (f"https://news.google.com/rss/search?q={query}+when:1d"
            f"&hl={lang}&gl={country}&ceid={ceid}")


# منابع اولویت‌دار
PRIORITY_FEEDS = [
    gnews("Iran+OR+Israel+OR+Hezbollah+OR+%22Middle+East%22"),
    gnews("Iran+politics+OR+Tehran"),
    gnews("%22Middle+East%22+war+OR+strike+OR+missile"),
    gnews("site:now14.co.il", "he", "IL"),   # کانال ۱۴
    gnews("site:13tv.co.il", "he", "IL"),    # کانال ۱۳
    gnews("site:kan.org.il", "he", "IL"),    # کان ۱۱
    "https://moxie.foxnews.com/google-publisher/world.xml",
    gnews("site:foxnews.com+Iran+OR+Israel+OR+%22Middle+East%22"),
    gnews("site:centcom.mil"),
    gnews("CENTCOM+OR+%22Central+Command%22"),
    "https://www.timesofisrael.com/feed/",
    "https://www.jpost.com/rss/rssfeedsheadlines.aspx",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
]

MAX_AGE_HOURS = 12  # خبرهای RSS قدیمی‌تر از این نادیده گرفته می‌شوند

# منابع عمومی برای وقتی که خبر اولویت‌دار نیست
GENERAL_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
]

PRIORITY_KEYWORDS = [
    "iran", "ایران", "israel", "اسرائیل", "trump", "ترامپ", "hezbollah",
    "حزب‌الله", "حزب الله", "tehran", "تهران", "idf", "irgc", "سپاه",
    "موشک", "missile", "حمله", "attack", "strike", "gaza", "غزه",
    "lebanon", "لبنان", "nuclear", "هسته", "houthi", "حوثی", "ukraine",
    "اوکراین", "nato", "ناتو", "europe", "اروپا",
]


# ---------- واژه‌نامه و بازنویسی (RAG) ----------
def load_glossary():
    try:
        with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
            g = json.load(f)
            print(f"📚 واژه‌نامه بارگذاری شد: {len(g.get('entries', []))} مدخل")
            return g
    except Exception as e:
        print(f"⚠️ واژه‌نامه پیدا نشد ({e}). بدون واژه‌نامه ادامه می‌دهم.")
        return {"entries": [], "style_rules": [], "examples": []}


GLOSSARY = load_glossary()


def norm(s):
    """یکسان‌سازی حروف عربی/فارسی و حذف نیم‌فاصله و اعراب برای تطبیق."""
    s = (s or "").replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    s = re.sub("[\u200c\u200e\u200f\u064b-\u065f]", "", s)
    return s.lower()


def term_in(term, ntext):
    t = norm(term).strip()
    if not t:
        return False
    if re.fullmatch(r"[a-z0-9 .\-']+", t):  # عبارت لاتین: با مرز کلمه
        return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", ntext) is not None
    return t in ntext


def retrieve(text, limit=12):
    """مدخل‌های واژه‌نامه که در متن خبر آمده‌اند."""
    nt = norm(text)
    found = []
    for e in GLOSSARY.get("entries", []):
        terms = [e["canonical"]] + e.get("aliases", []) + e.get("fix", [])
        if any(term_in(t, nt) for t in terms):
            found.append(e)
            if len(found) >= limit:
                break
    return found


def _fix_pairs():
    pairs = []
    for e in GLOSSARY.get("entries", []):
        for f in e.get("fix", []):
            pairs.append((f, e["canonical"]))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)  # عبارت بلندتر اول
    return pairs


FIX_PAIRS = _fix_pairs()


def fa_normalize(text):
    """حروف عربی ← فارسی (ي→ی، ك→ک) بدون تغییر دیگر."""
    return (text or "").replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")


def _banned_terms():
    """نام ادمین‌ها و کانال‌های تلگرامی که هرگز نباید در خبر بیایند."""
    terms = set()
    for e in GLOSSARY.get("entries", []):
        if e.get("banned"):
            for t in e.get("aliases", []):
                if t:
                    terms.add(fa_normalize(t))
    return sorted(terms, key=len, reverse=True)


BANNED_TERMS = _banned_terms()
ATTRIBUTION = r"(?:به گزارش|به نقل از|طبق گزارش|بنا بر گزارش|بر اساس گزارش|براساس گزارش|به نوشته)"


def strip_channel_noise(text):
    """حذف نام ادمین/کانال و خطاب‌ها از متن ورودی پیام‌های تلگرامی."""
    t = fa_normalize(text)
    for term in BANNED_TERMS:
        e = re.escape(term)
        t = re.sub(rf"(?m)^\s*(?:جناب\s+)?{e}\s*[:：،,\-–]\s*", "", t, flags=re.I)   # «اتاق جنگ : ...»
        t = re.sub(rf"(?:جناب\s+){e}\s*[،,]?\s*", "", t, flags=re.I)                # «جناب یاشار، ...»
        t = re.sub(rf"{e}\s+جان\s*[،,]?\s*", "", t, flags=re.I)                      # «یاشار جان ...»
        t = re.sub(rf"(?m)^\s*{e}\s*[،,]?\s+", "", t, flags=re.I)                    # «یاشار دوتا ...»
    return t.strip()


def has_banned(text):
    nt = norm(text)
    return any(term_in(t, nt) for t in BANNED_TERMS)


# ---------- تشخیص خبر تکراری ----------
STOPWORDS = {"است", "شده", "برای", "خود", "این", "کرد", "بود", "شود", "های", "یک",
             "the", "and", "for", "that", "with", "has", "have", "was", "were", "from",
             "said", "says", "will", "are", "its", "this", "after", "over", "his", "her"}


def tokens(text):
    t = norm(text)
    t = re.sub(r"[^\w\s]", " ", t)
    return {w for w in t.split() if (len(w) >= 3 or w.isdigit()) and w not in STOPWORDS}


def similar(a, b, threshold):
    if not a or not b:
        return False
    inter = len(a & b)
    jacc = inter / len(a | b)
    small = min(len(a), len(b))
    cont = inter / small
    return jacc >= threshold or (small >= 7 and cont >= 0.8)


RECENT = deque(maxlen=600)   # خبرهای ۲۴ ساعت اخیر: {"t", "orig", "text"}


def _recent_alive():
    cutoff = time.time() - DEDUP_HOURS * 3600
    return [r for r in RECENT if r["t"] >= cutoff]


def load_recent():
    """خبرهای اخیر را از بایگانی می‌خواند تا بین اجراها هم تکراری نیاید."""
    RECENT.clear()
    if not os.path.exists(ARCHIVE_FILE):
        return
    try:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-800:]
        cutoff = time.time() - DEDUP_HOURS * 3600
        for ln in lines:
            try:
                rec = json.loads(ln)
                ts = datetime.fromisoformat(rec["time"]).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                RECENT.append({"t": ts,
                               "orig": tokens(rec.get("title", "") + " " + rec.get("original", "")),
                               "text": tokens(rec.get("text", ""))})
        print(f"🧠 خبرهای اخیر برای تشخیص تکراری: {len(RECENT)}")
    except Exception as e:
        print(f"⚠️ خواندن بایگانی ناموفق: {e}")


def is_duplicate_original(title, summary):
    cand = tokens(title + " " + summary)
    return any(similar(cand, r["orig"], SIM_PRE) for r in _recent_alive())


def is_duplicate_text(text):
    cand = tokens(text)
    return any(similar(cand, r["text"], SIM_POST) for r in _recent_alive())


def remember(item, text):
    RECENT.append({"t": time.time(),
                   "orig": tokens(item["title"] + " " + item["summary"]),
                   "text": tokens(text)})


def apply_fixes(text):
    """جایگزینی قطعی نام‌های غلط/عامیانه با نام رسمی."""
    for wrong, right in FIX_PAIRS:
        text = re.sub(re.escape(wrong), right, text, flags=re.I)
    return text


def glossary_block(text):
    nt = norm(text)
    lines = []
    for e in retrieve(text):
        if e.get("banned"):
            hit = [a for a in e.get("aliases", []) if term_in(a, nt)]
            lines.append(f"- «{'، '.join(hit[:3])}»: نام ادمین/کانال تلگرامی است؛ "
                         "هرگز ننویس و خبر را به آن نسبت نده. " + e.get("note", ""))
            continue
        line = f"- {e['canonical']}"
        if e.get("aliases"):
            line += f" (معادل‌ها/املاهای دیگر: {'، '.join(e['aliases'][:4])})"
        if e.get("note"):
            line += f" — {e['note']}"
        lines.append(line)
    return "\n".join(lines) if lines else "(مورد خاصی نیست)"


def examples_block(text=""):
    """نمونه‌های عمومی + نمونه‌هایی که کلید «when» آن‌ها در متن آمده (RAG)."""
    nt = norm(text)
    general, matched = [], []
    for ex in GLOSSARY.get("examples", []):
        when = ex.get("when")
        if not when:
            general.append(ex)
        elif any(term_in(w, nt) for w in when):
            matched.append(ex)
    chosen = matched[:2] + general[:2]
    parts = [f"ورودی:\n{ex['input']}\nخروجی:\n{ex['output']}" for ex in chosen]
    return "\n\n".join(parts) if parts else "(ندارد)"


def clean_output(text):
    text = apply_fixes(fa_normalize(text or ""))
    for term in BANNED_TERMS:
        text = re.sub(rf"{ATTRIBUTION}\s+{re.escape(term)}\s*[،,:]?\s*", "", text, flags=re.I)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)            # هیچ آیدی و کانالی در متن نباشد
    text = text.replace("*", "").replace("`", "")
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if "تلگرام" in s or "t.me" in s.lower() or s.startswith("منبع"):
            continue
        s = re.sub(r"^[-–—]\s+", "• ", s)
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# ---------- دیتابیس ----------
def load_sent():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def save_sent(sent_list):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(sent_list[-DB_MAX:], f, ensure_ascii=False, indent=2)


def archive_news(item, final_text, media_url="", media_kind=""):
    rec = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "id": item["id"],
        "source": item.get("source", ""),
        "title": item["title"],
        "original": item["summary"][:1500],
        "text": final_text,
        "media": media_kind,
        "media_url": media_url,
    }
    with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def git_sync():
    """ذخیره‌ی داده‌ها در گیت هنگام اجرا روی GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        for path in (DB_FILE, ARCHIVE_FILE):
            if os.path.exists(path):
                subprocess.run(["git", "add", path], capture_output=True)
        r = subprocess.run(["git", "commit", "-m", "Update news data [skip ci]"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            subprocess.run(["git", "pull", "--rebase", "--autostash"], capture_output=True)
            p = subprocess.run(["git", "push"], capture_output=True, text=True)
            print("💾 داده‌ها در گیت ذخیره شد." if p.returncode == 0
                  else f"⚠️ push ناموفق: {p.stderr[:150]}")
    except Exception as e:
        print(f"⚠️ خطای git: {e}")


# ---------- تصویر و ویدئوی خبر ----------
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MIN_IMAGE_BYTES = 8_000
MAX_IMAGE_BYTES = 10_000_000
VIDEO_MAX_BYTES = 48_000_000     # سقف آپلود ربات تلگرام ~۵۰ مگابایت
ALLOW_VIDEO_THUMB = False        # اگر ویدئو دانلود نشد، فریم اول نگذار (فقط متن)

RESIZE_PARAMS = {"resize", "w", "h", "width", "height", "fit", "crop", "quality",
                 "q", "ssl", "strip", "format", "fm", "dpr", "size"}


def extract_entry_media(entry):
    """از فید RSS: (فهرست تصاویر از بزرگ به کوچک، آدرس ویدئو یا None)."""
    images, video = [], None
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            u = m.get("url")
            if not u or not u.startswith("http"):
                continue
            kind = (m.get("type") or "") + (m.get("medium") or "")
            if "video" in kind or u.lower().split("?")[0].endswith((".mp4", ".mov", ".webm")):
                video = video or u
                continue
            try:
                w = int(m.get("width") or 0)
            except ValueError:
                w = 0
            images.append((w, u))
    for l in entry.get("links", []) or []:
        if l.get("rel") == "enclosure":
            t = l.get("type") or ""
            if "image" in t:
                images.append((0, l.get("href")))
            elif "video" in t and not video:
                video = l.get("href")
    raw = entry.get("summary", "") or ""
    for c in entry.get("content", []) or []:
        raw += c.get("value", "")
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)', raw, re.I):
        if m.group(1).startswith("http"):
            images.append((0, html.unescape(m.group(1))))
    images.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, u in images:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out, video


def upgrade_image_url(url):
    """نسخه‌ی بزرگ‌تر احتمالی تصویر (بدون پارامتر کوچک‌سازی) + خود آدرس."""
    cands = []
    u = url
    # بی‌بی‌سی: /240/ ← /976/
    v = re.sub(r"/(?:\d{3})/(cpsprodpb/)", r"/976/\1", u)
    if v != u:
        cands.append(v)
    # حذف پارامترهای کوچک‌سازی از query
    try:
        p = urlparse(u)
        if p.query:
            kept = [kv for kv in p.query.split("&")
                    if kv.split("=")[0].lower() not in RESIZE_PARAMS]
            if len(kept) != len(p.query.split("&")):
                cands.append(p._replace(query="&".join(kept)).geturl())
    except Exception:
        pass
    # وردپرس: photo-300x200.jpg ← photo.jpg
    v = re.sub(r"-\d{2,4}x\d{2,4}(\.(?:jpe?g|png|webp))", r"\1", u, flags=re.I)
    if v != u:
        cands.append(v)
    cands.append(u)
    return cands


def fetch_og_image(link):
    """تصویر اصلی (باکیفیت) صفحه‌ی خبر از og:image."""
    try:
        r = requests.get(link, timeout=15, headers=UA, allow_redirects=True)
        if r.status_code != 200 or "news.google.com" in urlparse(r.url).netloc:
            return None
        page = r.text[:300000]
        for pat in (
            r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
        ):
            m = re.search(pat, page, re.I)
            if m:
                return html.unescape(m.group(1))
    except Exception:
        pass
    return None


def download_image(url):
    """(بایت‌ها، نوع) یا None."""
    try:
        r = requests.get(url, timeout=25, headers=UA)
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if (r.status_code == 200 and ctype.startswith("image/") and "svg" not in ctype
                and MIN_IMAGE_BYTES < len(r.content) <= MAX_IMAGE_BYTES):
            return r.content, ctype
    except Exception:
        pass
    return None


def download_video(url):
    """ویدئو را کامل دانلود می‌کند: (بایت‌ها، نوع) یا None."""
    try:
        with requests.get(url, headers=UA, timeout=60, stream=True) as r:
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            looks_video = ctype.startswith("video/") or \
                url.lower().split("?")[0].endswith((".mp4", ".mov", ".webm"))
            if r.status_code != 200 or not looks_video:
                return None
            if int(r.headers.get("Content-Length") or 0) > VIDEO_MAX_BYTES:
                print("⚠️ ویدئو بزرگ‌تر از سقف آپلود است.")
                return None
            buf = bytearray()
            for chunk in r.iter_content(1 << 20):
                buf.extend(chunk)
                if len(buf) > VIDEO_MAX_BYTES:
                    print("⚠️ ویدئو بزرگ‌تر از سقف آپلود است.")
                    return None
            if len(buf) < 20_000:
                return None
            return bytes(buf), (ctype if ctype.startswith("video/") else "video/mp4")
    except Exception as e:
        print(f"⚠️ دانلود ویدئو: {e}")
    return None


def get_media(item):
    """رسانه‌ی خبر: ویدئوی اصلی در اولویت، بعد بهترین کیفیت عکس.
    برمی‌گرداند: ((kind, bytes, ctype) یا None، آدرس رسانه)."""
    if item.get("video"):
        v = download_video(item["video"])
        if v:
            return ("video", v[0], v[1]), item["video"]
        print("⚠️ ویدئو دانلود نشد.")
        if item.get("is_video") and not ALLOW_VIDEO_THUMB:
            return None, ""   # فریم اول نمی‌گذاریم

    urls = []
    if item.get("link"):
        og = fetch_og_image(item["link"])   # معمولاً باکیفیت‌ترین
        if og:
            urls.append(og)
    urls.extend(item.get("images") or [])
    if item.get("image"):
        urls.append(item["image"])

    cands = []
    for u in urls:
        for x in upgrade_image_url(u):
            if x not in cands:
                cands.append(x)

    best, best_url = None, ""
    for u in cands[:5]:
        d = download_image(u)
        if d and (best is None or len(d[0]) > len(best[0])):
            best, best_url = d, u
    if best:
        return ("photo", best[0], best[1]), best_url
    return None, ""


# ---------- جمع‌آوری خبر ----------
def clean_html(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def fetch_telegram_channel_posts(username):
    url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    posts = []
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            parts = response.text.split('data-post="')
            for part in parts[1:]:
                post_id = part.split('"', 1)[0]
                m = re.search(
                    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                    part, re.DOTALL)
                if m:
                    clean_text = strip_channel_noise(clean_html(m.group(1)))
                    img = re.search(
                        r"tgme_widget_message_(?:photo_wrap|video_thumb|roundvideo_thumb)"
                        r"[^>]*?background-image:url\('([^']+)'\)", part)
                    image_url = html.unescape(img.group(1)) if img else None
                    vid = re.search(r'<video[^>]*\ssrc="([^"]+)"', part)
                    video_url = html.unescape(vid.group(1)) if vid else None
                    is_video = bool(video_url) or "tgme_widget_message_video" in part \
                        or "tgme_widget_message_roundvideo" in part
                    if len(clean_text) > (20 if (image_url or video_url) else 40):
                        posts.append({
                            "id": f"tg_{post_id}",
                            "title": clean_text.split("\n")[0][:70],
                            "summary": clean_text,
                            "priority": True,
                            "source": f"telegram:@{username}",
                            "image": image_url,
                            "video": video_url,
                            "is_video": is_video,
                        })
    except Exception as e:
        print(f"⚠️ خطا در کانال @{username}: {e}")
    return posts


def fetch_rss(url, limit, force_priority=False):
    items = []
    try:
        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        feed = feedparser.parse(resp.content)
        feed_title = (feed.feed.get("title") if feed.feed else "") or urlparse(url).netloc
        for entry in feed.entries[:limit]:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub and (time.time() - calendar.timegm(pub)) > MAX_AGE_HOURS * 3600:
                continue
            src = entry.get("source")
            src_title = src.get("title") if isinstance(src, dict) else None
            imgs, vid = extract_entry_media(entry)
            items.append({
                "id": entry.get("id", entry.get("link")),
                "title": clean_html(entry.get("title", "")),
                "summary": clean_html(entry.get("summary", "")),
                "priority": force_priority,
                "source": src_title or feed_title,
                "link": entry.get("link"),
                "image": imgs[0] if imgs else None,
                "images": imgs,
                "video": vid,
                "is_video": bool(vid),
            })
    except Exception as e:
        print(f"⚠️ خطا در RSS: {e}")
    return items


def is_priority(item):
    if item.get("priority"):
        return True
    t = (item["title"] + " " + item["summary"]).lower()
    return any(k in t for k in PRIORITY_KEYWORDS)


# ---------- هوش مصنوعی (Groq) ----------
def summarize(title, summary):
    title = apply_fixes(title)
    summary = apply_fixes(summary)[:1500]
    rules = "\n".join(f"- {r}" for r in GLOSSARY.get("style_rules", []))
    prompt = f"""
تو ویراستار یک کانال خبری فارسی‌زبان هستی. خبر زیر را (حتی اگر انگلیسی یا عبری است) به فارسی معیار، رسمی و امروزی بازنویسی کن؛ همان‌طور که خبرگزاری‌های حرفه‌ای فارسی می‌نویسند.

قالب خروجی (دقیقاً همین):
🚨 [تیتر کوتاه و رسمی]
• [فکت اول]
• [فکت دوم]
• [فکت سوم، فقط در صورت نیاز]

قوانین نگارش:
{rules}

قوانین ثابت:
- فقط خود خبر؛ هیچ تحلیل، نتیجه‌گیری، پیش‌بینی یا جمله درباره‌ی «اهمیت» یا «پیامد» خبر ننویس.
- نام افراد و نهادها را دقیقاً مطابق واژه‌نامه بنویس؛ لقب، کنیه و نام عامیانه ممنوع است.
- اگر خبر در متن به یک خبرگزاری یا رسانه‌ی معتبر نسبت داده شده (مثل رویترز، آسوشیتدپرس، ایرنا، تسنیم، کان، فاکس نیوز)، می‌توانی بنویسی «به گزارش …».
- هرگز نام کانال تلگرام، نام ادمین کانال، آیدی (@…)، لینک یا عبارتی مثل «پیام از کانال …» ننویس.
- پیام کانال‌های تلگرامی گاهی خطاب به ادمین یا گفتگومانند است؛ مخاطب پیام را فاعل خبر نگیر. اگر فاعل در متن مشخص نیست، جمله را مجهول بنویس و فاعل نساز.
- فقط از اطلاعات خود متن استفاده کن؛ چیزی اضافه یا حدس نزن.
- حجم کل کمتر از ۶۰ کلمه.
- هیچ پیش‌گفتار یا توضیح خارج از قالب ننویس.

واژه‌نامه‌ی مرتبط با این خبر:
{glossary_block(title + " " + summary)}

نمونه‌های نگارش درست:
{examples_block(title + ' ' + summary)}

خبر:
تیتر: {title}
متن: {summary}
"""
    for model in MODELS:
        try:
            payload = {
                "model": model,
                "temperature": 0.2,
                "max_completion_tokens": 800,
                "messages": [{"role": "user", "content": prompt}],
            }
            if "gpt-oss" in model:
                payload["reasoning_effort"] = "low"  # مصرف توکن کمتر
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
                timeout=45,
            )
            if r.status_code == 429:
                print(f"⏳ سهمیه {model} پر است، مدل بعدی...")
                continue
            if r.status_code != 200:
                print(f"⚠️ Groq {model}: {r.status_code} {r.text[:200]}")
                continue
            text = r.json()["choices"][0]["message"]["content"] or ""
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
            text = clean_output(text)
            if has_banned(text):
                print(f"⚠️ {model}: نام کانال/ادمین در خروجی بود؛ رد شد.")
                continue
            if len(text) >= 20:
                return text
        except Exception as e:
            print(f"⚠️ خطای Groq ({model}): {e}")
    return None


# ---------- ارسال (تلگرام و بله؛ هر دو API مشابه دارند) ----------
CAPTION_LIMIT = 1000


def _send(name, base_url, chat_id, message, media=None, extra=None):
    """اگر رسانه (عکس/ویدئو) باشد با sendPhoto/sendVideo می‌فرستد؛ در صورت خطا فقط متن."""
    extra = extra or {}
    try:
        if media:
            kind, data, ctype = media
            is_video = kind == "video"
            ext = mimetypes.guess_extension(ctype) or (".mp4" if is_video else ".jpg")
            caption_fits = len(message) <= CAPTION_LIMIT
            form = {"chat_id": chat_id}
            if caption_fits:
                form["caption"] = message
            if is_video:
                form["supports_streaming"] = "true"
            res = requests.post(
                f"{base_url}/{'sendVideo' if is_video else 'sendPhoto'}",
                data=form,
                files={("video" if is_video else "photo"): (f"news{ext}", data, ctype)},
                timeout=240 if is_video else 60).json()
            if res.get("ok"):
                if caption_fits:
                    return True
                # کپشن بلند است: متن جداگانه بعد از رسانه
            else:
                print(f"⚠️ {name} ارسال {kind}: {str(res)[:200]} → ارسال بدون رسانه")
        payload = {"chat_id": chat_id, "text": message, **extra}
        res = requests.post(f"{base_url}/sendMessage", json=payload, timeout=15).json()
        if not res.get("ok"):
            print(f"⚠️ {name}: {str(res)[:200]}")
            return False
        return True
    except Exception as e:
        print(f"⚠️ خطای {name}: {e}")
        return False


def send_to_telegram(message, media=None):
    return _send("تلگرام", f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}",
                 TELEGRAM_CHAT_ID, message, media,
                 {"disable_web_page_preview": True})


def send_to_bale(message, media=None):
    if not BALE_BOT_TOKEN or not BALE_CHAT_ID:
        return False
    chat_id = int(BALE_CHAT_ID) if BALE_CHAT_ID.lstrip("-").isdigit() else BALE_CHAT_ID
    return _send("بله", f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}", chat_id, message, media)


# ---------- یک دور کار ----------
FAILS = {}


def run_once():
    sent_list = load_sent()
    sent_set = set(sent_list)
    candidates = []

    for ch in TELEGRAM_CHANNELS:
        candidates.extend(fetch_telegram_channel_posts(ch)[-4:])
    for url in PRIORITY_FEEDS:
        candidates.extend(fetch_rss(url, 6, force_priority=True))
    for url in GENERAL_FEEDS:
        candidates.extend(fetch_rss(url, 8))

    seen = set()
    unprocessed = []
    dirty = False
    for c in candidates:
        if not c["id"] or c["id"] in sent_set or c["id"] in seen:
            continue
        seen.add(c["id"])
        if is_duplicate_original(c["title"], c["summary"]):
            print(f"🔁 تکراری (قبل از AI): {c['title'][:40]}")
            sent_list.append(c["id"])
            sent_set.add(c["id"])
            dirty = True
            continue
        unprocessed.append(c)
    if dirty:
        save_sent(sent_list)

    unprocessed.sort(key=lambda c: not is_priority(c))
    print(f"خبر جدید: {len(unprocessed)}")

    sent_count = 0
    tries = 0
    for item in unprocessed:
        if sent_count >= MAX_SEND_PER_CYCLE or tries >= MAX_TRIES_PER_CYCLE:
            break
        tries += 1
        tag = "⭐" if is_priority(item) else "📰"
        print(f"{tag} پردازش: {item['title'][:50]}  [{item.get('source', '')}]")

        text = summarize(item["title"], item["summary"])
        if not text:
            FAILS[item["id"]] = FAILS.get(item["id"], 0) + 1
            if FAILS[item["id"]] >= MAX_FAILS_PER_ITEM:
                print("⏭️ این خبر چند بار ناموفق بود؛ رد شد.")
                sent_list.append(item["id"])
                save_sent(sent_list)
            continue
        if is_duplicate_text(text):
            print("🔁 تکراری (بعد از AI)؛ ارسال نشد.")
            sent_list.append(item["id"])
            save_sent(sent_list)
            continue
        final = f"{text}\n\n{CHANNEL_TAG}"   # یک خط فاصله و آیدی کانال
        media, media_url = get_media(item)
        kind = media[0] if media else ""
        tg_ok = send_to_telegram(final, media)
        bale_ok = send_to_bale(final, media)
        if tg_ok or bale_ok:
            label = {"photo": "عکس", "video": "ویدئو"}.get(kind, "بدون رسانه")
            print(f"✅ ارسال شد. (تلگرام: {tg_ok} | بله: {bale_ok} | {label})")
            sent_list.append(item["id"])
            save_sent(sent_list)
            archive_news(item, text, media_url, kind)
            remember(item, text)
            sent_count += 1
            git_sync()   # بلافاصله ذخیره، تا با قطع اجرا خبر تکراری نشود


def main():
    deadline = time.time() + RUN_MINUTES * 60
    print(f"شروع؛ مدت اجرا: {RUN_MINUTES:.0f} دقیقه")
    load_recent()
    cycle = 0
    while True:
        cycle += 1
        try:
            run_once()
        except Exception as e:
            print(f"⚠️ خطای غیرمنتظره: {e}")
        if cycle % GIT_SYNC_EVERY == 0:
            git_sync()
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(INTERVAL_SECONDS, remaining))
    git_sync()
    print("پایان عملیات.")


if __name__ == "__main__":
    main()
