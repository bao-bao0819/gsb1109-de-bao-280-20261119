"""Helpers for generating patches to fix files."""

import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Optional

from sqlfluff.core.helpers.slice import is_zero_slice
from sqlfluff.core.parser import BaseSegment
from sqlfluff.core.parser.helpers import inside_next_segment_flags
from sqlfluff.core.parser.markers import PositionMarker
from sqlfluff.core.templaters import TemplatedFile
from sqlfluff.core.templaters.base import TemplatedFileSlice

linter_logger = logging.getLogger("sqlfluff.linter")


def _normalised_source_range(source_slice: slice) -> tuple[int, int]:
    """Return a hashable (start, stop) pair ignoring any slice step.

    Template slicing regularly produces zero length slices with a step (e.g.
    ``slice(2, 2, 1)``). Those compare unequal to ``slice(2, 2)`` even though
    they refer to exactly the same point in the source file. Normalising here
    means insertion conflicts are detected regardless of how the slice was
    constructed.
    """
    start = int(source_slice.start or 0)
    stop = int(source_slice.stop if source_slice.stop is not None else start)
    return start, stop


def _ranges_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Return whether two normalised source ranges overlap.

    Insertions (zero length ranges) only overlap another range if they sit
    strictly inside it. An insertion on either boundary can be applied safely
    alongside a replacement of the adjacent range.
    """
    first_start, first_stop = first
    second_start, second_stop = second

    if first_start == first_stop == second_start == second_stop:
        return True
    if first_start == first_stop:
        return second_start < first_start < second_stop
    if second_start == second_stop:
        return first_start < second_start < first_stop
    return max(first_start, second_start) < min(first_stop, second_stop)


def _source_expansions(
    templated_file: TemplatedFile, source_slice: slice, literal_only: bool = True
) -> dict[tuple[int, int], list[slice]]:
    """Map each relevant source sub-slice to its templated expansions.

    A loop (or any other repeated template construct) maps the same source
    range onto multiple templated positions. Grouping by the *source* slice is
    what distinguishes a genuine repetition (the same source slice appearing
    several times) from two ordinary adjacent slices which merely share a
    boundary point.

    A range may span several source slices (for example a literal wrapping a
    template tag). In that case every constituent slice is returned, and the
    caller takes the maximum repetition count to describe the whole range.

    By default only *literal* slices are considered. Template tags (``{% ... %}``,
    ``{{ ... }}``) are source-only constructs: a layout edit anchored on one of
    them is not a repeated edit of rendered SQL, so it should not be treated as
    an expansion which other renders must also agree on.
    """
    start, stop = _normalised_source_range(source_slice)
    expansions: dict[tuple[int, int], list[slice]] = defaultdict(list)

    def _eligible(tfs: TemplatedFileSlice) -> bool:
        return not literal_only or tfs.slice_type == "literal"

    if start == stop:
        # A point is only genuinely *repeated* if it sits strictly inside a
        # literal slice. An insertion at the boundary of a literal (e.g. just
        # before a `{% ... %}` tag) is anchored on the seam, not on repeated
        # rendered content, so it must not be multiplied by the loop count.
        # This keeps layout insertions around template tags from being
        # mistaken for divergent edits of repeated SQL.
        hosting = [
            tfs
            for tfs in templated_file.sliced_file
            if _eligible(tfs)
            and _normalised_source_range(tfs.source_slice)[0]
            < start
            < _normalised_source_range(tfs.source_slice)[1]
        ]
        selected = hosting
    else:
        selected = [
            tfs
            for tfs in templated_file.sliced_file
            if _eligible(tfs)
            and _ranges_overlap(
                (start, stop), _normalised_source_range(tfs.source_slice)
            )
        ]

    for tfs in selected:
        expansions[_normalised_source_range(tfs.source_slice)].append(
            tfs.templated_slice
        )
    return dict(expansions)


def _count_source_occurrences(
    templated_file: TemplatedFile, source_slice: slice
) -> int:
    """Return how many times a *literal* source range is expanded in a render.

    A source range may span several template slices (for example a literal
    which wraps a template tag). We take the *maximum* repetition count across
    the literal source sub-slices covered, because a loop repeats every
    sub-slice the same number of times and a range is only expandable as often
    as its most repeated literal part.

    Non-literal (template tag) anchors report ``0``: they are not repeated
    rendered SQL, so they must not be vetoed by an expansion count.
    """
    return max(
        (
            len(expansions)
            for expansions in _source_expansions(
                templated_file, source_slice, literal_only=True
            ).values()
        ),
        default=0,
    )


@dataclass
class FixPatch:
    """An edit patch for a source file."""

    templated_slice: slice
    fixed_raw: str
    # The patch category, functions mostly for debugging and explanation
    # than for function. It allows traceability of *why* this patch was
    # generated. It has no significance for processing.
    patch_category: str
    source_slice: slice
    templated_str: str
    source_str: str
    # Metadata used to explain where a patch came from when reporting
    # conflicts. It deliberately does not take part in equality comparisons:
    # two patches which edit the same source bytes in the same way are
    # equivalent, regardless of which render produced them.
    variant_idx: Optional[int] = field(default=None, compare=False, repr=False)
    # How many expansions of the source range exist in the render.
    occurrence_count: Optional[int] = field(default=None, compare=False, repr=False)
    # How many of those expansions proposed this exact fix.
    proposed_occurrences: Optional[int] = field(default=None, compare=False, repr=False)
    # Source position, for reporting conflicts without needing a templated file.
    line_no: int = field(default=0, compare=False, repr=False)
    line_pos: int = field(default=0, compare=False, repr=False)

    def dedupe_tuple(self) -> tuple[tuple[int, int], str]:
        """Generate a tuple of this fix for deduping."""
        return (
            _normalised_source_range(self.source_slice),
            self.fixed_raw,
        )

    def occurrence_tuple(
        self,
    ) -> tuple[tuple[int, int], str, Optional[int], int, int]:
        """Generate a tuple which distinguishes separate expansions.

        Two expansions of the same source range must be kept separate while we
        decide whether they agree. The render and templated position are what
        distinguish them.
        """
        return (
            _normalised_source_range(self.source_slice),
            self.fixed_raw,
            self.variant_idx,
            int(self.templated_slice.start or 0),
            int(self.templated_slice.stop or 0),
        )


@dataclass
class FixPatchConflict:
    """A source range which cannot be safely auto-fixed.

    The range is left untouched in the source file and the competing evidence
    is retained here so that it can be reported to the user, who can then make
    the decision manually.
    """

    source_slice: slice
    source_str: str
    candidates: list[FixPatch]
    reason: str
    line_no: int = 0
    line_pos: int = 0
    expected_occurrences: int = 0
    proposed_occurrences: int = 0
    # ``(variant_idx, templated_slice)`` for every expansion of the source
    # range which was observed while resolving the patches. ``variant_idx`` is
    # ``None`` for patches created outside the multi-variant lint path.
    source_occurrences: list[tuple[Optional[int], slice]] = field(default_factory=list)

    @property
    def source_range(self) -> tuple[int, int]:
        """The real source range (0-based, stop exclusive) of the conflict."""
        return _normalised_source_range(self.source_slice)

    @property
    def fix_texts(self) -> list[str]:
        """The distinct fix texts which were proposed for this range."""
        return sorted({patch.fixed_raw for patch in self.candidates})

    # Human readable aliases for the reason. These help callers (and tests)
    # reason about the conflict without depending on the internal spelling.
    @property
    def is_incompatible(self) -> bool:
        """Whether two expansions proposed different text for the same range."""
        return self.reason == "incompatible"

    @property
    def is_overlapping(self) -> bool:
        """Whether patches for overlapping (but not identical) ranges clashed."""
        return self.reason == "overlapping"

    @property
    def is_unconfirmed(self) -> bool:
        """Whether a patch was only proposed by a subset of expansions."""
        return self.reason == "unconfirmed"

    @property
    def expansion_evidence(self) -> list[dict[str, object]]:
        """Describe each expansion which proposed a fix for this range."""
        evidence: list[dict[str, object]] = []
        for patch in self.candidates:
            evidence.append(
                {
                    "variant_idx": patch.variant_idx,
                    "source_slice": list(_normalised_source_range(patch.source_slice)),
                    "templated_slice": [
                        int(patch.templated_slice.start or 0),
                        int(patch.templated_slice.stop or 0),
                    ],
                    "templated_str": patch.templated_str,
                    "fixed_raw": patch.fixed_raw,
                    "patch_category": patch.patch_category,
                }
            )
        return evidence

    def to_dict(self) -> dict[str, object]:
        """Return a JSON serialisable description of the conflict."""
        return {
            "source_range": list(self.source_range),
            "source_str": self.source_str,
            "line_no": self.line_no,
            "line_pos": self.line_pos,
            "reason": self.reason,
            "expected_occurrences": self.expected_occurrences,
            "proposed_occurrences": self.proposed_occurrences,
            "fix_texts": self.fix_texts,
            "expansions": [
                {
                    "variant_idx": variant_idx,
                    "templated_slice": [
                        int(templated_slice.start or 0),
                        int(templated_slice.stop or 0),
                    ],
                }
                for variant_idx, templated_slice in self.source_occurrences
            ],
            "candidates": self.expansion_evidence,
        }

    def __str__(self) -> str:
        """Summarise the conflict for logging."""
        return (
            f"{self.reason} source fix conflict at {self.source_range} "
            f"(line {self.line_no}, position {self.line_pos}): "
            f"{self.fix_texts!r} from {self.proposed_occurrences} of "
            f"{self.expected_occurrences} expansions"
        )


# Public aliases. The reporting concept is referred to as a "patch conflict"
# in some contexts and a "source fix conflict" in others.
PatchConflict = FixPatchConflict
SourceFixConflict = FixPatchConflict


@dataclass
class SourcePatchResolution:
    """The result of safely merging source patches from multiple renders."""

    patches: list[FixPatch]
    conflicts: list[FixPatchConflict]

    @property
    def safe_patches(self) -> list[FixPatch]:
        """Patches which may be written to the source file."""
        return self.patches

    @property
    def has_conflicts(self) -> bool:
        """Whether any source range required manual handling."""
        return bool(self.conflicts)

    @property
    def conflict_count(self) -> int:
        """How many source ranges required manual handling."""
        return len(self.conflicts)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON serialisable description of the resolution."""
        return {
            "safe_patch_count": len(self.patches),
            "conflict_count": len(self.conflicts),
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


# Alias for callers which prefer to think of the resolution as a report.
FixReport = SourcePatchResolution


def _iter_source_fix_patches(
    segment: BaseSegment, templated_file: TemplatedFile
) -> Iterator[FixPatch]:
    """Yield any source patches as fixes now.

    NOTE: This yields source fixes for the segment and any of its
    children, so it's important to call it at the right point in
    the recursion to avoid yielding duplicates.
    """
    for source_fix in segment.source_fixes:
        yield FixPatch(
            source_fix.templated_slice,
            source_fix.edit,
            patch_category="source",
            source_slice=source_fix.source_slice,
            templated_str=templated_file.templated_str[source_fix.templated_slice],
            source_str=templated_file.source_str[source_fix.source_slice],
        )


def _iter_templated_patches(
    segment: BaseSegment, templated_file: TemplatedFile
) -> Iterator[FixPatch]:
    """Iterate through the segments generating fix patches.

    The patches are generated in TEMPLATED space. This is important
    so that we defer dealing with any loops until later. At this stage
    everything *should* happen in templated order.

    Occasionally we have an insertion around a placeholder, so we also
    return a hint to deal with that.
    """
    # Does it match? If so we can ignore it.
    assert segment.pos_marker
    templated_raw = templated_file.templated_str[segment.pos_marker.templated_slice]
    matches = segment.raw == templated_raw
    if matches:
        # First yield any source fixes
        yield from _iter_source_fix_patches(segment, templated_file)
        # Then return.
        return

    # If we're here, the segment doesn't match the original.
    linter_logger.debug(
        "# Changed Segment Found: %s at %s: Original: [%r] Fixed: [%r]",
        type(segment).__name__,
        segment.pos_marker.templated_slice,
        templated_raw,
        segment.raw,
    )

    # If it's all literal, then we don't need to recurse.
    if segment.pos_marker.is_literal():
        # First yield any source fixes
        yield from _iter_source_fix_patches(segment, templated_file)
        # Then yield the position in the source file and the patch
        yield FixPatch(
            source_slice=segment.pos_marker.source_slice,
            templated_slice=segment.pos_marker.templated_slice,
            patch_category="literal",
            fixed_raw=segment.raw,
            templated_str=templated_file.templated_str[
                segment.pos_marker.templated_slice
            ],
            source_str=templated_file.source_str[segment.pos_marker.source_slice],
        )
    # Can we go deeper?
    elif not segment.segments:
        # It's not literal, but it's also a raw segment. If we're going
        # to yield a change, we would have done it from the parent, so
        # we just abort from here.
        return  # pragma: no cover TODO?
    else:
        # This segment isn't a literal, but has changed, we need to go deeper.

        # If there's an end of file segment or indent, ignore them just for the
        # purposes of patch iteration.
        # NOTE: This doesn't mutate the underlying `self.segments`.
        segments = segment.segments
        while segments and segments[-1].is_type("end_of_file", "indent"):
            segments = segments[:-1]

        # Iterate through the child segments
        source_idx = segment.pos_marker.source_slice.start
        templated_idx = segment.pos_marker.templated_slice.start
        insert_buff = ""
        first_segment_pos: Optional[PositionMarker] = None
        inside_next = inside_next_segment_flags(segments)
        for idx, seg in enumerate(segments):
            # First check for insertions.
            # At this stage, everything should have a position.
            assert seg.pos_marker
            # We know it's an insertion if it has length but not in the templated
            # file.
            if seg.raw and seg.pos_marker.is_point():
                # Add it to the insertion buffer if it has length:
                if seg.raw:
                    insert_buff += seg.raw
                    # We want to capture the first position where we have a point.
                    first_segment_pos = first_segment_pos or seg.pos_marker
                    linter_logger.debug(
                        "Appending insertion buffer. %r @idx: %s",
                        insert_buff,
                        templated_idx,
                    )
                continue

            # Special case: a template placeholder (e.g. {{ variable }}) whose
            # Jinja expression rendered to an empty string. These segments occupy
            # zero space in the templated file but have a non-zero source range.
            # Due to the way the lexer processes them, they may appear in the
            # segment list *before* an adjacent literal segment that starts at an
            # earlier templated position.  If we naively apply the start_diff
            # logic below, we would generate a spurious deletion patch for the
            # source characters between `templated_idx` and the placeholder's
            # templated position (e.g. the opening quote of a quoted literal).
            # See: https://github.com/sqlfluff/sqlfluff/issues/6261
            # The same applies to any zero-length segment (e.g. an `{% if %}`
            # placeholder or its indent) positioned inside a later sibling.
            # See: https://github.com/sqlfluff/sqlfluff/issues/8611
            if (
                seg.is_type("placeholder")
                and seg.raw == ""
                and is_zero_slice(seg.pos_marker.templated_slice)
                and not is_zero_slice(seg.pos_marker.source_slice)
                and getattr(seg, "block_type", "") == "templated"
            ) or inside_next[idx]:
                # Yield any embedded source fixes (rare, but possible).
                yield from _iter_source_fix_patches(seg, templated_file=templated_file)
                # Do NOT update templated_idx here.  The placeholder occupies no
                # space in the templated output, so advancing templated_idx would
                # make the next sibling appear to have a negative start_diff and
                # its source content would be silently skipped.
                # We do not update source_idx either; the source content covered by
                # this placeholder is implicitly part of the surrounding literal
                # segment's source range and will be left untouched.
                continue

            # If we get here, then we know it's an original. Check for deletions at
            # the point before this segment (vs the TEMPLATED).
            # Deletions in this sense could also mean source consumption.
            start_diff = seg.pos_marker.templated_slice.start - templated_idx

            # Check to see whether there's a discontinuity before the current
            # segment
            if start_diff > 0 or insert_buff:
                # If we have an insert buffer, then it's an edit, otherwise a
                # deletion.

                # For the start of the next segment, we need the position of the
                # first raw, not the pos marker of the whole thing. That accounts
                # better for loops.
                first_segment_pos = first_segment_pos or seg.pos_marker
                # A gap before this segment is a deletion, also when the insert
                # buffer is not empty, so the slice must cover the gap.
                # See: https://github.com/sqlfluff/sqlfluff/issues/8611
                templated_slice = slice(
                    templated_idx,
                    max(
                        first_segment_pos.templated_slice.start,
                        templated_idx,
                        seg.pos_marker.templated_slice.start,
                    ),
                )
                yield FixPatch(
                    # Whether the source slice is zero depends on the start_diff.
                    # A non-zero start diff implies a deletion, or more likely
                    # a consumed element of the source. We can use the tracking
                    # markers from the last segment to recreate where this element
                    # should be inserted in both source and template.
                    # The slices must never go backwards so the end of the slice must
                    # be greater than or equal to the start.
                    source_slice=(
                        templated_file.templated_slice_to_source_slice(templated_slice)
                        if start_diff > 0
                        else slice(
                            source_idx,
                            max(first_segment_pos.source_slice.start, source_idx),
                        )
                    ),
                    templated_slice=templated_slice,
                    patch_category="mid_point",
                    fixed_raw=insert_buff,
                    templated_str="",
                    source_str="",
                )

                # Reset the first position so we can move the pointer forward.
                first_segment_pos = None
                insert_buff = ""

            # Now we deal with any changes *within* the segment itself.
            yield from _iter_templated_patches(seg, templated_file=templated_file)

            # Once we've dealt with any patches from the segment, update
            # our position markers.
            source_idx = seg.pos_marker.source_slice.stop
            templated_idx = seg.pos_marker.templated_slice.stop

        # After the loop, we check whether there's a trailing deletion
        # or insert. Also valid if we still have an insertion buffer here.
        end_diff = segment.pos_marker.templated_slice.stop - templated_idx
        if end_diff or insert_buff:
            source_slice = slice(
                source_idx,
                segment.pos_marker.source_slice.stop,
            )
            templated_slice = slice(
                templated_idx,
                segment.pos_marker.templated_slice.stop,
            )
            # We determine the source_slice directly rather than
            # inferring it so that we can be very specific that
            # we ensure that fixes adjacent to source-only slices
            # (e.g. {% endif %}) are placed appropriately relative
            # to source-only slices.
            yield FixPatch(
                source_slice=source_slice,
                templated_slice=templated_slice,
                patch_category="end_point",
                fixed_raw=insert_buff,
                templated_str=templated_file.templated_str[templated_slice],
                source_str=templated_file.source_str[source_slice],
            )


def _log_hints(patch: FixPatch, templated_file: TemplatedFile) -> None:
    """Log hints for debugging during patch generation."""
    max_log_length = 10
    if patch.templated_slice.start >= max_log_length:
        pre_hint = templated_file.templated_str[
            patch.templated_slice.start - max_log_length : patch.templated_slice.start
        ]
    else:
        pre_hint = templated_file.templated_str[: patch.templated_slice.start]
    if patch.templated_slice.stop + max_log_length < len(templated_file.templated_str):
        post_hint = templated_file.templated_str[
            patch.templated_slice.stop : patch.templated_slice.stop + max_log_length
        ]
    else:
        post_hint = templated_file.templated_str[patch.templated_slice.stop :]
    linter_logger.debug("        Templated Hint: ...%r <> %r...", pre_hint, post_hint)


def generate_source_patches(
    tree: BaseSegment,
    templated_file: TemplatedFile,
    variant_idx: Optional[int] = None,
) -> list[FixPatch]:
    """Use the fixed tree to generate source patches.

    Importantly here we deduplicate and sort the patches from their position
    in the templated file into their intended order in the source file.

    Any source fixes are generated in `_iter_templated_patches` and included
    alongside any standard fixes. That means we treat them the same here.

    Patches are only deduplicated when they propose the same edit at the same
    templated position. Two expansions of the same source range (e.g. the two
    iterations of a loop) are kept separate so that their agreement - or lack
    of it - can be evaluated later.
    """
    # Iterate patches, filtering and translating as we go:
    linter_logger.debug("### Beginning Patch Iteration.")
    filtered_source_patches = []
    dedupe_buffer: set[tuple[tuple[int, int], str, Optional[int], int, int]] = set()
    # We use enumerate so that we get an index for each patch. This is entirely
    # so when debugging logs we can find a given patch again!
    for idx, patch in enumerate(
        _iter_templated_patches(tree, templated_file=templated_file)
    ):
        linter_logger.debug("  %s Yielded patch: %s", idx, patch)
        _log_hints(patch, templated_file)

        # Check for duplicates *at the same templated position*. Different
        # expansions of one source range must survive to be compared.
        occurrence_tuple = patch.occurrence_tuple()
        if occurrence_tuple in dedupe_buffer:
            linter_logger.info(
                "      - Skipping. Source space Duplicate: %s",
                occurrence_tuple,
            )
            continue

        # We now evaluate patches in the source-space for whether they overlap
        # or disrupt any templated sections unless designed to do so.
        # NOTE: We rely here on the patches being generated in order.

        # Get the affected raw slices.
        local_raw_slices = templated_file.raw_slices_spanning_source_slice(
            patch.source_slice
        )
        local_type_list = [slc.slice_type for slc in local_raw_slices]

        # Deal with the easy cases of 1) New code at end 2) only literals
        if not local_type_list or set(local_type_list) == {"literal"}:
            linter_logger.info(
                "      * Keeping patch on new or literal-only section.",
            )
            filtered_source_patches.append(patch)
            dedupe_buffer.add(occurrence_tuple)
        # Handle the easy case of an explicit source fix
        elif patch.patch_category == "source":
            linter_logger.info(
                "      * Keeping explicit source fix patch.",
            )
            filtered_source_patches.append(patch)
            dedupe_buffer.add(occurrence_tuple)
        # Is it a zero length patch.
        elif (
            patch.source_slice.start == patch.source_slice.stop
            and patch.source_slice.start == local_raw_slices[0].source_idx
        ):
            linter_logger.info(
                "      * Keeping insertion patch on slice boundary.",
            )
            filtered_source_patches.append(patch)
            dedupe_buffer.add(occurrence_tuple)
        else:  # pragma: no cover
            # We've got a situation where the ends of our patch need to be
            # more carefully mapped. This used to happen with greedy template
            # element matching, but should now never happen. In the event that
            # it does, we'll warn but carry on.
            linter_logger.warning(
                "Skipping edit patch on uncertain templated section [%s], "
                "Please report this warning on GitHub along with the query "
                "that produced it.",
                (patch.patch_category, patch.source_slice),
            )
            continue

    # Annotate each patch with its provenance so that conflicts can be
    # explained later without re-analysing the templated file.
    annotated_patches: list[FixPatch] = []
    for patch in filtered_source_patches:
        start, _ = _normalised_source_range(patch.source_slice)
        line_no, line_pos = templated_file.get_line_pos_of_char_pos(start, source=True)
        annotated_patches.append(
            replace(
                patch,
                variant_idx=variant_idx,
                occurrence_count=_count_source_occurrences(
                    templated_file, patch.source_slice
                ),
                line_no=line_no,
                line_pos=line_pos,
            )
        )

    # Sort the patches before building up the file.
    return sorted(
        annotated_patches,
        key=lambda x: (
            _normalised_source_range(x.source_slice),
            _normalised_source_range(x.templated_slice),
            x.fixed_raw,
        ),
    )


def _position_of_offset(source_str: str, offset: int) -> tuple[int, int]:
    """Translate a character offset into a 1-indexed (line, position)."""
    offset = min(max(offset, 0), len(source_str))
    line_no = source_str.count("\n", 0, offset) + 1
    last_newline = source_str.rfind("\n", 0, offset)
    line_pos = offset - last_newline
    return line_no, line_pos


def _expansion_key(patch: FixPatch) -> tuple[Optional[int], int, int]:
    """Identify the expansion (render + templated position) of a patch.

    Two patches with the same key are two findings about the *same* rendering
    of the same source, and can be collapsed. Two patches with different keys
    are different expansions of that source range and must agree before the
    source can be edited.
    """
    start, stop = _normalised_source_range(patch.templated_slice)
    return (patch.variant_idx, start, stop)


def _candidate_patches(patches: Iterable[FixPatch]) -> list[FixPatch]:
    """Return one deterministic representative per (expansion, fix text).

    Sorting is by content, never by input order, so reordering the renders or
    the patches within them cannot change which candidates are reported.
    """
    deduped: dict[tuple[Optional[int], int, int, str], FixPatch] = {}
    for patch in patches:
        key = (*_expansion_key(patch), patch.fixed_raw)
        deduped.setdefault(key, patch)
    return sorted(
        deduped.values(),
        key=lambda patch: (
            -1 if patch.variant_idx is None else patch.variant_idx,
            _normalised_source_range(patch.templated_slice),
            patch.fixed_raw,
        ),
    )


def resolve_source_patches(
    patch_buffers: list[list[FixPatch]],
    source_str: Optional[str] = None,
    templated_files: Optional[Sequence[Optional[TemplatedFile]]] = None,
) -> SourcePatchResolution:
    """Resolve source patches from multiple renders into safe edits + report.

    Patches from every render are grouped by their *real* source range. A
    group is only written back to the source file when every expansion which
    covers that range agrees on the same edit. This matters for templates with
    loops: the same source range is rendered several times, and a fix which is
    correct for one expansion may be wrong for another.

    The rules are:

    1. Identical edits for the same source range collapse to a single patch.
    2. Different edits for the same source range are ``incompatible``. The
       range is left untouched and the competing expansions are reported.
    3. When a range is rendered N times but only M < N expansions propose a
       *content* edit, the edit is ``unconfirmed``. Applying it would change an
       expansion which did not ask for the change, so it is not written.
       Whitespace-only edits are exempt: loop iterations share one source
       range, so a layout fix (indent, wrap) is expected from the first
       iteration and applying it is how the layout propagates to the rest.
       ``templated_files`` (aligned with ``patch_buffers``) supplies the true
       expansion count per render, including renders which proposed nothing.
    4. Patches whose ranges partially overlap cannot both be applied. Both are
       withheld and reported as ``overlapping``.

    The result does not depend on the order in which the renders or the patches
    were supplied: the winner of a conflict is never chosen by position in a
    list. Genuinely independent edits are always retained.
    """
    # Group by normalised source range. Grouping is content based, so the
    # order of `patch_buffers` is irrelevant from here on.
    by_range: dict[tuple[int, int], list[FixPatch]] = defaultdict(list)
    for patches in patch_buffers:
        for patch in patches:
            by_range[_normalised_source_range(patch.source_slice)].append(patch)

    # Align templated files with variants. Callers that provide them supply one
    # entry per patch buffer (``None`` where a variant produced no templated
    # file). If the lengths don't match we ignore them and fall back to the
    # per-patch metadata.
    aligned_templated_files: Optional[Sequence[Optional[TemplatedFile]]] = None
    if templated_files is not None and len(templated_files) == len(patch_buffers):
        aligned_templated_files = templated_files

    expected_occurrences: dict[tuple[int, int], int] = {}
    confirmed_patches: list[FixPatch] = []
    conflicts: list[FixPatchConflict] = []

    for source_range in sorted(by_range):
        patches = by_range[source_range]
        start, stop = source_range
        source_slice = slice(start, stop)
        candidates = _candidate_patches(patches)

        # Distinct fix texts proposed for this exact range, across all renders.
        fix_texts = {patch.fixed_raw for patch in candidates}

        # Judge agreement per variant: within a variant, every expansion of
        # the source range must both propose an edit and propose the *same*
        # edit. Different variants are independent renderings, so a variant
        # which doesn't render the range at all imposes no constraint.
        per_variant_expected: dict[Optional[int], int] = {}
        per_variant_proposed: dict[Optional[int], set[tuple[int, int]]] = defaultdict(
            set
        )
        for patch in candidates:
            declared = patch.occurrence_count or 1
            per_variant_expected[patch.variant_idx] = max(
                per_variant_expected.get(patch.variant_idx, 0), declared
            )
            per_variant_proposed[patch.variant_idx].add(
                _normalised_source_range(patch.templated_slice)
            )

        if aligned_templated_files is not None:
            # The templated file knows how many times the range is expanded,
            # even if a particular expansion produced no patch at all.
            for variant_idx, templated_file in enumerate(aligned_templated_files):
                if templated_file is None:  # pragma: no cover
                    continue
                rendered = _count_source_occurrences(templated_file, source_slice)
                if rendered:
                    per_variant_expected[variant_idx] = rendered
                    per_variant_proposed.setdefault(variant_idx, set())

        expected = max(per_variant_expected.values(), default=0)
        expected_occurrences[source_range] = expected
        # Completeness only matters for a range which a single render expands
        # more than once (e.g. the body of a loop). In that case *every*
        # expansion within that render must propose the edit: if only some do,
        # the others were already correct and the edit would break them.
        # Alternate variants are independent renders; a variant which proposes
        # nothing is not vetoing an edit discovered in a different variant.
        #
        # Whitespace-only edits are exempt. Loop iterations deliberately share
        # one source range, so a layout fix (e.g. re-indenting or wrapping) is
        # expected to be proposed from the first iteration only, and applying
        # it is exactly what propagates the layout to the other iterations.
        # Withholding those would break ordinary indentation fixes.
        non_whitespace_edit = any(
            patch.fixed_raw.strip() != patch.templated_str.strip()
            for patch in candidates
        )
        incomplete = non_whitespace_edit and any(
            expected_count > 1
            and len(per_variant_proposed.get(variant, set())) < expected_count
            for variant, expected_count in per_variant_expected.items()
        )
        proposed = len(candidates)

        if len(fix_texts) > 1:
            reason = "incompatible"
        elif incomplete:
            reason = "unconfirmed"
        else:
            reason = ""

        if reason:
            representative = candidates[0]
            line_no, line_pos = _position_of_offset(
                source_str if source_str is not None else representative.source_str,
                start,
            )
            conflicts.append(
                FixPatchConflict(
                    source_slice=slice(start, stop),
                    source_str=representative.source_str,
                    candidates=candidates,
                    reason=reason,
                    line_no=line_no,
                    line_pos=line_pos,
                    expected_occurrences=expected,
                    proposed_occurrences=proposed,
                    source_occurrences=[
                        (patch.variant_idx, patch.templated_slice)
                        for patch in candidates
                    ],
                )
            )
            linter_logger.info(
                "Withholding %s source fix on %s: %s",
                reason,
                source_range,
                sorted(fix_texts),
            )
            continue

        # Agreed edit. Keep one deterministic representative.
        confirmed_patches.append(candidates[0])

    # Now check for partial overlaps between otherwise-confirmed ranges.
    # Groups are already deduplicated by exact range, so any overlap here is
    # between two different ranges, at least one of which has length.
    final_patches: list[FixPatch] = []
    withheld_for_overlap: list[FixPatch] = []
    for patch in sorted(
        confirmed_patches,
        key=lambda patch: (
            _normalised_source_range(patch.source_slice),
            patch.fixed_raw,
        ),
    ):
        patch_range = _normalised_source_range(patch.source_slice)
        overlapping = [
            other
            for other in confirmed_patches
            if other is not patch
            and _normalised_source_range(other.source_slice) != patch_range
            and _ranges_overlap(
                _normalised_source_range(other.source_slice), patch_range
            )
        ]
        if overlapping:
            withheld_for_overlap.append(patch)
            continue
        final_patches.append(patch)

    # Report each withheld overlapping range once, with the patches it clashes
    # with as evidence.
    for conflict in _overlap_conflicts(
        withheld_for_overlap, source_str, expected_occurrences
    ):
        conflicts.append(conflict)

    conflicts.sort(key=lambda conflict: conflict.source_range)
    return SourcePatchResolution(patches=final_patches, conflicts=conflicts)


def _overlap_conflicts(
    withheld: list[FixPatch],
    source_str: Optional[str],
    expected_occurrences: dict[tuple[int, int], int],
) -> list[FixPatchConflict]:
    """Build conflict reports for patches withheld due to partial overlap."""
    # Withheld patches are already one-per-source-range (they come from the
    # per-range resolution above), so each range yields exactly one report.
    conflicts: list[FixPatchConflict] = []
    for patch in sorted(
        withheld,
        key=lambda patch: (
            _normalised_source_range(patch.source_slice),
            patch.fixed_raw,
        ),
    ):
        source_range = _normalised_source_range(patch.source_slice)
        start, stop = source_range
        overlapping = [
            other
            for other in withheld
            if other is not patch
            and _normalised_source_range(other.source_slice) != source_range
            and _ranges_overlap(
                _normalised_source_range(other.source_slice), source_range
            )
        ]
        line_no, line_pos = _position_of_offset(
            source_str if source_str is not None else patch.source_str, start
        )
        conflicts.append(
            FixPatchConflict(
                source_slice=slice(start, stop),
                source_str=patch.source_str,
                candidates=_candidate_patches([patch, *overlapping]),
                reason="overlapping",
                line_no=line_no,
                line_pos=line_pos,
                expected_occurrences=expected_occurrences.get(source_range, 0),
                proposed_occurrences=1,
                source_occurrences=[
                    (candidate.variant_idx, candidate.templated_slice)
                    for candidate in _candidate_patches([patch, *overlapping])
                ],
            )
        )
    return conflicts


def merge_source_patches(
    patch_buffers: list[list[FixPatch]],
    source_str: Optional[str] = None,
    templated_files: Optional[Sequence[Optional[TemplatedFile]]] = None,
) -> list[FixPatch]:
    """Merge source patches from multiple variants.

    Patches are deduplicated in source space. Conflicting patches are skipped so
    that the non-conflicting subset can still be applied safely. Use
    :func:`resolve_source_patches` when the conflicts themselves are needed,
    e.g. to report which source ranges require manual handling.
    """
    return resolve_source_patches(
        patch_buffers, source_str=source_str, templated_files=templated_files
    ).patches
