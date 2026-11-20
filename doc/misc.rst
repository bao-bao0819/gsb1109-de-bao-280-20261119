.. currentmodule:: bitformat
.. _misc:

Miscellaneous
=============

A collection of items that don't fit into any other category.

* :class:`Expression`: Allows values parsed to be used in an f-string like way elsewhere in a :class:`Format`.
* :class:`Options`: Used to set and get global options for the module.
* :class:`Register`: Experimental registry for supported data types. It is public for advanced use, but ordinary code should not depend on it yet.
* :class:`DtypeKind`: The kind of data type.
* :class:`Endianness`: The endianness of the data type.
* ``TibsLike``: A type alias for values that can be promoted to :class:`Tibs`.
* :class:`ChecksumFailure`: Raised when one or more checksum layers do not match; carries each layer's result.
* :class:`ChecksumResult`: The independent result of one checksum declaration.
* :class:`FrameError`: Base class for errors found while parsing a structured frame.
* :class:`TruncationError`: Raised when a frame ends before all of its input has been supplied.


----

.. _expression:

.. autoclass:: bitformat.Expression
   :members:
   :undoc-members:
   :member-order: groupwise

----

.. _options:

.. autoclass:: bitformat.Options
   :members:
   :undoc-members:
   :member-order: groupwise

----

.. autoclass:: bitformat.ChecksumFailure
   :members:
   :undoc-members:

----

.. autoclass:: bitformat.ChecksumResult
   :members:
   :undoc-members:

----

.. autoclass:: bitformat.TruncationError
   :members:
   :undoc-members:

----

.. _register:

.. autoclass:: bitformat.Register
   :members:
   :undoc-members:
   :member-order: groupwise

----

.. autoenum:: bitformat.DtypeKind
   :members:

----

.. autoenum:: bitformat.Endianness
   :members:
