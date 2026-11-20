.. currentmodule:: bitformat
.. _dtypes:

Dtypes
======

Data type classes are used to represents ways to interpret binary data.
The :class:`Dtype` class is an abstract base class, but its constructor can be used to conveniently create the correct sub-class.

The concrete data-type classes are:

* :class:`DtypeSingle`: An interpretation of a single value, such as a 32-bit float, or a 10 bit integer.
* :class:`DtypeArray`: Adds an item count to represent an array of values of the same type, such as 1000 ``u8`` or 5 ``bool`` flags.
* :class:`DtypeTuple`: An arbitrary sequence of other data types.
* :class:`DtypeScaled`: A signed integer interpreted with an exact rational scale and offset, with an optional reserved code for missing values.

These classes all have a ``from_params`` method to create them, but also have a particular formatting that can be used in the base :class:`Dtype` ``from_string`` method which will delegate to the correct sub-class.

It's usually best to create them via a call to ``Dtype('some_formatted_string')``.

In places where a ``Dtype`` is expected as a parameter you can just supply the string format and it will find the correct type automatically.

Some examples of equivalent types, going from most verbose to least::

    DtypeSingle.from_params(DtypeKind.UINT, 8)
    DtypeSingle('u8')
    Dtype('u8')
    'u8'  # When used as a parameter

::

    DtypeArray.from_params(DtypeKind.FLOAT, 16, 20)
    DtypeArray('[f16; 20]')
    Dtype('[f16; 20]')
    '[f16; 20]'  # When used as a parameter

::

    DtypeTuple.from_params([DtypeSingle.from_params(DtypeKind.UINT, 8), DtypeArray.from_params(DtypeKind.FLOAT, 16, 20)])
    DtypeTuple.from_params(['u8', '[f16; 20]'])
    DtypeTuple('(u8, [f16; 20])')
    Dtype('(u8, [f16; 20])')
    '(u8, [f16; 20])'  # When used as a parameter

----

.. _dtype:

.. autoclass:: bitformat.Dtype
   :members:
   :undoc-members:
   :member-order: groupwise
   :show-inheritance:


----

.. _dtypesingle:

.. autoclass:: bitformat.DtypeSingle
   :members:
   :undoc-members:
   :member-order: groupwise
   :show-inheritance:

----

.. _dtypearray:

.. autoclass:: bitformat.DtypeArray
   :members:
   :undoc-members:
   :member-order: groupwise
   :show-inheritance:


----

.. _dtypetuple:

.. autoclass:: bitformat.DtypeTuple
   :members:
   :undoc-members:
   :member-order: groupwise
   :show-inheritance:


----

.. _dtypescaled:

.. autoclass:: bitformat.DtypeScaled
   :members:
   :undoc-members:
   :member-order: groupwise
   :show-inheritance:

The ``si`` dtype kind interprets a two's complement signed integer ``raw`` as the
value ``raw * scale + offset``, where ``scale`` and ``offset`` are exact rational
numbers. An optional third parameter declares a reserved raw code that represents
a missing value. For example::

    Dtype('si8(1/2, 0)')         # 8 bits, units of 1/2, no offset
    Dtype('si8(1/4, 16, -128)')  # units of 1/4, offset 16, raw code -128 means missing
    Dtype('si8({s}, {o})')       # scale and offset given by expressions

Unpacking returns an exact ``fractions.Fraction``, or ``None`` for the reserved
missing code. Packing is strict and never rounds: values that cannot be exactly
represented raise :class:`bitformat.ScaledIntPrecisionError`, values needing a raw
code outside the signed range raise :class:`bitformat.ScaledIntRangeError`, and
ordinary values that would collide with the reserved missing code raise
:class:`bitformat.ScaledIntMissingError`. Only ``None`` may be written as a missing
value, and it is always written as the reserved raw code.
