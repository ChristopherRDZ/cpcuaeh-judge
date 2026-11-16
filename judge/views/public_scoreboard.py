"""The addresses of the public scoreboard of a contest (2026-11-10): the page and the snapshot it draws, and
(2026-11-15) where a letter of it leads.

The page and the snapshot skip the whole middleware chain (ShortCircuitMiddleware): no session, no account, no language. So neither may
raise — the error pages of the site need what the chain puts on the request — and both answer with plain responses.
What they answer is in judge/public_scoreboard.py.
"""
import json
import logging
import time

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.loader import get_template
from django.urls import reverse

from judge import public_scoreboard
from judge.utils.views import generic_message

logger = logging.getLogger('judge.public_scoreboard')

# The snapshot travels inside the page as JSON in a <script>: nothing in it may close the element or open a comment.
_INLINE = {ord('<'): '\\u003C', ord('>'): '\\u003E', ord('&'): '\\u0026', 0x2028: '\\u2028', 0x2029: '\\u2029'}


def _plain(text, status):
    response = HttpResponse(text, status=status, content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _entry(request, contest):
    """The stored snapshot, or the response that says why there is none."""
    if request.method not in ('GET', 'HEAD'):
        return None, HttpResponseNotAllowed(['GET', 'HEAD'])
    try:
        entry = public_scoreboard.entry_for(contest)
    except Exception:
        logger.exception('public scoreboard: could not build the one of %r', contest)
        return None, _plain('El marcador no está disponible en este momento. Vuelve a intentar en un minuto.', 503)
    if entry is None:
        return None, _plain('Este concurso no tiene marcador público.', 404)
    return entry, None


def _headers(response):
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    # The reader's clock may be off: the countdown and the bar are drawn from this one.
    response['X-Hora'] = str(int(time.time() * 1000))
    return response


def data(request, contest):
    entry, refusal = _entry(request, contest)
    if refusal is not None:
        return refusal
    if request.GET.get('s') == entry['tag']:
        # Nothing new since the one the reader has.
        response = HttpResponse(status=204)
    else:
        response = HttpResponse(entry['body'], content_type='application/json; charset=utf-8')
    response['X-Marca'] = entry['tag']
    return _headers(response)


def page(request, contest):
    entry, refusal = _entry(request, contest)
    if refusal is not None:
        return refusal
    use_events = bool(getattr(settings, 'EVENT_DAEMON_USE', False))
    config = {
        'datos': 'datos',
        'marca': entry['tag'],
        'hora': int(time.time() * 1000),
        'canales': ['contest_%d' % entry['id'], public_scoreboard.CHANNEL % entry['id']],
        'ws': getattr(settings, 'EVENT_DAEMON_GET', None) if use_events else None,
        'wss': getattr(settings, 'EVENT_DAEMON_GET_SSL', None) if use_events else None,
    }
    try:
        html = get_template('contest/public-scoreboard.html').render({
            'title': entry['name'],
            'config': json.dumps(config).translate(_INLINE),
            'snapshot': entry['body'].translate(_INLINE),
        })
    except Exception:
        logger.exception('public scoreboard: could not render the page of %r', contest)
        return _plain('El marcador no está disponible en este momento. Vuelve a intentar en un minuto.', 503)
    return _headers(HttpResponse(html, content_type='text/html; charset=utf-8'))


def problem(request, contest, letter):
    """A letter of the scoreboard (2026-11-15): to its problem on the site, for whoever may open it there. This one
    goes through the whole chain, session included, because who asks decides. Whoever may not open it gets the same
    page whatever the reason (no such letter, not started yet, a problem only for those competing)."""
    try:
        scoreboard, code = public_scoreboard.problem_code(contest, letter, request.user)
    except Exception:
        logger.exception('public scoreboard: could not look up letter %r of %r', letter, contest)
        return _plain('El marcador no está disponible en este momento. Vuelve a intentar en un minuto.', 503)
    if not scoreboard:
        return _plain('Este concurso no tiene marcador público.', 404)
    if code is None:
        response = generic_message(request, 'Problema no disponible',
                                   'Por ahora sólo lo ven quienes participan en el concurso.', status=404)
    else:
        response = HttpResponseRedirect(reverse('problem_detail', args=[code]))
    # Whoever asks may join the contest, or the problem may be made public: no browser keeps the answer.
    response['Cache-Control'] = 'no-store'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


data.short_circuit_middleware = True
page.short_circuit_middleware = True
