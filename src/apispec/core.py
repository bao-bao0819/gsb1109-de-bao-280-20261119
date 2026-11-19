"""Core apispec classes and functions."""

from __future__ import annotations

import typing
import warnings
from collections.abc import Sequence
from copy import deepcopy

from packaging.version import Version

from .exceptions import (
    APISpecError,
    DuplicateComponentNameError,
    DuplicateParameterError,
    InvalidParameterError,
    PluginMethodNotImplementedError,
)
from .utils import COMPONENT_SUBSECTIONS, build_reference, deepupdate

if typing.TYPE_CHECKING:
    from .plugin import BasePlugin


VALID_METHODS_OPENAPI_V2 = ["get", "post", "put", "patch", "delete", "head", "options"]

VALID_METHODS_OPENAPI_V3 = VALID_METHODS_OPENAPI_V2 + ["trace"]

VALID_METHODS = {2: VALID_METHODS_OPENAPI_V2, 3: VALID_METHODS_OPENAPI_V3}

MIN_INCLUSIVE_OPENAPI_VERSION = Version("2.0")
MAX_EXCLUSIVE_OPENAPI_VERSION = Version("4.0")


_NON_OPTION_KEYS = {
    "components",
    "definitions",
    "examples",
    "headers",
    "info",
    "links",
    "openapi",
    "parameters",
    "paths",
    "responses",
    "securityDefinitions",
    "securitySchemes",
    "swagger",
    "tags",
}


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _unescape_json_pointer(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _rewrite_ref(
    reference: str, renames: dict[str, dict[str, str]], openapi_major_version: int
) -> str:
    """Rewrite a local component reference using a rename mapping."""
    if not reference.startswith("#/"):
        return reference

    segments = reference[2:].split("/")
    if openapi_major_version >= 3:
        if len(segments) <= 2 or segments[0] != "components":
            return reference
        prefix_length = 2
    else:
        if len(segments) <= 1:
            return reference
        prefix_length = 1

    subsections = COMPONENT_SUBSECTIONS[openapi_major_version]
    obj_type = None
    for component_type, subsection in subsections.items():
        section_index = 1 if openapi_major_version >= 3 else 0
        if segments[section_index] == subsection:
            obj_type = component_type
            break
    if obj_type is None:
        return reference

    name = _unescape_json_pointer(segments[prefix_length])
    new_name = renames.get(obj_type, {}).get(name)
    if new_name is None:
        return reference
    segments[prefix_length] = _escape_json_pointer(new_name)
    return "#/" + "/".join(segments)


def _rewrite_refs(
    obj: typing.Any, renames: dict[str, dict[str, str]], openapi_major_version: int
) -> None:
    """Recursively rewrite all ``$ref`` values in a component or path object."""
    if isinstance(obj, dict):
        reference = obj.get("$ref")
        if isinstance(reference, str):
            obj["$ref"] = _rewrite_ref(reference, renames, openapi_major_version)
        # Security requirements refer to security schemes by name.
        security = obj.get("security")
        security_renames = renames.get("security_scheme", {})
        if isinstance(security, list) and security_renames:
            for requirement in security:
                if isinstance(requirement, dict):
                    renamed = {
                        security_renames.get(name, name): scopes
                        for name, scopes in requirement.items()
                    }
                    requirement.clear()
                    requirement.update(renamed)
        for value in obj.values():
            _rewrite_refs(value, renames, openapi_major_version)
    elif isinstance(obj, list):
        for value in obj:
            _rewrite_refs(value, renames, openapi_major_version)


def _namespaced_name(name: str, namespace: typing.Any, separator: str) -> str:
    """Build the namespaced name of a component."""
    if namespace in (None, ""):
        return name
    if callable(namespace):
        return namespace(name)
    if isinstance(namespace, dict):
        return namespace.get(name, name)
    if not isinstance(namespace, str):
        raise APISpecError("A namespace must be a string, a mapping or a callable.")
    if "{name}" in namespace:
        return namespace.replace("{name}", name)
    if "{}" in namespace:
        return namespace.replace("{}", name)
    if namespace.endswith(separator):
        return f"{namespace}{name}"
    return f"{namespace}{separator}{name}"


def _component_container(
    data: dict, obj_type: str, openapi_major_version: int
) -> dict | None:
    """Return the component mapping for a component type in a spec dict."""
    section = COMPONENT_SUBSECTIONS[openapi_major_version].get(obj_type)
    if section is None:
        return None
    if openapi_major_version >= 3:
        return (data.get("components") or {}).get(section)
    return data.get(section)


def _component_renames(
    spec: APISpec,
    namespace: typing.Any,
    separator: str,
    openapi_major_version: int,
    data: dict | None = None,
) -> dict[str, dict[str, str]]:
    """Map each component type's names to their namespaced names."""
    renames: dict[str, dict[str, str]] = {}
    for obj_type in COMPONENT_SUBSECTIONS[openapi_major_version]:
        names = set(spec.components._subsections.get(obj_type, {}))
        names.update(spec.components._subsections_lazy.get(obj_type, {}))
        # Components passed through options are rendered in ``data`` but are
        # not stored in ``spec.components``.
        if data is not None:
            container = _component_container(data, obj_type, openapi_major_version)
            if container:
                names.update(container)
        renames[obj_type] = {
            name: _namespaced_name(name, namespace, separator) for name in names
        }
    return renames


def _rename_component_keys(
    data: dict,
    renames: dict[str, dict[str, str]],
    openapi_major_version: int,
) -> None:
    """Rename component keys in a spec dict, using their namespaced names."""
    for obj_type, mapping in renames.items():
        container = _component_container(data, obj_type, openapi_major_version)
        if not container:
            continue
        renamed = {}
        for name, component in container.items():
            new_name = mapping.get(name, name)
            if new_name in renamed:
                raise DuplicateComponentNameError(
                    f'Another {obj_type.replace("_", " ")} with name "{new_name}" '
                    "is already registered."
                )
            renamed[new_name] = component
        container.clear()
        container.update(renamed)


def _merge_dicts(target: dict, source: dict) -> None:
    """Recursively merge ``source`` into ``target``.

    Dictionaries are merged and parameters lists are concatenated. Other
    conflicting values from ``source`` take precedence.
    """
    for key, value in source.items():
        if key not in target:
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_dicts(target[key], value)
        elif (
            key == "parameters"
            and isinstance(target[key], list)
            and isinstance(value, list)
        ):
            for parameter in value:
                if parameter not in target[key]:
                    target[key].append(parameter)
        elif target[key] != value:
            target[key] = value


def _merge_missing(target: dict, source: dict) -> None:
    """Fill missing values in ``target`` from ``source``, keeping existing ones."""
    for key, value in source.items():
        if key not in target:
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_missing(target[key], value)


def _merge_tags(target: list[dict], tags: list[dict]) -> None:
    known_names = {
        tag.get("name") for tag in target if isinstance(tag, dict) and "name" in tag
    }
    for tag in tags:
        name = tag.get("name") if isinstance(tag, dict) else None
        if name is not None and name in known_names:
            continue
        target.append(tag)
        if name is not None:
            known_names.add(name)


def _merge_component_data(
    components: Components, data: dict, openapi_major_version: int
) -> None:
    for obj_type in COMPONENT_SUBSECTIONS[openapi_major_version]:
        container = _component_container(data, obj_type, openapi_major_version)
        if not container:
            continue
        target = components._subsections[obj_type]
        for name, component in container.items():
            if name in target and target[name] != component:
                raise DuplicateComponentNameError(
                    f'Another {obj_type.replace("_", " ")} with name "{name}" '
                    "is already registered."
                )
            target[name] = component


def _flatten_merge_specs(
    base: typing.Any, specs: Sequence[typing.Any]
) -> list[APISpec]:
    """Flatten merge arguments, accepting a base spec or a sequence of specs."""
    values = list(specs)
    if isinstance(base, APISpec):
        values.insert(0, base)
    elif isinstance(base, (list, tuple)):
        values = [*base, *values]
    else:
        raise APISpecError("Can only merge APISpec instances.")

    flattened: list[APISpec] = []
    for value in values:
        if isinstance(value, APISpec):
            flattened.append(value)
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, APISpec) for item in value
        ):
            flattened.extend(value)
        else:
            raise APISpecError("Can only merge APISpec instances.")
    return flattened


def _merge_namespace_index(key: typing.Any, specs: Sequence[APISpec]) -> int:
    """Resolve a namespace mapping key to a spec index."""
    if isinstance(key, int):
        if not -len(specs) <= key < len(specs):
            raise APISpecError(f"namespaces index {key} is out of range.")
        return key % len(specs)
    if isinstance(key, str):
        matches = [index for index, spec in enumerate(specs) if spec.title == key]
        if len(matches) == 1:
            return matches[0]
    elif key in specs:
        return specs.index(key)
    raise APISpecError("namespaces keys must be specs, spec indices or titles.")


class Components:
    """Stores OpenAPI components

    Components are top-level fields in OAS v2.
    They became sub-fields of "components" top-level field in OAS v3.
    """

    def __init__(
        self,
        plugins: Sequence[BasePlugin],
        openapi_version: Version,
    ) -> None:
        self._plugins = plugins
        self.openapi_version = openapi_version
        self.schemas: dict[str, dict] = {}
        self.responses: dict[str, dict] = {}
        self.parameters: dict[str, dict] = {}
        self.headers: dict[str, dict] = {}
        self.examples: dict[str, dict] = {}
        self.links: dict[str, dict] = {}
        self.security_schemes: dict[str, dict] = {}
        self.schemas_lazy: dict[str, dict] = {}
        self.responses_lazy: dict[str, dict] = {}
        self.parameters_lazy: dict[str, dict] = {}
        self.headers_lazy: dict[str, dict] = {}
        self.examples_lazy: dict[str, dict] = {}
        self.links_lazy: dict[str, dict] = {}

        self._subsections = {
            "schema": self.schemas,
            "response": self.responses,
            "parameter": self.parameters,
            "header": self.headers,
            "example": self.examples,
            "link": self.links,
            "security_scheme": self.security_schemes,
        }
        self._subsections_lazy = {
            "schema": self.schemas_lazy,
            "response": self.responses_lazy,
            "parameter": self.parameters_lazy,
            "header": self.headers_lazy,
            "example": self.examples_lazy,
            "link": self.links_lazy,
        }

    def to_dict(self) -> dict[str, dict]:
        return {
            COMPONENT_SUBSECTIONS[self.openapi_version.major][k]: v
            for k, v in self._subsections.items()
            if v != {}
        }

    def _register_component(
        self,
        obj_type: str,
        component_id: str,
        component: dict,
        *,
        lazy: bool = False,
    ) -> None:
        subsection = (self._subsections if lazy is False else self._subsections_lazy)[
            obj_type
        ]
        subsection[component_id] = component

    def _do_register_lazy_component(
        self,
        obj_type: str,
        component_id: str,
    ) -> None:
        component_buffer = self._subsections_lazy[obj_type]
        # If component was lazy registered, register it for real
        if component_id in component_buffer:
            self._subsections[obj_type][component_id] = component_buffer.pop(
                component_id
            )

    def get_ref(
        self,
        obj_type: str,
        obj_or_component_id: dict | str,
    ) -> dict:
        """Return object or reference

        If obj is a dict, it is assumed to be a complete description and it is returned as is.
        Otherwise, it is assumed to be a reference name as string and the corresponding $ref
        string is returned.

        :param str subsection: "schema", "parameter", "response" or "security_scheme"
        :param dict|str obj: object in dict form or as ref_id string
        """
        if isinstance(obj_or_component_id, dict):
            return obj_or_component_id
        # Register the component if it was lazy registered
        self._do_register_lazy_component(obj_type, obj_or_component_id)
        return build_reference(
            obj_type, self.openapi_version.major, obj_or_component_id
        )

    def schema(
        self,
        component_id: str,
        component: dict | None = None,
        *,
        lazy: bool = False,
        **kwargs: typing.Any,
    ) -> Components:
        """Add a new schema to the spec.

        :param str component_id: identifier by which schema may be referenced
        :param dict component: schema definition
        :param bool lazy: register component only when referenced in the spec
        :param kwargs: plugin-specific arguments

        .. note::

            If you are using `apispec.ext.marshmallow`, you can pass fields' metadata as
            additional keyword arguments.

            For example, to add ``enum`` and ``description`` to your field: ::

                status = fields.String(
                    required=True,
                    metadata={
                        "description": "Status (open or closed)",
                        "enum": ["open", "closed"],
                    },
                )

        https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.2.md#schemaObject
        """
        if component_id in self.schemas:
            raise DuplicateComponentNameError(
                f'Another schema with name "{component_id}" is already registered.'
            )
        ret = deepcopy(component) or {}
        # Execute all helpers from plugins
        for plugin in self._plugins:
            try:
                ret.update(plugin.schema_helper(component_id, ret, **kwargs) or {})
            except PluginMethodNotImplementedError:
                continue
        self._resolve_refs_in_schema(ret)
        self._register_component("schema", component_id, ret, lazy=lazy)
        return self

    def response(
        self,
        component_id: str,
        component: dict | None = None,
        *,
        lazy: bool = False,
        **kwargs: typing.Any,
    ) -> Components:
        """Add a response which can be referenced.

        :param str component_id: ref_id to use as reference
        :param dict component: response fields
        :param bool lazy: register component only when referenced in the spec
        :param kwargs: plugin-specific arguments
        """
        if component_id in self.responses:
            raise DuplicateComponentNameError(
                f'Another response with name "{component_id}" is already registered.'
            )
        ret = deepcopy(component) or {}
        # Execute all helpers from plugins
        for plugin in self._plugins:
            try:
                ret.update(plugin.response_helper(ret, **kwargs) or {})
            except PluginMethodNotImplementedError:
                continue
        self._resolve_refs_in_response(ret)
        self._register_component("response", component_id, ret, lazy=lazy)
        return self

    def parameter(
        self,
        component_id: str,
        location: str,
        component: dict | None = None,
        *,
        lazy: bool = False,
        **kwargs: typing.Any,
    ) -> Components:
        """Add a parameter which can be referenced.

        :param str component_id: identifier by which parameter may be referenced
        :param str location: location of the parameter
        :param dict component: parameter fields
        :param bool lazy: register component only when referenced in the spec
        :param kwargs: plugin-specific arguments
        """
        if component_id in self.parameters:
            raise DuplicateComponentNameError(
                f'Another parameter with name "{component_id}" is already registered.'
            )
        ret = deepcopy(component) or {}
        ret.setdefault("name", component_id)
        ret["in"] = location

        # if "in" is set to "path", enforce required flag to True
        if location == "path":
            ret["required"] = True

        # Execute all helpers from plugins
        for plugin in self._plugins:
            try:
                ret.update(plugin.parameter_helper(ret, **kwargs) or {})
            except PluginMethodNotImplementedError:
                continue
        self._resolve_refs_in_parameter_or_header(ret)
        self._register_component("parameter", component_id, ret, lazy=lazy)
        return self

    def header(
        self,
        component_id: str,
        component: dict,
        *,
        lazy: bool = False,
        **kwargs: typing.Any,
    ) -> Components:
        """Add a header which can be referenced.

        :param str component_id: identifier by which header may be referenced
        :param dict component: header fields
        :param bool lazy: register component only when referenced in the spec
        :param kwargs: plugin-specific arguments

        https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.1.md#headerObject
        """
        ret = deepcopy(component) or {}
        if component_id in self.headers:
            raise DuplicateComponentNameError(
                f'Another header with name "{component_id}" is already registered.'
            )
        # Execute all helpers from plugins
        for plugin in self._plugins:
            try:
                ret.update(plugin.header_helper(ret, **kwargs) or {})
            except PluginMethodNotImplementedError:
                continue
        self._resolve_refs_in_parameter_or_header(ret)
        self._register_component("header", component_id, ret, lazy=lazy)
        return self

    def example(
        self, component_id: str, component: dict, *, lazy: bool = False
    ) -> Components:
        """Add an example which can be referenced

        :param str component_id: identifier by which example may be referenced
        :param dict component: example fields
        :param bool lazy: register component only when referenced in the spec

        https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.1.md#exampleObject
        """
        if component_id in self.examples:
            raise DuplicateComponentNameError(
                f'Another example with name "{component_id}" is already registered.'
            )
        self._register_component("example", component_id, component, lazy=lazy)
        return self

    def link(
        self, component_id: str, component: dict, *, lazy: bool = False
    ) -> Components:
        """Add a link which can be referenced

        :param str component_id: identifier by which link may be referenced
        :param dict component: link fields
        :param bool lazy: register component only when referenced in the spec

        https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.1.md#linkObject
        """
        if component_id in self.links:
            raise DuplicateComponentNameError(
                f'Another link with name "{component_id}" is already registered.'
            )
        self._register_component("link", component_id, component, lazy=lazy)
        return self

    def security_scheme(self, component_id: str, component: dict) -> Components:
        """Add a security scheme which can be referenced.

        :param str component_id: component_id to use as reference
        :param dict component: security scheme fields
        """
        if component_id in self.security_schemes:
            raise DuplicateComponentNameError(
                f'Another security scheme with name "{component_id}" is already registered.'
            )
        self._register_component("security_scheme", component_id, component)
        return self

    def _resolve_schema(self, obj) -> None:
        """Replace schema reference as string with a $ref if needed

        Also resolve references in the schema
        """
        if "schema" in obj:
            obj["schema"] = self.get_ref("schema", obj["schema"])
            self._resolve_refs_in_schema(obj["schema"])

    def _resolve_examples(self, obj) -> None:
        """Replace example reference as string with a $ref"""
        for name, example in obj.get("examples", {}).items():
            obj["examples"][name] = self.get_ref("example", example)

    def _resolve_refs_in_schema(self, schema: dict) -> None:
        if "properties" in schema:
            for key in schema["properties"]:
                schema["properties"][key] = self.get_ref(
                    "schema", schema["properties"][key]
                )
                self._resolve_refs_in_schema(schema["properties"][key])
        if "items" in schema:
            schema["items"] = self.get_ref("schema", schema["items"])
            self._resolve_refs_in_schema(schema["items"])
        for key in ("allOf", "oneOf", "anyOf"):
            if key in schema:
                schema[key] = [self.get_ref("schema", s) for s in schema[key]]
                for sch in schema[key]:
                    self._resolve_refs_in_schema(sch)
        if "not" in schema:
            schema["not"] = self.get_ref("schema", schema["not"])
            self._resolve_refs_in_schema(schema["not"])

    def _resolve_refs_in_parameter_or_header(self, parameter_or_header) -> None:
        self._resolve_schema(parameter_or_header)
        self._resolve_examples(parameter_or_header)
        # parameter content is OpenAPI v3+
        for media_type in parameter_or_header.get("content", {}).values():
            self._resolve_schema(media_type)

    def _resolve_refs_in_request_body(self, request_body) -> None:
        # requestBody is OpenAPI v3+
        for media_type in request_body["content"].values():
            self._resolve_schema(media_type)
            self._resolve_examples(media_type)

    def _resolve_refs_in_response(self, response) -> None:
        if self.openapi_version.major < 3:
            self._resolve_schema(response)
        else:
            for media_type in response.get("content", {}).values():
                self._resolve_schema(media_type)
                self._resolve_examples(media_type)
            for name, header in response.get("headers", {}).items():
                response["headers"][name] = self.get_ref("header", header)
                self._resolve_refs_in_parameter_or_header(response["headers"][name])
            for name, link in response.get("links", {}).items():
                response["links"][name] = self.get_ref("link", link)

    def _resolve_refs_in_operation(self, operation) -> None:
        if "parameters" in operation:
            parameters = []
            for parameter in operation["parameters"]:
                parameter = self.get_ref("parameter", parameter)
                self._resolve_refs_in_parameter_or_header(parameter)
                parameters.append(parameter)
            operation["parameters"] = parameters
        if "callbacks" in operation:
            for callback in operation["callbacks"].values():
                if isinstance(callback, dict):
                    for path in callback.values():
                        self.resolve_refs_in_path(path)
        if "requestBody" in operation:
            self._resolve_refs_in_request_body(operation["requestBody"])
        if "responses" in operation:
            responses = {}
            for code, response in operation["responses"].items():
                response = self.get_ref("response", response)
                self._resolve_refs_in_response(response)
                responses[code] = response
            operation["responses"] = responses

    def resolve_refs_in_path(self, path) -> None:
        if "parameters" in path:
            parameters = []
            for parameter in path["parameters"]:
                parameter = self.get_ref("parameter", parameter)
                self._resolve_refs_in_parameter_or_header(parameter)
                parameters.append(parameter)
            path["parameters"] = parameters
        for method in (
            "get",
            "put",
            "post",
            "delete",
            "options",
            "head",
            "patch",
            "trace",
        ):
            if method in path:
                self._resolve_refs_in_operation(path[method])


class APISpec:
    """Stores metadata that describes a RESTful API using the OpenAPI specification.

    :param str title: API title
    :param str version: API version
    :param list|tuple plugins: Plugin instances.
        See https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.2.md#infoObject
    :param str openapi_version: OpenAPI Specification version.
        Should be in the form '2.x' or '3.x.x' to comply with the OpenAPI standard.
    :param options: Optional top-level keys
        See https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.2.md#openapi-object
    """

    def __init__(
        self,
        title: str,
        version: str,
        openapi_version: str,
        plugins: Sequence[BasePlugin] = (),
        **options: typing.Any,
    ) -> None:
        self.title = title
        self.version = version
        self.options = options
        self.plugins = plugins
        self.openapi_version = Version(openapi_version)
        if not (
            MIN_INCLUSIVE_OPENAPI_VERSION
            <= self.openapi_version
            < MAX_EXCLUSIVE_OPENAPI_VERSION
        ):
            raise APISpecError(f"Not a valid OpenAPI version number: {openapi_version}")

        # Metadata
        self._tags: list[dict] = []
        self._paths: dict = {}

        # Components
        self.components = Components(self.plugins, self.openapi_version)

        # Plugins
        for plugin in self.plugins:
            plugin.init_spec(self)

    def to_dict(self) -> dict[str, typing.Any]:
        ret: dict[str, typing.Any] = {
            "paths": self._paths,
            "info": {"title": self.title, "version": self.version},
        }
        if self._tags:
            ret["tags"] = self._tags
        if self.openapi_version.major < 3:
            ret["swagger"] = str(self.openapi_version)
            ret.update(self.components.to_dict())
        else:
            ret["openapi"] = str(self.openapi_version)
            components_dict = self.components.to_dict()
            if components_dict:
                ret["components"] = components_dict
        ret = deepupdate(ret, self.options)
        return ret

    def to_yaml(self, yaml_dump_kwargs: typing.Any | None = None) -> str:
        """Render the spec to YAML. Requires PyYAML to be installed.

        :param dict yaml_dump_kwargs: Additional keyword arguments to pass to `yaml.dump`
        """
        from .yaml_utils import dict_to_yaml

        return dict_to_yaml(self.to_dict(), yaml_dump_kwargs)

    def merge(
        self,
        *specs: APISpec | Sequence[APISpec],
        namespace: str | dict | typing.Callable[[str], str] | None = None,
        namespaces: Sequence[str | dict | typing.Callable[[str], str] | None]
        | dict = (),
        prefix: str | dict | typing.Callable[[str], str] | None = None,
        title: str | None = None,
        version: str | None = None,
        openapi_version: str | None = None,
        separator: str = ".",
    ) -> APISpec:
        """Merge specs into a new spec, optionally namespacing components.

        The first spec is the base spec::

            APISpec.merge(orders, inventory, namespace="inventory")
            orders.merge(inventory, namespace="inventory")

        ``namespace`` applies to the specs following the first one. A namespace
        is a prefix (``"inventory"`` renames ``"Item"`` to ``"inventory.Item"``),
        a template containing ``{name}`` or ``{}``, or a callable/mapping used
        to compute the new names. ``namespaces`` can be used to pass one
        namespace per spec instead.

        Component names and all local component references are rewritten
        together, including references nested in schemas and operations, so
        each merged entry keeps pointing to its own components. ``$ref``
        strings are updated without being resolved.

        The merged spec is new and the input specs are left untouched: they
        still render exactly as before the merge.

        Components with the same final name must have equal definitions. Paths
        and options are merged recursively, and list-valued ``parameters`` are
        concatenated.

        :param specs: specs to merge with this one
        :param namespace: namespace for the specs following this one
        :param namespaces: optional namespace per spec, in order. A mapping may
            be keyed by spec, spec index or spec title.
        :param prefix: alias for ``namespace``
        :param title: title of the merged spec (defaults to this spec's)
        :param version: version of the merged spec (defaults to this spec's)
        :param openapi_version: OpenAPI version of the merged spec (defaults to
            this spec's)
        :param separator: separator between a prefix namespace and a name
        """
        all_specs = _flatten_merge_specs(self, specs)
        if not all_specs:
            raise APISpecError("At least one spec is required to merge.")
        openapi_major_version = all_specs[0].openapi_version.major
        for spec in all_specs:
            if not isinstance(spec, APISpec):
                raise APISpecError("Can only merge APISpec instances.")
            if spec.openapi_version.major != openapi_major_version:
                raise APISpecError(
                    "Cannot merge specs with different OpenAPI versions."
                )

        if namespace is None:
            namespace = prefix
        if (
            isinstance(namespace, dict)
            and namespace
            and any(isinstance(key, APISpec) for key in namespace)
        ):
            namespaces = namespace
            namespace = None

        if isinstance(namespace, (list, tuple)):
            namespaces = namespace
            namespace = None

        if isinstance(namespaces, dict):
            spec_namespaces = [None] * len(all_specs)
            for key, value in namespaces.items():
                index = _merge_namespace_index(key, all_specs)
                spec_namespaces[index] = value
        elif namespaces:
            if len(namespaces) == len(all_specs):
                spec_namespaces = list(namespaces)
            elif len(namespaces) == len(all_specs) - 1:
                spec_namespaces = [None, *namespaces]
            else:
                raise APISpecError(
                    "namespaces must contain one entry per spec, or one entry per "
                    "spec after the first one."
                )
        else:
            spec_namespaces = [None, *([namespace] * (len(all_specs) - 1))]

        first = all_specs[0]
        merged_version = (
            Version(openapi_version) if openapi_version else first.openapi_version
        )
        if merged_version.major != openapi_major_version:
            raise APISpecError("Cannot change the OpenAPI major version when merging.")
        # The merged spec is built from rendered data, so it does not need the
        # plugins of the input specs. Not forwarding them also prevents the
        # merged spec from rebinding plugin instances owned by the inputs.
        merged = (self.__class__ if isinstance(self, APISpec) else APISpec)(
            title=title if title is not None else first.title,
            version=version if version is not None else first.version,
            openapi_version=str(merged_version),
        )

        merged_options: dict[str, typing.Any] = {}
        for spec, spec_namespace in zip(all_specs, spec_namespaces, strict=True):
            spec_data = deepcopy(spec.to_dict())
            renames = _component_renames(
                spec, spec_namespace, separator, openapi_major_version, spec_data
            )
            _rewrite_refs(spec_data, renames, openapi_major_version)
            _rename_component_keys(spec_data, renames, openapi_major_version)
            _merge_component_data(merged.components, spec_data, openapi_major_version)
            _merge_dicts(merged._paths, spec_data.get("paths", {}))
            _merge_tags(merged._tags, spec_data.get("tags", []))

            if openapi_major_version >= 3:
                # Keep component sections apispec does not manage (e.g.
                # "requestBodies") and namespaced sections already renamed.
                managed_sections = set(
                    COMPONENT_SUBSECTIONS[openapi_major_version].values()
                )
                extra_sections = {
                    key: value
                    for key, value in (spec_data.get("components") or {}).items()
                    if key not in managed_sections
                }
                if extra_sections:
                    merged_options.setdefault("components", {})
                    _merge_missing(merged_options["components"], extra_sections)

            spec_options = {
                key: value
                for key, value in spec_data.items()
                if key not in _NON_OPTION_KEYS
            }
            info = {
                key: value
                for key, value in (spec_data.get("info") or {}).items()
                if key not in ("title", "version")
            }
            if info:
                spec_options["info"] = info
            _merge_missing(merged_options, spec_options)

        merged.options = merged_options
        return merged

    def tag(self, tag: dict) -> APISpec:
        """Store information about a tag.

        :param dict tag: the dictionary storing information about the tag.
        """
        self._tags.append(tag)
        return self

    def path(
        self,
        path: str | None = None,
        *,
        operations: dict[str, typing.Any] | None = None,
        summary: str | None = None,
        description: str | None = None,
        parameters: list[dict] | None = None,
        **kwargs: typing.Any,
    ) -> APISpec:
        """Add a new path object to the spec.

        https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.2.md#path-item-object

        :param str|None path: URL path component
        :param dict|None operations: describes the http methods and options for `path`
        :param str summary: short summary relevant to all operations in this path
        :param str description: long description relevant to all operations in this path
        :param list|None parameters: list of parameters relevant to all operations in this path
        :param kwargs: parameters used by any path helpers see :meth:`register_path_helper`
        """
        # operations and parameters must be deepcopied because they are mutated
        # in _clean_operations and operation helpers and path may be called twice
        operations = deepcopy(operations) or {}
        parameters = deepcopy(parameters) or []

        # Execute path helpers
        for plugin in self.plugins:
            try:
                ret = plugin.path_helper(
                    path=path, operations=operations, parameters=parameters, **kwargs
                )
            except PluginMethodNotImplementedError:
                continue
            if ret is not None:
                path = ret
        if not path:
            raise APISpecError("Path template is not specified.")

        # Execute operation helpers
        for plugin in self.plugins:
            try:
                plugin.operation_helper(path=path, operations=operations, **kwargs)
            except PluginMethodNotImplementedError:
                continue

        self._clean_operations(operations)

        # Process links if provided (OpenAPI 3+ only)
        if "links" in kwargs and self.openapi_version.major >= 3:
            self._process_links(operations, kwargs["links"])

        self._paths.setdefault(path, operations).update(operations)
        if summary is not None:
            self._paths[path]["summary"] = summary
        if description is not None:
            self._paths[path]["description"] = description
        if parameters:
            parameters = self._clean_parameters(parameters)
            self._paths[path]["parameters"] = parameters

        self.components.resolve_refs_in_path(self._paths[path])

        return self

    def _clean_parameters(
        self,
        parameters: list[dict],
    ) -> list[dict]:
        """Ensure that all parameters with "in" equal to "path" are also required
        as required by the OpenAPI specification, as well as normalizing any
        references to global parameters and checking for duplicates parameters

        See https ://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.2.md#parameterObject.

        :param list parameters: List of parameters mapping
        """
        seen = set()
        for parameter in [p for p in parameters if isinstance(p, dict)]:
            # check missing name / location
            missing_attrs = [attr for attr in ("name", "in") if attr not in parameter]
            if missing_attrs:
                raise InvalidParameterError(
                    f"Missing keys {missing_attrs} for parameter"
                )

            # OpenAPI Spec 3 and 2 don't allow for duplicated parameters
            # A unique parameter is defined by a combination of a name and location
            unique_key = (parameter["name"], parameter["in"])
            if unique_key in seen:
                raise DuplicateParameterError(
                    "Duplicate parameter with name {} and location {}".format(
                        parameter["name"], parameter["in"]
                    )
                )
            seen.add(unique_key)

            # Add "required" attribute to path parameters
            if parameter["in"] == "path":
                parameter["required"] = True

        return parameters

    def _clean_operations(
        self,
        operations: dict[str, dict],
    ) -> None:
        """Ensure that all parameters with "in" equal to "path" are also required
        as required by the OpenAPI specification, as well as normalizing any
        references to global parameters. Also checks for invalid HTTP methods.

        See https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.2.md#parameterObject.

        :param dict operations: Dict mapping status codes to operations
        """
        operation_names = set(operations)
        valid_methods = set(VALID_METHODS[self.openapi_version.major])
        invalid = {
            key for key in operation_names - valid_methods if not key.startswith("x-")
        }
        if invalid:
            raise APISpecError(
                "One or more HTTP methods are invalid: {}".format(", ".join(invalid))
            )

        for operation in (operations or {}).values():
            if "parameters" in operation:
                operation["parameters"] = self._clean_parameters(
                    operation["parameters"]
                )
            if "responses" in operation:
                responses = {}
                for code, response in operation["responses"].items():
                    try:
                        code = int(code)  # handles IntEnums like http.HTTPStatus
                    except (TypeError, ValueError):
                        if self.openapi_version.major < 3 and code != "default":
                            warnings.warn(
                                "Non-integer code not allowed in OpenAPI < 3",
                                UserWarning,
                                stacklevel=2,
                            )
                    responses[str(code)] = response
                operation["responses"] = responses

    def _process_links(
        self,
        operations: dict[str, dict],
        links_spec: dict[str, tuple],
    ) -> None:
        """Process links specification and inject into operation responses.

        :param dict operations: Dict mapping HTTP methods to operation objects
        :param dict links_spec: Dict mapping link names to (route, method) or (route, method, parameters) tuples
        """
        # Convert link tuples to proper link objects
        link_objects = {}
        for link_name, link_tuple in links_spec.items():
            if (
                not isinstance(link_tuple, tuple)
                or len(link_tuple) < 2
                or len(link_tuple) > 3
            ):
                raise APISpecError(
                    f"Link '{link_name}' must be a tuple of (route, method) or (route, method, parameters)"
                )

            route = link_tuple[0]
            method = link_tuple[1]
            parameters = link_tuple[2] if len(link_tuple) == 3 else {}

            if not isinstance(route, str):
                raise APISpecError(f"Link '{link_name}' route must be a string")

            # Validate method
            method = method.lower()
            valid_methods = set(VALID_METHODS[self.openapi_version.major])
            if method not in valid_methods:
                raise APISpecError(
                    f"Link '{link_name}' has invalid HTTP method: {method}"
                )

            # Prefer operationId if defined
            operation_id = (
                self._paths.get(route, {}).get(method.lower(), {}).get("operationId")
            )
            link_obj: dict[str, typing.Any] = (
                {"operationId": operation_id}
                if operation_id
                else {
                    "operationRef": f"#/paths/{route.replace('~', '~0').replace('/', '~1')}/{method}"
                }
            )
            if parameters:
                link_obj["parameters"] = parameters

            link_objects[link_name] = link_obj

        # Inject links into all responses of all operations
        for operation in operations.values():
            if isinstance(operation, dict) and "responses" in operation:
                for response in operation["responses"].values():
                    if isinstance(response, dict):
                        if "links" not in response:
                            response["links"] = {}
                        response["links"].update(deepcopy(link_objects))
