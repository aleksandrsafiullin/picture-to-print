"""Raster -> SVG, via potrace (potracer, pure Python).

Mono traces one thresholded mask. Color quantizes in Lab (k-means, or a
palette you pass in), then traces each swatch as its own filled path.
Layers are painted largest-first and dilated a hair so curve fitting does
not open seams. A hidden swatch is left out, so the background can be
transparent.

vtracer was evaluated as a second engine: its wheel segfaults on this Python
whenever an explicit option is passed, and a segfault takes the server down,
so it is deliberately not used.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


MAX_SIDE = 1600


@dataclass
class VecParams:
    mode: str = 'mono'           # mono | color
    threshold: int = 128
    invert: bool = False
    blur: float = 0.0            # px, smooths jpeg noise before the cut
    despeckle: int = 4           # drop specks smaller than this many px
    smooth: float = 1.0          # 0 = hard corners ... 1.334 = max curvature
    tolerance: float = 0.2       # curve optimisation tolerance
    upscale: float = 1.0         # trace at a larger size -> finer detail
    colors: int = 6              # color mode: how many swatches
    overlap: float = 1.0         # color mode: dilate each layer, px
    palette: list | None = None  # color mode: fixed #rrggbb targets
    skip: list | None = None     # color mode: swatches left transparent


def _hex_bgr(value):
    s = str(value).strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None
    return (b, g, r), '#%02x%02x%02x' % (r, g, b)


def _hex_list(value):
    if not isinstance(value, (list, tuple)):
        return None
    out = []
    for item in value:
        parsed = _hex_bgr(item)
        if parsed:
            out.append(parsed[1])
    return out[:16]


def _finite_float(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError('bad_number')
    if x != x or x == float('inf') or x == float('-inf'):
        raise ValueError('bad_number')
    return x


def params_from_dict(d: dict) -> VecParams:
    p = VecParams()
    raw = dict(d or {})
    palette = _hex_list(raw.pop('palette', None))
    skip = _hex_list(raw.pop('skip', None))
    for k, v in raw.items():
        if not hasattr(p, k) or k in ('palette', 'skip'):
            continue
        cur = getattr(p, k)
        if isinstance(cur, bool):
            v = v in (True, 'true', 'True', 1, '1')
        elif isinstance(cur, int):
            v = int(_finite_float(v))
        elif isinstance(cur, float):
            v = _finite_float(v)
        setattr(p, k, v)
    if p.mode not in ('mono', 'color'):
        p.mode = 'mono'
    p.threshold = max(0, min(255, int(p.threshold)))
    p.blur = max(0.0, min(8.0, float(p.blur)))
    p.despeckle = max(0, min(200, int(p.despeckle)))
    p.smooth = max(0.0, min(1.334, float(p.smooth)))
    p.tolerance = max(0.0, min(2.0, float(p.tolerance)))
    p.upscale = max(1.0, min(3.0, float(p.upscale)))
    p.colors = max(2, min(8, int(p.colors)))
    p.overlap = max(0.0, min(3.0, float(p.overlap)))
    p.palette = palette or None
    p.skip = skip or None
    return p


def _despeckle(mask, min_px):
    """Remove ink specks and pinholes smaller than min_px pixels."""
    out = mask.copy()
    for target, fill in ((255, 0), (0, 255)):
        n, lab, st, _ = cv2.connectedComponentsWithStats(
            (out == target).astype(np.uint8), 8)
        for i in range(1, n):
            if st[i, cv2.CC_STAT_AREA] < min_px:
                out[lab == i] = fill
    return out


def mask_of(data: bytes, p: VecParams):
    """Decode, optionally blur/upscale, threshold -> uint8 mask, 255 = ink."""
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise ValueError('image_unreadable')
    if arr.ndim == 3 and arr.shape[2] == 4:
        a = arr[:, :, 3:4].astype(np.float32) / 255.0
        arr = (arr[:, :, :3].astype(np.float32) * a + 255 * (1 - a)).astype(np.uint8)
    gray = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    if p.upscale and abs(p.upscale - 1.0) > 1e-3:
        gray = cv2.resize(gray, None, fx=p.upscale, fy=p.upscale,
                          interpolation=cv2.INTER_CUBIC)
    if p.blur > 0:
        k = int(p.blur) * 2 + 1
        gray = cv2.GaussianBlur(gray, (k, k), p.blur)
    mode = cv2.THRESH_BINARY if p.invert else cv2.THRESH_BINARY_INV
    _, mask = cv2.threshold(gray, int(p.threshold), 255, mode)
    if p.despeckle > 0:
        mask = _despeckle(mask, p.despeckle)
    return mask


def _trace_mask(mask, p):
    """Potrace one ink mask. Returns (d-commands, curves, segments) or Nones."""
    import potrace

    if float((mask > 0).mean()) < 1e-5:
        return None, 0, 0
    path = potrace.Bitmap(mask == 0).trace(
        turdsize=max(0, int(p.despeckle)), turnpolicy=4,
        alphamax=max(0.0, min(1.334, p.smooth)),
        opticurve=True, opttolerance=max(0.0, p.tolerance))
    xy = lambda pt: (pt.x, pt.y)
    d, curves, segs = [], 0, 0
    for curve in path:
        curves += 1
        d.append('M%.3f %.3f' % xy(curve.start_point))
        for s in curve.segments:
            segs += 1
            if s.is_corner:
                d.append('L%.3f %.3f L%.3f %.3f' % (*xy(s.c), *xy(s.end_point)))
            else:
                d.append('C%.3f %.3f %.3f %.3f %.3f %.3f'
                         % (*xy(s.c1), *xy(s.c2), *xy(s.end_point)))
        d.append('Z')
    if not d:
        return None, 0, 0
    return d, curves, segs


def _svg(w, h, body):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
            'viewBox="0 0 %d %d">%s</svg>' % (w, h, w, h, body))


def _path_el(commands, fill, index):
    attr = '' if index is None else ' data-i="%d"' % index
    return '<path%s d="%s" fill="%s" fill-rule="evenodd"/>' % (
        attr, ' '.join(commands), fill)


def _cap(img, max_side):
    h, w = img.shape[:2]
    side = max(h, w)
    if side <= max_side:
        return img
    s = max_side / side
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)


def _prepare_color(data, p):
    """BGR image plus an optional alpha, capped so a photo still traces."""
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise ValueError('image_unreadable')
    alpha = None
    if arr.ndim == 2:
        bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif arr.shape[2] == 4:
        alpha = arr[:, :, 3]
        bgr = arr[:, :, :3]
    else:
        bgr = arr[:, :, :3]
    if p.upscale and abs(p.upscale - 1.0) > 1e-3:
        bgr = cv2.resize(bgr, None, fx=p.upscale, fy=p.upscale,
                         interpolation=cv2.INTER_CUBIC)
        if alpha is not None:
            alpha = cv2.resize(alpha, (bgr.shape[1], bgr.shape[0]),
                               interpolation=cv2.INTER_CUBIC)
    bgr = _cap(bgr, MAX_SIDE)
    if alpha is not None and (alpha.shape[0] != bgr.shape[0]
                              or alpha.shape[1] != bgr.shape[1]):
        alpha = cv2.resize(alpha, (bgr.shape[1], bgr.shape[0]),
                           interpolation=cv2.INTER_AREA)
    if p.blur > 0:
        d = max(3, int(round(p.blur)) * 2 + 1)
        if d % 2 == 0:
            d += 1
        sigma = max(1.0, float(p.blur) * 12.0)
        bgr = cv2.bilateralFilter(bgr, d, sigma, sigma)
    return bgr, alpha


def _assign_flat(pixels, centers):
    """Nearest center for an (n, 3) Lab cloud. Returns (n,) int labels."""
    flat = np.ascontiguousarray(np.asarray(pixels, np.float32).reshape(-1, 3))
    out = np.empty(flat.shape[0], np.int32)
    c = np.ascontiguousarray(centers, np.float32)
    step = 250_000
    for i in range(0, flat.shape[0], step):
        chunk = flat[i:i + step]
        dist = ((chunk[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
        out[i:i + step] = dist.argmin(axis=1)
    return out


def _distinct_centers(centers, min_dist=10.0):
    """Drop centers that sit on top of a larger one. K-means splits flat
    areas (a gray wall, a jpeg sky) into twins and the seam becomes noise."""
    centers = np.asarray(centers, np.float32)
    kept = []
    for c in centers:
        if all(float(np.linalg.norm(c - k)) >= min_dist for k in kept):
            kept.append(c)
    return np.vstack(kept)


def _kmeans_centers(samples, k):
    k = min(k, len(samples))
    if k < 1:
        raise ValueError('no_color')
    if k == 1:
        return samples.mean(axis=0, keepdims=True)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 24, 0.8)
    cv2.setRNGSeed(0)
    last = None
    for attempt in range(k, 1, -1):
        try:
            _comp, _labels, centers = cv2.kmeans(
                samples, attempt, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
            return centers
        except cv2.error as e:
            last = e
    if last:
        raise ValueError('no_color')
    raise ValueError('no_color')


def _bgr_to_lab(bgr_rows):
    img = np.asarray(bgr_rows, np.uint8).reshape(1, -1, 3)
    return cv2.cvtColor(img, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)


def _lab_to_hex(centers):
    u8 = np.clip(np.round(centers), 0, 255).astype(np.uint8).reshape(1, -1, 3)
    bgr = cv2.cvtColor(u8, cv2.COLOR_LAB2BGR).reshape(-1, 3)
    return ['#%02x%02x%02x' % (int(p[2]), int(p[1]), int(p[0])) for p in bgr]


def _quantize(bgr, alpha, p):
    """Label map (-1 = transparent) and a list of #rrggbb in swatch order."""
    h, w = bgr.shape[:2]
    opaque = np.ones((h, w), bool) if alpha is None else alpha >= 128
    if int(opaque.sum()) < 2:
        raise ValueError('no_color')
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    fixed = []
    if p.palette:
        for hx in p.palette:
            parsed = _hex_bgr(hx)
            if parsed:
                fixed.append(parsed)
    if len(fixed) >= 2:
        centers = _bgr_to_lab([c[0] for c in fixed])
        hexes = [c[1] for c in fixed]
        labels = np.full((h, w), -1, np.int32)
        labels[opaque] = _assign_flat(lab[opaque], centers)
        return labels, hexes

    flat = lab[opaque]
    if flat.shape[0] > 60_000:
        pick = np.random.default_rng(0).choice(flat.shape[0], 60_000, replace=False)
        samples = np.ascontiguousarray(flat[pick])
    else:
        samples = np.ascontiguousarray(flat)
    centers = _kmeans_centers(samples, p.colors)
    sample_labels = _assign_flat(samples, centers)
    popularity = np.bincount(sample_labels, minlength=len(centers))
    centers = _distinct_centers(centers[np.argsort(-popularity)])
    labels = np.full((h, w), -1, np.int32)
    labels[opaque] = _assign_flat(lab[opaque], centers)
    counts = np.bincount(labels[opaque], minlength=len(centers))
    order = np.argsort(-counts)
    remapped = np.full_like(labels, -1)
    for new, old in enumerate(order):
        remapped[labels == old] = new
    return remapped, _lab_to_hex(centers[order])


def _absorb_specks(labels, min_px):
    """Fold tiny islands into a neighboring color. Alpha stays transparent."""
    if min_px <= 0:
        return labels
    out = labels.copy()
    removed = np.zeros(out.shape, bool)
    max_lab = int(out.max()) if out.size else -1
    for lab in range(max_lab + 1):
        mask = out == lab
        if not mask.any():
            continue
        n, cc, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        areas = stats[:, cv2.CC_STAT_AREA].copy()
        areas[0] = min_px
        kill = np.flatnonzero(areas < min_px)
        if kill.size:
            hit = np.isin(cc, kill)
            removed |= hit
            out[hit] = -1
    if not removed.any():
        return out
    iterations = min(24, int(min_px ** 0.5) + 1)
    for _ in range(iterations):
        holes = removed & (out < 0)
        if not holes.any():
            break
        acc = out
        changed = False
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            shifted = np.roll(out, (dy, dx), (0, 1))
            if dy > 0:
                shifted[:dy, :] = -1
            elif dy < 0:
                shifted[dy:, :] = -1
            if dx > 0:
                shifted[:, :dx] = -1
            elif dx < 0:
                shifted[:, dx:] = -1
            take = holes & (shifted >= 0) & (acc < 0)
            if not take.any():
                continue
            if acc is out:
                acc = out.copy()
            acc[take] = shifted[take]
            changed = True
        if not changed:
            break
        out = acc
    return out


def _dilate(mask, px):
    px = int(round(px))
    if px <= 0:
        return mask
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (px * 2 + 1, px * 2 + 1))
    return cv2.dilate(mask, k, iterations=1, borderType=cv2.BORDER_CONSTANT,
                      borderValue=0)


def _layers(labels, hexes, p, flat):
    """(paint-back-to-front path elements, curves, segs, palette, drawn)."""
    skip = set(p.skip or [])
    counts = np.bincount(labels[labels >= 0], minlength=len(hexes))
    total = max(1, labels.size)
    palette = []
    pending = []
    curves = segs = 0
    for i, hx in enumerate(hexes):
        area = int(counts[i]) if i < len(counts) else 0
        on = hx not in skip
        palette.append({
            'hex': hx,
            'pct': round(area * 100.0 / total, 1),
            'on': on,
        })
        if not on or area <= 0:
            continue
        mask = (labels == i).astype(np.uint8) * 255
        if flat:
            pending.append((area, i, mask, '#000000'))
            continue
        mask = _dilate(mask, p.overlap)
        commands, c, s = _trace_mask(mask, p)
        if not commands:
            continue
        curves += c
        segs += s
        pending.append((area, i, commands, hx))
    if flat:
        acc = np.zeros(labels.shape, np.uint8)
        for _area, _i, mask, _fill in pending:
            acc[mask > 0] = 255
        commands, curves, segs = _trace_mask(acc, p)
        if not commands:
            return [], 0, 0, palette, 0
        body = [_path_el(commands, '#000000', None)]
        return body, curves, segs, palette, 1
    pending.sort(key=lambda item: -item[0])
    body = [_path_el(commands, hx, i) for _area, i, commands, hx in pending]
    return body, curves, segs, palette, len(body)


def _stats(mask_shape, labels, palette, curves, segs, drawn, svg):
    h, w = mask_shape
    on = {item['hex'] for item in palette if item['on']}
    if on:
        keep = np.zeros(labels.shape, bool)
        for i, item in enumerate(palette):
            if item['on']:
                keep |= labels == i
        ink = float(keep.mean())
    else:
        ink = 0.0
    return {
        'paths': curves,
        'segments': segs,
        'width_px': int(w),
        'height_px': int(h),
        'ink_pct': round(ink * 100, 1),
        'svg_kb': round(len(svg.encode()) / 1024, 1),
        'colors': int(drawn),
        'palette': palette,
    }


def trace(data: bytes, p: VecParams, flat: bool = False):
    """Returns (svg_text, stats). flat paints every visible color as black."""
    if p.mode != 'color':
        mask = mask_of(data, p)
        ink = float((mask > 0).mean())
        if ink < 1e-5:
            raise ValueError('no_ink')
        if ink > 0.98:
            raise ValueError('all_ink')
        commands, curves, segs = _trace_mask(mask, p)
        if not commands:
            raise ValueError('no_ink')
        h, w = mask.shape
        svg = _svg(w, h, _path_el(commands, '#000000', None))
        return svg, {
            'paths': curves, 'segments': segs, 'width_px': int(w),
            'height_px': int(h), 'ink_pct': round(ink * 100, 1),
            'svg_kb': round(len(svg.encode()) / 1024, 1),
        }

    bgr, alpha = _prepare_color(data, p)
    labels, hexes = _quantize(bgr, alpha, p)
    labels = _absorb_specks(labels, int(p.despeckle))
    body, curves, segs, palette, drawn = _layers(labels, hexes, p, flat)
    if not body:
        raise ValueError('no_color')
    h, w = labels.shape
    svg = _svg(w, h, ''.join(body))
    return svg, _stats((h, w), labels, palette, curves, segs, drawn, svg)
