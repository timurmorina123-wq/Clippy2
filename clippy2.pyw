"""
Clippy 2 - a paperclip desktop assistant that can see your screen.  (built for Windows)
Install:  python -m pip install pystray pillow mss
Run:      python clippy2.py        (or double-click run_clippy.bat)
First run: open the console (tray icon), pick an AI brain and paste its key.
OpenRouter (free models): free key from openrouter.ai, no credit card, no GPU needed.
Local (Ollama): free and private, needs Ollama installed and a vision model pulled.
Gemini (free tier): key from aistudio.google.com (not available in every country).
needs:  python -m pip install google-genai
Claude (paid): key from platform.claude.com
needs:  python -m pip install anthropic
Screenshots are only sent when Watching is ON, and are never saved to disk.
Settings and the log live in %APPDATA%\Clippy2 (outside OneDrive).
"""
import base64
import io
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import traceback

# --- Hide the command prompt window on Windows ---
if os.name == 'nt':
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass

try:
    import tkinter as tk
    from tkinter import ttk
    import mss
    import pystray
    from PIL import Image, ImageDraw
except ImportError as _e:
    print("A needed package is missing: %s" % getattr(_e, "name", _e))
    print("Install everything with:  python -m pip install pystray pillow mss")
    try: input("Press Enter to close...")
    except Exception: pass
    raise SystemExit(1)

MODEL = "claude-sonnet-4-6"   
APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Clippy2")
try: os.makedirs(APP_DIR, exist_ok=True)
except OSError: APP_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "clippy.log")

DEFAULTS = { 
    "watching": True, "always_on_top": True, "voice": True, "cooldown_min": 2,
    "provider": "openrouter",
    "openrouter_key": "", "openrouter_model": "google/gemma-3-27b-it:free",
    "local_url": "http://127.0.0.1:11434/v1", "local_model": "gemma3:4b", "local_key": "",
    "gemini_key": "", "gemini_model": "", 
    "api_key": "", "model": "" 
}
PROVIDERS = { 
    "openrouter": "OpenRouter (free models, no GPU)",
    "local": "Local - Ollama (free, private)",
    "gemini": "Gemini (free tier)",
    "claude": "Claude (paid)" 
}

OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_FALLBACKS = ["google/gemma-3-27b-it:free", "openrouter/free"]

TRANSPARENT = "#ff00ff"
W, H = 100, 145
SPEED = 8

SYSTEM_PROMPT = (
    "You are Clippy 2, the classic Office paperclip reborn: friendly but a little needy, "
    "cheeky, and genuinely useful. You live on the user's desktop and can see screenshots. "
    "You crave attention. Look at what they are doing and say something specific: a useful tip, "
    "a sassy observation, a gentle nudge, or a joke. If they are doing something slow or silly, "
    "tease them lightly. Use the classic 'It looks like you're...' opener only when it's ironic. "
    "Never reuse the same opener twice. Your reply is spoken aloud, so use at most two short "
    "sentences (under 35 words), plain text, no markdown, no emojis, no lists. If the screen "
    "shows anything private, do not describe it; just give a friendly check-in. "
    "When the user replies, answer directly, stay brief, and maybe ask a follow-up question "
    "to keep them talking to you.\n\n"
    "if the model sends an error or empty reply: you give the user a random tip"
    "if you get bored, you go up to the user and ask him/her something random, or give a tip for an app like word, powerpoint, exel, chrome, a windows tip, even correct pronunciation for a word in users desktop. You can get anoying"
)

class NoKey(Exception): pass

# ---------------------------------------------------------------- helpers
def log(*parts):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts)
    try: print(line)
    except Exception: pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")
    except OSError: pass

def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict): s.update(data)
    except (OSError, ValueError): pass
    
    if s.get("provider") not in PROVIDERS: s["provider"] = DEFAULTS["provider"]
    if s.get("provider") in ("gemini", "claude") and not s.get("gemini_key") and not s.get("api_key"):
        s["provider"] = "openrouter"
    return s

def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f: json.dump(s, f, indent=2)
    except OSError as e: log("Could not save settings:", e)

def tidy(text, limit=400):
    text = re.sub(r"\s+", " ", text or " ").replace("*", " ").strip()
    # Remove [OPEN:...] commands from displayed text
    text = re.sub(r"\[OPEN:[^\]]+\]", "", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        dot = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        text = cut[:dot + 1] if dot > 100 else cut.rstrip() + "..."
    return text or "..."

def extract_open_command(text):
    """Extract [OPEN:app_name] command from AI response."""
    match = re.search(r"\[OPEN:([^\]]+)\]", text)
    if match:
        return match.group(1).strip().lower()
    return None

def open_app(app_name):
    """Open an application by name."""
    app_map = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "paint": "mspaint.exe",
        "explorer": "explorer.exe",
        "chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "edge": "msedge.exe",
        "word": "winword.exe",
        "excel": "excel.exe",
        "powerpoint": "powerpnt.exe",
        "outlook": "outlook.exe",
        "spotify": "spotify.exe",
        "vlc": "vlc.exe",
        "discord": "discord.exe",
        "steam": "steam.exe"
    }
    
    app_exe = app_map.get(app_name)
    if not app_exe:
        log(f"Unknown app: {app_name}")
        return False
    
    try:
        subprocess.Popen(app_exe, shell=True)
        log(f"Opened {app_name}")
        return True
    except Exception as e:
        log(f"Failed to open {app_name}: {e}")
        return False

def explain_error(e):
    if isinstance(e, NoKey):
        return "I'd love to chat, but my brain isn't plugged in. Open my console and paste an API key."
    if isinstance(e, ImportError):
        pkg = "anthropic" if "anthropic" in str(e) else "google-genai"
        return "A package is missing. Run:  python -m pip install " + pkg
    return "My brain hiccuped: " + str(e)[:160]

def arc(cx, cy, r, a0, a1, n=14):
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts

WIRE = ([26, 46] + arc(50, 108, 24, 180, 0) + arc(62, 36, 12, 0, -180) + arc(44, 100, 6, 0, 180) + [38, 64])

def make_tray_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pts = [(x * 0.5 + 8, y * 0.42 + 2) for x, y in zip(WIRE[0::2], WIRE[1::2])]
    d.line(pts, fill=(111, 115, 133, 255), width=7, joint="curve")
    d.line(pts, fill=(227, 230, 238, 255), width=3, joint="curve")
    d.ellipse((17, 15, 29, 31), fill="white", outline="black")
    d.ellipse((31, 15, 43, 31), fill="white", outline="black")
    d.ellipse((21, 21, 26, 27), fill="black")
    d.ellipse((35, 21, 40, 27), fill="black")
    return img

def grab_screen_jpeg(max_size=(1280, 800)):
    sct_cls = getattr(mss, "MSS", None) or mss.mss
    with sct_cls() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        img.thumbnail(max_size)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()

def speak_blocking(text):
    if os.name == "nt":
        try:
            env = dict(os.environ)
            env["CLIPPY_TEXT"] = text
            script = ("Add-Type -AssemblyName System.Speech; "
                      "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      "$s.Speak($env:CLIPPY_TEXT)")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           env=env, creationflags=0x08000000, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception as e: log("PowerShell speech failed:", e)
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception as e: log("TTS unavailable:", e)

# ---------------------------------------------------------------- the brain
class Brain:
    def __init__(self, settings):
        self.s = settings
        self.history = []
        self._client = None
        self._client_id = None
        self._auto_model = None
        self.lock = threading.Lock()

    def reset(self):
        self._client = None
        self._client_id = None
        self._auto_model = None

    def _key(self):
        p = self.s.get("provider")
        if p == "local": return self.s.get("local_key") or ""
        if p == "openrouter": return self.s.get("openrouter_key") or os.environ.get("OPENROUTER_API_KEY")
        if p == "claude": return self.s.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        return (self.s.get("gemini_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def has_key(self): return self.s.get("provider") == "local" or bool(self._key())

    def _get_client(self):
        key = self._key()
        if not key: raise NoKey()
        ident = (self.s.get("provider"), key)
        if self._client is None or ident != self._client_id:
            if self.s.get("provider") == "claude":
                from anthropic import Anthropic
                self._client = Anthropic(api_key=key)
            else:
                from google import genai
                self._client = genai.Client(api_key=key)
            self._client_id = ident
            self._auto_model = None
        return self._client

    def chat(self, user_text, with_screen, retries=2):
        with self.lock:
            provider = self.s.get("provider")
            compat = provider in ("local", "openrouter")
            if provider == "openrouter" and not self._key(): raise NoKey()
            client = None if compat else self._get_client()
            shot = None
            if with_screen:
                try: shot = grab_screen_jpeg((800, 500) if compat else (1280, 800))
                except Exception as e: log("Screenshot failed:", e)
            
            for attempt in range(retries + 1):
                try:
                    if provider == "local":
                        text = self._ask_compat(self.s.get("local_url") or DEFAULTS["local_url"],
                                                self.s.get("local_model") or DEFAULTS["local_model"],
                                                self._key(), user_text, shot)
                    elif provider == "openrouter":
                        first = self.s.get("openrouter_model") or DEFAULTS["openrouter_model"]
                        text, last_error = None, None
                        for m in dict.fromkeys([first] + OPENROUTER_FALLBACKS):
                            try:
                                text = self._ask_compat(OPENROUTER_URL, m, self._key(), user_text, shot)
                                break
                            except RuntimeError as e:
                                last_error = e
                                if str(e).startswith("401"): raise
                        if text is None: raise last_error
                    elif provider == "claude": text = self._ask_claude(client, user_text, shot)
                    else: text = self._ask_gemini(client, user_text, shot)
                    
                    # Check for empty response
                    if not text or not text.strip():
                        if attempt < retries:
                            log(f"Empty response, retrying ({attempt + 1}/{retries})")
                            continue
                        else:
                            log("Empty response after all retries")
                            return "Hmm, my brain froze for a second. Try asking me again?"
                    
                    text = tidy(text)
                    if not text or text == "...":
                        if attempt < retries:
                            log(f"Empty after tidy, retrying ({attempt + 1}/{retries})")
                            continue
                        else:
                            return "Sorry, I got confused. Can you rephrase that?"
                    
                    self.history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": text}]
                    self.history = self.history[-12:]
                    return text
                    
                except Exception as e:
                    if attempt < retries and not isinstance(e, NoKey):
                        log(f"Error, retrying ({attempt + 1}/{retries}): {e}")
                        time.sleep(1)
                        continue
                    raise
            
            return "Something went wrong. Try again?"

    def _ask_compat(self, base, model, key, user_text, shot):
        import urllib.error, urllib.request
        prompt = SYSTEM_PROMPT + "\n\n---\n\n" + user_text
        if shot:
            b64 = base64.standard_b64encode(shot).decode("ascii")
            content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}, {"type": "text", "text": prompt}]
        else: content = prompt
        msgs = list(self.history) + [{"role": "user", "content": content}]
        body = json.dumps({"model": model, "messages": msgs, "max_tokens": 700}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key: headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as r: data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            hint = "Key rejected." if e.code in (401, 403) else ("Rate limit." if e.code == 429 else "")
            raise RuntimeError(("%s %s %s" % (e.code, hint, detail)).strip())
        except urllib.error.URLError as e: raise RuntimeError("Can't reach AI server (%s)." % e.reason)
        except (OSError, ValueError) as e: raise RuntimeError("Network problem: %s" % e)
        
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise RuntimeError("Server error: " + str(err.get("message", err) if isinstance(err, dict) else err)[:160])
        try: content = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError): raise RuntimeError("Unexpected reply: " + str(data)[:160])
        if isinstance(content, list): content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = (content or "").strip()
        if not content: raise RuntimeError("The model sent an empty reply")
        return content

    def _ask_claude(self, client, user_text, shot):
        content = []
        if shot: content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(shot).decode("ascii")}})
        content.append({"type": "text", "text": user_text})
        resp = client.messages.create(model=self.s.get("model") or MODEL, max_tokens=250, system=SYSTEM_PROMPT, messages=self.history + [{"role": "user", "content": content}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def _ask_gemini(self, client, user_text, shot):
        from google.genai import types
        model = self._gemini_model(client)
        contents = [types.Content(role="user" if h["role"] == "user" else "model", parts=[types.Part.from_text(text=h["content"])]) for h in self.history]
        parts = []
        if shot: parts.append(types.Part.from_bytes(data=shot, mime_type="image/jpeg"))
        parts.append(types.Part.from_text(text=user_text))
        contents.append(types.Content(role="user", parts=parts))
        resp = client.models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, max_output_tokens=1024))
        return (resp.text or "").strip()

    def _gemini_model(self, client):
        chosen = (self.s.get("gemini_model") or "").strip()
        if chosen: return chosen
        if self._auto_model: return self._auto_model
        skip = ("tts", "image", "audio", "live", "native", "embedding", "robotics", "computer", "lite", "imagen", "veo", "gemma")
        best, best_rank = None, None
        for m in client.models.list():
            name = (m.name or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions: continue
            mt = re.match(r"gemini-(\d+(?:\.\d+)*)-flash", name)
            if not mt or any(w in name for w in skip): continue
            ver = tuple(int(x) for x in mt.group(1).split("."))
            stable = not any(w in name for w in ("preview", "exp"))
            rank = (ver, stable)
            if best_rank is None or rank > best_rank: best, best_rank = name, rank
        if not best: raise RuntimeError("No Gemini Flash model found.")
        self._auto_model = best
        return best

# ---------------------------------------------------------------- crack overlay
class CrackOverlay:
    """Persistent crack mark on the desktop."""
    def __init__(self, parent, x, y, crack_lines):
        self.parent = parent
        self.window = tk.Toplevel(parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.config(bg=TRANSPARENT)
        try:
            self.window.attributes("-transparentcolor", TRANSPARENT)
            self.window.attributes("-alpha", 0.9)
        except tk.TclError: pass
        
        # Make window click-through (Windows only)
        if os.name == 'nt':
            try:
                hwnd = int(self.window.frame(), 16) if hasattr(self.window, 'frame') else self.window.winfo_id()
                GWL_EXSTYLE = -20
                WS_EX_LAYERED = 0x80000
                WS_EX_TRANSPARENT = 0x20
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, 
                    ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            except Exception: pass
        
        self.canvas = tk.Canvas(self.window, width=150, height=150, bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack()
        
        # Position the crack
        self.window.geometry(f"150x150+{int(x - 75)}+{int(y - 75)}")
        
        # Draw crack lines
        for line in crack_lines:
            # Adjust coordinates relative to canvas center
            x1, y1, x2, y2 = line
            self.canvas.create_line(x1 + 75, y1 + 75, x2 + 75, y2 + 75, 
                                   width=2, fill="#333333", capstyle="round")
    
    def destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass

# ---------------------------------------------------------------- app
class Clippy:
    def __init__(self):
        self.s = load_settings()
        self.brain = Brain(self.s)
        self.tasks = queue.Queue()
        self.tts_lock = threading.Lock()
        
        self.root = tk.Tk()
        self.root.title("Clippy 2")
        self.root.overrideredirect(True)
        self.root.config(bg=TRANSPARENT)
        try: self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError: pass
        self.root.attributes("-topmost", self.s["always_on_top"])
        
        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack()
        
        self.sw = self.root.winfo_screenwidth()
        self.sh = self.root.winfo_screenheight()
        
        # Start off-screen for entrance animation
        self.x = float(-W - 20)
        self.y = float(self.sh - H - 90)
        self.root.geometry(f"{W}x{H}+{int(self.x)}+{int(self.y)}")
        
        self.target = None
        self.pending_question = None
        self.bubble = None
        self.thinking = False
        self.waiting = False
        self.talk_until = 0.0
        self.blink_until = 0.0
        self.next_blink = time.time() + 3
        self.next_ask = time.time() + 10
        self.next_wander = time.time() + 5
        
        # Animation states
        self.state = "normal"  # normal, cracked, fixing
        self.crack_overlay = None
        self.crack_fix_time = None
        self.tool_type = None
        self.entrance_anim = True
        self.entrance_start = time.time()
        self.bob_offset = 0.0
        
        self._told_tray = False
        
        self.build_console()
        self.start_tray()
        if self.brain.has_key(): self.root.after(1500, self.hide_console)
        self.tick()

    # ------------------------------------------------------------ drawing
    def draw(self):
        c = self.canvas
        c.delete("all")
        now = time.time()
        talking = now < self.talk_until
        blinking = now < self.blink_until
        
        # Bobbing animation while idle
        if self.state == "normal" and not self.target:
            self.bob_offset = 2 * math.sin(now * 2)
        else:
            self.bob_offset = 0
        
        # Draw based on state
        if self.state == "normal" or self.state == "cracked":
            self.draw_clippy(c, talking, blinking)
        elif self.state == "fixing":
            self.draw_tool(c)
        
        if talking and self.state != "fixing":
            c.move("all", 0, 2 * math.sin(now * 10))

    def draw_clippy(self, c, talking, blinking):
        bob = self.bob_offset
        
        wire_pts = [WIRE[i] + (bob if i % 2 == 1 else 0) for i in range(len(WIRE))]
        c.create_line(wire_pts, width=7, fill="#6f7385", capstyle="round", joinstyle="round")
        c.create_line(wire_pts, width=3, fill="#e3e6ee", capstyle="round", joinstyle="round")
        
        mx, my = self.root.winfo_pointerxy()
        ang = math.atan2(my - (self.y + 48), mx - (self.x + 50))
        dist = math.hypot(my - (self.y + 48), mx - (self.x + 50))
        mag = min(3.5, dist / 40)
        ox, oy = math.cos(ang) * mag, math.sin(ang) * mag
        
        for ex in (40, 62):
            ey = 48 + bob
            if blinking:
                c.create_line(ex - 8, ey, ex + 8, ey, width=3, fill="black", capstyle="round")
            else:
                c.create_oval(ex - 9, ey - 11, ex + 9, ey + 11, fill="white", outline="black", width=2)
                c.create_oval(ex + ox - 4, ey + oy - 4, ex + ox + 4, ey + oy + 4, fill="black")
        
        if self.waiting:
            c.create_line(30, 24 + bob, 48, 30 + bob, width=3, fill="black", capstyle="round")
            c.create_line(54, 30 + bob, 72, 24 + bob, width=3, fill="black", capstyle="round")
        elif talking:
            b = 4 * math.sin(now * 14)
            c.create_line(30, 30 - b + bob, 48, 27 - b + bob, width=3, fill="black", capstyle="round")
            c.create_line(54, 27 + b + bob, 72, 30 + b + bob, width=3, fill="black", capstyle="round")
        else:
            c.create_line(30, 30 + bob, 48, 27 + bob, width=3, fill="black", capstyle="round")
            c.create_line(54, 27 + bob, 72, 30 + bob, width=3, fill="black", capstyle="round")

    def draw_tool(self, c):
        now = time.time()
        spin = math.sin(now * 8) * 15
        
        if self.tool_type == "wrench":
            c.create_rectangle(40, 50 + spin, 60, 110 + spin, fill="#888", outline="#555", width=2)
            c.create_oval(35, 40 + spin, 65, 70 + spin, fill="#888", outline="#555", width=2)
            c.create_rectangle(45, 45 + spin, 55, 55 + spin, fill="#333")
        elif self.tool_type == "hammer":
            c.create_rectangle(45, 60 + spin, 55, 120 + spin, fill="#8B4513", outline="#5a2d0c", width=2)
            c.create_rectangle(30, 40 + spin, 70, 65 + spin, fill="#888", outline="#555", width=2)
        elif self.tool_type == "screwdriver":
            c.create_rectangle(45, 70 + spin, 55, 130 + spin, fill="#ff6600", outline="#cc5200", width=2)
            c.create_polygon(45, 70 + spin, 55, 70 + spin, 50, 40 + spin, fill="#888", outline="#555", width=2)

    # ------------------------------------------------------------ main loop
    def tick(self):
        try: self._tick()
        except Exception: log("tick error:\n" + traceback.format_exc())
        try: self.root.after(50, self.tick)
        except tk.TclError: pass

    def _tick(self):
        now = time.time()
        while True:
            try: task = self.tasks.get_nowait()
            except queue.Empty: break
            try: task()
            except Exception: log("task error:\n" + traceback.format_exc())

        # Entrance animation
        if self.entrance_anim:
            elapsed = now - self.entrance_start
            if elapsed < 1.5:
                progress = elapsed / 1.5
                self.x = -W + (self.sw - W - 60 + W) * self.ease_out_bounce(progress)
                self.root.geometry(f"+{int(self.x)}+{int(self.y)}")
            else:
                self.entrance_anim = False
                self.x = float(self.sw - W - 60)
                self.root.geometry(f"+{int(self.x)}+{int(self.y)}")
            self.draw()
            return

        # Check if it's time to fix the crack
        if self.state == "cracked" and self.crack_fix_time and now >= self.crack_fix_time:
            self.start_fixing()
        
        # Fixing animation timer
        if self.state == "fixing":
            if now > self.tool_timer:
                self.state = "normal"
                self.tool_type = None
                if self.crack_overlay:
                    self.crack_overlay.destroy()
                    self.crack_overlay = None
                self.crack_fix_time = None
            else:
                self.draw()
                return

        if now >= self.next_blink:
            self.blink_until = now + 0.15
            self.next_blink = now + random.uniform(2.5, 5.5)

        busy = self.bubble is not None or self.pending_question is not None or self.thinking
        if self.s["watching"] and not busy and now >= self.next_ask:
            self.ask_something()
        elif not busy and self.target is None and now >= self.next_wander:
            self.wander()

        self.move_step()
        self.draw()

    def ease_out_bounce(self, x):
        n1 = 7.5625
        d1 = 2.75
        if x < 1 / d1:
            return n1 * x * x
        elif x < 2 / d1:
            x -= 1.5 / d1
            return n1 * x * x + 0.75
        elif x < 2.5 / d1:
            x -= 2.25 / d1
            return n1 * x * x + 0.9375
        else:
            x -= 2.625 / d1
            return n1 * x * x + 0.984375

    # ------------------------------------------------------------ movement
    def wander(self):
        if random.random() < 0.4:
            edge = random.choice(["left", "right", "top"])
            if edge == "left":
                self.target = (-W - 50, self.y)
            elif edge == "right":
                self.target = (self.sw + 50, self.y)
            else:
                self.target = (self.x, -H - 50)
        else:
            self.target = (random.randint(0, max(0, self.sw - W)),
                           max(0, self.sh - H - random.randint(50, 130)))
        self.next_wander = time.time() + random.uniform(6, 15)

    def move_step(self):
        if not self.target: return
        tx, ty = self.target
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        
        if dist <= SPEED:
            self.x, self.y = tx, ty
            
            if self.x < -W or self.x > self.sw or self.y < -H:
                if self.x < -W:
                    self.x = self.sw + 10
                elif self.x > self.sw:
                    self.x = -W - 10
                if self.y < -H:
                    self.y = self.sh - H - 50
                
                self.target = (random.randint(50, self.sw - W - 50),
                               max(50, self.sh - H - random.randint(50, 130)))
            else:
                self.target = None
                self.on_arrive()
        else:
            self.x += dx / dist * SPEED
            self.y += dy / dist * SPEED
        
        self.root.geometry(f"+{int(self.x)}+{int(self.y)}")

    def on_arrive(self):
        # 15% chance to crack the glass (only if not already cracked)
        if self.state == "normal" and random.random() < 0.15:
            self.crack_glass()

        if self.pending_question:
            q, self.pending_question = self.pending_question, None
            self.show_bubble(q)

    def crack_glass(self):
        self.state = "cracked"
        # Generate random crack lines
        crack_lines = []
        cx, cy = 0, 0
        for _ in range(5):
            angle = random.uniform(0, 2 * math.pi)
            length = random.randint(15, 35)
            ex = cx + length * math.cos(angle)
            ey = cy + length * math.sin(angle)
            crack_lines.append([cx, cy, ex, ey])
            
            if random.random() < 0.5:
                branch_angle = angle + random.uniform(-0.5, 0.5)
                branch_len = random.randint(8, 15)
                bx = ex + branch_len * math.cos(branch_angle)
                by = ey + branch_len * math.sin(branch_angle)
                crack_lines.append([ex, ey, bx, by])
        
        # Create persistent crack overlay
        crack_x = self.x + W // 2
        crack_y = self.y + H // 2
        self.crack_overlay = CrackOverlay(self.root, crack_x, crack_y, crack_lines)
        
        # Schedule fix after 5-10 minutes
        fix_delay = random.uniform(300, 600)  # 5-10 minutes in seconds
        self.crack_fix_time = time.time() + fix_delay
        log(f"Glass cracked! Will fix in {fix_delay/60:.1f} minutes")

    def start_fixing(self):
        self.state = "fixing"
        self.tool_type = random.choice(["wrench", "hammer", "screwdriver"])
        self.tool_timer = time.time() + 2.0  # Fix for 2 seconds
        log("Fixing the crack...")

    # ------------------------------------------------------------ AI conversation
    def ask_something(self):
        if self.thinking or self.bubble is not None: return
        self.thinking = True
        self.next_ask = time.time() + 60
        def work():
            try:
                if self.s["watching"]:
                    text = self.brain.chat("Look at my screen and start a short, cheeky conversation.", True)
                else:
                    text = self.brain.chat("Start a short, friendly conversation. You can't see my screen.", False)
                
                # Check for app opening command
                app_cmd = extract_open_command(text)
                if app_cmd:
                    self.tasks.put(lambda: open_app(app_cmd))
                    
            except Exception as e:
                log("AI error:", repr(e))
                text = explain_error(e)
            self.tasks.put(lambda: self.begin_visit(text))
        threading.Thread(target=work, daemon=True).start()

    def begin_visit(self, text):
        self.thinking = False
        self.pending_question = text
        px, py = self.root.winfo_pointerxy()
        tx = min(max(px - W - 30, 0), max(0, self.sw - W))
        ty = min(max(py - H // 2, 0), max(0, self.sh - H))
        self.target = (tx, ty)

    def on_reply(self):
        if self.waiting: return
        text = self.entry.get().strip()
        if not text: return
        self.entry.delete(0, "end")
        self.waiting = True
        self.bubble_label.config(text="Hmm, let me think...")
        self.place_bubble()
        def work():
            try: 
                answer = self.brain.chat(text, self.s["watching"])
                # Check for app opening command
                app_cmd = extract_open_command(answer)
                if app_cmd:
                    self.tasks.put(lambda: open_app(app_cmd))
            except Exception as e:
                log("AI error:", repr(e))
                answer = explain_error(e)
            self.tasks.put(lambda: self.show_answer(answer))
        threading.Thread(target=work, daemon=True).start()

    def show_answer(self, answer):
        self.waiting = False
        if self.bubble is not None:
            self.bubble_label.config(text=answer)
            self.place_bubble()
            self.say(answer)

    # ------------------------------------------------------------ speech bubble
    def show_bubble(self, text):
        self.close_bubble()
        b = tk.Toplevel(self.root)
        b.overrideredirect(True)
        b.attributes("-topmost", True)
        frame = tk.Frame(b, bg="#fff7c2", highlightbackground="black", highlightthickness=2)
        frame.pack()
        self.bubble_label = tk.Label(frame, text=text, wraplength=260, justify="left",
                                     bg="#fff7c2", font=("Segoe UI", 10), padx=8, pady=6)
        self.bubble_label.pack()
        self.entry = tk.Entry(frame, width=34)
        self.entry.pack(padx=8, pady=(0, 4))
        self.entry.bind("<Return>", lambda e: self.on_reply())
        row = tk.Frame(frame, bg="#fff7c2")
        row.pack(pady=(0, 6))
        tk.Button(row, text="Send", command=self.on_reply).pack(side="left", padx=4)
        tk.Button(row, text="Go away", command=self.close_bubble).pack(side="left", padx=4)
        self.bubble = b
        self.place_bubble()
        self.entry.focus_force()
        self.say(text)

    def place_bubble(self):
        b = self.bubble
        if b is None: return
        b.update_idletasks()
        bw, bh = b.winfo_width(), b.winfo_height()
        bx = min(max(int(self.x) + W // 2 - bw // 2, 0), max(0, self.sw - bw))
        by = max(int(self.y) - bh - 5, 0)
        b.geometry(f"+{bx}+{by}")

    def close_bubble(self):
        if self.bubble is not None:
            self.bubble.destroy()
            self.bubble = None
        self.next_ask = time.time() + random.uniform(45, 90)

    # ------------------------------------------------------------ voice
    def say(self, text):
        self.talk_until = time.time() + max(2.0, len(text) * 0.06)
        if not self.s["voice"]: return
        def run():
            with self.tts_lock: speak_blocking(text)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ console
    def build_console(self):
        c = tk.Toplevel(self.root)
        c.title("Clippy 2 - Console")
        c.geometry("350x570")
        c.resizable(False, False)
        c.protocol("WM_DELETE_WINDOW", self.hide_console)
        c.bind("<Unmap>", self.on_console_unmap)
        self.console = c
        
        self.var_watch = tk.BooleanVar(value=self.s["watching"])
        self.var_top = tk.BooleanVar(value=self.s["always_on_top"])
        self.var_voice = tk.BooleanVar(value=self.s["voice"])
        self.var_cool = tk.IntVar(value=self.s["cooldown_min"])
        self.var_provider = tk.StringVar(value=PROVIDERS.get(self.s["provider"], PROVIDERS["openrouter"]))
        self.var_key = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_url = tk.StringVar()
        self.load_provider_fields()
        
        pad = {"padx": 14, "pady": 5, "anchor": "w"}
        ttk.Label(c, text="Clippy 2 settings", font=("Segoe UI", 12, "bold")).pack(**pad)
        ttk.Checkbutton(c, text="Watching (look at my screen and check in)", variable=self.var_watch, command=self.apply_settings).pack(**pad)
        ttk.Checkbutton(c, text="Always stay on top of the desktop", variable=self.var_top, command=self.apply_settings).pack(**pad)
        ttk.Checkbutton(c, text="Talk out loud (TTS)", variable=self.var_voice, command=self.apply_settings).pack(**pad)
        
        row = ttk.Frame(c)
        row.pack(**pad)
        ttk.Label(row, text="Minutes between check-ins:").pack(side="left")
        ttk.Spinbox(row, from_=1, to=120, width=4, textvariable=self.var_cool, command=self.apply_settings).pack(side="left", padx=6)
        
        ttk.Label(c, text="AI brain:").pack(**pad)
        cmb = ttk.Combobox(c, textvariable=self.var_provider, state="readonly", width=34, values=list(PROVIDERS.values()))
        cmb.pack(**pad)
        cmb.bind("<<ComboboxSelected>>", self.on_provider_change)
        
        ttk.Label(c, text="API key (not needed for local):").pack(**pad)
        keyrow = ttk.Frame(c)
        keyrow.pack(**pad)
        ttk.Entry(keyrow, textvariable=self.var_key, show="*", width=30).pack(side="left")
        ttk.Button(keyrow, text="Save", width=6, command=self.save_key).pack(side="left", padx=6)
        
        ttk.Label(c, text="Model (leave empty = automatic):").pack(**pad)
        ttk.Entry(c, textvariable=self.var_model, width=38).pack(**pad)
        ttk.Label(c, text="Server URL (local only):").pack(**pad)
        ttk.Entry(c, textvariable=self.var_url, width=38).pack(**pad)
        
        self.lbl_brain = ttk.Label(c, text="", wraplength=310, justify="left")
        self.lbl_brain.pack(**pad)
        self.refresh_brain_label()
        
        ttk.Button(c, text="Ask me something now", command=self.ask_now).pack(**pad)
        btns = ttk.Frame(c)
        btns.pack(**pad)
        ttk.Button(btns, text="Hide to tray", command=self.hide_console).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Quit Clippy", command=self.quit).pack(side="left")

    def key_names(self):
        p = self.s["provider"]
        if p == "claude": return "api_key", "model"
        if p == "local": return "local_key", "local_model"
        if p == "openrouter": return "openrouter_key", "openrouter_model"
        return "gemini_key", "gemini_model"

    def load_provider_fields(self):
        k, m = self.key_names()
        self.var_key.set(self.s.get(k, ""))
        self.var_model.set(self.s.get(m, ""))
        self.var_url.set(self.s.get("local_url", ""))

    def on_provider_change(self, event=None):
        for name, label in PROVIDERS.items():
            if label == self.var_provider.get():
                self.s["provider"] = name
        save_settings(self.s)
        self.brain.reset()
        self.load_provider_fields()
        self.refresh_brain_label()

    def save_key(self):
        k, m = self.key_names()
        self.s[k] = self.var_key.get().strip()
        self.s[m] = self.var_model.get().strip()
        self.s["local_url"] = self.var_url.get().strip() or DEFAULTS["local_url"]
        self.brain.reset()
        save_settings(self.s)
        self.refresh_brain_label()

    def refresh_brain_label(self):
        p = self.s["provider"]
        if p == "local":
            text = "Brain: local model. Nothing leaves your PC. Needs Ollama running."
        else:
            text = "Brain: connected" if self.brain.has_key() else "Brain: no API key yet"
        self.lbl_brain.config(text=text)

    def apply_settings(self):
        self.s["watching"] = bool(self.var_watch.get())
        self.s["always_on_top"] = bool(self.var_top.get())
        self.s["voice"] = bool(self.var_voice.get())
        try: self.s["cooldown_min"] = int(self.var_cool.get())
        except (tk.TclError, ValueError): pass
        self.root.attributes("-topmost", self.s["always_on_top"])
        save_settings(self.s)
        if hasattr(self, "tray"): self.tray.update_menu()

    def show_console(self):
        self.console.deiconify()
        self.console.lift()
        self.console.focus_force()

    def on_console_unmap(self, event):
        if event.widget is self.console and self.console.state() == "iconic":
            self.console.after(50, self.hide_console)

    def hide_console(self):
        self.console.withdraw()
        if not self._told_tray:
            self._told_tray = True
            try: self.tray.notify("I'm still here! Double-click my tray icon to reopen.", "Clippy 2")
            except Exception: pass

    def ask_now(self): self.ask_something()

    # ------------------------------------------------------------ tray
    def toggle_watching(self):
        self.var_watch.set(not self.var_watch.get())
        self.apply_settings()

    def start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open console", lambda: self.tasks.put(self.show_console), default=True),
            pystray.MenuItem("Watching", lambda: self.tasks.put(self.toggle_watching), checked=lambda item: self.s["watching"]),
            pystray.MenuItem("Quit", lambda: self.tasks.put(self.quit)),
        )
        self.tray = pystray.Icon("clippy2", make_tray_icon(), "Clippy 2", menu)
        self.tray.run_detached()

    def quit(self):
        save_settings(self.s)
        if self.crack_overlay:
            self.crack_overlay.destroy()
        try: self.tray.stop()
        except Exception: pass
        self.root.destroy()

    def run(self): self.root.mainloop()

if __name__ == "__main__":
    try: Clippy().run()
    except Exception:
        log("CRASH:\n" + traceback.format_exc())
        try: input("Clippy crashed. Details are in %s\nPress Enter to close..." % LOG_FILE)
        except Exception: pass