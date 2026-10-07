"""lineart2print — web service around core.py.

Heavy endpoints share an in-process queue of two workers. That count is only
true with a single uvicorn process (`--workers 1`). Jobs and ticket bytes
live in this process until the idle TTL, an explicit delete, or a download.
"""
from __future__ import annotations

import asyncio
import os
import time
import traceback
import uuid
import threading
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response, JSONResponse
from fastapi.staticfiles import StaticFiles

import core
import vectorize

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD = 20 * 1024 * 1024
JOB_TTL = 10 * 60
TICKET_TTL = 3 * 60
WORKERS = 2
QUEUE_CAP = 30
JOB_CAP = 40

# ahead = queued tickets strictly in front of you. Running work is not
# counted. position in the 202 body is ahead + 1, so 1 means "you are next".
COALESCE_KINDS = frozenset({'preview', 'vectorize'})

APP_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)
SOURCE_CSP = "default-src 'none'; sandbox"

_KNOWN = frozenset({
    'image_unreadable', 'no_ink', 'all_ink', 'no_color', 'empty_geometry',
    'bad_format',
})


def _fail(status, code, detail=None):
    body = {'error': code}
    if detail:
        body['detail'] = str(detail)
    raise HTTPException(status, body)


def _fail_exc(e, fallback, status=400):
    msg = str(e)
    if msg in _KNOWN:
        _fail(status, msg)
    _fail(status, fallback, msg)


class _File:
    """Binary result kept on a ticket until download or ticket TTL."""

    __slots__ = ('data', 'media', 'disposition')

    def __init__(self, data: bytes, media: str, disposition: str):
        self.data = data
        self.media = media
        self.disposition = disposition


class _Ticket:
    __slots__ = (
        'id', 'kind', 'job_id', 'fn', 'status', 'ahead', 'seq', 'held',
        'result', 'error', 'detail', 'http', 'created', 'finished', 'event',
    )

    def __init__(self, kind: str, job_id: str, fn):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.job_id = job_id
        self.fn = fn
        self.status = 'queued'
        self.ahead = 0
        self.seq = 0
        self.held = False
        self.result = None
        self.error = None
        self.detail = None
        self.http = 400
        self.created = time.time()
        self.finished = 0.0
        self.event = asyncio.Event()


_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_tickets: dict[str, _Ticket] = {}
_dq: deque[_Ticket] = deque()
_cv: asyncio.Condition | None = None
_running = 0
_seq = 0


def _gc():
    now = time.time()
    with _lock:
        for k in [k for k, v in _jobs.items() if now - v['ts'] > JOB_TTL]:
            _jobs.pop(k, None)


def _gc_tickets_locked():
    now = time.time()
    dead = []
    for k, t in _tickets.items():
        if t.status in ('done', 'error') and t.finished and now - t.finished > TICKET_TTL:
            dead.append(k)
        elif t.status == 'queued' and now - t.created > JOB_TTL:
            t.status = 'error'
            t.error = 'ticket_missing'
            t.fn = None
            t.finished = now
            t.event.set()
            dead.append(k)
    for k in dead:
        _tickets.pop(k, None)
    if dead:
        gone = set(dead)
        kept = deque(t for t in _dq if t.id not in gone)
        _dq.clear()
        _dq.extend(kept)


def _job(job_id: str) -> dict:
    _gc()
    with _lock:
        j = _jobs.get(job_id)
        if j:
            j['ts'] = time.time()
    if not j:
        _fail(404, 'job_missing')
    return j


def _polys(job: dict, p: core.Params, threshold: int, invert: bool):
    """Source polygons, re-traced when raster threshold/invert changed."""
    key = (threshold, invert)
    if job['kind'] == 'bitmap' and job.get('key') != key:
        polys = core.polygons_from_bitmap(job['data'], threshold, invert)
        with _lock:
            if job.get('key') != key:
                job['polys'] = polys
                job['key'] = key
    if not job['polys']:
        _fail(400, 'no_fills_threshold')
    return job['polys']


def _as_threshold(value) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError('bad_number')
        th = int(float(value))
    except (TypeError, ValueError):
        raise ValueError('bad_number')
    if th != th:
        raise ValueError('bad_number')
    return max(0, min(255, th))


def _waiting_locked() -> int:
    return sum(1 for t in _tickets.values() if t.status == 'queued')


def _ahead_locked(ticket: _Ticket) -> int:
    return sum(1 for t in _tickets.values()
               if t.status == 'queued' and t.seq < ticket.seq)


def _drop_from_deque_locked(ids: set[str]):
    if not ids:
        return
    kept = deque(t for t in _dq if t.id not in ids)
    _dq.clear()
    _dq.extend(kept)


def _supersede_locked(job_id: str, kind: str):
    """Cancel not-yet-started tickets of this kind for this job."""
    drop = []
    for t in _dq:
        if t.job_id == job_id and t.kind == kind and t.status == 'queued':
            t.status = 'error'
            t.error = 'superseded'
            t.detail = None
            t.fn = None
            t.finished = time.time()
            t.event.set()
            drop.append(t.id)
    _drop_from_deque_locked(set(drop))


def _cancel_job_locked(job_id: str):
    drop = []
    for t in _dq:
        if t.job_id == job_id and t.status == 'queued':
            t.status = 'error'
            t.error = 'job_missing'
            t.fn = None
            t.http = 404
            t.finished = time.time()
            t.event.set()
            drop.append(t.id)
    _drop_from_deque_locked(set(drop))


def _fail_ticket(ticket: _Ticket, exc: HTTPException):
    detail = exc.detail
    ticket.http = exc.status_code
    ticket.finished = time.time()
    ticket.status = 'error'
    ticket.fn = None
    if isinstance(detail, dict) and 'error' in detail:
        ticket.error = str(detail.get('error') or 'server_error')
        extra = detail.get('detail')
        ticket.detail = None if extra is None else str(extra)
    else:
        ticket.error = str(detail)
        ticket.detail = None


def _ticket_view(ticket: _Ticket) -> dict:
    if ticket.status == 'queued':
        return {'status': 'queued', 'ahead': _ahead_locked(ticket)}
    if ticket.status == 'running':
        return {'status': 'running'}
    if ticket.status == 'done':
        if isinstance(ticket.result, _File):
            return {'status': 'done', 'download': True}
        return {'status': 'done', 'result': ticket.result}
    body = {'status': 'error', 'error': ticket.error or 'server_error'}
    if ticket.detail:
        body['detail'] = ticket.detail
    return body


def _materialize(ticket: _Ticket):
    if ticket.status == 'error':
        body = {'error': ticket.error or 'server_error'}
        if ticket.detail:
            body['detail'] = ticket.detail
        raise HTTPException(ticket.http or 400, body)
    if ticket.status != 'done':
        _fail(500, 'server_error', 'incomplete')
    result = ticket.result
    ticket.result = None
    ticket.fn = None
    _tickets.pop(ticket.id, None)
    if isinstance(result, _File):
        return Response(content=result.data, media_type=result.media, headers={
            'Content-Disposition': result.disposition})
    return result


async def _submit(kind: str, job_id: str, fn, *, coalesce: bool = False):
    global _running, _seq
    if _cv is None:
        _fail(500, 'server_error', 'queue not ready')
    ticket = _Ticket(kind, job_id, fn)
    async with _cv:
        _gc_tickets_locked()
        if coalesce and job_id:
            _supersede_locked(job_id, kind)
        waiting = _waiting_locked()
        fast = _running < WORKERS and waiting == 0
        if fast:
            _running += 1
            ticket.held = True
            ticket.status = 'running'
            ticket.ahead = 0
        else:
            if waiting >= QUEUE_CAP:
                _fail(429, 'queue_full')
            ticket.status = 'queued'
            ticket.ahead = waiting
        _seq += 1
        ticket.seq = _seq
        _tickets[ticket.id] = ticket
        _dq.append(ticket)
        _cv.notify()
        queued_now = not fast
    if queued_now:
        return JSONResponse({
            'queued': True,
            'ticket': ticket.id,
            'position': ticket.ahead + 1,
            'ahead': ticket.ahead,
            'kind': kind,
        }, status_code=202)
    await ticket.event.wait()
    return _materialize(ticket)


async def _worker():
    global _running
    while True:
        assert _cv is not None
        async with _cv:
            while True:
                while not _dq:
                    await _cv.wait()
                ticket = _dq.popleft()
                if ticket.status != 'queued' and ticket.status != 'running':
                    ticket.event.set()
                    continue
                if ticket.status == 'queued':
                    ticket.status = 'running'
                    _running += 1
                    ticket.held = True
                break
        try:
            try:
                result = await asyncio.to_thread(ticket.fn)
            except HTTPException as exc:
                _fail_ticket(ticket, exc)
            except Exception as exc:
                traceback.print_exc()
                ticket.status = 'error'
                ticket.error = 'server_error'
                ticket.detail = str(exc)
                ticket.http = 500
                ticket.finished = time.time()
                ticket.fn = None
            else:
                ticket.result = result
                ticket.status = 'done'
                ticket.finished = time.time()
                ticket.fn = None
        finally:
            async with _cv:
                if ticket.held:
                    _running -= 1
                    ticket.held = False
            ticket.event.set()


async def _sweep():
    while True:
        await asyncio.sleep(20)
        _gc()
        if _cv is not None:
            async with _cv:
                _gc_tickets_locked()


@asynccontextmanager
async def _lifespan(app):
    global _cv, _running, _seq
    _dq.clear()
    _tickets.clear()
    _running = 0
    _seq = 0
    _cv = asyncio.Condition()
    workers = [asyncio.create_task(_worker(), name='lineart-worker-%d' % i)
               for i in range(WORKERS)]
    sweep = asyncio.create_task(_sweep(), name='lineart-sweep')
    try:
        yield
    finally:
        for task in workers:
            task.cancel()
        sweep.cancel()
        await asyncio.gather(*workers, sweep, return_exceptions=True)


def reset_for_tests():
    """Drop in-memory jobs and tickets. Call when workers are not running."""
    global _running, _seq
    _jobs.clear()
    _tickets.clear()
    _dq.clear()
    _running = 0
    _seq = 0


app = FastAPI(title='lineart2print', lifespan=_lifespan)


@app.middleware('http')
async def _security_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    if path.startswith('/api/source/'):
        response.headers['Content-Security-Policy'] = SOURCE_CSP
        response.headers['X-Content-Type-Options'] = 'nosniff'
    elif not path.startswith('/api/'):
        response.headers['Content-Security-Policy'] = APP_CSP
        response.headers.setdefault('Referrer-Policy', 'no-referrer')
    return response


def _new_job(data: bytes, name: str) -> dict:
    polys, kind, th, inv = core.prepare_upload(data, name)
    if not polys:
        _fail(400, 'no_fills')
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _lock:
        for k in [k for k, v in _jobs.items() if now - v['ts'] > JOB_TTL]:
            _jobs.pop(k, None)
        if len(_jobs) >= JOB_CAP:
            _fail(429, 'busy')
        _jobs[job_id] = {'polys': polys, 'data': data, 'kind': kind, 'name': name,
                         'ts': now, 'key': (th, inv)}
    from shapely.ops import unary_union
    u = unary_union([g.buffer(0) for g in polys])
    body = {'job': job_id, 'kind': kind, 'name': name,
            'islands': len(core._parts(u))}
    if kind == 'bitmap':
        body['threshold'] = th
        body['invert'] = inv
    return body


def _preview_work(payload: dict) -> dict:
    try:
        job = _job(str(payload.get('job') or ''))
        p = core.params_from_dict(payload.get('params') or {})
        th = _as_threshold(payload.get('threshold', 128))
        inv = bool(payload.get('invert', False))
        r = core.build(_polys(job, p, th, inv), p)
        thin = bool(payload.get('detail_thin', False))
        press = bool(payload.get('detail_press', False))
        try:
            spread = float(payload.get('spread_mm') or 0)
        except (TypeError, ValueError):
            spread = 0.0
        svg = core.preview_svg(r, p, show_bridges=bool(payload.get('show_bridges', True)),
                               detail_thin=thin, detail_press=press, spread_mm=spread)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        _fail_exc(e, 'geometry_failed')
    warnings = list(r.warnings)
    if press and r.stats.get('gaps_closed'):
        warnings.append({'code': 'gaps_closed', 'n': r.stats['gaps_closed'],
                         'mm': r.stats.get('spread_mm', 0)})
    if thin and r.stats.get('gaps_nozzle'):
        warnings.append({'code': 'gaps_nozzle', 'n': r.stats['gaps_nozzle'],
                         'mm': r.stats.get('nozzle_mm', p.nozzle_mm)})
    return {'svg': svg, 'stats': r.stats, 'warnings': warnings}


def _export_work(payload: dict):
    try:
        job = _job(str(payload.get('job') or ''))
        p = core.params_from_dict(payload.get('params') or {})
        fmt = payload.get('format', '3mf')
        part = payload.get('part', 'all')
        if fmt not in ('stl', '3mf', 'obj', 'ply'):
            _fail(400, 'bad_format')
        r = core.build(_polys(job, p, _as_threshold(payload.get('threshold', 128)),
                              bool(payload.get('invert', False))), p)
        if part == 'all':
            data, name = core.export(r, p, fmt, combine=bool(payload.get('combine')))
        else:
            data, name = core.export_part(r, p, part, fmt)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        _fail_exc(e, 'export_failed')
    stem = os.path.splitext(os.path.basename(job['name']))[0][:40] or 'art'
    fname = '%s_%s' % (stem, name)
    mime = {'3mf': 'model/3mf', 'stl': 'model/stl',
            'obj': 'model/obj', 'ply': 'application/ply'}[fmt]
    return _File(data, mime, 'attachment; filename="%s"' % fname)


def _traced(job: dict, params: dict, flat: bool = False):
    if job['kind'] != 'bitmap':
        _fail(400, 'already_vector')
    try:
        p = vectorize.params_from_dict(params)
        return vectorize.trace(job['data'], p, flat=flat)
    except HTTPException:
        raise
    except ValueError as e:
        _fail_exc(e, 'vectorize_failed')
    except Exception as e:
        traceback.print_exc()
        _fail_exc(e, 'vectorize_failed')


def _vectorize_work(payload: dict) -> dict:
    job = _job(str(payload.get('job') or ''))
    svg, stats = _traced(job, payload.get('params') or {})
    return {'svg': svg, 'stats': stats, 'palette': stats.get('palette')}


def _vectorize_download_work(payload: dict):
    job = _job(str(payload.get('job') or ''))
    svg, _ = _traced(job, payload.get('params') or {})
    stem = os.path.splitext(os.path.basename(job['name']))[0][:40] or 'art'
    return _File(svg.encode(), 'image/svg+xml',
                 'attachment; filename="%s_traced.svg"' % stem)


def _vectorize_use_work(payload: dict) -> dict:
    job = _job(str(payload.get('job') or ''))
    svg, _ = _traced(job, payload.get('params') or {}, flat=True)
    stem = os.path.splitext(os.path.basename(job['name']))[0][:40] or 'art'
    return _new_job(svg.encode(), '%s_traced.svg' % stem)


def _sniff(data: bytes) -> str:
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:2] == b'BM':
        return 'image/bmp'
    return 'image/svg+xml' if b'<svg' in data[:400] else 'application/octet-stream'


@app.post('/api/upload')
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        _fail(400, 'empty_file')
    if len(data) > MAX_UPLOAD:
        _fail(413, 'file_too_big')
    try:
        return await asyncio.to_thread(_new_job, data, file.filename or 'art')
    except HTTPException:
        raise
    except Exception as e:
        _fail_exc(e, 'parse_failed')


@app.post('/api/preview')
async def preview(payload: dict):
    return await _submit('preview', str(payload.get('job') or ''),
                         lambda: _preview_work(payload), coalesce=True)


@app.post('/api/export')
async def export(payload: dict):
    fmt = payload.get('format', '3mf')
    if fmt not in ('stl', '3mf', 'obj', 'ply'):
        _fail(400, 'bad_format')
    return await _submit('export', str(payload.get('job') or ''),
                         lambda: _export_work(payload))


@app.get('/api/source/{job_id}')
async def source(job_id: str):
    job = _job(job_id)
    return Response(content=job['data'], media_type=_sniff(job['data']), headers={
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': SOURCE_CSP,
    })


@app.post('/api/vectorize')
async def vectorize_preview(payload: dict):
    return await _submit('vectorize', str(payload.get('job') or ''),
                         lambda: _vectorize_work(payload), coalesce=True)


@app.post('/api/vectorize/download')
async def vectorize_download(payload: dict):
    return await _submit('vectorize_download', str(payload.get('job') or ''),
                         lambda: _vectorize_download_work(payload))


@app.post('/api/vectorize/use')
async def vectorize_use(payload: dict):
    """Trace, then hand the result to the model pipeline as a fresh job."""
    return await _submit('vectorize_use', str(payload.get('job') or ''),
                         lambda: _vectorize_use_work(payload))


@app.get('/api/queue/{ticket_id}')
async def queue_status(ticket_id: str):
    if _cv is None:
        return JSONResponse({'error': 'ticket_missing'}, status_code=404)
    async with _cv:
        _gc_tickets_locked()
        ticket = _tickets.get(ticket_id)
        if not ticket:
            return JSONResponse({'error': 'ticket_missing'}, status_code=404)
        return _ticket_view(ticket)


@app.get('/api/queue/{ticket_id}/file')
async def queue_file(ticket_id: str):
    if _cv is None:
        return JSONResponse({'error': 'ticket_missing'}, status_code=404)
    async with _cv:
        ticket = _tickets.get(ticket_id)
        if not ticket or ticket.status != 'done' or not isinstance(ticket.result, _File):
            return JSONResponse({'error': 'ticket_missing'}, status_code=404)
        f = ticket.result
        _tickets.pop(ticket_id, None)
    return Response(content=f.data, media_type=f.media, headers={
        'Content-Disposition': f.disposition,
        'X-Content-Type-Options': 'nosniff',
    })


@app.delete('/api/job/{job_id}', status_code=204)
async def delete_job(job_id: str):
    with _lock:
        _jobs.pop(job_id, None)
    if _cv is not None:
        async with _cv:
            _cancel_job_locked(job_id)
    return Response(status_code=204)


@app.exception_handler(HTTPException)
async def http_err(request, exc):
    detail = exc.detail
    if isinstance(detail, dict) and 'error' in detail:
        return JSONResponse(detail, status_code=exc.status_code)
    return JSONResponse({'error': str(detail)}, status_code=exc.status_code)


@app.get('/health')
async def health():
    return {'ok': True, 'jobs': len(_jobs)}


app.mount('/', StaticFiles(directory=os.path.join(HERE, 'static'), html=True),
          name='static')
