# Compression of CSS and JavaScript

django-compressor joins and minifies the CSS and JavaScript of each
`{% compress %}` block. By default it does so when a page first needs a block,
and writes the result under `STATIC_ROOT/CACHE/`. That is the setup these guides
use: `COMPRESS_OFFLINE` stays off, and the web service may write that one folder
while the rest of the checkout stays read-only
([setup guide](../setup-guide.md#5-styles-and-static-files)).

## Offline compression (advanced)

With `COMPRESS_OFFLINE = True`, every block must be generated in advance into a
manifest, and a page whose block is missing answers 500
(`OfflineGenerationError`). In this tree that is not a single command: the
templates are Jinja2, so `manage.py compress` finds no blocks by default, and a
block depends on the language and on whether the reader is signed in, so the
manifest needs one pass per language and per context. This repository does not
ship a generator for it.

If you build one, regenerate the manifest and restart the web service whenever
you change:

- a template that contains a `{% compress %}` block;
- CSS, SCSS or JavaScript in `resources/`;
- a translation catalog or the `LANGUAGES` setting;
- django-compressor or the minifiers (`rcssmin`, `rjsmin`).

Editing a template **outside** its `{% compress %}` blocks does not invalidate
the manifest. After a rebuild, load the home page, `/problems/`, `/submissions/`
and `/stats/language/` in each language (`Accept-Language`), signed out and
signed in.

On WSL, files written into a Windows drive from Linux may lose their ownership
attributes; check the result after publishing a build.
