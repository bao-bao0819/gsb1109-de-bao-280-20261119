.. _categorical-coding:

Coding categorical data
=======================

.. currentmodule:: patsy

Patsy allows great flexibility in how categorical data is coded,
via the function :func:`C`. :func:`C` marks some data as being
categorical (including data which would not automatically be treated
as categorical, such as a column of integers), while also optionally
setting the preferred coding scheme and level ordering.

Let's get some categorical data to work with:

.. ipython:: python

   from patsy import dmatrix, dmatrices, demo_data, ContrastMatrix, Poly
   data = demo_data("a", nlevels=3)
   data

As you know, simply giving Patsy a categorical variable causes it
to be coded using the default :class:`Treatment` coding
scheme. (Strings and booleans are treated as categorical by default.)

.. ipython:: python

   dmatrix("a", data)

We can also alter the level ordering, which is useful for, e.g.,
:class:`Diff` coding:

.. ipython:: python

   l = ["a3", "a2", "a1"]
   dmatrix("C(a, levels=l)", data)

But the default coding is just that -- a default. The easiest
alternative is to use one of the other built-in coding schemes, like
orthogonal polynomial coding:

.. ipython:: python

   dmatrix("C(a, Poly)", data)

There are a number of built-in coding schemes; for details you can
check the :ref:`API reference <categorical-coding-ref>`. But we aren't
restricted to those. We can also provide a custom contrast matrix,
which allows us to produce all kinds of strange designs:

.. ipython:: python

   contrast = [[1, 2], [3, 4], [5, 6]]
   dmatrix("C(a, contrast)", data)
   dmatrix("C(a, [[1], [2], [-4]])", data)

Hmm, those ``[custom0]``, ``[custom1]`` names that Patsy
auto-generated for us are a bit ugly looking. We can attach names to
our contrast matrix by creating a :class:`ContrastMatrix` object, and
make things prettier:

.. ipython:: python

   contrast_mat = ContrastMatrix(contrast, ["[pretty0]", "[pretty1]"])
   dmatrix("C(a, contrast_mat)", data)

And, finally, if we want to get really fancy, we can also define our
own "smart" coding schemes like :class:`Poly`. Just define a class
that has two methods, :meth:`code_with_intercept` and
:meth:`code_without_intercept`. They have identical signatures, taking
a list of levels as their argument and returning a
:class:`ContrastMatrix`. Patsy will automatically choose the
appropriate method to call to produce a full-rank design matrix
without redundancy; see :ref:`redundancy` for the full details on how
Patsy makes this decision.

As an example, here's a simplified version of the built-in
:class:`Treatment` coding object:

.. literalinclude:: _examples/example_treatment.py

.. ipython:: python
   :suppress:

   with open("_examples/example_treatment.py") as f:
       exec(f.read())

And it can now be used just like the built-in methods:

.. ipython:: python

   # Full rank:
   dmatrix("0 + C(a, MyTreat)", data)
   # Reduced rank:
   dmatrix("C(a, MyTreat)", data)
   # With argument:
   dmatrix("C(a, MyTreat(2))", data)

Reserving a level for unseen values
-----------------------------------

By default, once a design has been built, every categorical value it
encounters must match one of the levels that were present during
training. Feeding it an unknown value raises an error. This is
inconvenient when you want to apply a fitted model to new data that
contains a category that did not exist when the model was trained --
for example, a supply region that opened after the training data was
collected.

:func:`C` accepts an ``unknown_value`` argument that reserves an extra
level to catch exactly these values. The reserved level is appended to
the factor's ordinary levels and participates in the design like any
other level, but it is *not* expected to appear in the training data:

.. ipython:: python

   train = {"region": ["north", "south", "north"],
            "y": [1.0, 2.0, 3.0]}
   _, design = dmatrices(
       "y ~ C(region, levels=['north', 'south'], unknown_value='UNK')",
       train)
   design
   design.design_info.term_names

Notice the extra ``[T.UNK]`` column, even though no training row ever
takes that value. When the frozen :class:`DesignInfo` is later used to
score new data, any region that is not one of the known levels is coded
against this reserved level instead of raising an error:

.. ipython:: python

   from patsy import build_design_matrices
   new_data = {"region": ["north", "west", "south"]}
   build_design_matrices([design.design_info], new_data)[0]

Important points:

* The ordinary level ordering and the reference level are frozen at
  training time and never change when scoring new data. No columns are
  added during prediction; the unseen values simply reuse the reserved
  level's columns, including any interaction columns that involve the
  factor.
* Ordinary missing values are unaffected. They are still handled by the
  :class:`~patsy.missing.NAAction` you choose (e.g. dropped) and are
  *not* redirected into the reserved bucket. If an upstream process has
  already converted an unseen value into a missing code that cannot be
  recovered, then that row is simply treated as missing.
* If a value equal to ``unknown_value`` actually occurs in the training
  data, or it collides with one of the ordinary levels, Patsy raises a
  :class:`~patsy.PatsyError` while building the design. The reserved
  bucket is never silently merged with a real level.
* Categorical factors that do not set ``unknown_value`` keep the
  default strict behaviour: unseen values raise
  ``UnknownValueError`` (a :class:`~patsy.PatsyError` subclass
  available as ``patsy.categorical.UnknownValueError``).

The same behaviour works whether the prediction data uses plain strings
or a :class:`pandas.Categorical`. The pandas categorical may even use a
different category ordering or explicitly declare the new values; Patsy
maps each value onto the frozen levels and routes anything unknown into
the reserved bucket.
