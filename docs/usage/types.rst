.. _types:

=====
Types
=====

Types are the smallest definition of structure in Schematics.  They represent
structure by offering functions to inspect or mutate the data in some way.

According to Schematics, a type is an instance of a way to do three things:

1. Coerce the data type into an appropriate representation in Python
2. Convert the Python representation into other formats suitable for
   serialization
3. Offer a precise method of validating data of many forms

These properties are implemented as ``to_native``, ``to_primitive``, and
``validate``. 


Coercion
========

A simple example is the ``DateTimeType``.

::

  >>> from schematics.types import DateTimeType
  >>> dt_t = DateTimeType()

The ``to_native`` function transforms an ISO8601 formatted date string into a 
Python ``datetime.datetime``.

::

  >>> dt = dt_t.to_native('2013-08-31T02:21:21.486072')
  >>> dt
  datetime.datetime(2013, 8, 31, 2, 21, 21, 486072)


Conversion
==========

The ``to_primitive`` function changes it back to a language agnostic form, in
this case an ISO8601 formatted string, just like we used above.

::

  >>> dt_t.to_primitive(dt)
  '2013-08-31T02:21:21.486072'


Validation
==========

Validation can be as simple as successfully calling ``to_native``, but
sometimes more is needed.
data or behavior during a typical use, like serialization.

Let's look at the ``StringType``.  We'll set a ``max_length`` of 10.

::

  >>> st = StringType(max_length=10)
  >>> st.to_native('this is longer than 10')
  u'this is longer than 10'

It converts to a string just fine.  Now, let's attempt to validate it.

::

  >>> st.validate('this is longer than 10')
  Traceback (most recent call last):
    File "<stdin>", line 1, in <module>
    File "schematics/types/base.py", line 164, in validate
      raise ValidationError(errors)
  schematics.exceptions.ValidationError: [u'String value is too long.']


Tracked lists
=============

``ListType`` keeps its ordinary strict behavior by default.  An opt-in ingest
entry point is available when a list arrives as a one-shot iterator and the
caller needs to keep valid records while addressing rejected ones at their
original positions::

    >>> from schematics.models import Model
    >>> from schematics.types import IntType, StringType
    >>> from schematics.types.compound import ListType, ModelType
    >>> class Detail(Model):
    ...     code = StringType(required=True)
    ...     amount = IntType(required=True)
    >>> class Order(Model):
    ...     lines = ListType(ModelType(Detail))

    >>> result = Order.lines.ingest((item for item in [
    ...     None,
    ...     {'code': 'A-1', 'amount': 5},
    ...     {'code': 'A-1', 'amount': 'bad'},
    ...     {'code': 'A-1', 'amount': 0},
    ... ]))
    >>> [detail.amount for detail in result.items]
    [5, 0]
    >>> result.positions
    [1, 3]
    >>> result.skipped
    [0]
    >>> [(rejection.detail, rejection.parent) for rejection in result.errors]
    [(2, None)]

``None`` is treated as a placeholder and skipped; a numeric ``0`` is a valid
amount.  The input iterator is read exactly once.  ``min_size`` and
``max_size`` are checked against the retained items, not the raw positions.
The ordinary ``Order({...})`` and ``validate()`` paths are unchanged.

Setting ``ingest=True`` on the field enables the same reporting behavior on
the ordinary import path: it raises ``IngestError`` carrying the same report,
so a parent is never delivered with only some of its details.  Either way,
:meth:`ListType.ingest` can be called directly; an ordinary import only skips
placeholders when the field opts in with ``ingest=True`` or
``skip_none=True``.

When the parent items themselves contain a tracked list, ``ingest()`` returns
a ``BatchIngestResult`` and keeps parent and detail positions separate::

    >>> class Batch(Model):
    ...     batch_no = StringType(required=True)
    ...     orders = ListType(ModelType(Order))
    >>> batch = Batch.orders.ingest([{'order_no': 'O-1', 'lines': [
    ...     {'code': 'MIXED', 'amount': 100},
    ...     {'code': 'DISC', 'amount': -10}]}])
    >>> detail = batch.result_for(0).tracked[0]
    >>> detail.positions
    (0, 0)

Split, merged and reordered values keep their provenance on the tracked
object rather than on the amount or business number::

    >>> from schematics.provenance import split, merge, describe_positions
    >>> result = Order.lines.ingest([
    ...     {'code': 'MIXED', 'amount': 100},
    ...     {'code': 'DISC', 'amount': -10},
    ... ])
    >>> goods, freight = split(result.tracked[0],
    ...                        [('goods', 100), ('freight', 0)])
    >>> net = merge([goods, result.tracked[1]], 90, kind='net_goods')
    >>> report = describe_positions([freight, net], [0, 1])[1]
    >>> [(source['detail'], source['kind']) for source in report['sources']]
    [(0, 'goods'), (1, 'lines')]

An order-level rule reports positions in the reorganized list.
``describe_positions`` maps each position back to its original detail and to
every participating derived item.  Two values that compare equal still map to
different sources because resolution uses list position and object identity,
never value equality.


Custom types
============

If the types provided by the schematics library don't meet all of your needs,
you can also create new types. Do so by extending
``schematics.types.BaseType``, and decide which based methods you need to
override.

`to_native`
~~~~~~~~~~~

By default, this method on ``schematics.types.BaseType`` just returns the
primitive value it was given. Override this if you want to convert it to a
specific native value. For example, suppose we are implementing a type that
represents the net-location portion of a URL, which consists of a hostname and
optional port number::

    >>> from schematics.types import BaseType
    >>> class NetlocType(BaseType):
    ...     def to_native(self, value):
    ...         if ':' in value:
    ...             return tuple(value.split(':', 1))
    ...         return (value, None)

`to_primitive`
~~~~~~~~~~~~~~

By default, this method on ``schematics.types.BaseType`` just returns the
native value it was given. Override this to convert any non-primitive values to
primitive data values. The following types can pass through safely:

* int
* float
* bool
* basestring
* NoneType
* lists or dicts of any of the above or containing other similarly constrained
  lists or dicts

To cover values that fall outside of these definitions, define a primitive
conversion::

    >>> from schematics.types import BaseType
    >>> class NetlocType(BaseType):
    ...     def to_primitive(self, value):
    ...         host, port = value
    ...         if port:
    ...             return u'{0}:{1}'.format(host, port)
    ...         return host

validation
~~~~~~~~~~

The base implementation of `validate` runs individual validators defined:

* At type class definition time, as methods named in a specific way
* At instantiation time as arguments to the type's init method.

The second type is explained by ``schematics.types.BaseType``, so we'll focus
on the first option.

Declared validation methods take names of the form
`validate_constraint(self, value)`, where `constraint` is an arbitrary name you
give to the check being performed. If the check fails, then the method should
raise ``schematics.exceptions.ValidationError``::

    >>> from schematics.exceptions import ValidationError
    >>> from schematics.types import BaseType
    >>> class NetlocType(BaseType):
    ...     def validate_netloc(self, value):
    ...         if ':' not in value:
    ...             raise ValidationError('Value must be a valid net location of the form host[:port]')

However, schematics types do define an organized way to define and manage coded
error messages. By defining a `MESSAGES` dict, you can assign error messages to
your constraint name. Then the message is available as
`self.message['my_constraint']` in validation methods. Sub-classes can add
messages for new codes or replace messages for existing codes. However, they
will inherit messages for error codes defined by base classes.

So, to enhance the prior example::

    >>> from schematics.exceptions import ValidationError
    >>> from schematics.types import BaseType
    >>> class NetlocType(BaseType):
    ...     MESSAGES = {
    ...         'netloc': 'Value must be a valid net location of the form host[:port]'
    ...     }
    ...     def validate_netloc(self, value):
    ...         if ':' not in value:
    ...             raise ValidationError(self.messages['netloc'])

Parameterizing types
~~~~~~~~~~~~~~~~~~~~

There may be times when you want to override `__init__` and parameterize your
type. When you do so, just ensure two things:

* Don't redefine any of the initialization parameters defined for
  ``schematics.types.BaseType``.
* After defining your specific parameters, ensure that the base parameters are
  given to the base init method. The simplest way to ensure this is to accept
  `*args` and `**kwargs` and pass them through to the super init method, like
  so::

    >>> from schematics.types import BaseType
    >>> class NetlocType(BaseType):
    ...     def __init__(self, verify_location=False, *args, **kwargs):
    ...         super(NetlocType, self).__init__(*args, **kwargs)
    ...         self.verify_location = verify_location


More Information
================

To learn more about **Types**, visit the :ref:`Types API <api_doc_types>`