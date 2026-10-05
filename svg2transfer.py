"""SVG line art -> flat single-colour TPU patch for iron-on transfer.

Extrudes ONLY the black (filled) areas of the SVG into a thin flat part.
Optionally links the separate islands with hair-thin bridges so the whole
design prints and transfers as one piece.

Usage:
  svg2transfer.py <in.svg> <outdir> [--width 150] [--thickness 0.4]
                  [--bridge 0.7] [--grow 0] [--mirror]
"""
import os
import sys
import numpy as np
import trimesh
from shapely.geometry import LineString
from shapely.ops import unary_union, nearest_points

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from svg2solid import load_rings, evenodd_polygons, to_mm, extrude, write_3mf


def parts_of(geom):
    return list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]


def bridge_islands(art, width):
    """Greedily link every island to the growing main body, nearest first."""
    islands = sorted(parts_of(art), key=lambda p: -p.area)
    merged = islands[0]
    pending = islands[1:]
    bridges = []
    while pending:
        best = min(((i, merged.distance(p)) for i, p in enumerate(pending)),
                   key=lambda t: t[1])
        i, gap = best
        isl = pending.pop(i)
        a, b = nearest_points(merged, isl)
        # extend the link slightly into both parts so the union is clean
        v = np.array([b.x - a.x, b.y - a.y])
        n = np.linalg.norm(v)
        v = v / n * 0.3 if n > 1e-9 else np.array([0.3, 0.0])
        link = LineString([(a.x - v[0], a.y - v[1]),
                           (b.x + v[0], b.y + v[1])]).buffer(width / 2.0, cap_style=2)
        bridges.append((gap, link))
        merged = unary_union([merged, isl, link])
    return merged, bridges


def main():
    src, outdir = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]

    def opt(flag, default):
        return float(args[args.index(flag) + 1]) if flag in args else default

    width = opt('--width', 150.0)
    thickness = opt('--thickness', 0.4)
    bridge_w = opt('--bridge', 0.5)
    grow = opt('--grow', 0.0)
    mirror = '--mirror' in args
    tag = opt('--tag', 0)  # unused placeholder to keep opt() simple

    os.makedirs(outdir, exist_ok=True)
    polys = to_mm(evenodd_polygons(load_rings(src)), width)
    art = unary_union([p.buffer(0) for p in polys]).buffer(0.01).buffer(-0.01)
    if grow > 0:
        art = art.buffer(grow, join_style=1).buffer(0)
    if mirror:
        from shapely.affinity import scale as sscale
        art = sscale(art, xfact=-1, yfact=1, origin='center')

    loose = parts_of(art)
    bridged, bridges = bridge_islands(art, bridge_w)
    bparts = parts_of(bridged)

    minx, miny, maxx, maxy = art.bounds
    print('patch %.1f x %.1f mm, thickness %.2f mm' % (maxx - minx, maxy - miny, thickness))
    print('loose pieces: %d | after bridging: %d piece(s), %d bridges, longest gap %.2f mm'
          % (len(loose), len(bparts), len(bridges),
             max([g for g, _ in bridges]) if bridges else 0))
    for name, geom, n in (('loose', art, len(loose)), ('bridged', bridged, len(bparts))):
        vol = geom.area * thickness / 1000.0
        print('  %-8s black area %6.1f cm2  ->  %.2f cm3 TPU (~%.1f g)'
              % (name, geom.area / 100, vol, vol * 1.21))

    suffix = '_%gmm' % thickness + ('_mirrored' if mirror else '')
    m_loose = extrude(loose, thickness, 0.0)
    m_brid = extrude(bparts, thickness, 0.0)
    for nm, m in (('cats_patch_loose' + suffix, m_loose),
                  ('cats_patch_bridged' + suffix, m_brid)):
        m.export(os.path.join(outdir, nm + '.stl'))
        mm = m.copy()
        mm.apply_translation([-m.bounds[0][0], -m.bounds[0][1], 0])
        write_3mf(os.path.join(outdir, nm + '.3mf'), [(nm, mm)])
        print('  wrote %s.stl / .3mf  (%d tris, watertight=%s)'
              % (nm, len(m.faces), m.is_watertight))
    return art, bridged, bridges


if __name__ == '__main__':
    main()
