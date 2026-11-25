"""transcribe_worker.py: StrixFold UI's dictation, kept warm by the Mac app (studio_transcribe.py) and run by the whisper venv's
Python (~/Library/Application Support/StrixFoldUI/whisper-venv: mlx-whisper on this Mac's GPU, no cloud).

One request per line on stdin, {"wav": PATH} (a 16 kHz mono 16-bit WAV that afconvert wrote into the private store),
one answer per line on stdout, {"text": ...} or {"error": ...}. {"wav": PATH, "words": true} answers
{"segments": [{"start", "end", "text", "words": [{"word", "start", "end"}]}]} instead. It never writes the text or the
path anywhere else.
The model, mlx-community/whisper-large-v3-turbo, comes from Hugging Face's cache (downloaded once).
"""
import json
import sys
import wave

MODEL = "mlx-community/whisper-large-v3-turbo"


def read_wav(path):
    import numpy as np
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono audio")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def kept(segs):
    """The segments Whisper heard speech in (what heard() keeps)."""
    return [s for s in segs or [] if not ((s.get("no_speech_prob") or 0) > 0.6 and (s.get("avg_logprob") or 0) < -1.0)
            and (s.get("no_speech_prob") or 0) <= 0.85]


def heard(out):
    """The words, without what Whisper invents over silence ("Thank you.", "Thanks for watching!"): a segment it thinks
    holds no speech (no_speech_prob over 0.6 with a low average log-probability, or over 0.85 at all) is dropped."""
    segs = out.get("segments")
    if segs is None:
        return (out.get("text") or "").strip()
    keep = [s for s in segs if not ((s.get("no_speech_prob") or 0) > 0.6 and (s.get("avg_logprob") or 0) < -1.0)
            and (s.get("no_speech_prob") or 0) <= 0.85]
    return " ".join((s.get("text") or "").strip() for s in keep).strip()


def main():
    import mlx_whisper
    import numpy as np
    mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=MODEL)  # load the model now: warm for the first request
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            words = bool(req.get("words"))
            out = mlx_whisper.transcribe(read_wav(req["wav"]), path_or_hf_repo=MODEL, condition_on_previous_text=False,
                                         **({"word_timestamps": True} if words else {}))
            if words:
                segs = [{"start": float(s.get("start") or 0), "end": float(s.get("end") or 0), "text": (s.get("text") or "").strip(),
                         "words": [{"word": (w.get("word") or "").strip(), "start": float(w.get("start") or 0),
                                    "end": float(w.get("end") or 0)} for w in s.get("words") or []]} for s in kept(out.get("segments"))]
                print(json.dumps({"segments": segs, "language": out.get("language")}), flush=True)
            else:
                print(json.dumps({"text": heard(out), "language": out.get("language")}), flush=True)
        except Exception as e:  # noqa: BLE001  (the app hears it and the worker stays up)
            print(json.dumps({"error": type(e).__name__}), flush=True)


if __name__ == "__main__":
    main()
