# This file is part of Patsy
# Copyright (C) 2026
# See file LICENSE.txt for license information.

# Serialization of DesignInfo objects into a self-contained, human-readable
# description that can be used to rebuild the design rules in another process
# which has access to neither the original training data nor the training
# process's local variables.

# Only the stateful transforms shipped with Patsy itself can be described.
# Formulas that call functions defined in the training process (including
# custom stateful transforms) are rejected at export time, so that no
# half-working description is ever produced.

__all__ = [
    "design_info_to_dict",
    "design_info_from_dict",
    "design_info_to_json",
    "design_info_from_json",
    "save_design_info",
    "load_design_info",
]

import json
import os

import numpy as np

from patsy import PatsyError
from patsy.compat import OrderedDict
from patsy.contrasts import ContrastMatrix
from patsy.design_info import DesignInfo, FactorInfo, SubtermInfo
from patsy.desc import Term
from patsy.eval import EvalEnvironment, EvalFactor
from patsy.state import Center, Standardize
from patsy.splines import BS
from patsy.mgcv_cubic_splines import CR, CC, TE

# Bump this if the on-disk layout ever changes in an incompatible way.
_DESCRIPTION_FORMAT = "patsy-design-info-v1"

# The full list of stateful transforms that know how to describe and restore
# their learned state. Each entry is (type name, class, attribute names).
_TRANSFORM_REGISTRY = [
    ("center", Center, ("_sum", "_count")),
    ("standardize", Standardize, ("current_n", "current_mean", "current_M2")),
    ("bs", BS, ("_degree", "_all_knots")),
    ("cr", CR, ("_name", "_cyclic", "_all_knots", "_constraints")),
    ("cc", CC, ("_name", "_cyclic", "_all_knots", "_constraints")),
    ("te", TE, ("_constraints",)),
]


def _builtins_namespace():
    # EvalFactor evaluates formulas with patsy.builtins glued on as the
    # outermost namespace; reuse exactly that namespace on reconstruction.
    from patsy.eval import _builtins_dict

    return _builtins_dict


# JSON cannot represent tuples, byte strings, or numpy arrays, so mark them
# explicitly to make the conversion reversible.
def _to_jsonable(obj):
    if isinstance(obj, dict):
        return dict((key, _to_jsonable(value)) for key, value in obj.items())
    if isinstance(obj, tuple):
        return {"__tuple__": [_to_jsonable(value) for value in obj]}
    if isinstance(obj, list):
        return [_to_jsonable(value) for value in obj]
    if isinstance(obj, np.ndarray):
        return {
            "__ndarray__": _to_jsonable(obj.tolist()),
            "dtype": str(obj.dtype),
        }
    if isinstance(obj, np.generic):
        return _to_jsonable(obj.item())
    if isinstance(obj, bytes):
        return {"__bytes__": obj.decode("latin1")}
    if isinstance(obj, (bool, int, float, str)) or obj is None:
        return obj
    raise PatsyError(
        "cannot describe value of type %r; only simple JSON-compatible values "
        "(plus tuples, bytes and numpy arrays) are supported" % (type(obj),)
    )


def _from_jsonable(obj):
    if isinstance(obj, dict):
        if "__tuple__" in obj:
            return tuple(_from_jsonable(value) for value in obj["__tuple__"])
        if "__ndarray__" in obj:
            return np.asarray(
                _from_jsonable(obj["__ndarray__"]), dtype=obj["dtype"]
            )
        if "__bytes__" in obj:
            return obj["__bytes__"].encode("latin1")
        return dict(
            (key, _from_jsonable(value)) for key, value in obj.items()
        )
    if isinstance(obj, list):
        return [_from_jsonable(value) for value in obj]
    return obj


def _describe_transform(obj):
    for type_name, cls, attrs in _TRANSFORM_REGISTRY:
        if isinstance(obj, cls):
            actual_attrs = set(vars(obj))
            expected_attrs = set(attrs)
            missing = expected_attrs - actual_attrs
            unexpected = actual_attrs - expected_attrs
            if missing or unexpected:
                raise PatsyError(
                    "stateful transform %r is in an invalid state (missing "
                    "attributes %r, unexpected attributes %r); it can only be "
                    "exported after fitting has finished"
                    % (type_name, sorted(missing), sorted(unexpected))
                )
            return type_name, dict(
                (attr, _to_jsonable(getattr(obj, attr))) for attr in attrs
            )
    raise PatsyError(
        "cannot export a design info that uses the custom stateful transform "
        "%r of type %r; only Patsy's built-in stateful transforms "
        "(center, standardize, scale, bs, cr, cc, te) can be described"
        % (getattr(obj, "__name__", obj), type(obj).__name__)
    )


def _restore_transform(description):
    type_name = description["type"]
    for registry_name, cls, attrs in _TRANSFORM_REGISTRY:
        if registry_name != type_name:
            continue
        obj = cls()
        state = description["state"]
        if set(state) != set(attrs):
            raise PatsyError(
                "corrupt design description: transform %r expects state "
                "attributes %r, got %r"
                % (type_name, list(attrs), sorted(state))
            )
        for attr in attrs:
            setattr(obj, attr, _from_jsonable(state[attr]))
        # The constructors create a temporary memorization scratch dict that
        # is deleted once fitting has finished; drop it on restoration too so
        # a rebuilt design can itself be exported again.
        if hasattr(obj, "_tmp"):
            delattr(obj, "_tmp")
        return obj
    raise PatsyError(
        "unknown stateful transform %r in design description" % (type_name,)
    )


def _check_supported_factor(factor, factor_info):
    if not isinstance(factor, EvalFactor):
        raise PatsyError(
            "cannot export factor %r: only factors produced from Patsy "
            "formula strings can be described" % (factor.name(),)
        )
    state = factor_info.state
    for required_key in ("transforms", "eval_env", "eval_code", "pass_bins"):
        if required_key not in state:
            raise PatsyError(
                "factor %r does not carry the state Patsy produces when "
                "building a formula; cannot describe it" % (factor.code,)
            )
    # The EvalEnvironment captured during training may contain references to
    # the caller's local variables. Everything a portable formula needs is
    # available in the patsy.builtins namespace, so anything else betrays a
    # dependency on the training process.
    builtins = _builtins_namespace()
    env_names = []
    for namespace in state["eval_env"]._namespaces:
        for name, value in namespace.items():
            if name not in builtins:
                raise PatsyError(
                    "factor %r refers to %r, which is not one of Patsy's "
                    "built-in formula functions or variables. Functions and "
                    "variables defined in the training process cannot be "
                    "reproduced in another process, so this design info "
                    "cannot be exported" % (factor.code, name)
                )
            if value is not builtins[name]:
                raise PatsyError(
                    "factor %r uses %r, but in the training process this "
                    "name was redefined to a different object than Patsy's "
                    "built-in. Such a shadowed definition cannot be "
                    "reproduced in another process, so this design info "
                    "cannot be exported" % (factor.code, name)
                )
            if name not in env_names:
                env_names.append(name)
    for transform_obj in state["transforms"].values():
        _describe_transform(transform_obj)
    return env_names


def _describe_factor(factor, factor_info):
    env_names = _check_supported_factor(factor, factor_info)
    description = {
        "code": factor.code,
        "type": factor_info.type,
        "eval_code": factor_info.state["eval_code"],
        "env_names": env_names,
        "pass_bins": [
            sorted(obj_names) for obj_names in factor_info.state["pass_bins"]
        ],
        "transforms": [],
    }
    if factor_info.type == "numerical":
        description["num_columns"] = factor_info.num_columns
    else:
        description["categories"] = _to_jsonable(factor_info.categories)
    for obj_name, transform_obj in factor_info.state["transforms"].items():
        type_name, transform_state = _describe_transform(transform_obj)
        description["transforms"].append(
            {
                "name": obj_name,
                "type": type_name,
                "state": transform_state,
            }
        )
    return description


def _describe_term(term, subterm_infos):
    subterms = []
    for subterm in subterm_infos:
        contrasts = []
        for factor, contrast in subterm.contrast_matrices.items():
            if not isinstance(contrast, ContrastMatrix):
                raise PatsyError(
                    "expected a ContrastMatrix for factor %r"
                    % (factor.name(),)
                )
            contrasts.append(
                {
                    "factor": factor.code,
                    "matrix": _to_jsonable(contrast.matrix),
                    "column_suffixes": list(contrast.column_suffixes),
                }
            )
        subterms.append(
            {
                "factors": [factor.code for factor in subterm.factors],
                "contrasts": contrasts,
                "num_columns": subterm.num_columns,
            }
        )
    return {
        "factors": [factor.code for factor in term.factors],
        "subterms": subterms,
    }


def design_info_to_dict(design_info):
    """Encode a :class:`DesignInfo` as a plain JSON-compatible dictionary.

    The returned dictionary is fully self-contained: it records the column
    names and ordering, the factor expressions, categorical levels and their
    ordering, contrast matrices, interaction structure, and the learned state
    of every built-in stateful transform. Factors that depend on functions or
    variables defined in the training process are rejected with a
    :class:`PatsyError`, rather than being partially described.
    """
    if not isinstance(design_info, DesignInfo):
        raise PatsyError(
            "expected a DesignInfo object, got %r" % (type(design_info),)
        )
    if design_info.factor_infos is None or design_info.term_codings is None:
        raise PatsyError(
            "this DesignInfo carries no factor or term information (it was "
            "not built from a formula) and cannot be described"
        )

    factor_descriptions = []
    for factor, factor_info in design_info.factor_infos.items():
        factor_descriptions.append(_describe_factor(factor, factor_info))

    term_descriptions = []
    for term, subterm_infos in design_info.term_codings.items():
        term_descriptions.append(_describe_term(term, subterm_infos))

    return {
        "format": _DESCRIPTION_FORMAT,
        "column_names": list(design_info.column_names),
        "factors": factor_descriptions,
        "terms": term_descriptions,
    }


def design_info_to_json(design_info):
    """Return a human-readable JSON string describing `design_info`.

    See :func:`design_info_to_dict` for what is captured and when a
    :class:`PatsyError` is raised.
    """
    description = design_info_to_dict(design_info)
    return json.dumps(description, indent=2, sort_keys=False)


def _build_factor(description, builtins):
    factor = EvalFactor(description["code"])
    transforms = OrderedDict()
    for transform_description in description["transforms"]:
        transforms[transform_description["name"]] = _restore_transform(
            transform_description
        )
    env_namespace = {}
    for name in description["env_names"]:
        if name not in builtins:
            raise PatsyError(
                "corrupt design description: %r is not a Patsy "
                "builtin" % (name,)
            )
        env_namespace[name] = builtins[name]
    state = {
        "transforms": transforms,
        "eval_env": EvalEnvironment([env_namespace]),
        "eval_code": description["eval_code"],
        "pass_bins": [
            set(obj_names) for obj_names in description["pass_bins"]
        ],
    }
    if description["type"] == "numerical":
        factor_info = FactorInfo(
            factor,
            "numerical",
            state,
            num_columns=description["num_columns"],
        )
    else:
        factor_info = FactorInfo(
            factor,
            "categorical",
            state,
            categories=tuple(_from_jsonable(description["categories"])),
        )
    return factor, factor_info


def design_info_from_dict(description):
    """Rebuild a :class:`DesignInfo` from a dictionary produced by
    :func:`design_info_to_dict`.

    No training data and no training-process variables are required; the
    learned statistics (e.g. centering means) are taken verbatim from the
    description rather than being re-estimated from new data.
    """
    if not isinstance(description, dict) or description.get("format") != (
        _DESCRIPTION_FORMAT
    ):
        raise PatsyError(
            "not a Patsy design description (expected format %r)"
            % (_DESCRIPTION_FORMAT,)
        )
    builtins = _builtins_namespace()

    factors = OrderedDict()
    factor_infos = OrderedDict()
    for factor_description in description["factors"]:
        factor, factor_info = _build_factor(factor_description, builtins)
        factors[factor.code] = factor
        factor_infos[factor] = factor_info

    term_codings = OrderedDict()
    for term_description in description["terms"]:
        term_factors = [factors[code] for code in term_description["factors"]]
        term = Term(term_factors)
        subterm_infos = []
        for subterm_description in term_description["subterms"]:
            subterm_factors = [
                factors[code] for code in subterm_description["factors"]
            ]
            contrast_matrices = {}
            for contrast_description in subterm_description["contrasts"]:
                contrast_factor = factors[contrast_description["factor"]]
                contrast_matrices[contrast_factor] = ContrastMatrix(
                    _from_jsonable(contrast_description["matrix"]),
                    list(contrast_description["column_suffixes"]),
                )
            subterm_infos.append(
                SubtermInfo(
                    subterm_factors,
                    contrast_matrices,
                    subterm_description["num_columns"],
                )
            )
        term_codings[term] = subterm_infos

    # DesignInfo validates that terms, factors, contrast matrices and column
    # counts all agree with each other.
    design_info = DesignInfo(
        list(description["column_names"]),
        factor_infos=dict(factor_infos),
        term_codings=term_codings,
    )

    # Independently regenerate the column names from the coded structure, to
    # guarantee the rebuilt design produces exactly the advertised columns.
    from patsy.build import _subterm_column_names_iter

    regenerated_names = []
    for subterm_infos in term_codings.values():
        for subterm in subterm_infos:
            regenerated_names.extend(
                _subterm_column_names_iter(factor_infos, subterm)
            )
    if regenerated_names != design_info.column_names:
        raise PatsyError(
            "corrupt design description: stored column names %r do not match "
            "the names implied by the coded terms %r"
            % (design_info.column_names, regenerated_names)
        )
    return design_info


def design_info_from_json(json_text):
    """Rebuild a :class:`DesignInfo` from a JSON string produced by
    :func:`design_info_to_json`."""
    try:
        description = json.loads(json_text)
    except ValueError as exc:
        raise PatsyError("invalid JSON in design description: %s" % (exc,))
    return design_info_from_dict(description)


def _is_path(obj):
    return isinstance(obj, (str, os.PathLike))


def save_design_info(design_info, file):
    """Write a portable JSON description of `design_info` to `file`.

    `file` may be a path, or a text file-like object opened for writing.
    """
    text = design_info_to_json(design_info)
    if _is_path(file):
        with open(file, "w") as handle:
            handle.write(text)
    else:
        file.write(text)


def load_design_info(file):
    """Read a :class:`DesignInfo` back from a description written by
    :func:`save_design_info`.

    `file` may be a path, or a text file-like object opened for reading.
    """
    if _is_path(file):
        with open(file, "r") as handle:
            text = handle.read()
    else:
        text = file.read()
    return design_info_from_json(text)
