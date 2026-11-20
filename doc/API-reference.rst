``patsy`` API reference
==========================

This is a complete reference for everything you get when you `import
patsy`.

.. module:: patsy

.. ipython:: python
   :suppress:

   from patsy import *

Basic API
---------

.. autofunction:: dmatrix
.. autofunction:: dmatrices

.. autofunction:: incr_dbuilders
.. autofunction:: incr_dbuilder

.. autoexception:: PatsyError
   :members:

Convenience utilities
---------------------

.. autofunction:: balanced

.. autofunction:: demo_data

Design metadata
---------------

.. autoclass:: DesignInfo

   Here's an example of the most common way to get a :class:`DesignInfo`:

   .. ipython:: python

      mat = dmatrix("a + x", demo_data("a", "x", nlevels=3))
      di = mat.design_info

   .. attribute:: column_names

      The names of each column, represented as a list of strings in
      the proper order. Guaranteed to exist.

      .. ipython:: python

         di.column_names

   .. attribute:: column_name_indexes

      An :class:`~collections.OrderedDict` mapping column names (as
      strings) to column indexes (as integers). Guaranteed to exist
      and to be sorted from low to high.

      .. ipython:: python

         di.column_name_indexes

   .. attribute:: term_names

      The names of each term, represented as a list of strings in
      the proper order. Guaranteed to exist. There is a one-to-many
      relationship between columns and terms -- each term generates
      one or more columns.

      .. ipython:: python

         di.term_names

   .. attribute:: term_name_slices

      An :class:`~collections.OrderedDict` mapping term names (as
      strings) to Python :func:`slice` objects indicating which
      columns correspond to each term. Guaranteed to exist. The slices
      are guaranteed to be sorted from left to right and to cover the
      whole range of columns with no overlaps or gaps.

      .. ipython:: python

         di.term_name_slices

   .. attribute:: terms

      A list of :class:`Term` objects representing each term. May be
      None, for example if a user passed in a plain preassembled
      design matrix rather than using the Patsy machinery.

      .. ipython:: python

         di.terms
         [term.name() for term in di.terms]

   .. attribute:: term_slices

      An :class:`~collections.OrderedDict` mapping :class:`Term`
      objects to Python :func:`slice` objects indicating which columns
      correspond to which terms. Like :attr:`terms`, this may be None.

      .. ipython:: python

         di.term_slices

   .. attribute:: factor_infos

      A dict mapping factor objects to :class:`FactorInfo` objects
      providing information about each factor. Like :attr:`terms`,
      this may be None.

      .. ipython:: python

         di.factor_infos

   .. attribute:: term_codings

      An :class:`~collections.OrderedDict` mapping each :class:`Term`
      object to a list of :class:`SubtermInfo` objects which together
      describe how this term is encoded in the final design
      matrix. Like :attr:`terms`, this may be None.

      .. ipython:: python

         di.term_codings

   .. attribute:: builder

      In versions of patsy before 0.4.0, this returned a
      ``DesignMatrixBuilder`` object which could be passed to
      :func:`build_design_matrices`. Starting in 0.4.0,
      :func:`build_design_matrices` now accepts :class:`DesignInfo`
      objects directly, and writing ``f(design_info.builder)`` is now a
      deprecated alias for simply writing ``f(design_info)``.

   A number of convenience methods are also provided that take
   advantage of the above metadata:

   .. automethod:: describe

   .. automethod:: linear_constraint

   .. automethod:: slice

   .. automethod:: subset

   .. automethod:: from_array

.. autoclass:: FactorInfo

.. autoclass:: SubtermInfo

.. autoclass:: DesignMatrix

   .. automethod:: __new__

Portable design descriptions
----------------------------

These functions convert a :class:`DesignInfo` into a self-contained,
human-readable JSON description, and rebuild an equivalent
:class:`DesignInfo` in another process -- one that has neither the
original training data nor any of the training process's local
variables. The description records column names and ordering, factor
expressions, categorical levels and their ordering, contrast matrices,
interaction structure, and the learned state of every built-in
stateful transform (such as the means used by :func:`center`). Learned
statistics are reused as-is when processing new data; they are never
re-estimated from the new batch.

Only formulas built from column names, :func:`Q`, :func:`C`,
arithmetic, interactions and Patsy's built-in stateful transforms can
be described. Formulas that call functions or stateful transforms
defined in the training process are rejected at export time with a
:class:`PatsyError`, rather than producing a description that would
fail later in the target process.

.. autofunction:: design_info_to_dict

.. autofunction:: design_info_from_dict

.. autofunction:: design_info_to_json

.. autofunction:: design_info_from_json

.. autofunction:: save_design_info

.. autofunction:: load_design_info

.. _stateful-transforms-list:

Stateful transforms
-------------------

Patsy comes with a number of :ref:`stateful transforms
<stateful-transforms>` built in:

.. autofunction:: center

.. autofunction:: standardize

.. function:: scale(x, center=True, rescale=True, ddof=0)

   An alias for :func:`standardize`, for R compatibility.

Finally, this is not itself a stateful transform, but it's useful if
you want to define your own:

.. autofunction:: stateful_transform

.. _categorical-coding-ref:

Handling categorical data
-------------------------

.. autoclass:: Treatment
.. autoclass:: Diff
.. autoclass:: Poly
.. autoclass:: Sum
.. autoclass:: Helmert

.. autoclass:: ContrastMatrix

Spline regression
-----------------

.. autofunction:: bs
.. autofunction:: cr
.. autofunction:: cc
.. autofunction:: te

Working with formulas programmatically
--------------------------------------

.. autoclass:: Term

.. data:: INTERCEPT

   This is a pre-instantiated zero-factors :class:`Term` object
   representing the intercept, useful for making your code clearer. Do
   remember though that this is not a singleton object, i.e., you
   should compare against it using ``==``, not ``is``.

.. autoclass:: LookupFactor

.. autoclass:: EvalFactor

.. autoclass:: ModelDesc

Working with the Python execution environment
---------------------------------------------

.. autoclass:: EvalEnvironment
   :members:

Building design matrices
------------------------

.. autofunction:: design_matrix_builders

.. autofunction:: build_design_matrices

Missing values
--------------

.. autoclass:: NAAction
   :members:

Linear constraints
------------------

.. autoclass:: LinearConstraint

Origin tracking
---------------

.. autoclass:: Origin
   :members:
