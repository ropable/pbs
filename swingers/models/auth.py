from __future__ import (division, print_function, unicode_literals,
                        absolute_import)
import reversion
import threading
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible

# we can't do `from swingers import models` because that causes circular import
from swingers.models import Model, ForeignKey, DateTimeField


logger = logging.getLogger("log." + __name__)

_locals = threading.local()


def get_locals():
    """
    Setup locals for a request thread so we can attach stuff and make it
    available globally.
    import from request.models::
        from request.models import get_locals
        _locals = get_locals
        _locals.request.user = amazing
    """
    return _locals


@python_2_unicode_compatible
class Audit(Model):
    class Meta:
        abstract = True

    creator = ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='%(app_label)s_%(class)s_created', editable=False)
    modifier = ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='%(app_label)s_%(class)s_modified', editable=False)
    created = DateTimeField(default=timezone.now, editable=False)
    modified = DateTimeField(auto_now=True, editable=False)

    def __init__(self, *args, **kwargs):
        super(Audit, self).__init__(*args, **kwargs)
        self._changed_data = None
        self._initial = {}
        if self.pk:
            for field in self._meta.fields:
                self._initial[field.attname] = getattr(self, field.attname)

    def has_changed(self):
        """
        Returns true if the current data differs from initial.
        """
        return bool(self.changed_data)

    def _get_changed_data(self):
        if self._changed_data is None:
            self._changed_data = []
            for field, value in self._initial.items():
                if field in ["modified", "modifier_id"]:
                    continue
                if getattr(self, field) != value:
                    self._changed_data.append(field)
        return self._changed_data
    changed_data = property(_get_changed_data)

    def save(self, *args, **kwargs):
        """
        This mandates a logged in user for save actions,
        or that creator and modifier are specifed (for shell
        sessions).
        Requires swingers.middleware.auth.AuthenticationMiddleware
        at the end of MIDDLEWARE_CLASSES to attach the request
        object to the local thread.

        Don't write to the thread locals object anywhere outside of
        middleware as its not thread safe!!!
        """
        if hasattr(_locals, "request"):  # Assume not a shell session
            if not self.pk:
                self.creator = _locals.request.user
            self.modifier = _locals.request.user

        if not self.pk:
            created = True
        else:
            created = False

        super(Audit, self).save(*args, **kwargs)

        if created:
            with reversion.create_revision():
                reversion.set_comment('Initial version.')
        else:
            if self.has_changed():
                comment = 'Changed ' + ', '.join(self.changed_data) + '.'
                with reversion.create_revision():
                    reversion.set_comment(comment)
            else:
                with reversion.create_revision():
                    reversion.set_comment('Nothing changed.')

    def __str__(self):
        return str(self.pk)

    def get_absolute_url(self):
        opts = self._meta.app_label, self._meta.module_name
        return reverse("admin:%s_%s_change" % opts, args=(self.pk, ))

    def clean_fields(self, exclude=None):
        """
        Override clean_fields to do what model validation should have done
        in the first place -- call clean_FIELD during model validation.
        """
        errors = {}

        for f in self._meta.fields:
            if f.name in exclude:
                continue
            if hasattr(self, "clean_%s" % f.attname):
                try:
                    getattr(self, "clean_%s" % f.attname)()
                except ValidationError as e:
                    # TODO: Django 1.6 introduces new features to
                    # ValidationError class, update it to use e.error_list
                    errors[f.name] = e.messages

        try:
            super(Audit, self).clean_fields(exclude)
        except ValidationError as e:
            errors = e.update_error_dict(errors)

        if errors:
            raise ValidationError(errors)
