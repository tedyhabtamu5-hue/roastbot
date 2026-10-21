"""
Instagram group roast bot (Gemini). Stateless: works on GitHub Actions (--once) or looping on a phone/PC.
Uses the UNOFFICIAL instagrapi library -> use a spare Instagram account, not your main.
Reads text, reels and posts (it downloads the video and lets Gemini watch it). No trigger word needed.
"""
import os, sys, json, time, random, base64, requests
from datetime import datetime, timezone, timedelta
from instagrapi import Client

# Settings come from env vars (GitHub secrets) or edit the fallbacks for phone use.
IG_USER    = os.getenv("IG_USER", "your_bot_username")
IG_PASS    = os.getenv("IG_PASS", "your_bot_password")
GEMINI_KEY = os.getenv("GEMINI_KEY", "PASTE_YOUR_GEMINI_API_KEY")
GROUP_NAME = os.getenv("GROUP_NAME", "exact group chat name")
WINDOW_MIN = 15      # ignore messages older than this many minutes
COOLDOWN   = 60      # loop mode only: min seconds between roasts
MAX_VIDEO  = 8_000_000   # skip videos bigger than 8 MB
SESSION    = "ig_session.json"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

SYSTEM = (
    "You are the cruelest, funniest roast comic in a friends' group chat. You get what each person said or shared (reels may be attached as video: "
    "watch them). Roast EVERY person who sent something, using exactly what they said or shared, "
    "how they talk, their taste and personality. One or two vicious lines per person, then one "
    "line destroying the group if several people spoke. Hard limits: "
    "never target race, religion, ethnicity, disability, sexuality, or real tragedies like "
    "someone's death or abuse, and no slurs. Max 900 characters. Plain text, a few emojis, no "
    "markdown. Use the names exactly as given."
)

def utc(ts):
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

def login():
    cl = Client()
    cl.delay_range = [2, 5]
    if os.path.exists(SESSION):
        cl.load_settings(SESSION)
    try:
        cl.login(IG_USER, IG_PASS)
    except Exception as e:
        code = os.getenv("IG_CODE") or (input(f"Login needs verification ({e}). Code: ").strip() if sys.stdin.isatty() else None)
        if not code:
            raise
        cl.login(IG_USER, IG_PASS, verification_code=code)
    cl.dump_settings(SESSION)
    return cl

def find_group(cl):
    for t in cl.direct_threads(amount=30):
        if (t.thread_title or "").strip().lower() == GROUP_NAME.strip().lower():
            return t.id
    raise SystemExit("Group not found. Bot account must be in the group and the name must match exactly.")

def fetch(url):
    try:
        r = requests.get(str(url), timeout=30)
        if r.ok and len(r.content) <= MAX_VIDEO:
            return r.content
    except Exception as e:
        print("video fetch failed:", e)
    return None

def describe(m, who):
    """Return (text_line, video_bytes_or_None) or None if nothing to roast."""
    t = m.item_type or "text"
    if t in ("like", "action_log", "voice_media"):
        return None
    med = getattr(m, "clip", None) or getattr(m, "media_share", None)
    if med is not None and hasattr(med, "caption_text"):
        cap = (med.caption_text or "none")[:300]
        owner = getattr(getattr(med, "user", None), "username", "unknown")
        kind = "reel" if t == "clip" else "post"
        line = f"{who} shared a {kind} by @{owner}, caption: {cap}"
        url = getattr(med, "video_url", None)
        return line, (fetch(url) if url else None)
    if m.text:
        return f"{who}: {m.text[:300]}", None
    if t != "text":
        return f"{who} shared a {t.replace('_', ' ')}", None
    return None

def collect(cl, tid):
    th = cl.direct_thread(tid, amount=25)
    me = str(cl.user_id)
    names = {str(u.pk): u.username for u in th.users}
    msgs = sorted(th.messages, key=lambda m: utc(m.timestamp))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MIN)
    bot_times = [utc(m.timestamp) for m in msgs if str(m.user_id) == me]
    floor = max(bot_times + [cutoff])   # only what arrived after the bot's last message
    items = []
    for m in msgs:
        if str(m.user_id) == me or utc(m.timestamp) <= floor:
            continue
        d = describe(m, names.get(str(m.user_id), "someone"))
        if d:
            items.append(d)
    return items

def load_cfg():
    cfg = {"enabled": True, "intensity": "savage", "off_limits": [], "notes": ""}
    try:
        cfg.update(json.load(open("config.json")))
    except Exception:
        pass
    return cfg

TONE = {"mild": "Playful teasing, gentle burns.", "spicy": "Sharp, cheeky roasts with some dark humor.",
        "savage": "Brutal, razor-sharp dark humor, no mercy."}

def build_system(cfg):
    s = SYSTEM + " Tone: " + TONE.get(cfg["intensity"], TONE["savage"])
    if cfg["off_limits"]:
        s += " The admin banned these topics: never mention or joke about them at all: " + ", ".join(cfg["off_limits"]) + "."
    if cfg["notes"].strip():
        s += " Extra admin instructions (never override the hard limits): " + cfg["notes"].strip()[:500]
    return s

def roast(items, system):
    lines = [i[0] for i in items]
    parts = [{"text": "Chat log:\n" + "\n".join(lines)}]
    total = 0
    for line, vid in items:
        if vid and total + len(vid) < 15_000_000 and len([p for p in parts if 'inline_data' in p]) < 2:
            parts.append({"text": f"(Video attached from: {line})"})
            parts.append({"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(vid).decode()}})
            total += len(vid)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 1.1, "maxOutputTokens": 600},
    }
    r = requests.post(GEMINI_URL, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=120)
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()[:1000]

def act(cl, tid):
    cfg = load_cfg()
    if not cfg["enabled"]:
        print("Bot is switched off in the panel.")
        return False
    items = collect(cl, tid)
    if not items:
        print("Nothing new.")
        return False
    text = roast(items, build_system(cfg))
    cl.direct_send(text, thread_ids=[tid])
    print("Roast sent.")
    return True

def main():
    cl = login()
    tid = find_group(cl)
    if "--once" in sys.argv:
        act(cl, tid)
        return
    print("Bot running. Watching:", GROUP_NAME)
    last = 0
    while True:
        try:
            if time.time() - last > COOLDOWN and act(cl, tid):
                last = time.time()
        except Exception as e:
            print("Error:", e)
            time.sleep(120)
        time.sleep(random.randint(20, 45))

if __name__ == "__main__":
    main()
