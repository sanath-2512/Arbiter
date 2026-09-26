Rename RemovedInDjango71Warning after the calendar version that removes the features

Per DEP 20, Django's version numbers are calendar-based from 2028, so the release that removes the
features currently marked with `RemovedInDjango71Warning` is Django 2029, not "7.1". Deprecation warning
classes should be named after that calendar version.

Please rename `django.utils.deprecation.RemovedInDjango71Warning` to `RemovedInDjango2029Warning`
(still a `PendingDeprecationWarning`), make `RemovedAfterNextVersionWarning` refer to it, and update every
place that emits or mentions the old class (including the `# RemovedInDjango71Warning` removal markers) to
use the new name. User-facing deprecation messages that say "Django 7.1" should say "Django 2029". Nothing
else about the deprecated behaviours should change: the same code paths warn, with the same messages
apart from the version.
