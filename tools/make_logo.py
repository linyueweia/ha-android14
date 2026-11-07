#!/usr/bin/env python3
"""Render H88K boot logos.

Boot logos are picky about geometry and colour depth: U-Boot blits the bitmap
as-is, so a wrong width/height shows a black screen or a torn image.

Two targets exist in this project:
    FriendlyElec Android 14 boot.img : 512x128 24bpp
    Vendor H88K Android 12 boot.img  : 654x270 8bpp

Render at 4x and downsample with LANCZOS, so edges stay clean at 128 px.

Usage:
    python make_logo.py --w 512 --h 128 --bpp 24 --layout lockup -o logo.bmp
    python make_logo.py --w 654 --h 270 --bpp 8  --layout lockup -o logo.bmp
"""
import argparse
import os
import struct

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = r"C:\Windows\Fonts\segoeuib.ttf"
FONT_SEMI = r"C:\Windows\Fonts\seguisb.ttf"

SS = 4  # supersample factor


def font(path, px):
    try:
        return ImageFont.truetype(path, px)
    except OSError:
        return ImageFont.truetype(FONT_BOLD, px)


def tracked_width(draw, text, f, tracking):
    w = 0
    for ch in text:
        w += draw.textlength(ch, font=f) + tracking
    return w - tracking if text else 0


def draw_tracked(draw, xy, text, f, fill, tracking):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + tracking
    return x


def layout_lockup(d, W, H, text, sub1, sub2, accent):
    """H88K | HINLINK / ANDROID 14 -- shrink until it fits the canvas."""
    scale = 1.0
    while True:
        f_main = font(FONT_BOLD, max(8, int(H * 0.56 * scale)))
        f_s1 = font(FONT_SEMI, max(6, int(H * 0.155 * scale)))
        f_s2 = font(FONT_SEMI, max(6, int(H * 0.135 * scale)))
        tr1 = int(H * 0.045 * scale)
        tr2 = int(H * 0.035 * scale)
        gap = int(H * 0.13 * scale)
        line_w = max(2, int(H * 0.012 * scale))

        bbox = d.textbbox((0, 0), text, font=f_main)
        main_w, main_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        sub_w = max(tracked_width(d, sub1, f_s1, tr1), tracked_width(d, sub2, f_s2, tr2))
        block_w = main_w + gap + line_w + gap + sub_w
        block_h = main_h
        if (block_w <= W * 0.90 and block_h <= H * 0.80) or scale < 0.3:
            break
        scale *= 0.96

    x0 = (W - block_w) // 2
    d.text((x0 - bbox[0], (H - main_h) // 2 - bbox[1]), text, font=f_main, fill=(255, 255, 255))

    lx = x0 + main_w + gap
    d.rectangle([lx, int(H * 0.28), lx + line_w - 1, int(H * 0.72)], fill=accent)

    sx = lx + line_w + gap
    b1 = d.textbbox((0, 0), sub1, font=f_s1)
    b2 = d.textbbox((0, 0), sub2, font=f_s2)
    h1, h2 = b1[3] - b1[1], b2[3] - b2[1]
    vgap = int(H * 0.10 * scale)
    total = h1 + vgap + h2
    y1 = (H - total) // 2 - b1[1]
    draw_tracked(d, (sx, y1), sub1, f_s1, (205, 210, 217), tr1)
    y2 = y1 + b1[1] + h1 + vgap - b2[1]
    draw_tracked(d, (sx, y2), sub2, f_s2, (135, 142, 150), tr2)

    # thin accent underline under the wordmark only
    uy = (H - main_h) // 2 + main_h + int(H * 0.07)
    if uy < H * 0.94:
        d.rectangle([x0, uy, x0 + main_w - 1, uy + max(2, int(H * 0.012))], fill=accent)


def layout_stack(d, W, H, text, sub1, sub2, accent):
    """H88K centred, HINLINK tracked underneath."""
    scale = 1.0
    while True:
        f_main = font(FONT_BOLD, max(8, int(H * 0.42 * scale)))
        f_sub = font(FONT_SEMI, max(6, int(H * 0.095 * scale)))
        tr = int(H * 0.10 * scale)
        bbox = d.textbbox((0, 0), text, font=f_main)
        mw, mh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        sw = tracked_width(d, sub1, f_sub, tr)
        gap = int(H * 0.06 * scale)
        if (max(mw, sw) <= W * 0.90 and mh + gap + f_sub.size <= H * 0.86) or scale < 0.3:
            break
        scale *= 0.96

    total_h = mh + gap + f_sub.size
    y = (H - total_h) // 2
    d.text(((W - mw) // 2 - bbox[0], y - bbox[1]), text, font=f_main, fill=(255, 255, 255))
    b2 = d.textbbox((0, 0), sub1, font=f_sub)
    draw_tracked(d, ((W - sw) // 2, y + mh + gap - b2[1]), sub1, f_sub, (160, 167, 175), tr)


LAYOUTS = {"lockup": layout_lockup, "stack": layout_stack}


def render(w, h, layout, text, sub1, sub2, accent):
    W, H = w * SS, h * SS
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    LAYOUTS[layout](d, W, H, text, sub1, sub2, accent)
    return img.resize((w, h), Image.LANCZOS)


def save(img, path, bpp, topdown=False):
    """Write a BMP, optionally as a top-down DIB (negative biHeight).

    FriendlyElec ships logo.bmp with biHeight = -128 (top-down). U-Boot honours
    the sign, so the replacement has to keep the same orientation or the artwork
    comes out mirrored vertically.
    """
    root, _ = os.path.splitext(path)
    if bpp == 24:
        img.convert("RGB").save(path, "BMP")
    elif bpp == 8:
        img.convert("P", palette=Image.ADAPTIVE, colors=256).save(path, "BMP")
    else:
        raise SystemExit("unsupported bpp %d" % bpp)

    if topdown:
        raw = bytearray(open(path, "rb").read())
        data_off = struct.unpack_from("<I", raw, 10)[0]
        hdr_size = struct.unpack_from("<I", raw, 14)[0]
        w, h = struct.unpack_from("<ii", raw, 18)
        if h > 0:
            stride = ((w * bpp + 31) // 32) * 4
            body = bytes(raw[data_off:data_off + stride * h])
            rows = [body[i * stride:(i + 1) * stride] for i in range(h)]
            rows.reverse()
            out = bytearray(raw[:data_off]) + b"".join(rows)
            struct.pack_into("<i", out, 22, -h)
            open(path, "wb").write(bytes(out))
            del hdr_size

    img.save(root + ".png", "PNG")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--h", type=int, required=True)
    ap.add_argument("--bpp", type=int, default=24, choices=[8, 24])
    ap.add_argument("--layout", default="lockup", choices=sorted(LAYOUTS))
    ap.add_argument("--text", default="H88K")
    ap.add_argument("--sub1", default="HINLINK")
    ap.add_argument("--sub2", default="ANDROID 14")
    ap.add_argument("--accent", default="#2F81F7")
    ap.add_argument("--topdown", action="store_true", help="write biHeight<0 (matches FriendlyElec stock)")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    acc = a.accent.lstrip("#")
    accent = tuple(int(acc[i:i + 2], 16) for i in (0, 2, 4))
    img = render(a.w, a.h, a.layout, a.text, a.sub1, a.sub2, accent)
    save(img, a.out, a.bpp, a.topdown)
    print("%s  %dx%d %dbpp" % (a.out, a.w, a.h, a.bpp))


if __name__ == "__main__":
    main()
