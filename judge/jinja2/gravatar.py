"""The pictures of the accounts. They still come from Gravatar, but through this site (privacidad-datos-personales-1,
2026-10-28): a Gravatar URL carries the MD5 of the email, and the user search handed those of every account to
anyone, which reverses most of them in seconds (112 have the form aa999999@uaeh.edu.mx). The browser now gets
/avatar/<profile id>/<size>; judge.views.user.user_avatar asks Gravatar for the picture from the server and keeps it
in the cache, so the hash never leaves the server.

When Gravatar cannot be reached (fotos-arreglos, 2026-11-17), the page gets the last good picture of that account
instead of the grey square: an instant without network on the server used to leave the public scoreboard with grey
squares for minutes (the review of 2026-11-16, from the journal).
"""
import hashlib
import logging
import time

import requests
from django.contrib.auth.models import AbstractUser
from django.core.cache import cache
from django.urls import reverse
from django.utils.http import urlencode

from judge.models import Profile
from judge.utils.unicode import utf8bytes
from . import registry

logger = logging.getLogger('judge.gravatar')

# Every size the site asks for (512 is the countdown of the arena). Any other takes the next one up, so that nobody
# fills the cache with sizes the site never shows.
AVATAR_SIZES = (10, 32, 48, 64, 75, 80, 128, 135, 256, 512)
PICTURE_TYPES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
# A 512 px photo from Gravatar weighs tens of KB; anything bigger is not kept (the page shows the grey square).
PICTURE_MAX_BYTES = 256 * 1024
# A day for a picture; five minutes for a definite answer without one (Gravatar answered, but not with a picture this
# site keeps), so that it is not asked again on every page.
PICTURE_TIMEOUT = 86400
FAILURE_TIMEOUT = 300
# The last good picture of each email, size and identicon, kept apart and for longer (fotos-arreglos, 2026-11-17).
# It is only given when Gravatar cannot be reached (an error, a slow answer cut short or the outage below), never
# after a definite answer, and every new good picture replaces it. Seven days: an account seen once a week (a weekly
# training) keeps its picture through an outage, and the bytes of an account that changed its email or was cancelled
# (no email, not active: the view does not even ask for those) are gone within a week on their own.
LAST_GOOD_TIMEOUT = 7 * 86400
# Each picture is asked while a worker waits. If Gravatar does not answer, nobody asks it again for a minute: a page
# with a hundred pictures (the reveal of a contest) would otherwise hold every worker of the site for seconds.
OUTAGE_KEY = 'avatar:gravatar-down'
OUTAGE_TIMEOUT = 60
CONNECT_TIMEOUT, READ_TIMEOUT, TOTAL_TIMEOUT = 2, 4, 6
SLOW_SECONDS = 2


def avatar_size(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return 80
    return next((allowed for allowed in AVATAR_SIZES if allowed >= size), AVATAR_SIZES[-1])


@registry.function
def gravatar(user, size=80, default=None):
    """The address of the picture of a profile, of a user or of a profile id. `default` is kept for the callers
    but no longer used: the view itself gives the identicon to muted accounts, from their profile."""
    if isinstance(user, Profile):
        profile_id = user.id
    elif isinstance(user, AbstractUser):
        profile_id = user.profile.id
    else:
        profile_id = int(user)
    return reverse('user_avatar', args=(profile_id, avatar_size(size)))


def gravatar_picture(email, size, identicon):
    """(content type, bytes) of the picture of this email, or None if there is none to show. Asked from the server
    and kept in the cache; `identicon` asks for the generated picture even if the person has a photo.

    Three outcomes (fotos-arreglos, 2026-11-17): a picture, also one that came slowly but whole (kept for a day, and
    as the last good one for a week); a definite answer without one, as before (another type, empty, too big, or
    any other status, a 5xx included: remembered for five minutes); and Gravatar out of reach (an error, an answer
    cut short by the deadline, or the outage flag), which is not remembered, so that the first page after the
    outage window asks again, and gives the last good picture if there is one."""
    digest = hashlib.md5(utf8bytes(email.strip().lower())).hexdigest()
    key = 'avatar:%s:%d:%d' % (digest, size, bool(identicon))
    last_key = key + ':last'
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    if cache.get(OUTAGE_KEY):
        return cache.get(last_key)
    args = {'d': 'identicon', 's': str(size)}
    if identicon:
        args['f'] = 'y'
    picture = None
    reached = True
    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT
    try:
        with requests.get('https://www.gravatar.com/avatar/%s?%s' % (digest, urlencode(args)), stream=True,
                          timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as response:
            kind = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if response.status_code == 200 and kind in PICTURE_TYPES:
                body = b''
                for chunk in response.iter_content(65536):
                    body += chunk
                    if len(body) > PICTURE_MAX_BYTES:
                        break
                    if time.monotonic() > deadline:
                        reached = False
                        break
                else:
                    picture = (kind, body) if body else None
    except requests.RequestException:
        reached = False
        cache.set(OUTAGE_KEY, True, OUTAGE_TIMEOUT)
        logger.warning('Gravatar did not answer for a picture of size %d', size, exc_info=True)
    else:
        # A slow answer holds a worker as much as none (2026-10-28, adversarial review): nobody waits for it again
        # for a minute either.
        if time.monotonic() - started > SLOW_SECONDS:
            cache.set(OUTAGE_KEY, True, OUTAGE_TIMEOUT)
            logger.warning('Gravatar was slow for a picture of size %d', size)
    if picture:
        cache.set(key, picture, PICTURE_TIMEOUT)
        cache.set(last_key, picture, LAST_GOOD_TIMEOUT)
        return picture
    if not reached:
        return cache.get(last_key)
    cache.set(key, False, FAILURE_TIMEOUT)
    return None
