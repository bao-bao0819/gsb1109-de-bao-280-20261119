.. _models:

======
Models
======

Schematics models are the next form of structure above types. They are a
collection of types in a class. When a `Type` is given a name inside a `Model`,
it is called a `field`.


.. _simple_model:

Simple Model
============

Let's say we want to build a social network for weather. At its core, we'll
need a way to represent some temperature information and where that temperature
was found.

::

  import datetime
  from schematics.models import Model
  from schematics.types import StringType, DecimalType, DateTimeType

  class WeatherReport(Model):
      city = StringType()
      temperature = DecimalType()
      taken_at = DateTimeType(default=datetime.datetime.now)

That'll do.  Let's try using it.

::

  >>> wr = WeatherReport({'city': 'NYC', 'temperature': 80})
  >>> wr.temperature
  Decimal('80.0')

And remember that ``DateTimeType`` we set a default callable for?

::

  >>> wr.taken_at
  datetime.datetime(2013, 8, 21, 13, 6, 38, 11883)


.. _model_configuration:

Model Configuration
===================

Models offer a few configuration options.  Options are attached in the form of a
class.

:: 

  class Whatever(Model):
      ...
      class Options:
          option = value

``namespace`` is a namespace identifier that can be used with persistence
layers.

:: 

  class Whatever(Model):
      ...
      class Options:
          namespace = "whatever_bucket"

``roles`` is a dictionary that stores whitelists and blacklists.

::

  class Whatever(Model):
      ...
      class Options:
          roles = {
              'public': whitelist('some', 'fields'),
              'owner': blacklist('some', 'internal', 'stuff'),
          }

``serialize_when_none`` can be ``True`` or ``False``.  It's behavior is
explained here: :ref:`exporting_serialize_when_none`.

::

  class Whatever(Model):
      ...
      class Options:
          serialize_when_none = False


.. _model_mocking:

Model Mocking
=============

Testing typically involves creating lots of fake (but plausible) objects. Good
tests use random values so that multiple tests can run in parallel without
overwriting each other. Great tests exercise many possible valid input values
to make sure the code being tested can deal with various combinations.

Schematics models can help you write great tests by automatically generating
mock objects. Starting with our ``WeatherReport`` model from earlier:

::

  class WeatherReport(Model):
      city = StringType()
      temperature = DecimalType()
      taken_at = DateTimeType(default=datetime.datetime.now)

we can ask Schematic to generate a mock object with reasonable values:

::

  >>> WeatherReport.get_mock_object().to_primitive()
  {'city': u'zLmeEt7OAGOWI', 'temperature': u'8', 'taken_at': '2014-05-06T17:34:56.396280'}

If you've set a constraint on a field that the mock can't satisfy - such as
putting a ``max_length`` on a URL field so that it's too small to hold a
randomly-generated URL - then ``get_mock_object`` will raise a
``MockCreationError`` exception:

::

  from schematics.types import URLType

  class OverlyStrict(Model):
      url = URLType(max_length=11, required=True)

  >>> OverlyStrict.get_mock_object()
  ...
  schematics.exceptions.MockCreationError: url: This field is too short to hold the mock data


.. _model_migration:

Trial Migration
===============

``Model.migrate`` converts an existing instance into another model class
without importing into or validating the source.  It is useful when an
already-loaded object must be tried against a newer schema: the source keeps
its validated values, its pending edits and its unvalidated data untouched.

::

  class NewAddress(Model):
      street = StringType()
      city = StringType()

  class NewOrder(Model):
      billing = ModelType(NewAddress)
      shipping = ModelType(NewAddress)
      placed_at = DateTimeType()

  >>> converted = order.migrate(NewOrder)
  >>> converted.placed_at
  datetime.datetime(2020, 2, 2, 10, 0)

No result is returned until every conversion step and the new model's normal
validation have passed.  On failure a ``MigrationStageError`` (conversion) or
``MigrationValidationError`` (new-model validation) is raised.  Both expose
``stage`` and ``field_path``::

  >>> try:
  ...     order.migrate(NewOrder)
  ... except MigrationError as error:
  ...     print(error.stage, error.field_path)

Object identity is preserved inside the converted graph.  If two source fields
point at the same nested model, the two corresponding target fields point at
the same new nested model.  Equal-but-distinct source values stay distinct.
The converted graph is independent of the source: editing it never changes the
old instance.

``migrate`` accepts an optional ``normalize`` callable, applied to converted
values before validation, and the usual ``partial`` and ``app_data`` options.
``Model.try_migrate`` returns ``(target, error)`` instead of raising.


More Information
================

To learn more about **Models**, visit the :ref:`Models API <api_doc_models>`
