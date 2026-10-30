import base64
import logging
import os
import tempfile
import uuid

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext

logger = logging.getLogger('judge.problem.pdf')


PDFOID_URL = settings.DMOJ_PDF_PDFOID_URL
PDF_RENDERING_ENABLED = PDFOID_URL is not None

# CPC (problemas-prueba-pdf-1, 2026-10-30): pdfoid prints one PDF at a time, and each request waiting for it holds a
# thread of uWSGI. At most this many renders at once in the whole site, the PDFs of problems and of contests
# together; a request that finds no slot for it does not wait (its page asks again by itself a few seconds later).
PDF_RENDER_SLOTS = 2


def take_pdf_lock(key, seconds):
    """`key` for this request alone, as (key, token) for release_pdf_lock, or None if another request holds it.

    It lives in the shared cache, so every worker sees the same one, and expires after `seconds`, longer than a render
    may take, so that a worker killed while rendering (harakiri, a reload) does not keep it for good.
    """
    token = uuid.uuid4().hex
    return (key, token) if cache.add(key, token, seconds) else None


def release_pdf_lock(held):
    key, token = held
    # Only while it is still this one: once expired, it may already belong to another request.
    if cache.get(key) == token:
        cache.delete(key)


def render_slot_key(number):
    return 'pdf_render_slot:%d' % number


def free_render_slots():
    return [number for number in range(PDF_RENDER_SLOTS) if cache.get(render_slot_key(number)) is None]


def take_render_slot(seconds, last=False):
    """One of the PDF_RENDER_SLOTS of the site, as take_pdf_lock gives it, or None if there is none for this render.

    Only the PDF of a contest may take the last free slot (last=True). The PDF of a problem takes the first one, and
    only while another one stays free: one at a time. Otherwise whoever asked for PDFs of problems without pause
    could keep every slot taken, and the PDF of a contest, asked again every few seconds by each page that waits for
    it, would keep losing them right when the contest starts and everybody downloads it.
    """
    if last:
        # The slots that the PDFs of problems never take go first.
        for number in reversed(range(PDF_RENDER_SLOTS)):
            held = take_pdf_lock(render_slot_key(number), seconds)
            if held is not None:
                return held
        return None
    held = take_pdf_lock(render_slot_key(0), seconds)
    if held is not None and not free_render_slots():
        # It was the last free one.
        release_pdf_lock(held)
        return None
    return held


# A slot is let go of like a lock.
release_render_slot = release_pdf_lock


def keep_pdf(path, pdf):
    """Writes `pdf` to `path`, in the PDF cache: aside first and then moved into place at once, so that nobody reads a
    half-written file, and a write that fails (a full disk) leaves nothing behind."""
    folder, basename = os.path.split(path)
    stream = tempfile.NamedTemporaryFile(dir=folder, prefix=basename, suffix='.part', delete=False)
    try:
        with stream:
            stream.write(pdf)
        os.replace(stream.name, path)
    except BaseException:
        try:
            os.unlink(stream.name)
        except OSError:
            pass
        raise


def render_pdf(*, title: str, html: str, footer: bool = False, timeout=None) -> bytes:
    if not PDF_RENDERING_ENABLED:
        raise RuntimeError("pdfoid is not configured, can't render PDFs")

    if footer:
        footer_template = (
            '<center style="margin: 0 auto; font-family: Segoe UI; font-size: 10px">' +
            gettext('Page {page_number} of {total_pages}') +
            '</center>')
    else:
        footer_template = None

    response = requests.post(
        PDFOID_URL,
        data={
            'html': html,
            'title': title,
            'footer-template': footer_template,
            'wait-for-class': 'math-loaded',
            'wait-for-duration-secs': 15,
        },
        # CPC: the PDF of a problem or of a whole contest gives up before uWSGI kills the worker waiting for it.
        timeout=timeout,
    )

    response.raise_for_status()
    data = response.json()

    if not data['success']:
        raise RuntimeError(data['error'])

    return base64.b64decode(data['pdf'])
