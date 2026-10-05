"""SVG line-art -> printable solids (base plate + raised line art).

Usage: svg2solid.py <in.svg> <outdir> [--width MM] [--base MM] [--relief MM]
"""
import os
import sys
import zipfile
import numpy as np
from svgelements import SVG, Path, Shape, Line, Close, Move
from shapely.geometry import Polygon
from shapely.ops import unary_union
import trimesh

FLAT = 1.2  # max sample step in svg user units


def sample_subpath(sp):
    pts = []
    for seg in sp:
        if isinstance(seg, Move):
            pts.append((seg.end.x, seg.end.y))
            continue
        if isinstance(seg, (Line, Close)):
            pts.append((seg.end.x, seg.end.y))
            continue
        try:
            ln = seg.length(error=1e-3)
        except Exception:
            ln = 10.0
        n = max(2, min(200, int(ln / FLAT) + 2))
        ts = np.linspace(0, 1, n)[1:]
        for t in ts:
            p = seg.point(t)
            pts.append((p.x, p.y))
    return pts


def load_rings(src):
    svg = SVG.parse(src)
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
            pts = sample_subpath(Path(sub))
            if len(pts) < 4:
                continue
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area < 1e-6:
                continue
            rings.append(Polygon(pts))
    return rings


def evenodd_polygons(rings):
    """Nest rings by containment depth: even depth = solid, odd = hole."""
    valid = []
    for r in rings:
        rr = r if r.is_valid else r.buffer(0)
        if rr.geom_type == 'Polygon' and not rr.is_empty:
            valid.append(Polygon(r.exterior))
    order = sorted(range(len(valid)), key=lambda i: -valid[i].area)
    depth = [0] * len(valid)
    parent = [None] * len(valid)
    for ii, i in enumerate(order):
        pi = valid[i].representative_point()
        for j in order[:ii]:
            if valid[j].contains(pi):
                depth[i] += 1
                parent[i] = j  # last (smallest) container wins
    out = []
    for i in range(len(valid)):
        if depth[i] % 2 == 0:
            holes = [valid[k].exterior.coords for k in range(len(valid))
                     if parent[k] == i and depth[k] % 2 == 1]
            out.append(Polygon(valid[i].exterior.coords, holes))
    return out


def to_mm(polys, width_mm):
    """Flip Y (SVG y-down -> model y-up), scale to width, centre on origin."""
    allp = unary_union([p.buffer(0) for p in polys])
    minx, miny, maxx, maxy = allp.bounds
    s = width_mm / (maxx - minx)
    from shapely.affinity import affine_transform
    # x' = (x-minx)*s - w/2 ; y' = (maxy-y)*s - h/2
    h = (maxy - miny) * s
    m = [s, 0, 0, -s, -minx * s - width_mm / 2.0, maxy * s - h / 2.0]
    return [affine_transform(p, m) for p in polys]


def extrude(polys, height, z0=0.0):
    meshes = []
    for p in polys:
        if p.is_empty or p.area <= 0:
            continue
        m = trimesh.creation.extrude_polygon(p, height, engine='earcut')
        m.apply_translation([0, 0, z0])
        meshes.append(m)
    return trimesh.util.concatenate(meshes)


def write_3mf(path, named_meshes):
    """Minimal but valid 3MF (millimetre units) with one object per mesh."""
    objs, items = [], []
    for oid, (name, m) in enumerate(named_meshes, start=1):
        v = '\n'.join('     <vertex x="%.4f" y="%.4f" z="%.4f"/>' % tuple(p)
                      for p in m.vertices)
        t = '\n'.join('     <triangle v1="%d" v2="%d" v3="%d"/>' % tuple(f)
                      for f in m.faces)
        objs.append(
            '  <object id="%d" type="model" name="%s">\n'
            '   <mesh>\n    <vertices>\n%s\n    </vertices>\n'
            '    <triangles>\n%s\n    </triangles>\n   </mesh>\n  </object>'
            % (oid, name, v, t))
        items.append('  <item objectid="%d"/>' % oid)
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        ' <metadata name="Application">svg2solid</metadata>\n'
        ' <resources>\n%s\n </resources>\n <build>\n%s\n </build>\n</model>\n'
        % ('\n'.join(objs), '\n'.join(items)))
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            '</Relationships>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
          '</Types>')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('_rels/.rels', rels)
        z.writestr('3D/3dmodel.model', model)


def main():
    src = sys.argv[1]
    outdir = sys.argv[2]
    args = sys.argv[3:]

    def opt(flag, default):
        return float(args[args.index(flag) + 1]) if flag in args else default

    width = opt('--width', 150.0)
    base_h = opt('--base', 2.4)
    relief_h = opt('--relief', 0.8)
    min_island = opt('--min-island', 4.0)  # mm^2, drop tiny base islands

    os.makedirs(outdir, exist_ok=True)
    rings = load_rings(src)
    polys = evenodd_polygons(rings)
    polys = to_mm(polys, width)
    art = unary_union([p.buffer(0) for p in polys])
    art = art.buffer(0.01).buffer(-0.01)  # weld hairline gaps

    art_parts = list(art.geoms) if art.geom_type == 'MultiPolygon' else [art]
    # base = artwork silhouette with interior holes filled
    filled = unary_union([Polygon(p.exterior) for p in art_parts])
    base_parts = list(filled.geoms) if filled.geom_type == 'MultiPolygon' else [filled]
    kept = [p for p in base_parts if p.area >= min_island]
    dropped = len(base_parts) - len(kept)
    base = unary_union(kept)
    base_parts = list(base.geoms) if base.geom_type == 'MultiPolygon' else [base]
    # keep only artwork that actually sits on the base
    art_parts = [p for p in art_parts if base.intersects(p.representative_point())]

    minx, miny, maxx, maxy = base.bounds
    print('size: %.1f x %.1f mm | base islands: %d (dropped %d) | art islands: %d'
          % (maxx - minx, maxy - miny, len(base_parts), dropped, len(art_parts)))

    m_base = extrude(base_parts, base_h, 0.0)
    m_art = extrude(art_parts, relief_h, base_h)
    print('base: %d tris watertight=%s | art: %d tris watertight=%s'
          % (len(m_base.faces), m_base.is_watertight,
             len(m_art.faces), m_art.is_watertight))

    m_base.export(os.path.join(outdir, 'cats_base_plate.stl'))
    m_art.export(os.path.join(outdir, 'cats_line_art.stl'))
    combined = trimesh.util.concatenate([m_base, m_art])
    combined.export(os.path.join(outdir, 'cats_single_color.stl'))

    # 3MF: shift both parts by the same offset into positive space
    off = [-minx, -miny, 0.0]
    b3, a3 = m_base.copy(), m_art.copy()
    b3.apply_translation(off)
    a3.apply_translation(off)
    write_3mf(os.path.join(outdir, 'cats_two_color.3mf'),
              [('base_plate', b3), ('line_art', a3)])
    print('written to', outdir)


if __name__ == '__main__':
    main()
