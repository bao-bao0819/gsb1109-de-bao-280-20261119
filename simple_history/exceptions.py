"""
django-simple-history exceptions and warnings classes.
"""


class MultipleRegistrationsError(Exception):
    """The model has been registered to have history tracking more than once"""

    pass


class NotHistoricalModelError(TypeError):
    """No related history model found."""

    pass


class RelatedNameConflictError(Exception):
    """Related name conflicting with history manager"""

    pass


class AlternativeManagerError(Exception):
    """Manager does not belong to model"""

    pass


class NotEffectiveDatedModelError(Exception):
    """
    The historical model does not carry a business effective date field, so
    effective-dated queries cannot run against it.
    """

    pass


class MissingBatchIdFieldError(Exception):
    """
    The historical model has no import/batch identifier field, so batch
    replay cannot be made idempotent.
    """

    pass
