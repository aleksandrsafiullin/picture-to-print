"""Public-service limits: clamps, in-process queue, TTL, SVG headers."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import core
import server
import vectorize

CATS = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'samples' / 'cats.svg'


def _upload(client, name='cats.svg', data=None, mime='image/svg+xml'):
    body = CATS.read_bytes() if data is None else data
    r = client.post('/api/upload', files={'file': (name, body, mime)})
    assert r.status_code == 200, r.text
    return r.json()


def _wait_status(client, ticket, want, timeout=8):
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        r = client.get('/api/queue/' + ticket)
        assert r.status_code == 200, r.text
        seen = r.json()
        if seen['status'] in want:
            return seen
        time.sleep(0.02)
    raise AssertionError('ticket %s stuck at %s' % (ticket, seen))


@contextmanager
def _fill_slots(client, monkeypatch):
    """Hold both worker slots inside the patched preview body."""
    gate = threading.Event()
    entered = []

    def slow(payload):
        entered.append(payload.get('job'))
        gate.wait(5)
        return {'svg': '<svg/>', 'stats': {'width_mm': 1}, 'warnings': []}

    monkeypatch.setattr(server, '_preview_work', slow)
    pool = ThreadPoolExecutor(max_workers=2)
    futs = [
        pool.submit(client.post, '/api/preview', json={'job': 'hold-%d' % i, 'params': {}})
        for i in range(2)
    ]
    try:
        deadline = time.time() + 3
        while len(entered) < 2 and time.time() < deadline:
            time.sleep(0.01)
        if len(entered) < 2:
            gate.set()
            raise AssertionError('workers did not start (%d)' % len(entered))
        yield
    finally:
        gate.set()
        for fut in futs:
            fut.result(timeout=8)
        pool.shutdown(wait=True)


def test_params_clamped_and_rejected():
    p = core.params_from_dict({
        'size_mm': 10 ** 9,
        'mode': 'nope',
        'fit': 'sideways',
        'base_shape': 'circle',
        'thickness_mm': 99,
        'base_mm': 0,
        'relief_mm': -1,
        'grow_mm': -3,
        'simplify_mm': 4,
        'bridge_mm': 9,
        'base_margin_mm': 80,
        'corner_r_mm': 90,
        'min_island_mm2': 9000,
        'hole_d_mm': 0.1,
        'hole_rim_mm': 0,
        'hole_x': 5,
        'hole_y': -1,
        'nozzle_mm': 5,
        'mirror': 'true',
        'bridges': 0,
        'hole': 1,
    })
    assert p.size_mm == 300
    assert p.mode == 'patch'
    assert p.fit == 'width'
    assert p.base_shape == 'silhouette'
    assert p.thickness_mm == 8
    assert p.base_mm == 0.2
    assert p.relief_mm == 0.2
    assert p.grow_mm == 0
    assert p.simplify_mm == 1
    assert p.bridge_mm == 3
    assert p.base_margin_mm == 30
    assert p.corner_r_mm == 40
    assert p.min_island_mm2 == 500
    assert p.hole_d_mm == 1
    assert p.hole_rim_mm == 0.4
    assert p.hole_x == 1
    assert p.hole_y == 0
    assert p.nozzle_mm == 1
    assert p.mirror is True
    assert p.bridges is False
    assert p.hole is True
    try:
        core.params_from_dict({'size_mm': 'nope'})
    except ValueError:
        pass
    else:
        raise AssertionError('non-numeric size_mm should raise')

    v = vectorize.params_from_dict({
        'threshold': 999,
        'blur': -2,
        'despeckle': 10 ** 6,
        'smooth': 9,
        'tolerance': -1,
        'upscale': 9,
        'colors': 1,
        'overlap': 9,
        'mode': 'rgb',
        'palette': ['#fff', 'nope'] + ['#112233'] * 20,
    })
    assert v.threshold == 255
    assert v.blur == 0
    assert v.despeckle == 200
    assert v.smooth == 1.334
    assert v.tolerance == 0
    assert v.upscale == 3
    assert v.colors == 2
    assert v.overlap == 3
    assert v.mode == 'mono'
    assert v.palette is not None and len(v.palette) == 16
    try:
        vectorize.params_from_dict({'blur': 'wide'})
    except ValueError:
        pass
    else:
        raise AssertionError('non-numeric blur should raise')


def test_preview_clamps_absurd_size(client):
    job = _upload(client)['job']
    r = client.post('/api/preview', json={
        'job': job,
        'params': {'size_mm': 10 ** 12, 'mode': 'nope', 'fit': 'width'},
    })
    assert r.status_code == 200, r.text
    width = r.json()['stats']['width_mm']
    assert 299 <= width <= 301, width


def test_bad_number_is_400(client):
    job = _upload(client)['job']
    r = client.post('/api/preview', json={'job': job, 'params': {'size_mm': 'nope'}})
    assert r.status_code == 400, r.text
    assert r.json()['error'] == 'geometry_failed'


def test_upload_cats_and_empty(client):
    body = _upload(client)
    assert body['kind'] == 'svg'
    assert body['name'] == 'cats.svg'
    assert body['islands'] >= 1
    r = client.post('/api/upload', files={'file': ('empty.svg', b'', 'image/svg+xml')})
    assert r.status_code == 400
    assert r.json()['error'] == 'empty_file'


def test_queue_ahead_then_completes(client, monkeypatch):
    with _fill_slots(client, monkeypatch):
        first = client.post('/api/preview', json={'job': 'c', 'params': {}})
        assert first.status_code == 202, first.text
        body = first.json()
        assert body['queued'] is True
        assert body['kind'] == 'preview'
        assert body['ahead'] == 0
        assert body['position'] == 1
        second = client.post('/api/preview', json={'job': 'd', 'params': {}})
        assert second.status_code == 202, second.text
        assert second.json()['ahead'] == 1
        assert second.json()['position'] == 2
        live = client.get('/api/queue/' + body['ticket']).json()
        assert live['status'] == 'queued'
        assert live['ahead'] == 0
        ticket = body['ticket']
    done = _wait_status(client, ticket, {'done', 'error'})
    assert done['status'] == 'done', done
    assert done['result']['svg'] == '<svg/>'
    missing = client.get('/api/queue/does-not-exist')
    assert missing.status_code == 404
    assert missing.json()['error'] == 'ticket_missing'


def test_preview_supersedes_waiting_ticket(client, monkeypatch):
    with _fill_slots(client, monkeypatch):
        older = client.post('/api/preview', json={'job': 'same', 'params': {'size_mm': 20}})
        newer = client.post('/api/preview', json={'job': 'same', 'params': {'size_mm': 30}})
        assert older.status_code == 202, older.text
        assert newer.status_code == 202, newer.text
        assert newer.json()['ahead'] == 0
        stale = client.get('/api/queue/' + older.json()['ticket'])
        assert stale.status_code == 200, stale.text
        assert stale.json()['status'] == 'error'
        assert stale.json()['error'] == 'superseded'
        fresh = client.get('/api/queue/' + newer.json()['ticket']).json()
        assert fresh['status'] == 'queued'
        assert fresh['ahead'] == 0


def test_queue_full(client, monkeypatch):
    with _fill_slots(client, monkeypatch):
        for i in range(server.QUEUE_CAP):
            r = client.post('/api/preview', json={'job': 'q-%d' % i, 'params': {}})
            assert r.status_code == 202, (i, r.status_code, r.text)
            assert r.json()['ahead'] == i
        overflow = client.post('/api/preview', json={'job': 'overflow', 'params': {}})
        assert overflow.status_code == 429, overflow.text
        assert overflow.json()['error'] == 'queue_full'


def test_delete_and_ttl_drop_job(client):
    job = _upload(client)['job']
    gone = client.delete('/api/job/' + job)
    assert gone.status_code == 204
    assert gone.content == b''
    later = client.post('/api/preview', json={'job': job, 'params': {}})
    assert later.status_code == 404
    assert later.json()['error'] == 'job_missing'

    job = _upload(client)['job']
    with server._lock:
        server._jobs[job]['ts'] = 0
    server._gc()
    expired = client.post('/api/preview', json={'job': job, 'params': {}})
    assert expired.status_code == 404
    assert expired.json()['error'] == 'job_missing'


def test_source_svg_cannot_run_script(client):
    job = _upload(client)['job']
    r = client.get('/api/source/' + job)
    assert r.status_code == 200
    assert r.headers['x-content-type-options'] == 'nosniff'
    assert r.headers['content-type'].startswith('image/svg+xml')
    csp = r.headers['content-security-policy']
    assert "default-src 'none'" in csp
    assert 'sandbox' in csp
    assert 'allow-scripts' not in csp
    assert 'unsafe-inline' not in csp
    assert 'unsafe-eval' not in csp
    page = client.get('/')
    page_csp = page.headers['content-security-policy']
    assert "script-src 'self'" in page_csp
    assert 'unsafe-inline' not in page_csp
    assert b'id="qstatus"' in page.content


def test_queued_export_file(client, monkeypatch):
    def fake_export(payload):
        return server._File(b'STLDATA', 'model/stl', 'attachment; filename="cat.stl"')

    monkeypatch.setattr(server, '_export_work', fake_export)
    with _fill_slots(client, monkeypatch):
        r = client.post('/api/export', json={'job': 'x', 'format': 'stl', 'params': {}})
        assert r.status_code == 202, r.text
        assert r.json()['kind'] == 'export'
        ticket = r.json()['ticket']
    done = _wait_status(client, ticket, {'done', 'error'})
    assert done == {'status': 'done', 'download': True}
    f = client.get('/api/queue/' + ticket + '/file')
    assert f.status_code == 200, f.text
    assert f.content == b'STLDATA'
    assert 'filename="cat.stl"' in f.headers['content-disposition']
    assert 'model/stl' in f.headers['content-type']
    again = client.get('/api/queue/' + ticket + '/file')
    assert again.status_code == 404
    assert again.json()['error'] == 'ticket_missing'


def test_bad_export_format(client):
    r = client.post('/api/export', json={'format': 'exe', 'job': 'x'})
    assert r.status_code == 400
    assert r.json()['error'] == 'bad_format'


def _png(img):
    import cv2
    ok, buf = cv2.imencode('.png', img)
    assert ok
    return buf.tobytes()


def test_black_lineart_keeps_threshold_128():
    import cv2
    import numpy as np
    img = np.full((48, 48), 255, np.uint8)
    cv2.line(img, (6, 6), (40, 40), 0, 3)
    assert core.suggest_bitmap_cut(img) == (128, False)
    polys, kind, th, inv = core.prepare_upload(_png(img), 'line.png')
    assert kind == 'bitmap' and th == 128 and inv is False and polys


def test_light_artwork_opens(client):
    """Gold on white is lighter than 128, so a fixed cut used to reject the file."""
    import numpy as np
    img = np.full((80, 120, 3), 255, np.uint8)
    img[20:60, 30:90] = (109, 166, 194)  # BGR, same ballpark as the ornaments
    raw = _png(img)
    assert core.polygons_from_bitmap(raw, 128, False) == []
    body = _upload(client, 'gold.png', raw, 'image/png')
    assert body['kind'] == 'bitmap'
    assert body['invert'] is False
    assert body['threshold'] >= 168
    assert body['islands'] >= 1
    svg, stats = vectorize.trace(raw, vectorize.params_from_dict({
        'threshold': body['threshold'], 'invert': False,
    }))
    assert '<path' in svg
    assert stats['ink_pct'] > 1
