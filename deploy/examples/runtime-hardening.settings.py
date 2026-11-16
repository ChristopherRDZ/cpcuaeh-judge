"""Optional fragment to merge into private settings.

This is not a complete settings module or an automatic deployment procedure.
Leave COMPRESS_OFFLINE off: offline compression needs a manifest generator this
repository does not ship (docs/security/offline-compression.md).
"""

CPC_CUSTOM_TEST_MAX_IN_FLIGHT = 2
CPC_CUSTOM_TEST_MAX_PER_MINUTE = 12
CPC_CUSTOM_TEST_MAX_PER_HOUR = 200

# The LOGGING of dmoj/local_settings.example.py sends django.request errors to
# the console, which reaches the service journal. To also email them, define a
# 'mail_admins' handler (dmoj.throttle_mail.ThrottledEmailHandler; see
# docs/security/email.md) and add it to django.request's handlers.
