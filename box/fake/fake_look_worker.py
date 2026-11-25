"""A stand-in for mac/look_worker.py (tests and the --fake app): the same line protocol, no model. A description names
the image's size in bytes; an answer repeats the question. Standard library only.

fake_look_worker.py [--delay S] [--log FILE] [--fail] [--say TEXT]
  --delay: seconds per request (to see "Looking..."); --log: each request's prompt kind and image size, one line each;
  --fail: every request answers {"error"}; --say: the description's text instead of the default.
"""
import json
import os
import sys
import time


def main(argv):
    delay, log, fail, say = 0.0, None, False, None
    it = iter(argv)
    for a in it:
        if a == "--delay":
            delay = float(next(it))
        elif a == "--log":
            log = next(it)
        elif a == "--fail":
            fail = True
        elif a == "--say":
            say = next(it)
    print(json.dumps({"ready": True, "load_s": 0.01}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        path, prompt = req.get("image"), str(req.get("prompt") or "")
        ok = isinstance(path, str) and os.path.isabs(path) and "://" not in path and os.path.isfile(path)
        size = os.path.getsize(path) if ok else 0
        kind = "ask" if prompt.startswith("Answer this question") else "describe"
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({"kind": kind, "bytes": size, "side": req.get("side")}) + "\n")
        time.sleep(delay)
        if fail or not ok:
            print(json.dumps({"error": "Fake" if fail else "ValueError"}), flush=True)
            continue
        if kind == "ask":
            q = prompt.split("\n", 1)[0][len("Answer this question about the image: "):]
            text = f"Fake look answer to: {q}"
        else:
            text = say or f"A fake description of an image of {size} bytes."
        print(json.dumps({"text": text, "prompt_tokens": 10, "gen_tokens": 5, "secs": delay, "gen_tps": 50.0, "peak_gb": 0.1,
                          "finish": "stop"}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
