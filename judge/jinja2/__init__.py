import itertools
import json
from urllib.parse import quote

from jinja2.ext import Extension
from mptt.utils import get_cached_trees
from statici18n.templatetags.statici18n import inlinei18n

from judge.highlight_code import highlight_code
from judge.user_translations import gettext
from . import (camo, datetime, filesize, format, gravatar, language, markdown, rating, reference, render, social,
               spaceless, submission, timedelta)
from . import registry

registry.function('str', str)
registry.filter('str', str)
registry.filter('json', json.dumps)
registry.filter('highlight', highlight_code)
registry.filter('urlquote', quote)
registry.filter('roundfloat', round)
registry.function('inlinei18n', inlinei18n)
registry.function('mptt_tree', get_cached_trees)
registry.function('user_trans', gettext)


@registry.function
def counter(start=1):
    return itertools.count(start).__next__


@registry.function
def mass_email_review_count(user):
    """Emails to users waiting for approval, for the envelope of the bar (base.html); 0 for whoever does not
    approve. The admin needs is_staff, so the rest of the accounts cost no query."""
    if not user.is_authenticated or not user.is_active or not user.is_staff or \
            not user.has_perm('judge.approve_mass_email'):
        return 0
    from judge.mass_email import review_count
    return review_count()


class DMOJExtension(Extension):
    def __init__(self, env):
        super(DMOJExtension, self).__init__(env)
        env.globals.update(registry.globals)
        env.filters.update(registry.filters)
        env.tests.update(registry.tests)
