"""Geometry core: line art (SVG or bitmap) -> printable flat/relief solids.

Two products share one pipeline:
  * patch  - flat single-colour plate of the black areas only (iron-on TPU transfer)
  * plaque - filled silhouette base + the black art raised on top (keychain / sign)
"""
from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass, field, asdict

import numpy as np
import trimesh
from shapely.affinity import affine_transform, scale as sscale
from shapely.geometry import Polygon, LineString, Point, box
from shapely.ops import unary_union, nearest_points

FLATTEN = 1.2       # max curve sampling step, in source units
TPU_DENSITY = 1.21  # g/cm3, used for the weight estimate


# --------------------------------------------------------------------------- #
# input -> polygons (source units, y pointing down)
# --------------------------------------------------------------------------- #

def _sample_subpath(sp):
    from svgelements import Line, Close, Move
    pts = []
    for seg in sp:
        if isinstance(seg, (Move, Line, Close)):
            pts.append((seg.end.x, seg.end.y))
            continue
        try:
            ln = seg.length(error=1e-3)
        except Exception:
            ln = 10.0
        n = max(2, min(200, int(ln / FLATTEN) + 2))
        for t in np.linspace(0, 1, n)[1:]:
            p = seg.point(t)
            pts.append((p.x, p.y))
    return pts


def rings_from_svg(data: bytes):
    from svgelements import SVG, Path, Shape
    svg = SVG.parse(io.BytesIO(data))
    rings = []
    for el in svg.elements():
        if not isinstance(el, Shape):
            continue
        try:
            p = Path(el)
        except Exception:
            continue
        if len(p) == 0:
            continue
        for sub in p.as_subpaths():
            pts = _sample_subpath(Path(sub))
            if len(pts) >= 4:
                poly = Polygon(pts)
                if poly.is_valid or not poly.buffer(0).is_empty:
                    rings.append(Polygon(pts))
    return rings


def _evenodd(rings):
    """Nest rings by containment depth: even depth = solid, odd = hole."""
    valid = []
    for r in rings:
        rr = r if r.is_valid else r.buffer(0)
        if not rr.is_empty and abs(r.area) > 1e-9:
            valid.append(Polygon(r.exterior))
    order = sorted(range(len(valid)), key=lambda i: -valid[i].area)
    depth = [0] * len(valid)
    parent = [None] * len(valid)
    for ii, i in enumerate(order):
        pi = valid[i].representative_point()
        for j in order[:ii]:
            if valid[j].contains(pi):
                depth[i] += 1
                parent[i] = j
    out = []
    for i in range(len(valid)):
        if depth[i] % 2 == 0:
            holes = [valid[k].exterior.coords for k in range(len(valid))
                     if parent[k] == i and depth[k] % 2 == 1]
            out.append(Polygon(valid[i].exterior.coords, holes))
    return out


def polygons_from_bitmap(data: bytes, threshold: int = 128, invert: bool = False):
    """Trace the dark pixels of a raster image into polygons with holes."""
    import cv2
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise ValueError('image_unreadable')
    if arr.ndim == 3 and arr.shape[2] == 4:               # composite alpha on white
        a = arr[:, :, 3:4].astype(np.float32) / 255.0
        arr = (arr[:, :, :3].astype(np.float32) * a + 255 * (1 - a)).astype(np.uint8)
    gray = arr if arr.ndim == 2 else cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    mode = cv2.THRESH_BINARY if invert else cv2.THRESH_BINARY_INV
    _, mask = cv2.threshold(gray, int(threshold), 255, mode)
    cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return []
    hier = hier[0]
    polys = []
    for i, c in enumerate(cnts):
        if hier[i][3] != -1 or len(c) < 4:                # only outer contours here
            continue
        holes = []
        h = hier[i][2]
        while h != -1:
            if len(cnts[h]) >= 4:
                holes.append(cnts[h].reshape(-1, 2))
            h = hier[h][0]
        try:
            p = Polygon(c.reshape(-1, 2), holes)
            p = p if p.is_valid else p.buffer(0)
            if not p.is_empty and p.area > 1:
                polys.append(p)
        except Exception:
            continue
    return polys


def load_source(data: bytes, filename: str, threshold: int = 128, invert: bool = False):
    """Returns (polygons in source units, kind)."""
    head = data[:400].lstrip()
    is_svg = filename.lower().endswith('.svg') or head.startswith(b'<?xml') \
        or head.startswith(b'<svg')
    if is_svg:
        return _evenodd(rings_from_svg(data)), 'svg'
    return polygons_from_bitmap(data, threshold, invert), 'bitmap'


# --------------------------------------------------------------------------- #
# parameters
# --------------------------------------------------------------------------- #

@dataclass
class Params:
    mode: str = 'patch'            # patch | plaque
    fit: str = 'width'             # width | height
    size_mm: float = 150.0
    mirror: bool = False
    grow_mm: float = 0.0           # uniform dilation of the art
    simplify_mm: float = 0.02      # contour simplification (mesh size)
    nozzle_mm: float = 0.4         # stroke width compared with this nozzle
    # patch
    thickness_mm: float = 0.4
    bridges: bool = True
    bridge_mm: float = 0.5
    # plaque
    base_mm: float = 2.4
    relief_mm: float = 0.8
    base_shape: str = 'silhouette'  # silhouette | rect
    base_margin_mm: float = 0.0
    corner_r_mm: float = 6.0
    min_island_mm2: float = 4.0
    # hole
    hole: bool = False
    hole_d_mm: float = 4.0
    hole_x: float = 0.08           # 0..1 across the bounding box
    hole_y: float = 0.92
    hole_rim_mm: float = 2.0       # material around the hole -> tab if outside


# User-controlled numbers are clamped here. Unknown enum values fall back
# to the dataclass default. Non-numeric input raises ValueError (HTTP 400).
_RANGES = {
    'size_mm': (10.0, 300.0),
    'thickness_mm': (0.2, 8.0),
    'base_mm': (0.2, 8.0),
    'relief_mm': (0.2, 8.0),
    'grow_mm': (0.0, 1.5),
    'simplify_mm': (0.0, 1.0),
    'bridge_mm': (0.0, 3.0),
    'base_margin_mm': (0.0, 30.0),
    'corner_r_mm': (0.0, 40.0),
    'min_island_mm2': (0.0, 500.0),
    'hole_d_mm': (1.0, 12.0),
    'hole_rim_mm': (0.4, 8.0),
    'hole_x': (0.0, 1.0),
    'hole_y': (0.0, 1.0),
    'nozzle_mm': (0.2, 1.0),
}
_ENUMS = {
    'mode': ('patch', 'plaque'),
    'fit': ('width', 'height'),
    'base_shape': ('silhouette', 'rect'),
}


def _finite_float(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError('bad_number')
    if x != x or x == float('inf') or x == float('-inf'):
        raise ValueError('bad_number')
    return x


def params_from_dict(d: dict) -> Params:
    p = Params()
    for k, v in (d or {}).items():
        if not hasattr(p, k):
            continue
        cur = getattr(p, k)
        if isinstance(cur, bool):
            v = v in (True, 'true', 'True', 1, '1')
        elif isinstance(cur, float):
            v = _finite_float(v)
            lo, hi = _RANGES.get(k, (None, None))
            if lo is not None:
                if v < lo:
                    v = lo
                elif v > hi:
                    v = hi
        elif isinstance(cur, str):
            allowed = _ENUMS.get(k)
            if allowed is not None and v not in allowed:
                continue
        setattr(p, k, v)
    return p


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def _parts(geom):
    if geom.is_empty:
        return []
    return list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]


def _fit(polys, p: Params):
    """Flip Y (source is y-down), scale to the requested size, centre on origin."""
    allp = unary_union([g.buffer(0) for g in polys])
    minx, miny, maxx, maxy = allp.bounds
    w, h = maxx - minx, maxy - miny
    s = p.size_mm / (w if p.fit == 'width' else h)
    W, H = w * s, h * s
    m = [s, 0, 0, -s, -minx * s - W / 2.0, maxy * s - H / 2.0]
    return [affine_transform(g, m) for g in polys]


def bridge_islands(art, width):
    """Greedily link each island to the growing body, nearest gap first."""
    islands = sorted(_parts(art), key=lambda g: -g.area)
    if len(islands) < 2:
        return art, []
    merged, pending, bridges = islands[0], islands[1:], []
    while pending:
        i, gap = min(((i, merged.distance(g)) for i, g in enumerate(pending)),
                     key=lambda t: t[1])
        isl = pending.pop(i)
        a, b = nearest_points(merged, isl)
        v = np.array([b.x - a.x, b.y - a.y])
        n = float(np.linalg.norm(v))
        v = v / n * 0.3 if n > 1e-9 else np.array([0.3, 0.0])
        link = LineString([(a.x - v[0], a.y - v[1]),
                           (b.x + v[0], b.y + v[1])]).buffer(width / 2.0, cap_style=2)
        bridges.append({'gap': round(gap, 2), 'geom': link})
        merged = unary_union([merged, isl, link])
    return merged, bridges


def _hole_geoms(bounds, p: Params):
    minx, miny, maxx, maxy = bounds
    cx = minx + p.hole_x * (maxx - minx)
    cy = miny + p.hole_y * (maxy - miny)
    c = Point(cx, cy)
    return (c.buffer(p.hole_d_mm / 2.0 + p.hole_rim_mm, 64),
            c.buffer(p.hole_d_mm / 2.0, 64))


@dataclass
class Result:
    art: object = None            # raised / flat black geometry
    base: object = None           # plaque base, None for patch mode
    bridges: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def build(src_polys, p: Params) -> Result:
    r = Result()
    tab_ok = True
    polys = _fit(src_polys, p)
    art = unary_union([g.buffer(0) for g in polys]).buffer(0.01).buffer(-0.01)
    if p.grow_mm > 0:
        art = art.buffer(p.grow_mm, join_style=1).buffer(0)
    if p.mirror:
        art = sscale(art, xfact=-1, yfact=1, origin='center')
    if p.simplify_mm > 0:
        art = art.simplify(p.simplify_mm, preserve_topology=True).buffer(0)

    loose_n = len(_parts(art))
    bounds = art.bounds

    if p.mode == 'plaque':
        filled = unary_union([Polygon(g.exterior) for g in _parts(art)])
        kept = [g for g in _parts(filled) if g.area >= p.min_island_mm2]
        dropped = len(_parts(filled)) - len(kept)
        base = unary_union(kept) if kept else filled
        if p.base_shape == 'rect':
            minx, miny, maxx, maxy = art.bounds
            m, cr = max(p.base_margin_mm, 1.0), p.corner_r_mm
            base = box(minx - m, miny - m, maxx + m, maxy + m)
            if cr > 0:
                base = base.buffer(-cr, join_style=1).buffer(cr, join_style=1)
        elif p.base_margin_mm > 0:
            base = base.buffer(p.base_margin_mm, join_style=1).buffer(0)
        if p.hole:
            tab, hole = _hole_geoms(base.bounds, p)
            tab_ok = base.intersects(tab)
            base = unary_union([base, tab]).difference(hole)
            art = art.difference(hole)
        art = unary_union([g for g in _parts(art)
                           if base.intersects(g.representative_point())])
        r.base = base
        if dropped:
            r.warnings.append({'code': 'islands_dropped', 'n': dropped,
                               'area': round(p.min_island_mm2, 1)})
    else:
        if p.bridges:
            art, r.bridges = bridge_islands(art, p.bridge_mm)
        if p.hole:
            tab, hole = _hole_geoms(bounds, p)
            tab_ok = art.intersects(tab)
            art = unary_union([art, tab]).difference(hole)

    r.art = art
    # physically separate bodies: on a plaque the art rests on the base
    whole = unary_union([r.base, art]) if r.base is not None else art
    final_n = len(_parts(whole))

    minx, miny, maxx, maxy = (r.base if r.base is not None else art).bounds
    if r.base is not None:
        vol = r.base.area * p.base_mm + art.area * p.relief_mm
        total_h = p.base_mm + p.relief_mm
    else:
        vol = art.area * p.thickness_mm
        total_h = p.thickness_mm
    nozzle = min(2.0, max(0.1, float(p.nozzle_mm)))
    thin = 0.0
    if art.area > 0:
        eroded = art.buffer(-nozzle / 2.0)
        thin = 100 * (1 - (0.0 if eroded.is_empty else eroded.area) / art.area)

    r.stats = {
        'width_mm': round(maxx - minx, 1), 'height_mm': round(maxy - miny, 1),
        'total_h_mm': round(total_h, 2),
        'loose_islands': loose_n, 'pieces': final_n,
        'bridges': len(r.bridges),
        'longest_gap_mm': round(max([b['gap'] for b in r.bridges]), 2) if r.bridges else 0,
        'area_cm2': round(art.area / 100, 1),
        'volume_cm3': round(vol / 1000, 2),
        'weight_g': round(vol / 1000 * TPU_DENSITY, 1),
        'thin_pct': round(thin, 1),
        'nozzle_mm': nozzle,
    }

    if p.mode == 'patch' and final_n > 1:
        r.warnings.append({'code': 'pieces_split', 'n': final_n})
    if thin > 20:
        r.warnings.append({'code': 'thin_area', 'pct': int(round(thin)), 'mm': nozzle})
    if p.mode == 'patch' and p.bridges and p.bridge_mm + 1e-9 < nozzle:
        r.warnings.append({'code': 'bridge_thin', 'bridge': round(p.bridge_mm, 2),
                           'mm': nozzle})
    if p.hole and not tab_ok:
        r.warnings.append({'code': 'hole_detached'})
    return r


# --------------------------------------------------------------------------- #
# preview (SVG)
# --------------------------------------------------------------------------- #

def _path_d(poly):
    def ring(cs):
        return 'M' + ' L'.join('%.3f %.3f' % (x, -y) for x, y in cs) + ' Z'
    return ' '.join([ring(poly.exterior.coords)] +
                    [ring(h.coords) for h in poly.interiors])


def _poly_parts(geom):
    if geom is None or geom.is_empty:
        return []
    gt = geom.geom_type
    if gt == 'Polygon':
        return [geom]
    if gt == 'MultiPolygon':
        return [g for g in geom.geoms if not g.is_empty]
    if gt == 'GeometryCollection':
        out = []
        for g in geom.geoms:
            out.extend(_poly_parts(g))
        return out
    return []


def _holes(art):
    """Interior openings large enough to be a real gap, not a sliver."""
    holes = []
    for g in _parts(art):
        if g.geom_type != 'Polygon':
            continue
        for ring in g.interiors:
            h = Polygon(ring)
            if not h.is_valid:
                h = h.buffer(0)
            if h.is_empty or h.geom_type != 'Polygon' or h.area < 0.2:
                continue
            holes.append(h)
    return holes


def _covered(holes, cover):
    if cover is None or cover.is_empty:
        return 0
    n = 0
    for h in holes:
        if cover.intersects(h.representative_point()):
            n += 1
    return n


# Slider value is the edge shift for a patch this thick. Thicker patches
# push more plastic sideways under the same iron.
SPREAD_REF_MM = 0.4


def iron_spread(slider_mm, thickness_mm):
    slider = min(1.5, max(0.0, float(slider_mm or 0)))
    thick = min(2.0, max(0.0, float(thickness_mm or 0)))
    return slider * (thick / SPREAD_REF_MM)


def detail_overlay(art, nozzle, spread):
    """Preview-only. Lost strokes (thinner than the nozzle) and iron-on bleed.

    Does not modify the mesh. A hole counts as closed when the grown plastic
    covers a point inside it. `spread` is the edge shift in mm, already scaled
    by patch thickness.
    """
    nozzle = min(2.0, max(0.1, float(nozzle)))
    spread = min(8.0, max(0.0, float(spread or 0)))
    holes = _holes(art)
    eroded = art.buffer(-nozzle / 2.0)
    if not eroded.is_empty and not eroded.is_valid:
        eroded = eroded.buffer(0)
    lost = art if eroded.is_empty else art.difference(eroded)
    bead = art.buffer(nozzle / 2.0, join_style=1)
    ironed = art.buffer(spread, join_style=1) if spread > 1e-9 else None
    bleed = None if ironed is None else ironed.difference(art)
    return {
        'lost': lost,
        'bleed': bleed,
        'gaps_nozzle': _covered(holes, bead),
        'gaps_closed': _covered(holes, ironed) if ironed is not None else 0,
        'spread': round(spread, 2),
        'nozzle': nozzle,
    }


def preview_svg(r: Result, p: Params, show_bridges=True, detail_thin=False,
                detail_press=False, spread_mm=0.0) -> str:
    host = r.base if r.base is not None else r.art
    minx, miny, maxx, maxy = host.bounds
    pad = max(maxx - minx, maxy - miny) * 0.04
    vb = (minx - pad, -(maxy + pad), (maxx - minx) + 2 * pad, (maxy - miny) + 2 * pad)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.2f %.2f %.2f %.2f" '
           'width="100%%" height="100%%" preserveAspectRatio="xMidYMid meet">' % vb]
    if r.base is not None:
        for g in _parts(r.base):
            out.append('<path d="%s" fill="#f5f5f7" stroke="#d1d1d6" '
                       'stroke-width="0.25" fill-rule="evenodd"/>' % _path_d(g))
    for g in _parts(r.art):
        out.append('<path d="%s" fill="#1d1d1f" fill-rule="evenodd"/>' % _path_d(g))
    if (detail_thin or detail_press) and r.art is not None and not r.art.is_empty:
        press = detail_press and p.mode == 'patch'
        spread = iron_spread(spread_mm, p.thickness_mm) if press else 0.0
        layers = detail_overlay(r.art, p.nozzle_mm, spread)
        if detail_thin:
            r.stats['gaps_nozzle'] = layers['gaps_nozzle']
            for g in _poly_parts(layers['lost']):
                out.append('<path d="%s" fill="#ff9f0a" fill-rule="evenodd"/>' % _path_d(g))
        if press:
            r.stats['gaps_closed'] = layers['gaps_closed']
            r.stats['spread_mm'] = layers['spread']
            for g in _poly_parts(layers['bleed']):
                out.append('<path d="%s" fill="#ff3b30" fill-rule="evenodd"/>' % _path_d(g))
    if show_bridges and r.bridges:
        for b in r.bridges:
            for g in _parts(b['geom']):
                out.append('<path d="%s" fill="#ff3b30"/>' % _path_d(g))
    out.append('</svg>')
    return ''.join(out)


# --------------------------------------------------------------------------- #
# meshes + export
# --------------------------------------------------------------------------- #

def _extrude(polys, height, z0=0.0):
    ms = []
    for g in polys:
        if g.is_empty or g.area <= 0:
            continue
        try:
            m = trimesh.creation.extrude_polygon(g, height, engine='earcut')
        except Exception:
            continue
        m.apply_translation([0, 0, z0])
        ms.append(m)
    if not ms:
        raise ValueError('empty_geometry')
    return trimesh.util.concatenate(ms)


def meshes(r: Result, p: Params) -> dict:
    if r.base is not None:
        return {'base_plate': _extrude(_parts(r.base), p.base_mm, 0.0),
                'line_art': _extrude(_parts(r.art), p.relief_mm, p.base_mm)}
    return {'patch': _extrude(_parts(r.art), p.thickness_mm, 0.0)}


def write_3mf(named) -> bytes:
    objs, items = [], []
    for oid, (name, m) in enumerate(named, start=1):
        v = '\n'.join('<vertex x="%.4f" y="%.4f" z="%.4f"/>' % tuple(x) for x in m.vertices)
        t = '\n'.join('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(f) for f in m.faces)
        objs.append('<object id="%d" type="model" name="%s"><mesh><vertices>%s</vertices>'
                    '<triangles>%s</triangles></mesh></object>' % (oid, name, v, t))
        items.append('<item objectid="%d"/>' % oid)
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" '
             'xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/'
             'core/2015/02"><metadata name="Application">lineart2print</metadata>'
             '<resources>%s</resources><build>%s</build></model>'
             % (''.join(objs), ''.join(items)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension='
                   '"rels" ContentType="application/vnd.openxmlformats-package.'
                   'relationships+xml"/><Default Extension="model" ContentType='
                   '"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr('_rels/.rels',
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://'
                   'schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                   'Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.'
                   'com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr('3D/3dmodel.model', model)
    return buf.getvalue()


def _as_bytes(data) -> bytes:
    return data.encode('utf-8') if isinstance(data, str) else data


def _mesh_file(ms: dict, fmt: str, combine: bool = False, single_name: str = '') -> bytes:
    """STL and PLY are one mesh. OBJ keeps a named object per part."""
    if fmt == 'obj' and not combine and len(ms) > 1:
        scene = trimesh.Scene()
        for name, mesh in ms.items():
            scene.add_geometry(mesh, geom_name=name)
        return _as_bytes(scene.export(file_type='obj', header='lineart2print'))
    m = (next(iter(ms.values())) if len(ms) == 1
         else trimesh.util.concatenate(list(ms.values())))
    if fmt == 'obj':
        m = m.copy()
        m.metadata['name'] = single_name or next(iter(ms))
        return _as_bytes(m.export(file_type='obj', header='lineart2print'))
    return _as_bytes(m.export(file_type=fmt))


def export(r: Result, p: Params, fmt: str, combine: bool = False):
    """Returns (bytes, filename). fmt: stl | 3mf | obj | ply"""
    ms = meshes(r, p)
    base_name = 'plaque' if r.base is not None else 'patch'
    tag = '_%gmm' % (p.base_mm + p.relief_mm if r.base is not None else p.thickness_mm)
    if fmt == '3mf':
        if combine or len(ms) == 1:
            m = trimesh.util.concatenate(list(ms.values()))
            named = [(base_name, m)]
        else:
            named = list(ms.items())
        off = [-min(m.bounds[0][0] for _, m in named),
               -min(m.bounds[0][1] for _, m in named), 0]
        shifted = []
        for n, m in named:
            mm = m.copy()
            mm.apply_translation(off)
            shifted.append((n, mm))
        return write_3mf(shifted), '%s%s.3mf' % (base_name, tag)
    if fmt not in ('stl', 'obj', 'ply'):
        raise ValueError('bad_format')
    return _mesh_file(ms, fmt, combine, base_name), '%s%s.%s' % (base_name, tag, fmt)


def export_part(r: Result, p: Params, part: str, fmt: str):
    ms = meshes(r, p)
    if part not in ms:
        raise KeyError(part)
    m = ms[part]
    if fmt == '3mf':
        mm = m.copy()
        mm.apply_translation([-m.bounds[0][0], -m.bounds[0][1], 0])
        return write_3mf([(part, mm)]), '%s.3mf' % part
    if fmt not in ('stl', 'obj', 'ply'):
        raise ValueError('bad_format')
    return _mesh_file({part: m}, fmt, single_name=part), '%s.%s' % (part, fmt)
