"""StrixFold UI's photo look on this Mac: a small vision model describes a photo the user attached, for the chat models that read text
only (DeepSeek, official and uncensored), and answers their narrower follow-up questions about it (look_at_image,
chat_look.py).

The model (MODEL: Qwen3.5 9B, 4-bit MLX, pinned by revision and each file's sha256 in its folder's MANIFEST.json) runs
in a warm worker (look_worker.py, the look venv's mlx-vlm) on this Mac's GPU, started on the first photo and stopped
after IDLE_S unused (10 min), as dictation's is; it does not start while the Mac's memory pressure is at warning. It
runs under Seatbelt with no network and a deny-default profile (profile()): it reads its venv, the model and its own
folder in the private store, and nothing else. A photo is copied into that folder for the request and deleted at once;
its description is kept beside the upload (refs/<id>.look.json, deleted with it) and reused for the same bytes (the
upload's sha256). Nothing about a photo goes into a log.

Chosen by tools/look_eval (twenty synthetic images, Qwen3.5 9B against 4B; docs/results/look-eval-1): every fact and
piece of text right, 93% of the questions (its misses are counts), ~9 s a description and ~4 s a question when warm.
"""
import hashlib
import json
import os
import secrets
import select
import shutil
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.expanduser("~/Library/Application Support/StrixFoldUI/look-venv")
WORKER = os.path.join(HERE, "look_worker.py")
MANIFEST = os.path.join(os.path.dirname(HERE), "tools", "look-model.json")  # the reviewed pins (setup_look.sh checks them too)
MODEL = {"name": "Qwen3.5 9B", "repo": "mlx-community/Qwen3.5-9B-MLX-4bit", "revision": "938d8919941c6e7efd3c7150eff7fe9d12afa631",
         "dir": os.path.expanduser("~/models/Qwen3.5-9B-MLX-4bit@938d891")}
IDLE_S = 10 * 60          # the warm worker (6.2 GB; it takes this 24 GB Mac from normal memory pressure to warning) goes
                          # after this long unused: shorter than dictation's 15 min; a cold start costs ~12 s
START_S = 180             # a cold start reads ~6 GB from disk
DESCRIBE_SIDE, ASK_SIDE = 1280, 1600   # the longer side the model sees (pixels): a description, a closer look
DESCRIBE_TOKENS, ASK_TOKENS = 700, 300
BLOCK_CHARS = DESCRIBE_TOKENS * 4 + 300  # the most a description block can add to a message (~4 characters a token, its label)
PROMPT_V = 1              # a description is reused only for the same bytes, model revision and prompt version
SANDBOX = "/usr/bin/sandbox-exec"
# what Metal needs inside the deny-default profile (probed: without IOSurfaceRootUserClient MLX can't make
# its shared event; without the compiler service it can't load a kernel)
GPU = ('(allow iokit-open (iokit-user-client-class "AGXDeviceUserClient" "IOSurfaceRootUserClient"))',
       '(allow mach-lookup (global-name "com.apple.MTLCompilerService"))')

DESCRIBE_PROMPT = (
    "Describe this image for someone who cannot see it, as a plain factual report.\n"
    "1. First, what kind of image it is (a photo, screenshot, chart, document, drawing...) and its main subject.\n"
    "2. The main things in it: what they are, their colours, where they are (left, right, top, bottom) and how many.\n"
    "3. Every piece of readable text, copied exactly as written, in reading order.\n"
    "4. For a chart or a table: its title, labels and every value you can read.\n"
    "Say what is unclear instead of guessing. No opinions about style or mood. At most 250 words.")
ASK_PROMPT = ("Answer this question about the image: {q}\n"
              "Answer briefly and exactly, and copy any text exactly as written. If the image does not show the answer, "
              "say so instead of guessing.")


def profile(tmp, venv=VENV, model_dir=None):
    """sandbox-exec's profile for the worker: sandbox.strict_profile (deny default, no network, no
    LaunchServices or Keychain) reading only the venv, the model and this folder (look_worker.py), writing only `tmp`,
    plus the GPU's two driver clients and Metal's compiler service."""
    import sandbox
    return sandbox.strict_profile([tmp], [venv, model_dir or MODEL["dir"], HERE]) + "\n".join(GPU) + "\n"


_VERIFIED = {}  # model folder -> {file: (size, mtime_ns, ctime_ns, inode)} as last hashed and matched in this app run


def verify_model(model_dir, manifest=MANIFEST):
    """None when every file the pinned manifest lists is in the model folder with its sha256, else what is wrong (in
    words). A file in the folder that the manifest doesn't list is refused too (a loader picks up extras such as
    added_tokens.json or chat_template.json; MANIFEST.json, setup_look's copy, is the one allowed). Hashed in full once
    per app run (~6 GB, a few seconds, on the first start); after that a file is hashed again only when its size,
    mtime, ctime or inode changed (mtime can be set back, ctime can't). Run before each start of the real worker."""
    try:
        with open(manifest) as f:
            files = json.load(f)["files"]
    except (OSError, ValueError, KeyError):
        return "its pinned manifest is missing"
    try:
        extra = sorted(set(os.listdir(model_dir)) - set(files) - {"MANIFEST.json"})
    except OSError:
        return "its folder is missing"
    if extra:
        return f"{extra[0]} is not one of its pinned files"
    seen, now_ = _VERIFIED.get(model_dir) or {}, {}
    for name, want in sorted(files.items()):
        p = os.path.join(model_dir, name)
        try:
            st = os.stat(p)
        except OSError:
            return f"{name} is missing"
        if st.st_size != want["bytes"]:
            return f"{name} has the wrong size"
        key = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        if seen.get(name) != key:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for ch in iter(lambda: f.read(1 << 22), b""):
                    h.update(ch)
            if h.hexdigest() != want["sha256"]:
                return f"{name} does not match its pinned sha256"
        now_[name] = key
    _VERIFIED[model_dir] = now_
    return None


def pressure():
    """The kernel's memory pressure level: 1 normal, 2 warning, 4 critical (None when it can't be read). The page
    counts mislead on a Mac that compresses a lot."""
    try:
        return int(subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True,
                                  timeout=10).stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


_pressure = pressure


class Looker:
    """The warm worker and the descriptions. One request at a time (the GPU does one well); a description started at
    upload is waited for, never run twice."""

    def __init__(self, store, venv=VENV, model_dir=None, worker_cmd=None, sandbox=True, tmp=None, pressure="auto",
                 manifest=MANIFEST):
        self.s = store
        self.venv, self.model_dir = venv, model_dir or MODEL["dir"]
        self.python = os.path.join(venv, "bin", "python")
        self.manifest = manifest
        self.tmp = tmp or store.path("look")
        self.cmd = worker_cmd or [self.python, "-I", WORKER, self.model_dir]
        self.sandbox = sandbox and worker_cmd is None
        # the memory guard ("auto": the kernel's level for the real model; none for a fake worker, which loads nothing)
        self.pressure = (_pressure if worker_cmd is None else None) if pressure == "auto" else pressure
        self.mu = threading.Lock()      # the worker: one request at a time
        self.jobs_mu = threading.Lock()
        self.jobs = {}                  # upload id -> Event while its description is being made
        self.failed = set()             # upload ids whose description could not be made (the chip says so)
        self.proc = None
        self.used = 0.0
        self.stats = {"starts": 0, "last_start_s": None}
        threading.Thread(target=self._reaper, name="look", daemon=True).start()

    # ------------------------------------------------------------ the worker
    def available(self):
        if self.cmd[0] != self.python:  # a test's fake worker
            return True
        return os.path.exists(self.python) and os.path.isfile(os.path.join(self.model_dir, "config.json"))

    def _reaper(self):
        while True:
            time.sleep(60)
            self.reap()

    def reap(self, now=None):
        """Stop the worker once it has gone IDLE_S unused (its memory back to the Mac). True when it stopped it."""
        with self.mu:
            if self.proc and (now or time.time()) - self.used > IDLE_S:
                self.close()
                return True
        return False

    def command(self):
        """The worker's command line: in its Seatbelt profile (no network) unless it is a test's fake."""
        os.makedirs(self.tmp, mode=0o700, exist_ok=True)  # the profile names only folders that exist
        return [SANDBOX, "-p", profile(self.tmp, self.venv, self.model_dir)] + self.cmd if self.sandbox else list(self.cmd)

    def close(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()
            p.wait(5)

    def _worker(self):
        self.used = time.time()
        if self.proc and self.proc.poll() is None:
            return self.proc
        level = self.pressure() if self.pressure else None
        if level is not None and level >= 2:  # loading ~6 GB under warning pressure would push the whole Mac into swap
            raise RuntimeError("this Mac is short of memory right now")
        if self.cmd[0] == self.python:  # the real model: its files as pinned (a test's fake worker loads none)
            why = verify_model(self.model_dir, self.manifest)
            if why:
                raise RuntimeError(f"the photo model's files are not as pinned: {why}")
        os.makedirs(self.tmp, mode=0o700, exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmp, "TMPDIR": self.tmp, "HF_HOME": self.tmp, "HF_HUB_OFFLINE": "1",
               "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false"}
        t = time.time()
        self.proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                     cwd=self.tmp)
        ready = self._read(self.proc, START_S)
        if not ready or not ready.get("ready"):
            self.close()
            raise RuntimeError("the photo model did not start")
        self.stats.update(starts=self.stats["starts"] + 1, last_start_s=round(time.time() - t, 2))
        return self.proc

    @staticmethod
    def _read(proc, timeout):
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            r, _, _ = select.select([proc.stdout], [], [], max(0.0, end - time.time()))
            if not r:
                break
            ch = os.read(proc.stdout.fileno(), 65536)
            if not ch:
                return None
            buf += ch
            if b"\n" in buf:
                try:
                    return json.loads(buf.split(b"\n", 1)[0])
                except ValueError:
                    return None
        return None

    def run(self, path, prompt, side, max_tokens, timeout=180):
        """One look: the image at `path` (copied into the worker's folder for the request, then deleted) and a prompt.
        The worker's answer ({"text", "secs", ...}, "cold": the worker was started for it). RuntimeError when the
        model is not set up, could not start or did not answer."""
        if not self.available():
            raise RuntimeError("the photo model is not set up on this Mac")
        ext = os.path.splitext(path)[1].lower()[:6] or ".img"
        with self.mu:
            os.makedirs(self.tmp, mode=0o700, exist_ok=True)
            tmp = os.path.join(self.tmp, f"p{secrets.token_hex(8)}{ext}")
            try:
                shutil.copyfile(path, tmp)
                os.chmod(tmp, 0o600)
                cold = not (self.proc and self.proc.poll() is None)
                t = time.time()
                proc = self._worker()
                proc.stdin.write((json.dumps({"image": tmp, "prompt": prompt, "side": side, "max_tokens": max_tokens}) + "\n").encode())
                proc.stdin.flush()
                out = self._read(proc, timeout)
                self.used = time.time()
            except (OSError, ValueError) as e:  # a worker that died under the write
                self.close()
                raise RuntimeError("the photo model stopped") from e
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if out is None:
            self.close()
            raise RuntimeError("the photo model did not answer")
        if out.get("error"):
            raise RuntimeError("the photo model could not read this image")
        return dict(out, cold=cold, wall_s=round(time.time() - t, 3))

    # ------------------------------------------------------------ descriptions
    def _image(self, uid):
        u = self.s.upload(uid)
        if u["kind"] != "image":
            raise ValueError("not an image")
        return u, self.s.path("refs", f"{uid}.{u['type']}")

    def state(self, uid):
        """For the photo's chip: "seen" (described), "looking", "failed", or None (not started, or not set up)."""
        if look_text(self.s, uid) is not None:
            return "seen"
        if uid in self.jobs:
            return "looking"
        return "failed" if uid in self.failed else None

    def start(self, uid):
        """Describe an attached photo in the background (from its upload, while the user types). False when there is
        nothing to do: not an image, already described, or no model."""
        try:
            u, _ = self._image(uid)
        except (KeyError, ValueError):
            return False
        if not self.available() or look_text(self.s, uid) is not None or self._reuse(uid, u):
            return False
        with self.jobs_mu:
            if uid in self.jobs:
                return False
            ev = self.jobs[uid] = threading.Event()
        threading.Thread(target=self._describe_job, args=(uid, ev), name="look-describe", daemon=True).start()
        return True

    def _describe_job(self, uid, ev):
        try:
            self._describe(uid)
        except Exception:  # noqa: BLE001  (the chip says it could not; the chat falls back to the text read from it)
            self.failed.add(uid)
        finally:
            with self.jobs_mu:
                self.jobs.pop(uid, None)
            ev.set()

    def _reuse(self, uid, u):
        """The same bytes described before (another upload with the same sha256, the same model and prompt): copied."""
        for r in self.s.q("SELECT id FROM uploads WHERE sha256=? AND kind='image' AND id<>?", (u["sha256"], uid)):
            rec = _read_look(self.s, r["id"])
            if rec and _current(rec, u["sha256"]):
                self.s.write_file(f"refs/{uid}.look.json", json.dumps(dict(rec, reused=r["id"])).encode())
                return True
        return False

    def _describe(self, uid):
        u, path = self._image(uid)
        if look_text(self.s, uid) is not None or self._reuse(uid, u):
            return look_text(self.s, uid)
        out = self.run(path, DESCRIBE_PROMPT, DESCRIBE_SIDE, DESCRIBE_TOKENS)
        text = (out.get("text") or "").strip()
        if not text:
            raise RuntimeError("the photo model said nothing")
        rec = {"text": text, "sha256": u["sha256"], "model": MODEL["name"], "revision": MODEL["revision"], "v": PROMPT_V,
               "secs": out.get("secs"), "cold": out.get("cold"), "at": time.time()}
        self.s.write_file(f"refs/{uid}.look.json", json.dumps(rec).encode())
        self.failed.discard(uid)
        return text

    def describe(self, uid, timeout=240):
        """The photo's description: the stored one, the one being made (waited for, up to `timeout` s) or a new one.
        None when there is none to give (not set up, failed, or still not done)."""
        if look_text(self.s, uid) is not None:
            return look_text(self.s, uid)
        if not self.available():
            return None
        with self.jobs_mu:
            ev = self.jobs.get(uid)
        if ev is None:
            try:
                return self._describe(uid)
            except (RuntimeError, KeyError, ValueError):
                self.failed.add(uid)
                return None
        ev.wait(timeout)
        return look_text(self.s, uid)

    def wait(self, uids, timeout=120, stop=None):
        """The photos in `uids` described before a text-only model is asked: each started if it is not yet (one at a
        time on the worker), then waited for up to `timeout` s in all, or until `stop` is set (the chat's Stop). The
        model then gets what is ready; a photo still not described goes with the text read from it.
        {uid: description or None}."""
        for u in uids:
            self.start(u)
        end = time.time() + timeout
        while time.time() < end and not (stop is not None and stop.is_set()):
            with self.jobs_mu:
                if not any(u in self.jobs for u in uids):
                    break
            time.sleep(0.2)
        return {u: look_text(self.s, u) for u in uids}

    def ask(self, uid, question):
        """A narrower look at a photo (look_at_image): the answer's text. RuntimeError when the model can't answer."""
        _, path = self._image(uid)
        out = self.run(path, ASK_PROMPT.format(q=question), ASK_SIDE, ASK_TOKENS)
        return (out.get("text") or "").strip() or "(the photo model gave no answer)"


def _read_look(store, uid):
    try:
        with open(store.path("refs", f"{uid}.look.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _current(rec, sha256):
    return rec.get("sha256") == sha256 and rec.get("revision") == MODEL["revision"] and rec.get("v") == PROMPT_V and bool(rec.get("text"))


def look_text(store, uid):
    """The stored description of an attached photo, or None (no model call)."""
    rec = _read_look(store, uid)
    if not rec:
        return None
    try:
        u = store.upload(uid)
    except KeyError:
        return None
    return rec["text"] if _current(rec, u["sha256"]) else None


def block(store, uid, cap=None):
    """What a text-only model reads about one attached photo: its description, labelled as data from a small local
    model (it can be wrong about small details), with the id look_at_image takes; an older turn's shortened to `cap`
    characters. "" when there is no description."""
    text = look_text(store, uid)
    if text is None:
        return ""
    if cap and len(text) > cap:
        text = text[:cap] + "\n(shortened here)"
    from agents_web import untrusted
    return (f"[A description of this photo (image_id {uid}), made on the user's Mac by a small vision model. It can miss or "
            "misread small details.]\n" + untrusted(f"photo {uid}", text))
