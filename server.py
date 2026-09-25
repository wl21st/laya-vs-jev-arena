"""Static file server + model backends for the Typed Decision Lab.

Layout: /snake and /fight are the two arenas, /shared holds the model plumbing both
use. The server serves this whole folder and both API routes from one process.

Two decision backends, one request shape (TypeSafe System One: state + questions):

  POST /api/jev    -> proxied to api.typesafe.ai (the key stays in this process,
                      the browser never sees it)
  POST /api/laya   -> convaiinnovations/laya running locally, in-process, via the
                      `laya` package. Same state/questions payload, same answer
                      shape, no network hop.

    python server.py [port]
"""
import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
UPSTREAM = "https://api.typesafe.ai/v1/systemone"
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
LAYA_CHECKPOINT = os.environ.get("LAYA_CHECKPOINT", "typed-decisions")
# the Docker image turns Laya off: on a CPU-only VPS it needs ~1.8 s a decision
ENABLE_LAYA = os.environ.get("ENABLE_LAYA", "1") not in ("0", "false", "no")
HOST = os.environ.get("HOST", "127.0.0.1")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") in ("1", "true", "yes")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Guards for a public instance, where every visitor spends the server's Jev key.
# A visitor who brings their own key (X-Jev-Key header) skips the daily cap.
RATE_PER_IP = env_int("JEV_RATE_PER_MIN", 360)      # calls / minute / visitor (one Jev side ~ 150)
MAX_INFLIGHT = env_int("JEV_MAX_INFLIGHT", 24)      # upstream calls at once, whole server
DAILY_CAP = env_int("JEV_DAILY_CAP", 20000)         # calls / day on the server key, 0 = no cap


class Limits:
    def __init__(self):
        self.lock = threading.Lock()
        self.hits = defaultdict(deque)
        self.inflight = 0
        self.day = time.strftime("%Y-%m-%d")
        self.used = 0

    def admit(self, ip, own_key):
        """None if the call may go ahead, else (status, message)."""
        now = time.time()
        with self.lock:
            q = self.hits[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_IP:
                return 429, "slow down: too many AI calls from you this minute"
            if self.inflight >= MAX_INFLIGHT:
                return 503, "server busy: too many AI matches right now, try again in a moment"
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.used = today, 0
            if not own_key and DAILY_CAP and self.used >= DAILY_CAP:
                return 429, "today's free Jev calls are used up - paste your own key on the front page"
            q.append(now)
            self.inflight += 1
            if not own_key:
                self.used += 1
            if len(self.hits) > 5000:  # forget idle visitors
                for k in [k for k, v in self.hits.items() if not v]:
                    del self.hits[k]
        return None

    def release(self):
        with self.lock:
            self.inflight -= 1


LIMITS = Limits()


def load_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


API_KEY = load_key()

_laya_router = None
_laya_error = None
# ThreadingHTTPServer handles each browser request in its own thread. Laya's
# checkpoint loading and inference share device state and are not safe to overlap
# (on macOS this can fail inside the AGX/Metal command buffer code).
_laya_load_lock = threading.Lock()
_laya_predict_lock = threading.Lock()


def laya_router():
    """Load the local Laya model once, on first use."""
    global _laya_router, _laya_error
    if _laya_router is not None or _laya_error is not None:
        return _laya_router
    with _laya_load_lock:
        # Several Laya agents can prime at the same time, so re-check after
        # waiting for the thread that won the initialization race.
        if _laya_router is not None or _laya_error is not None:
            return _laya_router
        try:
            from laya import Router
            print("loading laya (first call warms the checkpoints)...")
            _laya_router = Router(preload=True, max_loaded=3)
            print("laya ready")
        except Exception as exc:
            _laya_error = f"laya unavailable: {exc}. Install it with: pip install laya"
            print(_laya_error)
    return _laya_router


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):  # keep the console readable
        if "/api/jev" not in (self.path or ""):
            return
        sys.stderr.write("jev %s\n" % (fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/config":
            return self._json(200, {"laya": ENABLE_LAYA, "serverKey": bool(API_KEY)})
        if self.hidden():
            return self._json(404, {"error": "not found"})
        return super().do_GET()

    def do_HEAD(self):
        if self.hidden():
            return self._json(404, {"error": "not found"})
        return super().do_HEAD()

    def hidden(self):
        """never hand out .env, .git or the server source"""
        parts = urllib.parse.unquote(self.path.split("?")[0]).replace("\\", "/").split("/")
        return any(p.startswith(".") for p in parts if p) or parts[-1].endswith((".py", ".toml"))

    def client_ip(self):
        # behind Caddy the real visitor is in X-Forwarded-For; without a proxy that
        # header is whatever the visitor typed, so only trust it when told to
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            if fwd:
                return fwd
        return self.client_address[0]

    def do_POST(self):
        route = self.path.split("?")[0]
        if route not in ("/api/jev", "/api/laya"):
            return self._json(404, {"error": "not found"})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            incoming = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": f"bad request body: {exc}"})

        if route == "/api/laya":
            return self._laya(incoming)
        return self._typesafe(incoming)

    def _laya(self, incoming):
        if not ENABLE_LAYA:
            return self._json(503, {"error": "Laya is switched off on this server (ENABLE_LAYA=0)"})
        router = laya_router()
        if router is None:
            return self._json(503, {"error": _laya_error})
        try:
            # Laya's router picks a checkpoint by language and by matching question ids to
            # its built-in workflows. Custom questions match none, so it falls back to the
            # base English checkpoint -- near chance on typed decisions, and on the snake it
            # answered HARD_LEFT whatever the state. Ask for the typed-decisions one.
            # The Router may be shared by several browser-side workers. Keep
            # inference single-file so CPU/GPU backends cannot encode concurrent
            # work into the same device command buffer.
            with _laya_predict_lock:
                res = router.predict(incoming.get("state"), incoming.get("questions"),
                                     model=incoming.get("laya_checkpoint") or LAYA_CHECKPOINT)
        except Exception as exc:
            return self._json(500, {"error": f"laya predict failed: {exc}"})
        if not isinstance(res, dict):
            return self._json(500, {"error": "laya returned an unexpected payload"})
        res.setdefault("model", incoming.get("model") or "laya")
        return self._json(200, res)

    def _typesafe(self, incoming):
        own = (self.headers.get("X-Jev-Key") or "").strip()
        key = own or API_KEY
        if not key:
            return self._json(401, {"error": "no Jev API key: paste yours on the front page"})
        denied = LIMITS.admit(self.client_ip(), bool(own))
        if denied:
            return self._json(denied[0], {"error": denied[1]})
        try:
            return self._forward(incoming, key)
        finally:
            LIMITS.release()

    def _forward(self, incoming, key):
        payload = {
            "model": incoming.get("model", MODEL),
            "state": incoming.get("state"),
            "questions": incoming.get("questions"),
        }
        req = urllib.request.Request(
            UPSTREAM,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return self._json(resp.status, json.loads(resp.read()))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            return self._json(exc.code, {"error": f"typesafe {exc.code}: {detail}"})
        except Exception as exc:  # network, timeout, malformed upstream JSON
            return self._json(502, {"error": f"upstream failure: {exc}"})

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else env_int("PORT", 8732)
    if not API_KEY:
        print("No TYPESAFE_API_KEY set: players must paste their own Jev key on the front page.")
    print(f"Lab    ->  http://localhost:{port}/")
    print(f"Snake  ->  http://localhost:{port}/snake/")
    print(f"Kombat ->  http://localhost:{port}/fight/")
    print(f"Jev proxy -> {UPSTREAM} (model {MODEL})")
    if ENABLE_LAYA:
        print(f"Laya      -> local in-process, checkpoint '{LAYA_CHECKPOINT}' (loaded on first call)")
    else:
        print("Laya      -> off (ENABLE_LAYA=0)")
    print(f"Limits    -> {RATE_PER_IP}/min per visitor, {MAX_INFLIGHT} at once, daily cap {DAILY_CAP or 'none'}")
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
