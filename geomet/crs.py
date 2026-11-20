#  Copyright 2013 Lars Butler & individual contributors
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Utilities for validating CRS/SRID declarations in GeoJSON-like geometry
trees before they are written to a format (such as WKT or WKB) which can
only encode a single spatial reference system.

Declarations are read from two places on any geometry (root or nested
member of a ``GeometryCollection``):

- ``meta.srid`` (for example ``{'meta': {'srid': 4326}}``)
- ``crs.properties.name`` (for example
  ``{'crs': {'type': 'name', 'properties': {'name': 'EPSG4326'}}}``)

A member which declares nothing inherits the SRID of its nearest
declaring ancestor. Only explicit declarations are considered; coordinate
values are never inspected, so a point whose numbers happen to look like
longitude/latitude is not treated as a declaration (or a conflict) on
its own.
"""


class InvalidCRSException(ValueError):
    """
    Raised when CRS/SRID declarations within a geometry (including nested
    geometries of a collection) are ambiguous or contradictory.
    """


def declared_srid(obj, path='root'):
    """
    Get the SRID declared directly on a single geometry `obj`.

    :param dict obj:
        A GeoJSON-like `dict`.
    :param str path:
        Location of `obj` within its geometry tree, used in error
        messages.
    :returns:
        The declared SRID as a string, or `None` if `obj` does not
        declare a CRS/SRID.
    :raises InvalidCRSException:
        If `obj` declares both ``meta.srid`` and ``crs`` and the two
        contradict each other.
    """
    meta = obj.get('meta') or {}
    crs = obj.get('crs') or {}
    meta_srid = meta.get('srid')
    crs_name = (crs.get('properties') or {}).get('name')
    crs_srid = None
    if crs_name is not None:
        # Shave off the EPSG prefix to give us the SRID:
        crs_srid = crs_name.replace('EPSG', '')

    if (meta_srid is not None and
            crs_srid is not None and
            str(meta_srid) != str(crs_srid)):
        raise InvalidCRSException(
            'Ambiguous CRS/SRID values at %s: %s and %s'
            % (path, meta_srid, crs_srid)
        )
    if meta_srid is not None:
        return str(meta_srid)
    return crs_srid


def _validate(obj, inherited, path, state):
    """
    Recursively validate CRS/SRID declarations in a geometry tree.

    :param inherited:
        SRID (string) inherited from the nearest declaring ancestor, or
        `None` if no ancestor declares one.
    :param state:
        Mutable dict used to record whether any declaration was seen.
    :returns:
        The effective SRID (string) for `obj`, or `None`.
    """
    declared = declared_srid(obj, path)
    if declared is not None:
        state['any_declared'] = True
        if inherited is not None and declared != inherited:
            raise InvalidCRSException(
                'Conflicting CRS/SRID declaration at %s: this geometry '
                'declares SRID %s, but it inherits SRID %s from an '
                'ancestor' % (path, declared, inherited)
            )
    effective = declared if declared is not None else inherited
    for index, geom in enumerate(obj.get('geometries') or []):
        _validate(geom, effective, '%s.geometries[%d]' % (path, index),
                  state)
    return effective


def validate_uniform_srid(obj):
    """
    Validate that a geometry tree can be written using a single spatial
    reference system, and return that SRID.

    Every level of the tree is checked:

    - A member declaring both ``meta.srid`` and ``crs`` with contradictory
      values is reported (the error identifies that member).
    - A member whose declaration contradicts the SRID inherited from an
      ancestor is reported, along with the inherited SRID.
    - Members without any declaration simply inherit and are not errors.

    If nothing in the tree declares a CRS/SRID, `None` is returned and
    the geometry is written without any SRID, as before. If some member
    declares a SRID but the root does not, the tree is rejected: a SRID
    would have to be assumed for the undeclared members, which is not
    done without confirmation from the caller (in the form of an
    explicit root-level declaration).

    :param dict obj:
        A GeoJSON-like `dict`, possibly a ``GeometryCollection`` with
        nested geometries.
    :returns:
        The uniform SRID for the whole tree as a string, or `None` if
        the tree carries no CRS/SRID declarations at all.
    :raises InvalidCRSException:
        If declarations are contradictory or cannot be confirmed.
    """
    state = {'any_declared': False}
    _validate(obj, None, 'root', state)
    if not state['any_declared']:
        return None
    root_declared = declared_srid(obj, 'root')
    if root_declared is None:
        raise InvalidCRSException(
            'Cannot confirm a single CRS/SRID for this geometry: the '
            'root does not declare one, but some members do. Refusing '
            'to assume a SRID for undeclared members without caller '
            'confirmation; add an explicit root-level CRS/SRID '
            'declaration to confirm.'
        )
    return root_declared


def root_srid_only(obj, srid):
    """
    Return a copy of a geometry tree in which only the root carries
    CRS/SRID information.

    The root's ``meta.srid`` is set to `srid` (unless `srid` is `None`),
    and CRS/SRID declarations are removed from all nested geometries, so
    that writers encode the reference system exactly once. The input
    object is not modified, and coordinate data is shared (never copied
    or altered).

    :param dict obj:
        A GeoJSON-like `dict`.
    :param srid:
        The uniform SRID for the tree (as returned by
        :func:`validate_uniform_srid`), or `None`.
    """
    result = dict(obj)
    if srid is not None:
        meta = dict(result.get('meta') or {})
        meta['srid'] = srid
        result['meta'] = meta
    if result.get('type') == 'GeometryCollection':
        result['geometries'] = [
            _strip_nested_declarations(geom)
            for geom in result['geometries']
        ]
    return result


def _strip_nested_declarations(obj):
    """
    Copy a nested geometry, removing its ``crs`` and ``meta.srid``
    declarations (recursively for nested collections). Other metadata
    and all coordinates are left untouched.
    """
    result = dict(obj)
    meta = dict(result.get('meta') or {})
    meta.pop('srid', None)
    if meta:
        result['meta'] = meta
    else:
        result.pop('meta', None)
    result.pop('crs', None)
    if result.get('type') == 'GeometryCollection':
        result['geometries'] = [
            _strip_nested_declarations(geom)
            for geom in result['geometries']
        ]
    return result
