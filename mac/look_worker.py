"""look_worker.py: StrixFold UI's photo look, kept warm by the Mac app (look.py) and run by the look venv's Python
(~/Library/Application Support/StrixFoldUI/look-venv: mlx-vlm on this Mac's GPU) inside a Seatbelt profile with no
network at all (look.profile): a photo, a question and an answer never leave the Mac.

argv[1] is the model's folder: pinned weights on disk, never a Hugging Face name, so nothing is ever fetched.
One request per line on stdin: {"image": ABSOLUTE PATH, "prompt": str, "max_tokens": n, "side": px}.
One answer per line on stdout: {"text", "prompt_tokens", "gen_tokens", "secs", "gen_tps", "peak_gb"} or {"error": name}.
It writes nothing anywhere else: not the image, the question or the answer.
"""
import json
import os
import re
import sys
import time

THINK = re.compile(r"<think>.*?</think>", re.S)


def image_path(path):
    """The request's image: an absolute path to a file, never a URL (mlx-vlm would fetch one) or anything else."""
    if not isinstance(path, str) or not os.path.isabs(path) or "://" in path or not os.path.isfile(path):
        raise ValueError("image: an absolute path to a file")
    return path


def fit(path, side):
    """The image as RGB, upright, its longer side at most `side` pixels (a phone photo's 4032 px would be ~16,000 vision
    tokens; at 1280 it is ~1,200). Transparency goes onto white."""
    from PIL import Image, ImageOps
    im = Image.open(path)
    im.load()
    im = ImageOps.exif_transpose(im)  # a phone photo the right way up (a no-op when it has no orientation tag)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    else:
        im = im.convert("RGB")
    if max(im.size) > side:
        k = side / float(max(im.size))
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    return im


def main():
    t0 = time.time()
    import mlx.core as mx
    from PIL import Image
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    model_dir = sys.argv[1]
    if not os.path.isfile(os.path.join(model_dir, "config.json")):
        print(json.dumps({"error": "NoModel"}), flush=True)
        return
    model, processor = load(model_dir)

    def run(im, prompt, max_tokens):
        text = apply_chat_template(processor, model.config, prompt, num_images=1)  # thinking off (mlx-vlm's default)
        return generate(model, processor, text, image=[im], max_tokens=max_tokens, temperature=0.0, verbose=False)

    run(Image.new("RGB", (64, 64), "white"), "Hi", 1)  # the GPU kernels are built now: the first photo is warm
    mx.clear_cache()
    print(json.dumps({"ready": True, "load_s": round(time.time() - t0, 2)}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            path = image_path(req["image"])
            t = time.time()
            mx.reset_peak_memory()
            r = run(fit(path, int(req.get("side") or 1280)), str(req["prompt"]), int(req.get("max_tokens") or 512))
            print(json.dumps({"text": THINK.sub("", r.text or "").strip(), "prompt_tokens": r.prompt_tokens,
                              "gen_tokens": r.generation_tokens, "secs": round(time.time() - t, 3),
                              "gen_tps": round(r.generation_tps, 1), "peak_gb": round(mx.get_peak_memory() / 1e9, 2),
                              "finish": "length" if r.generation_tokens >= int(req.get("max_tokens") or 512) else "stop"}), flush=True)
        except Exception as e:  # noqa: BLE001  (the app hears it and the worker stays up)
            print(json.dumps({"error": type(e).__name__}), flush=True)
        finally:
            mx.clear_cache()


if __name__ == "__main__":
    main()
