import re
from collections.abc import Callable
from typing import Any

from typing_extensions import TypeForm

from mashumaro.codecs._cache import (
    bind_template,
    get_template,
    remember_template,
)
from mashumaro.core.meta.code.builder import CodeBuilder
from mashumaro.core.meta.helpers import is_optional, is_type_var_any
from mashumaro.core.meta.types.common import (
    AttrsHolder,
    FieldContext,
    ValueSpec,
)
from mashumaro.core.meta.types.pack import PackerRegistry
from mashumaro.core.meta.types.unpack import UnpackerRegistry

CALL_EXPR = re.compile(r"^([^ ]+)\(value\)$")


class CodecCodeBuilder(CodeBuilder):
    @classmethod
    def new(cls, **kwargs: Any) -> "CodecCodeBuilder":
        if "attrs" not in kwargs:
            kwargs["attrs"] = AttrsHolder()
        return cls(AttrsHolder("__root__"), **kwargs)  # type: ignore

    def add_decode_method(
        self,
        shape_type: TypeForm,
        decoder_obj: Any,
        pre_decoder_func: Callable[[Any], Any] | None = None,
    ) -> None:
        template = get_template(
            "decode",
            self,
            decoder_obj,
            shape_type,
            pre_decoder_func,
        )
        if template is not None:
            bind_template(template, decoder_obj, "decode")
            return
        self.reset()
        with self.indent("def decode(value):"):
            if pre_decoder_func:
                self.ensure_object_imported(pre_decoder_func, "decoder")
                self.add_line("value = decoder(value)")
            could_be_none = (
                shape_type in (Any, type(None), None)
                or is_type_var_any(self.get_real_type("", shape_type))
                or is_optional(
                    shape_type, self.get_field_resolved_type_params("")
                )
            )
            unpacked_value = UnpackerRegistry.get(
                ValueSpec(
                    type=shape_type,
                    expression="value",
                    builder=self,
                    field_ctx=FieldContext(name="", metadata={}),
                    could_be_none=could_be_none,
                )
            )
            self.add_line(f"return {unpacked_value}")
        self.add_line("setattr(decoder_obj, 'decode', decode)")
        if pre_decoder_func is None:
            m = CALL_EXPR.match(unpacked_value)
            if m:
                method_name = m.group(1)
                self.lines.reset()
                self.add_line(f"setattr(decoder_obj, 'decode', {method_name})")
        self.ensure_object_imported(decoder_obj, "decoder_obj")
        self.ensure_object_imported(self.cls, "cls")
        self.compile()
        remember_template(
            "decode",
            self,
            decoder_obj,
            shape_type,
            pre_decoder_func,
        )

    def add_encode_method(
        self,
        shape_type: TypeForm,
        encoder_obj: Any,
        post_encoder_func: Callable[[Any], Any] | None = None,
    ) -> None:
        template = get_template(
            "encode",
            self,
            encoder_obj,
            shape_type,
            post_encoder_func,
        )
        if template is not None:
            bind_template(template, encoder_obj, "encode")
            return
        self.reset()
        with self.indent("def encode(value):"):
            could_be_none = (
                shape_type in (Any, type(None), None)
                or is_type_var_any(self.get_real_type("", shape_type))
                or is_optional(
                    shape_type, self.get_field_resolved_type_params("")
                )
            )
            packed_value = PackerRegistry.get(
                ValueSpec(
                    type=shape_type,
                    expression="value",
                    builder=self,
                    field_ctx=FieldContext(name="", metadata={}),
                    could_be_none=could_be_none,
                    no_copy_collections=self.get_dialect_or_config_option(
                        "no_copy_collections", ()
                    ),
                )
            )
            if post_encoder_func:
                self.ensure_object_imported(post_encoder_func, "encoder")
                self.add_line(f"return encoder({packed_value})")
            else:
                self.add_line(f"return {packed_value}")
        self.add_line("setattr(encoder_obj, 'encode', encode)")
        if post_encoder_func is None:
            m = CALL_EXPR.match(packed_value)
            if m:
                method_name = m.group(1)
                self.lines.reset()
                self.add_line(f"setattr(encoder_obj, 'encode', {method_name})")
        self.ensure_object_imported(encoder_obj, "encoder_obj")
        self.ensure_object_imported(self.cls, "cls")
        self.ensure_object_imported(self.cls, "self")
        self.compile()
        remember_template(
            "encode",
            self,
            encoder_obj,
            shape_type,
            post_encoder_func,
        )
