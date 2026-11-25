"""StrixFold UI's dictation on this Mac: a recording from the chat's microphone becomes text in the composer, never sent by itself.

The browser records (iPhone Safari: AAC in MP4); macOS's afconvert turns it into a 16 kHz mono WAV; mlx-whisper
(large-v3-turbo, on this Mac's GPU) transcribes it in a warm worker (transcribe_worker.py, run by the whisper venv's
Python with Hugging Face offline: the model is in its cache and nothing goes out). One clip at a time, two minutes at
most. The audio is written only into the private store (600) and deleted as soon as it is read; no text, file name or
length goes into a log.
"""
import json
import os
import secrets
import select
import struct
import subprocess
import threading
import time
import wave

VENV = os.path.expanduser("~/Library/Application Support/StrixFoldUI/whisper-venv")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcribe_worker.py")
MAX_BYTES = 12 << 20
MAX_SECONDS = 120
MEDIA_SECONDS = 600  # an attached audio or video file: its first ten minutes are transcribed for the chat
IDLE_S = 15 * 60  # the warm worker (about 1.6 GB of the GPU's memory) goes after this long unused
SILENCE_DBFS = -50.0  # quieter than this over the whole clip counts as nothing said
SPEECH_SWING_DB = 10.0  # speech has syllable peaks well above its floor; steady noise does not
QUIET_DBFS = -35.0
FILLERS = {"thank you", "thanks for watching", "thank you for watching", "you", "bye", "thank you so much", ""}
TYPES = ("audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/wave", "audio/webm",
         "audio/ogg", "video/mp4", "application/octet-stream")


class Transcriber:
    def __init__(self, store, venv=VENV, worker_cmd=None, convert=None):
        self.s = store
        self.python = os.path.join(venv, "bin", "python")
        self.cmd = worker_cmd or [self.python, WORKER]
        self.convert = convert or afconvert
        self.mu = threading.Lock()
        self.proc = None
        self.used = 0.0
        threading.Thread(target=self._reaper, name="dictation", daemon=True).start()

    def _reaper(self):
        while True:
            time.sleep(60)
            with self.mu:
                if self.proc and time.time() - self.used > IDLE_S:
                    self.close()

    def available(self):
        return os.path.exists(self.cmd[0])

    def _worker(self, timeout):
        self.used = time.time()
        if self.proc and self.proc.poll() is None:
            return self.proc
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        ready = self._read(self.proc, timeout)
        if not ready or not ready.get("ready"):
            self.close()
            raise RuntimeError("the dictation model did not start")
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
                line = buf.split(b"\n", 1)[0]
                try:
                    return json.loads(line)
                except ValueError:
                    return None
        return None

    def close(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()
            p.wait(5)

    def warm(self):
        """Start the worker ahead of the first recording (the chat page asks when its microphone is about to be used)."""
        if not self.available():
            return False
        with self.mu:
            try:
                self._worker(180)
                return True
            except RuntimeError:
                return False

    def transcribe_media(self, data, max_seconds=None):
        """The words in an attached audio or video file (its sound track), its first max_seconds: (text, seconds in the
        file, seconds read). ValueError when its sound cannot be read; RuntimeError when the model is not set up or does
        not answer. As dictation: written only into the private store (600), deleted as soon as it is read."""
        if not self.available():
            raise RuntimeError("dictation is not set up on this Mac")
        max_seconds = max_seconds or MEDIA_SECONDS
        tag = secrets.token_hex(8)
        src = self.s.write_file(f"refs/m{tag}.in", data)
        dst = self.s.path("refs", f"m{tag}.wav")
        try:
            if not self.convert(src, dst):
                raise ValueError("its sound could not be read")
            os.chmod(dst, 0o600)
            try:
                pcm, rate, channels = read_pcm16(dst)
            except (ValueError, OSError, struct.error) as e:
                raise ValueError("its sound could not be read") from e
            frame, rate = 2 * max(1, channels), rate or 16000
            total = len(pcm) / frame / float(rate)
            pcm = pcm[:int(max_seconds * rate) * frame]
            secs = len(pcm) / frame / float(rate)
            if secs < 0.2 or rms_dbfs(pcm) < SILENCE_DBFS or dynamics_db(pcm, rate) < SPEECH_SWING_DB:
                return "", total, secs
            write_pcm16(dst, pcm, rate, channels)
            with self.mu:
                proc = self._worker(180)
                proc.stdin.write((json.dumps({"wav": dst}) + "\n").encode())
                proc.stdin.flush()
                out = self._read(proc, 120 + secs)
            if out is None:
                self.close()
                raise RuntimeError("the dictation model did not answer")
            if out.get("error"):
                raise RuntimeError("the dictation model could not read the sound")
            return (out.get("text") or "").strip(), total, secs
        finally:
            for p in (src, dst):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def transcribe(self, data, ctype=""):
        """The words in a recording. ValueError for what the person can fix (too long, unreadable); RuntimeError when the
        dictation model is not set up or does not answer."""
        if not self.available():
            raise RuntimeError("dictation is not set up on this Mac")
        if not data:
            raise ValueError("nothing was recorded")
        if len(data) > MAX_BYTES:
            raise ValueError("recordings up to two minutes")
        tag = secrets.token_hex(8)
        src = self.s.write_file(f"refs/a{tag}.in", data)
        dst = self.s.path("refs", f"a{tag}.wav")
        try:
            if not self.convert(src, dst):
                raise ValueError("this recording format cannot be read on the Mac; record in Safari, or use the keyboard's dictation")
            os.chmod(dst, 0o600)
            try:  # afconvert writes WAVE_FORMAT_EXTENSIBLE from some inputs (an m4a); the worker gets plain PCM
                pcm, rate, channels = read_pcm16(dst)
            except (ValueError, OSError, struct.error) as e:
                raise ValueError("this recording could not be read; try again, or use the keyboard's dictation") from e
            write_pcm16(dst, pcm, rate, channels)
            secs = len(pcm) / 2.0 / max(1, channels) / float(rate or 16000)
            if secs > MAX_SECONDS + 1:
                raise ValueError("recordings up to two minutes")
            if secs < 0.2:
                raise ValueError("nothing was recorded")
            level, swing = rms_dbfs(pcm), dynamics_db(pcm, rate)
            if level < SILENCE_DBFS or swing < SPEECH_SWING_DB:
                return ""  # silence or steady noise: no words, rather than what Whisper invents over it ("Thank you.")
            with self.mu:
                proc = self._worker(180)
                proc.stdin.write((json.dumps({"wav": dst}) + "\n").encode())
                proc.stdin.flush()
                out = self._read(proc, 60 + secs)
            if out is None:
                self.close()
                raise RuntimeError("the dictation model did not answer")
            if out.get("error"):
                raise RuntimeError("the dictation model could not read the recording")
            text = (out.get("text") or "").strip()
            if level < QUIET_DBFS and text.lower().strip(" .!?") in FILLERS:
                return ""  # a quiet clip that came back as one of Whisper's stock phrases for silence
            return text
        finally:
            for p in (src, dst):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def read_pcm16(path):
    """(16-bit PCM bytes, rate, channels) from a WAV, plain (format 1) or extensible (65534 with the PCM subformat)."""
    with open(path, "rb") as f:
        d = f.read()
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    i, fmt, pcm = 12, None, None
    while i + 8 <= len(d):
        cid, n = d[i:i + 4], struct.unpack("<I", d[i + 4:i + 8])[0]
        body = d[i + 8:i + 8 + n]
        if cid == b"fmt ":
            tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:
                tag = struct.unpack("<H", body[24:26])[0]  # the subformat GUID starts with the format code
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            pcm = body
        i += 8 + n + (n & 1)
    if not fmt or pcm is None:
        raise ValueError("a WAV without its format or data")
    tag, ch, rate, bits = fmt
    if tag != 1 or bits != 16 or not ch:
        raise ValueError(f"not 16-bit PCM (format {tag}, {bits} bits)")
    return pcm[:len(pcm) - len(pcm) % (2 * ch)], rate, ch


def rms_dbfs(pcm):
    """The clip's loudness (RMS of its 16-bit samples) in dB below full scale; -inf for digital silence."""
    import array
    import math
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if not a:
        return float("-inf")
    step = max(1, len(a) // 200000)  # a long clip is sampled, not read whole
    vals = a[::step]
    rms = math.sqrt(sum(v * v for v in vals) / len(vals))
    return 20 * math.log10(rms / 32768.0) if rms > 0 else float("-inf")


def dynamics_db(pcm, rate):
    """How far the loudest 30 ms frames rise above the clip's floor (90th minus 20th percentile of frame levels, dB).
    Speech swings 15-40 dB; hiss and steady room noise a few dB."""
    import array
    import math
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    n = max(1, int(rate * 0.03))
    levels = []
    for i in range(0, len(a) - n + 1, n):
        f = a[i:i + n:4]
        e = math.sqrt(sum(v * v for v in f) / len(f))
        levels.append(20 * math.log10(e / 32768.0) if e > 0 else -120.0)
    if len(levels) < 3:
        return 0.0
    levels.sort()
    return levels[int(len(levels) * 0.9)] - levels[int(len(levels) * 0.2)]


def write_pcm16(path, pcm, rate, channels):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    os.chmod(path, 0o600)


def afconvert(src, dst):
    """macOS's own converter (no ffmpeg on this Mac): MP4/AAC, M4A, MP3, WAV, CAF in; 16 kHz mono 16-bit WAV out."""
    try:
        r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, dst], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 44
