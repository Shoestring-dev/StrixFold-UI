"""mediainfo.py: what a media file is, from its bytes (Python 3.9 standard library only).

sniff(data) names the type from its magic bytes; info(data) measures it: width and height of PNG, JPEG, WEBP and GIF
images; width, height, duration, video frames, fps and whether there is an audio track for MP4/MOV (the ISO box
tree: mvhd, tkhd, mdhd, hdlr, stsz). A file is checked with these numbers only (never by looking at it), and what it
says of where and how it was made is taken out (strip_metadata, png_strip_text).
"""
import struct
import zlib

IMAGE = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif", "heic": "image/heic", "avif": "image/avif"}
VIDEO = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}
AUDIO = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "ogg": "audio/ogg", "flac": "audio/flac"}
MIME = dict(IMAGE, **VIDEO, **AUDIO)


def sniff(data):
    """The file type from its first bytes, or None: png, jpeg, webp, gif, mp4, mov, m4a, webm, mp3, wav, ogg, flac."""
    h = data[:64]
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "webp"
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return "wav"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"  # a phone photo (HEIF): Studio turns it into a JPEG without its metadata
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"M4A ", b"M4B "):
            return "m4a"
        if brand == b"qt  ":
            return "mov"
        return "mp4"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):
        return "mp3"
    if h[:4] == b"OggS":
        return "ogg"
    if h[:4] == b"fLaC":
        return "flac"
    return None


def kind_of(t):
    return "image" if t in IMAGE else "video" if t in VIDEO else "audio" if t in AUDIO else None


def info(data):
    """{"type", "kind", "mime", "bytes", and width/height (images, video), duration_s, frames, fps, audio (video)}."""
    t = sniff(data)
    out = {"type": t, "kind": kind_of(t), "mime": MIME.get(t), "bytes": len(data)}
    try:
        if t == "png":
            out["width"], out["height"] = struct.unpack(">II", data[16:24])
            out["text_chunks"] = sum(1 for c, _ in png_chunks(data) if c in (b"tEXt", b"iTXt", b"zTXt"))
        elif t == "jpeg":
            out.update(_jpeg(data))
        elif t == "webp":
            out.update(_webp(data))
        elif t == "gif":
            out["width"], out["height"] = struct.unpack("<HH", data[6:10])
        elif t in ("mp4", "mov", "m4a"):
            out.update(mp4(data))
        elif t == "wav":
            out.update(_wav(data))
    except (struct.error, ValueError, IndexError):
        out["error"] = "could not measure"
    return out


def _wav(d):
    """A WAV's length: its data chunk's size over the fmt chunk's byte rate (a new voice's sample is checked by it)."""
    i, rate = 12, 0
    while i + 8 <= len(d):
        cid, n = d[i:i + 4], struct.unpack("<I", d[i + 4:i + 8])[0]
        if cid == b"fmt " and n >= 16:
            rate = struct.unpack("<I", d[i + 16:i + 20])[0]
        elif cid == b"data" and rate:
            return {"duration_s": round(min(n, len(d) - i - 8) / rate, 3)}
        i += 8 + n + (n & 1)
    return {}


def _jpeg(d):
    i = 2
    while i + 9 < len(d):
        if d[i] != 0xFF:
            i += 1
            continue
        m = d[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        n = struct.unpack(">H", d[i + 2:i + 4])[0]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return {"width": w, "height": h}
        i += 2 + n
    return {}


def _webp(d):
    c = d[12:16]
    if c == b"VP8 ":
        w, h = struct.unpack("<HH", d[26:30])
        return {"width": w & 0x3FFF, "height": h & 0x3FFF}
    if c == b"VP8L":
        b = int.from_bytes(d[21:25], "little")
        return {"width": (b & 0x3FFF) + 1, "height": ((b >> 14) & 0x3FFF) + 1}
    if c == b"VP8X":
        return {"width": int.from_bytes(d[24:27], "little") + 1, "height": int.from_bytes(d[27:30], "little") + 1}
    return {}


# ---------------------------------------------------------------- MP4 / MOV
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}


def boxes(d, start=0, end=None):
    """(type, payload start, payload end) for each box between start and end."""
    end = len(d) if end is None else end
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack(">I4s", d[i:i + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", d[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        yield typ, i + hdr, i + size
        i += size


def mp4(d):
    out = {"audio": False}
    for typ, a, b in boxes(d):
        if typ != b"moov":
            continue
        for t2, a2, b2 in boxes(d, a, b):
            if t2 == b"mvhd":
                v = d[a2]
                if v == 1:
                    ts, dur = struct.unpack(">IQ", d[a2 + 20:a2 + 32])
                else:
                    ts, dur = struct.unpack(">II", d[a2 + 12:a2 + 20])
                if ts:
                    out["duration_s"] = round(dur / ts, 3)
            elif t2 == b"trak":
                tr = _trak(d, a2, b2)
                if tr.get("handler") == b"soun":
                    out["audio"] = True
                elif tr.get("handler") == b"vide" and "width" not in out:
                    out["width"], out["height"] = tr.get("width"), tr.get("height")
                    if tr.get("samples"):
                        out["frames"] = tr["samples"]
                        if tr.get("duration_s"):
                            out["fps"] = round(tr["samples"] / tr["duration_s"], 2)
    return out


def _trak(d, a, b):
    r = {}
    for typ, a2, b2 in boxes(d, a, b):
        if typ == b"tkhd":
            r["width"] = struct.unpack(">I", d[b2 - 8:b2 - 4])[0] >> 16
            r["height"] = struct.unpack(">I", d[b2 - 4:b2])[0] >> 16
        elif typ == b"mdia":
            for t3, a3, b3 in boxes(d, a2, b2):
                if t3 == b"hdlr":
                    r["handler"] = d[a3 + 8:a3 + 12]
                elif t3 == b"mdhd":
                    v = d[a3]
                    ts, dur = (struct.unpack(">IQ", d[a3 + 20:a3 + 32]) if v == 1 else struct.unpack(">II", d[a3 + 12:a3 + 20]))
                    if ts:
                        r["duration_s"] = dur / ts
                elif t3 == b"minf":
                    for t4, a4, b4 in boxes(d, a3, b3):
                        if t4 == b"stbl":
                            for t5, a5, b5 in boxes(d, a4, b4):
                                if t5 == b"stsz":
                                    r["samples"] = struct.unpack(">I", d[a5 + 8:a5 + 12])[0]
    return r


# ---------------------------------------------------------------- PNG
def png_chunks(d):
    i = 8
    while i + 12 <= len(d):
        n, typ = struct.unpack(">I4s", d[i:i + 8])
        yield typ, d[i + 8:i + 8 + n]
        i += 12 + n
        if typ == b"IEND":
            break


def png_strip_text(d, also=()):
    """The PNG without its text chunks (tEXt, iTXt, zTXt)."""
    out = [d[:8]]
    for typ, body in png_chunks(d):
        if typ in (b"tEXt", b"iTXt", b"zTXt") + tuple(also):
            continue
        out.append(struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))
    return b"".join(out)


def png(width, height, row):
    """A PNG (8-bit RGB) from row(y) -> bytes of width * 3: test patterns for the fakes and the tests."""
    raw = b"".join(b"\x00" + row(y) for y in range(height))

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def test_pattern(width, height, seed=0):
    """Colour bars shifted by seed, with a light diagonal band: obviously synthetic."""
    bars = [(235, 235, 235), (230, 200, 40), (40, 200, 210), (60, 190, 60), (200, 60, 200), (210, 50, 45), (40, 60, 200), (20, 20, 20)]
    bars = bars[seed % 8:] + bars[:seed % 8]
    bw = max(1, width // len(bars))
    base = b"".join(bytes(bars[min(x // bw, len(bars) - 1)]) for x in range(width))

    def row(y):
        if (y * 7) % height < height // 12:
            return bytes(min(255, c + 60) for c in base)
        return base
    return png(width, height, row)


# ---------------------------------------------------------------- metadata that can identify (location, camera, names)
def strip_metadata(data):
    """The file without what can identify where and how it was made: JPEG EXIF/XMP/IPTC segments and comments (the
    Orientation tag is kept: a portrait photo stays upright), PNG
    text and eXIf chunks, WEBP EXIF/XMP chunks, MP4/MOV udta/meta boxes (zeroed, renamed free: offsets stay), the MP3
    ID3v2 tag. Pixels and samples are untouched. HEIC/AVIF need a conversion first (macOS's sips)."""
    t = sniff(data)
    if t == "jpeg":
        return _jpeg_strip(data)
    if t == "png":
        return png_strip_text(data, also=(b"eXIf",))
    if t == "webp":
        return _webp_strip(data)
    if t in ("mp4", "mov", "m4a"):
        return _mp4_strip(data)
    if t == "mp3" and data[:3] == b"ID3" and len(data) > 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return data[10 + size + (10 if data[5] & 0x10 else 0):]
    return data


def jpeg_orientation(d):
    """The EXIF Orientation (1-8) of a JPEG, or None: phones store a portrait photo as landscape pixels plus this tag."""
    i = 2
    while i + 4 <= len(d) and d[i] == 0xFF:
        m = d[i + 1]
        if m == 0xDA:
            break
        n = struct.unpack(">H", d[i + 2:i + 4])[0]
        seg = d[i + 4:i + 2 + n]
        if m == 0xE1 and seg[:6] == b"Exif\x00\x00":
            t = seg[6:]
            e = "<" if t[:2] == b"II" else ">"
            try:
                off = struct.unpack(e + "I", t[4:8])[0]
                count = struct.unpack(e + "H", t[off:off + 2])[0]
                for k in range(count):
                    tag, typ, cnt = struct.unpack(e + "HHI", t[off + 2 + 12 * k:off + 10 + 12 * k])
                    if tag == 0x0112:
                        return struct.unpack(e + "H", t[off + 10 + 12 * k:off + 12 + 12 * k])[0]
            except struct.error:
                return None
        i += 2 + n
    return None


def _orientation_app1(o):
    """An EXIF block with the Orientation tag and nothing else (no camera, time or place)."""
    tiff = b"MM\x00*" + struct.pack(">I", 8) + struct.pack(">H", 1) + struct.pack(">HHIHH", 0x0112, 3, 1, o, 0) + struct.pack(">I", 0)
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def _jpeg_strip(d):
    orient = jpeg_orientation(d)
    out, i = [d[:2]], 2
    kept_orientation = not orient or orient == 1
    while i + 4 <= len(d):
        if d[i] != 0xFF:
            break
        m = d[i + 1]
        if not kept_orientation and m != 0xE0:  # the rotation goes back in right after SOI (and JFIF)
            out.append(_orientation_app1(orient))
            kept_orientation = True
        if m == 0xDA:  # start of scan: the rest is image data
            out.append(d[i:])
            return b"".join(out)
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            out.append(d[i:i + 2])
            i += 2
            continue
        n = struct.unpack(">H", d[i + 2:i + 4])[0]
        keep = not (0xE1 <= m <= 0xEF and m not in (0xE2, 0xEE)) and m != 0xFE  # APP0 JFIF, APP2 ICC, APP14 Adobe stay
        if keep:
            out.append(d[i:i + 2 + n])
        i += 2 + n
    out.append(d[i:])
    return b"".join(out)


def _webp_strip(d):
    chunks, i = [], 12
    while i + 8 <= len(d):
        typ, n = d[i:i + 4], struct.unpack("<I", d[i + 4:i + 8])[0]
        body = d[i + 8:i + 8 + n]
        if typ == b"VP8X":
            body = bytes([body[0] & ~0x0C]) + body[1:]  # the EXIF and XMP flags off
        if typ not in (b"EXIF", b"XMP "):
            chunks.append(typ + struct.pack("<I", n) + body + (b"\x00" if n % 2 else b""))
        i += 8 + n + (n % 2)
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _mp4_strip(d):
    d = bytearray(d)

    def walk(a, b):
        i = a
        while i + 8 <= b:
            size, typ = struct.unpack(">I4s", bytes(d[i:i + 8]))
            hdr = 8
            if size == 1:
                size, hdr = struct.unpack(">Q", bytes(d[i + 8:i + 16]))[0], 16
            elif size == 0:
                size = b - i
            if size < hdr or i + size > b:
                return
            if typ in (b"udta", b"meta"):
                d[i + 4:i + 8] = b"free"
                d[i + hdr:i + size] = bytes(size - hdr)  # the location and the names gone, not just unreferenced
            elif typ in (b"moov", b"trak", b"mdia", b"minf"):
                walk(i + hdr, i + size)
            i += size
    walk(0, len(d))
    return bytes(d)
