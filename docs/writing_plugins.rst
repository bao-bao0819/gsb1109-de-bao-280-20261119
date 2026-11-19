Writing Plugins
===============

A plugins is a subclass of `apispec.plugin.BasePlugin`.


Helper Methods
--------------

Plugins provide "helper" methods that augment the behavior of `apispec.APISpec` methods.

There are five types of helper methods:

* Schema helpers
* Parameter helpers
* Response helpers
* Path helpers
* Operation helpers

Helper functions modify `apispec.APISpec` methods. For example, path helpers modify `apispec.APISpec.path`.


A plugin with a path helper function may look something like this:

.. code-block:: python

    from apispec import BasePlugin
    from apispec.yaml_utils import load_operations_from_docstring


    class MyPlugin(BasePlugin):
        def path_helper(self, path, operations, func, **kwargs):
            """Path helper that parses docstrings for operations. Adds a
            ``func`` parameter to `apispec.APISpec.path`.
            """
            operations.update(load_operations_from_docstring(func.__doc__))


All plugin helpers must accept extra `**kwargs`, allowing custom plugins to define new arguments if required.

A plugin with an operation helper that adds `deprecated` flag may look like this

.. code-block:: python

   # deprecated_plugin.py

   from apispec import BasePlugin
   from apispec.yaml_utils import load_operations_from_docstring


   class DeprecatedPlugin(BasePlugin):
       def operation_helper(self, path, operations, **kwargs):
           """Operation helper that add `deprecated` flag if in `kwargs`"""
           if kwargs.pop("deprecated", False) is True:
               for key, value in operations.items():
                   value["deprecated"] = True


Using this plugin

.. code-block:: python

   import json
   from apispec import APISpec
   from deprecated_plugin import DeprecatedPlugin

   spec = APISpec(
       title="Gisty",
       version="1.0.0",
       openapi_version="3.0.2",
       plugins=[DeprecatedPlugin()],
   )

   # path will call operation_helper on operations
   spec.path(
       path="/gists/{gist_id}",
       operations={"get": {"responses": {"200": {"description": "standard response"}}}},
       deprecated=True,
   )
   print(json.dumps(spec.to_dict()["paths"]))
   # {"/gists/{gist_id}": {"get": {"responses": {"200": {"description": "standard response"}}, "deprecated": true}}}



The ``init_spec`` Method
------------------------

`BasePlugin` has an `init_spec` method that `APISpec` calls on each plugin at initialization with the spec object itself as parameter. It is no-op by default, but a plugin may override it to access and store useful information on the spec object.

A typical use case is conditional code depending on the OpenAPI version, which is stored as ``openapi_version`` on the `spec` object. See source code for `apispec.ext.marshmallow.MarshmallowPlugin </_modules/apispec/ext/marshmallow.html>`_ for an example.


Transaction Hooks
-----------------

Every call to :meth:`apispec.APISpec.path` runs inside a transaction. If building the path fails, the spec restores the paths, tags and components it held before the call, so the failed call can be retried on the same `apispec.APISpec` instance with corrected arguments.

A plugin that keeps its own state may need to undo changes made during a failed call. It can do so by overriding the transaction hooks:

* ``begin_transaction`` is called before the path is built. It may return any snapshot object.
* ``rollback_transaction`` is called with that object when the path build fails; the plugin must restore its previous state.
* ``commit_transaction`` is called with that object when the path build succeeds; the snapshot may simply be discarded.

The default implementations are no-ops, so plugins without extra state have nothing to do. The built-in `MarshmallowPlugin` uses these hooks to roll back the schema references it caches while resolving nested schemas.

Example: ::

    class StatefulPlugin(BasePlugin):
        def init_spec(self, spec):
            self.spec = spec
            self.known_schemas = set()

        def begin_transaction(self):
            return set(self.known_schemas)

        def rollback_transaction(self, snapshot):
            self.known_schemas = snapshot

        def operation_helper(self, path=None, operations=None, **kwargs):
            # Registering state here is safe: it is undone on failure.
            ...


Example: Docstring-parsing Plugin
---------------------------------

Here's a plugin example involving conditional processing depending on the OpenAPI version:

.. code-block:: python

    # docplugin.py

    from apispec import BasePlugin
    from apispec.yaml_utils import load_operations_from_docstring


    class DocPlugin(BasePlugin):
        def init_spec(self, spec):
            super(DocPlugin, self).init_spec(spec)
            self.openapi_major_version = spec.openapi_version.major

        def operation_helper(self, operations, func, **kwargs):
            """Operation helper that parses docstrings for operations. Adds a
            ``func`` parameter to `apispec.APISpec.path`.
            """
            doc_operations = load_operations_from_docstring(func.__doc__)
            # Apply conditional processing
            if self.openapi_major_version < 3:
                "...Mutating doc_operations for OpenAPI v2..."
            else:
                "...Mutating doc_operations for OpenAPI v3+..."
            operations.update(doc_operations)


To use the plugin:

.. code-block:: python

    from apispec import APISpec
    from docplugin import DocPlugin

    spec = APISpec(
        title="Gisty", version="1.0.0", openapi_version="3.0.2", plugins=[DocPlugin()]
    )


    def gist_detail(gist_id):
        """Gist detail view.
        ---
        get:
          responses:
            200:
              content:
                application/json:
                  schema: '#/definitions/Gist'
        """
        pass


    spec.path(path="/gists/{gist_id}", func=gist_detail)
    print(dict(spec.to_dict()["paths"]))
    # {'/gists/{gist_id}': {'get': {'responses': {200: {'content': {'application/json': {'schema': '#/definitions/Gist'}}}}}}}


Next Steps
----------

To learn more about how to write plugins:

* Consult the :doc:`Core API docs <api_core>` for `BasePlugin <apispec.BasePlugin>`
* View the source for an existing apispec plugin, e.g. `FlaskPlugin <https://github.com/marshmallow-code/apispec-webframeworks/blob/master/src/apispec_webframeworks/flask.py>`_.
* Check out some projects using apispec: https://github.com/marshmallow-code/apispec/wiki/Ecosystem
