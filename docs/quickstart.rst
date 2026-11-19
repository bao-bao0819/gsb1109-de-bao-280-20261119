Quickstart
==========

Basic Usage
-----------

First, create an `APISpec <apispec.APISpec>` object, passing basic information about your API.

.. code-block:: python

    from apispec import APISpec

    spec = APISpec(
        title="Gisty",
        version="1.0.0",
        openapi_version="3.0.2",
        info=dict(description="A minimal gist API"),
    )

Add schemas to your spec using `spec.components.schema <apispec.core.Components.schema>`.

.. code-block:: python

    spec.components.schema(
        "Gist",
        {
            "properties": {
                "id": {"type": "integer", "format": "int64"},
                "name": {"type": "string"},
            }
        },
    )


Add paths to your spec using `path <apispec.APISpec.path>`.

.. code-block:: python


    spec.path(
        path="/gist/{gist_id}",
        operations=dict(
            get=dict(
                responses={"200": {"content": {"application/json": {"schema": "Gist"}}}}
            )
        ),
    )


The API is chainable, allowing you to combine multiple method calls in
one statement:

.. code-block:: python

    spec.path(...).path(...).tag(...)

    spec.components.schema(...).parameter(...)

To output your OpenAPI spec, invoke the `to_dict <apispec.APISpec.to_dict>` method.

.. code-block:: python

    from pprint import pprint

    pprint(spec.to_dict())
    # {'components': {'parameters': {},
    #                 'responses': {},
    #                 'schemas': {'Gist': {'properties': {'id': {'format': 'int64',
    #                                                            'type': 'integer'},
    #                                                     'name': {'type': 'string'}}}}},
    #  'info': {'description': 'A minimal gist API',
    #           'title': 'Gisty',
    #           'version': '1.0.0'},
    #  'openapi': '3.0.2',
    #  'paths': {'/gist/{gist_id}':
    #               {'get': {'responses': {'200': {'content': {'application/json': {'schema': {'$ref': '#/definitions/Gist'}}}}}}}},
    #  'tags': []}

Use `to_yaml <apispec.APISpec.to_yaml>` to export your spec to YAML.

.. code-block:: python

    print(spec.to_yaml())
    # components:
    #   parameters: {}
    #   responses: {}
    #   schemas:
    #     Gist:
    #       properties:
    #         id: {format: int64, type: integer}
    #         name: {type: string}
    # info: {description: A minimal gist API, title: Gisty, version: 1.0.0}
    # openapi: 3.0.2
    # paths:
    #   /gist/{gist_id}:
    #     get:
    #       responses:
    #         '200':
    #           content:
    #             application/json:
    #               schema: {$ref: '#/definitions/Gist'}
    # tags: []

.. seealso::
    For a full reference of the `APISpec <apispec.APISpec>` class, see the :doc:`Core API Reference <api_core>`.

Rendering a Different OpenAPI 3 Version
---------------------------------------

An `APISpec <apispec.APISpec>` is created for one OpenAPI version, but the same
components can be rendered for another OpenAPI 3 minor version. Pass the target
version to `to_dict <apispec.APISpec.to_dict>` or
`to_yaml <apispec.APISpec.to_yaml>`:

.. code-block:: python

    spec = APISpec(
        title="Gisty",
        version="1.0.0",
        openapi_version="3.1.0",
        plugins=[MarshmallowPlugin()],
    )
    # ... register components and paths ...

    document_31 = spec.to_dict("3.1.0")
    document_30 = spec.to_dict("3.0.0")
    assert spec.to_dict("3.1.0") == document_31

`Components.to_dict <apispec.core.Components.to_dict>` accepts the same target
version to translate only the registered components.

Rendering does not modify the spec, so components registered once can be
exported repeatedly and in any order. This makes it possible to serve a 3.1
document to one consumer and a 3.0 document to another.

Nullable fields are translated across versions, including nested fields inside
arrays, dictionaries and combinators:

- 3.1 ``{"type": ["string", "null"]}`` becomes
  3.0 ``{"type": "string", "nullable": true}``.
- 3.1 ``{"anyOf": [{"$ref": "..."}, {"type": "null"}]}`` becomes
  3.0 ``{"anyOf": [{"type": "object", "nullable": true}, {"$ref": "..."}]}``.
- 3.0 ``{"$ref": "...", "description": "..."}`` becomes
  3.1 ``{"$ref": "...", "description": "..."}``. Sibling keywords next to a
  ``$ref`` are only allowed in 3.1.

.. note::
    Versions 3.0 and 3.1 describe some values differently, so translating a
    document is lossy in a few edge cases (for example, a nullable
    ``fields.Raw``). Translation between major versions (2 and 3) is not
    supported.


Next Steps
----------

We've learned how to programmatically construct an OpenAPI spec, but defining our entities was verbose.

In the next section, we'll learn how to let plugins do the dirty work: :doc:`Using Plugins <using_plugins>`.
