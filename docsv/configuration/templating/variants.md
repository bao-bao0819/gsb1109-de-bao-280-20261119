# Template Variant Rendering

SQLFluff normally lints the rendered SQL produced by a templater. For a template
with control flow such as `if` or `for`, a single render can only exercise one
path through that template.

Template variant rendering allows SQLFluff to render multiple versions of the
same source file and combine the linting results. This improves coverage for
templated SQL by allowing SQLFluff to inspect branches that would otherwise stay
unreached in a single render.

This currently matters most for the [Jinja templater](jinja) and
[dbt templater](dbt), because they can contain conditional branches which change
the rendered SQL substantially.

## How It Works

At a high level, SQLFluff:

1. Renders the template once using the configured templater context.
2. Identifies literal slices in the source file that were not reached in that
   render.
3. Generates additional renderings designed to cover those unreached areas.
4. Lints the returned variants and maps the resulting violations back onto the
   original source file.

This does **not** guarantee exhaustive coverage of every possible branch
combination. Instead, it is a practical best-effort mechanism which improves
lint coverage while keeping runtime bounded.

Note that in cases where no other variants can be generated, the performance
impact is minimal as variants are only linted if they are meaningfully different
from the primary render. In cases where many variants are generated, the performance
impact can be more substantial.

## Configuration

Variant rendering is controlled by the top-level `render_variant_limit`
setting:

```ini
[sqlfluff]
render_variant_limit = 5
```

This is the maximum number of rendered variants SQLFluff will lint for a single
file, including the primary render.

Recommended values:

* `1`: Disable additional variant rendering and restore the historical
  single-render behaviour.
* `5`: Default. A moderate setting which improves branch coverage without
  opening the search space too aggressively.
* Higher values: Useful for heavily branched templates, but can increase
  templating and linting time noticeably.

## When To Use It

Variant rendering is most useful when your templated SQL contains control flow
that changes the SQL structure, for example:

* `{% if %}` / `{% elif %}` / `{% else %}` blocks
* conditional `WHERE`, `JOIN`, or `SELECT` fragments
* dbt models whose branches produce different SQL layouts

If your templates are mostly simple substitutions, macros, or library calls,
variant rendering is less likely to change lint coverage.

## Fixing Across Variants

Auto-fixing is more delicate when the same source range is rendered more than
once. This happens whenever a template repeats a fragment, most commonly the
body of a `{% for %}` loop, but also when the same literal is rendered from two
different `{% if %}` branches.

SQLFluff resolves the fixes proposed by each expansion in source space. A
source range is only written back when the evidence is unambiguous:

* **Every expansion agrees.** The same edit is proposed by every expansion of
  that source range, so it is written once, regardless of how many times the
  fragment is rendered. This also deduplicates identical fixes from different
  variants.
* **Expansions disagree.** If two expansions of the same source range propose
  different text, SQLFluff does **not** pick a winner. Writing either edit
  would change an expansion which renders differently, so the range is left
  untouched and reported for manual handling.
* **Only some expansions propose an edit.** If a source range is rendered
  several times inside one render but only some expansions propose a fix, the
  remaining expansions were already correct. Applying the fix would break them,
  so it is withheld and reported.
* **Patches overlap partially.** Two edits which cover different but
  overlapping source ranges cannot both be applied. Both are withheld and
  reported.

Fixes which are genuinely independent (for example an edit outside the loop)
are still applied. A conflict on one range never blocks safe fixes elsewhere in
the same file.

Reporting is emitted by `sqlfluff fix` as a `MANUAL FIX REQUIRED` line per
conflicting range, identifying the file, line and position, the real source
range, and the competing fixes. The same information is available from the
Python API via `LintedFile.source_fix_conflicts()` and `LintedFile.fix_report()`,
which also include the templated position of each expansion as evidence.

Resolution never depends on the order in which expansions or variants were
processed: swapping the loop body (or the order of renders) changes which fix
text belongs to which expansion, but not whether the range is safe to fix.
Re-running `fix` on the resulting file does not change ranges which were
already fixed safely.

## Limitations

There are still some important limits:

* SQLFluff does not attempt to enumerate every possible execution path.
* Additional variants are capped by `render_variant_limit`.
* Support depends on the templater implementation. Jinja-derived templaters,
  including dbt, currently provide the most meaningful support.
* More variants mean more work, so runtime and the number of surfaced issues can
  increase.
* Alternate variants can surface additional lint violations, but parse or lex
  failures in those alternate variants are not treated as fatal if SQLFluff has
  a valid root variant to lint. That allows SQLFluff to keep the extra lint
  coverage without failing an otherwise valid file.
* Some templater errors may still be repeated across variants while the feature
  continues to mature.

## Practical Guidance

Start with the default and only tune upward if you have concrete examples where
important branches are still being missed.

If you are debugging surprising output, temporarily setting
`render_variant_limit = 1` is a useful way to compare current behaviour
with the old single-render model.

If increasing `render_variant_limit` surfaces new issues, that usually means
SQLFluff found lint violations in a branch which the historical single-render
flow did not reach. By contrast, parse failures in alternate branches are only
surfaced when SQLFluff cannot find a valid root variant to lint.

For teams using the [dbt templater](dbt), this setting complements the existing
dbt configuration rather than replacing it. Accurate dbt rendering still depends
on project, profile, and macro configuration.
