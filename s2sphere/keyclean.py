# -*- coding: utf-8 -*-
"""Clean historical S2 partition keys into standard S2 tokens.

Two historical writings of the same 64 bit cell id exist in the exported
lists:

* the current exporter emits the canonical S2 token: the id is hex encoded
  and trailing zero nibbles are stripped, so parsing right-pads the value
  (this is :meth:`s2sphere.CellId.from_token`);
* an older tool emitted the bare 64 bit integer as hex and stripped the
  *leading* zeros instead, so parsing left-aligns nothing and the raw
  integer is the cell id.

A short key such as ``01`` is a legal value under both writings but denotes
two different cells (here a level 2 cell on face 0 versus the level 30 leaf
cell whose raw id is 1).  Inputs may additionally carry an ASCII ``0x``
prefix or upper case letters.  The same append batch always uses one single
writing, but the writing of each individual row was never persisted.

Disambiguation therefore has to come from trusted evidence attached to the
rows: a face, a level, a full 64 bit reference id, same-cell / parent-child
links to records in other lists, or the single old-to-new style switch that
a source file assembled from two exporter versions may contain.  Rows that
stay genuinely ambiguous are kept verbatim with an explicit reason instead
of being guessed from conclusions reached for another group.

The existing :class:`~s2sphere.CellId` constructors are intentionally not
modified; everything here is a layer on top of them.
"""
from __future__ import absolute_import, division, print_function

from collections import namedtuple

from .sphere import CellId

HEX_DIGITS = frozenset('0123456789abcdef')

STANDARD = 'standard'
LEGACY = 'legacy'
CANONICAL = 'canonical'

SAME = 'same'
PARENT = 'parent'
CHILD = 'child'
ALIAS = 'alias'

# Reasons used for rows that cannot even be parsed.  They are reported
# separately and never influence unrelated valid files.
EMPTY = 'empty text'
TOO_LONG = 'more than 16 hex digits'
LEXICAL = 'only parseable as an integer lexical variant'
INVALID_FACE = 'face bits outside 0..5'
INVALID_LEVEL_BITS = 'lsb is not a valid level marker'


Reading = namedtuple('Reading', 'style cell')
Link = namedtuple('Link', 'relation target')
Refs = namedtuple('Refs', 'face level id_')
Inspection = namedtuple('Inspection', 'text readings error')
InvalidRecord = namedtuple(
    'InvalidRecord', 'source seq append text reason detail')
ResolvedRecord = namedtuple(
    'ResolvedRecord',
    'source seq append text style cell token face level evidence')
AmbiguousRecord = namedtuple(
    'AmbiguousRecord',
    'source seq append text readings reason evidence')
Conflict = namedtuple('Conflict', 'rows reason evidence')
ConflictOutcome = namedtuple('ConflictOutcome', 'source seq append conflict')
OldKey = namedtuple(
    'OldKey', 'source seq append text style token id_ face level evidence')
SwitchReport = namedtuple(
    'SwitchReport',
    'source appends cut_range feasible_cuts cut_location per_cut_ids '
    'row_id_stable stable notes')


class InvalidKey(ValueError):
    """Raised when a textual key cannot be turned into any valid cell."""

    def __init__(self, reason, detail=''):
        super(InvalidKey, self).__init__('{0}: {1}'.format(reason, detail))
        self.reason = reason
        self.detail = detail


def _strip_prefix(text):
    low = text.lower()
    if low.startswith('0x'):
        return low[2:]
    return low


def inspect_text(text):
    """Parse one textual key without consulting any evidence.

    Returns an :class:`Inspection`.  ``readings`` contains every valid
    :class:`Reading` (one or two for short keys, one for 16 digit values);
    ``error`` is an :class:`InvalidKey` when no valid reading exists.
    """
    if text is None:
        return Inspection('', [], InvalidKey(EMPTY, 'None'))
    stripped = text.strip()
    if stripped == '':
        return Inspection(stripped, [], InvalidKey(EMPTY, repr(text)))

    nibbles = _strip_prefix(stripped)
    if not nibbles or any(ch not in HEX_DIGITS for ch in nibbles):
        parsed = False
        try:
            int(stripped, 0)
            parsed = True
        except (ValueError, TypeError):
            try:
                int(stripped)
                parsed = True
            except (ValueError, TypeError):
                parsed = False
        detail = 'integer parsing {0}succeed'.format(
            'does ' if parsed else 'does not ')
        return Inspection(
            stripped, [], InvalidKey(LEXICAL, detail + ' on ' + repr(text)))

    if len(nibbles) > 16:
        return Inspection(
            stripped, [],
            InvalidKey(TOO_LONG, '{0} nibbles'.format(len(nibbles))))

    standard_cell = CellId(int(nibbles.ljust(16, '0'), 16))
    legacy_cell = CellId(int(nibbles, 16))

    readings = []
    failures = []
    seen_ids = set()
    for style, cell in ((STANDARD, standard_cell), (LEGACY, legacy_cell)):
        if cell.id() in seen_ids:
            continue
        seen_ids.add(cell.id())
        if cell.is_valid():
            readings.append(Reading(style, cell))
        else:
            reason = (INVALID_FACE if cell.face() >= CellId.NUM_FACES
                      else INVALID_LEVEL_BITS)
            failures.append('{0} {1} ({2})'.format(
                style, reason, format(cell.id(), '016x')))

    if not readings:
        first_reason = (INVALID_FACE
                        if standard_cell.face() >= CellId.NUM_FACES
                        else INVALID_LEVEL_BITS)
        return Inspection(
            stripped, [],
            InvalidKey(first_reason, '; '.join(failures)))
    return Inspection(stripped, readings, None)


def _coerce_id(value):
    if isinstance(value, int):
        return value
    text = value.strip().lower()
    if text.startswith('0x'):
        text = text[2:]
    return int(text, 16)


class _DSU(object):
    def __init__(self, items):
        self.parent = dict((item, item) for item in items)

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left, right):
        self.parent[self.find(left)] = self.find(right)


class BatchCleaner(object):
    """Accumulate source files and clean the whole batch on demand.

    Sources are added in file arrival order; each source describes the
    append batches it was built from (with whatever partial version range
    the caller happens to know).  Records keep their append sequence number,
    which is the only ordering with semantic meaning for the style switch.
    """

    def __init__(self):
        self.source_order = []
        self.append_facts = {}
        self.records = {}

    def add_source(self, name, appends, version_styles=None):
        """Register a source file.

        ``appends`` is a list of ``{'append': id, 'version': name}``
        dictionaries in append order.  The version may be ``None`` when the
        caller does not know it.  ``version_styles`` maps the versions the
        caller does know about to ``legacy`` or ``standard``; a partial map
        is allowed because the exact switch point was never saved.  The
        style of one append is uniform, so a switch can only sit on a
        boundary between two appends.
        """
        if name in self.append_facts:
            raise ValueError('duplicate source {0!r}'.format(name))
        self.source_order.append(name)
        self.append_facts[name] = {
            'appends': list(appends),
            'version_styles': dict(version_styles or {}),
        }

    def add_record(self, source, seq, text, append=None, face=None,
                   level=None, id_=None, links=()):
        """Add one exported row.

        ``links`` is a sequence of ``(relation, target_source, target_seq)``
        where relation is one of ``same``, ``parent`` (this row is the
        parent of the target), ``child`` (this row is a child of the
        target) or ``alias``.  Alias links are never used to merge cells.
        """
        key = (source, seq)
        if key in self.records:
            raise ValueError('duplicate record {0!r}'.format(key, ))
        refs = Refs(face=face, level=level,
                    id_=None if id_ is None else _coerce_id(id_))
        norm_links = tuple(
            Link(rel, (target_source, target_seq))
            for rel, target_source, target_seq in links)
        self.records[key] = (
            dict(source=source, seq=seq, append=append, text=text,
                 refs=refs, links=norm_links))
        return key

    def drop_ref(self, source, seq, kind):
        """Remove one trusted evidence piece (face/level/id_)."""
        record = self.records[(source, seq)]
        refs = record['refs']
        if getattr(refs, kind) is None:
            raise KeyError((source, seq, kind))
        record['refs'] = refs._replace(**{kind: None})

    def set_ref(self, source, seq, kind, value):
        """Attach or replace one piece of trusted evidence."""
        if kind not in ('face', 'level', 'id_'):
            raise KeyError(kind)
        record = self.records[(source, seq)]
        if kind == 'id_' and value is not None:
            value = _coerce_id(value)
        record['refs'] = record['refs']._replace(**{kind: value})

    def add_link(self, source, seq, relation, target_source, target_seq):
        record = self.records[(source, seq)]
        record['links'] = record['links'] + (
            Link(relation, (target_source, target_seq)),)

    def drop_link(self, source, seq, relation, target_source, target_seq):
        target = (target_source, target_seq)
        record = self.records[(source, seq)]
        remaining = tuple(link for link in record['links']
                          if not (link.relation == relation
                                  and link.target == target))
        if len(remaining) == len(record['links']):
            raise KeyError((source, seq, relation, target))
        record['links'] = remaining

    # ------------------------------------------------------------------
    # solving
    # ------------------------------------------------------------------

    def clean(self):
        invalids, valid = self._inspect_records()
        rows, bad_evidence = self._row_domains(valid)
        components, dsu = self._same_components(rows)
        parent_edges, alias_pairs = self._directed_edges(rows, dsu)

        conflicts = []
        evidence_roots = self._register_evidence_conflicts(
            bad_evidence, rows, components, conflicts)

        comp_domain, comp_evidence = self._component_domains(
            components, rows, parent_edges)
        empty_roots = self._register_domain_conflicts(
            comp_domain, components, comp_evidence, conflicts,
            evidence_roots)

        possible = self._solve_with_isolation(
            comp_domain, parent_edges, components, comp_evidence,
            conflicts, empty_roots, evidence_roots)

        resolved, ambiguous, row_style = self._outcomes(
            possible, components, rows, comp_evidence, conflicts)

        switch_reports = self._switch_reports(row_style, rows, conflicts)
        ordered, outcomes = self._build_outputs(
            resolved, ambiguous, invalids, conflicts)

        old_key_map = {}
        for record in resolved:
            roundtrip = CellId.from_token(record.token)
            assert roundtrip.id() == record.cell.id()
            old_key_map[(record.source, record.seq)] = OldKey(
                record.source, record.seq, record.append, record.text,
                record.style, record.token, record.cell.id(),
                record.face, record.level, record.evidence)

        return CleanReport(
            ordered=ordered, outcomes=outcomes,
            resolved=tuple(resolved), ambiguous=tuple(ambiguous),
            conflicts=tuple(conflicts), invalid=tuple(invalids),
            switch_reports=tuple(switch_reports),
            old_key_map=old_key_map, alias_pairs=tuple(alias_pairs))

    def _inspect_records(self):
        invalids = []
        valid = {}
        for key in sorted(self.records):
            record = self.records[key]
            inspection = inspect_text(record['text'])
            if inspection.error is not None:
                invalids.append(InvalidRecord(
                    key[0], key[1], record['append'], inspection.text,
                    inspection.error.reason, inspection.error.detail))
            else:
                valid[key] = inspection.readings
        return invalids, valid

    def _row_domains(self, valid):
        rows = {}
        bad_evidence = {}
        for key, readings in valid.items():
            record = self.records[key]
            candidates = dict(
                (reading.style, reading.cell) for reading in readings)
            evidence = []
            refs = record['refs']
            if refs.id_ is not None:
                ref_cell = CellId(refs.id_)
                evidence.append(
                    'ref id={0:016x} from {1}/{2}'.format(
                        refs.id_, key[0], key[1]))
                if not ref_cell.is_valid():
                    bad_evidence[key] = (
                        'reference id {0:016x} is not a valid cell'.format(
                            refs.id_), evidence)
                else:
                    candidates = dict(
                        (style, cell) for style, cell in candidates.items()
                        if cell.id() == refs.id_)
            if refs.face is not None:
                evidence.append(
                    'ref face={0} from {1}/{2}'.format(
                        refs.face, key[0], key[1]))
                if not 0 <= refs.face < CellId.NUM_FACES:
                    bad_evidence[key] = (
                        'reference face {0} outside 0..5'.format(refs.face),
                        evidence)
                else:
                    candidates = dict(
                        (style, cell) for style, cell in candidates.items()
                        if cell.face() == refs.face)
            if refs.level is not None:
                evidence.append(
                    'ref level={0} from {1}/{2}'.format(
                        refs.level, key[0], key[1]))
                if not 0 <= refs.level <= CellId.MAX_LEVEL:
                    bad_evidence[key] = (
                        'reference level {0} outside 0..{1}'.format(
                            refs.level, CellId.MAX_LEVEL), evidence)
                else:
                    candidates = dict(
                        (style, cell) for style, cell in candidates.items()
                        if cell.level() == refs.level)
            if not candidates and key not in bad_evidence:
                bad_evidence[key] = (
                    'no legal reading of {0!r} matches the trusted '
                    'reference values'.format(record['text']), evidence)
            rows[key] = dict(candidates=candidates, evidence=evidence,
                             record=record)
        return rows, bad_evidence

    def _same_components(self, rows):
        dsu = _DSU(rows.keys())
        for key, info in rows.items():
            for link in info['record']['links']:
                if link.relation == SAME and link.target in rows:
                    dsu.union(key, link.target)
        components = {}
        for key in rows:
            components.setdefault(dsu.find(key), []).append(key)
        return components, dsu

    def _directed_edges(self, rows, dsu):
        parent_edges = set()
        alias_pairs = []
        for key, info in rows.items():
            for link in info['record']['links']:
                target = link.target
                if target not in rows:
                    continue
                if link.relation == ALIAS:
                    alias_pairs.append((key, target))
                elif link.relation == PARENT:
                    parent_edges.add((dsu.find(key), dsu.find(target)))
                elif link.relation == CHILD:
                    parent_edges.add((dsu.find(target), dsu.find(key)))
        return parent_edges, alias_pairs

    def _register_evidence_conflicts(self, bad_evidence, rows, components,
                                     conflicts):
        member_to_root = {}
        for comp_root, members in components.items():
            for key in members:
                member_to_root[key] = comp_root
        roots = set()
        for key, (reason, evidence) in bad_evidence.items():
            root = member_to_root.get(key)
            roots.add(root)
            members = components.get(root, [key])
            conflicts.append(Conflict(
                tuple(sorted(members)),
                'trusted evidence cannot match any legal reading: ' + reason,
                tuple(sorted(set(evidence)))))
        return roots

    def _component_domains(self, components, rows, parent_edges):
        comp_domain = {}
        comp_evidence = {}
        for root, members in components.items():
            domain = None
            evidence = set()
            for member in members:
                cells = set(rows[member]['candidates'].values())
                domain = cells if domain is None else domain & cells
                evidence.update(rows[member]['evidence'])
                for link in rows[member]['record']['links']:
                    if link.relation == SAME:
                        evidence.add('same-link {0} <-> {1}'.format(
                            member, link.target))
            for parent, child in parent_edges:
                if root in (parent, child):
                    evidence.add('parent-link {0} -> {1}'.format(
                        parent, child))
            comp_domain[root] = domain if domain is not None else set()
            comp_evidence[root] = tuple(sorted(evidence))
        return comp_domain, comp_evidence

    def _register_domain_conflicts(self, comp_domain, components,
                                   comp_evidence, conflicts, evidence_roots):
        empty_roots = set()
        for root, domain in comp_domain.items():
            if root in evidence_roots:
                continue
            if not domain:
                empty_roots.add(root)
                conflicts.append(Conflict(
                    tuple(sorted(components[root])),
                    'same-cell linked rows have no reading in common',
                    comp_evidence[root]))
        return empty_roots

    def _solve_with_isolation(self, comp_domain, parent_edges, components,
                              comp_evidence, conflicts, empty_roots,
                              evidence_roots):
        """Solve the hierarchy CSP, isolating conflicting component clusters.

        The parent/child graph splits into independent connected clusters;
        each cluster is solved on its own so an unsatisfiable cluster can
        never poison unrelated components.  Inside an unsolvable cluster a
        minimal unsat core is located by deletion, which yields exactly the
        mutually conflicting input rows instead of blaming the whole file.
        """
        graph = dict((root, set()) for root in components)
        self_roots = set()
        for parent, child in parent_edges:
            if parent == child:
                self_roots.add(parent)
            elif parent in graph and child in graph:
                graph[parent].add(child)
                graph[child].add(parent)

        possible = dict((root, set()) for root in components)
        excluded = set(empty_roots) | set(self_roots) | set(evidence_roots)

        clusters = []
        seen = set()
        for root in components:
            if root in seen or root in excluded:
                continue
            cluster = set()
            stack = [root]
            while stack:
                current = stack.pop()
                if current in cluster or current in excluded:
                    continue
                cluster.add(current)
                stack.extend(graph[current] - cluster)
            seen |= cluster
            clusters.append(cluster)

        for cluster in clusters:
            active = set(cluster)
            while True:
                live = sorted(active, key=lambda item: item)
                live_edges = set(
                    (parent, child) for parent, child in parent_edges
                    if parent in active and child in active
                    and parent != child)
                if self._sat(comp_domain, live_edges, live):
                    for root in live:
                        for cell in comp_domain[root]:
                            if self._sat(comp_domain, live_edges, live,
                                         fixed=(root, cell)):
                                possible[root].add(cell)
                    break
                core = self._unsat_core(
                    active, comp_domain, parent_edges)
                self._register_core_conflict(
                    core, components, comp_evidence, self_roots, conflicts)
                excluded |= core
                active -= core
                if not active:
                    break

        for root in self_roots:
            if root in empty_roots or root in evidence_roots:
                continue
            self._register_core_conflict(
                {root}, components, comp_evidence, self_roots, conflicts)
        return possible

    def _unsat_core(self, active, comp_domain, parent_edges):
        """Shrink an unsatisfiable component set to one minimal unsat core."""
        core = set(active)
        for candidate in sorted(active, key=lambda item: item):
            trial = core - {candidate}
            live_edges = set(
                (parent, child) for parent, child in parent_edges
                if parent in trial and child in trial and parent != child)
            if not self._sat(
                    comp_domain, live_edges,
                    sorted(trial, key=lambda item: item)):
                core = trial
        return core

    def _register_core_conflict(self, core, components, comp_evidence,
                                self_roots, conflicts):
        members = tuple(sorted(
            key for root in core for key in components[root]))
        evidence = set()
        for root in core:
            evidence.update(comp_evidence.get(root, ()))
        if core & self_roots:
            reason = (
                'a same-cell group is declared a strict parent or child '
                'of itself, which no cell can satisfy')
        else:
            reason = (
                'individually legal rows jointly violate the trusted '
                'hierarchy or same-cell links')
        conflicts.append(Conflict(members, reason, tuple(sorted(evidence))))

    def _sat(self, comp_domain, parent_edges, live_roots, fixed=None):
        """Decide whether the hierarchy constraints are satisfiable.

        Only feasibility matters, so the search stops at the first complete
        assignment instead of enumerating them; ``fixed`` pins one component
        to one candidate cell for per-component possible-value checks.
        """
        live_roots = list(live_roots)
        adjacency = dict((root, []) for root in live_roots)
        for parent, child in parent_edges:
            if parent not in adjacency or child not in adjacency:
                continue
            if parent == child:
                # A cell cannot be the strict parent of itself.
                adjacency[parent].append((child, 'self-parent'))
            else:
                adjacency[parent].append((child, 'parent-of'))
                adjacency[child].append((parent, 'child-of'))

        assignment = {}

        def satisfies(relation, cell, other):
            if relation == 'self-parent':
                return False
            if relation == 'parent-of':
                return (cell.level() < other.level()
                        and cell.contains(other))
            return (other.level() < cell.level()
                    and other.contains(cell))

        def backtrack(index):
            if index == len(live_roots):
                return True
            root = live_roots[index]
            if fixed is not None and root == fixed[0]:
                candidates = [fixed[1]]
            else:
                candidates = sorted(
                    comp_domain[root], key=lambda item: item.id())
            for cell in candidates:
                if all(satisfies(relation, cell, assignment[neighbour])
                       for neighbour, relation in adjacency[root]
                       if neighbour in assignment):
                    assignment[root] = cell
                    if backtrack(index + 1):
                        return True
                    del assignment[root]
            return False

        return backtrack(0)

    def _outcomes(self, possible, components, rows, comp_evidence,
                  conflicts):
        conflicted = set(
            key for conflict in conflicts for key in conflict.rows)
        resolved = []
        ambiguous = []
        row_style = {}
        for root, members in components.items():
            cells = possible[root]
            if not cells or any(key in conflicted for key in members):
                continue
            evidence = comp_evidence[root]
            if len(cells) == 1:
                cell = next(iter(cells))
                for key in members:
                    info = rows[key]
                    styles = tuple(sorted(
                        style for style, candidate
                        in info['candidates'].items()
                        if candidate == cell))
                    style = (styles[0] if len(styles) == 1
                             else CANONICAL)
                    row_style[key] = (style, cell)
                    record = info['record']
                    resolved.append(ResolvedRecord(
                        key[0], key[1], record['append'], record['text'],
                        style, cell, cell.to_token(), cell.face(),
                        cell.level(), evidence))
            else:
                for key in members:
                    info = rows[key]
                    record = info['record']
                    readings = tuple(sorted(
                        (style, cell, cell.to_token())
                        for style, cell in info['candidates'].items()))
                    reason = (
                        'both writings stay consistent with the available '
                        'evidence and yield different ids; manual review '
                        'required')
                    ambiguous.append(AmbiguousRecord(
                        key[0], key[1], record['append'], record['text'],
                        readings, reason, evidence))
        resolved.sort(key=lambda item: (item.source, item.seq))
        ambiguous.sort(key=lambda item: (item.source, item.seq))
        return resolved, ambiguous, row_style

    # ------------------------------------------------------------------
    # switch point analysis
    # ------------------------------------------------------------------

    def _switch_reports(self, row_style, rows, conflicts):
        conflicted = set(
            key for conflict in conflicts for key in conflict.rows)
        reports = []
        for source in self.source_order:
            keys = sorted(key for key in self.records if key[0] == source)
            facts = self.append_facts.get(source, {})
            appends = [fact['append'] for fact in facts.get('appends', ())]
            version_of = dict(
                (fact['append'], fact.get('version'))
                for fact in facts.get('appends', ()))
            known_styles = facts.get('version_styles', {})

            pins = {}
            for key in keys:
                if key in conflicted or key not in rows:
                    continue
                pinned = row_style.get(key)
                if pinned is None or pinned[0] == CANONICAL:
                    continue
                append = self.records[key]['append']
                pins.setdefault(append, set()).add(pinned[0])

            if appends:
                boundaries = list(range(len(appends) + 1))
            else:
                boundaries = list(range(len(keys) + 1))

            feasible = [cut for cut in boundaries
                        if self._cut_feasible(
                            cut, appends, keys, version_of, known_styles,
                            pins)]
            cut_lo = feasible[0] if feasible else None
            cut_hi = feasible[-1] if feasible else None

            per_cut = {}
            for cut in feasible:
                id_map = {}
                for idx, key in enumerate(keys):
                    if key in conflicted or key not in rows:
                        continue
                    if key in row_style:
                        id_map[key] = row_style[key][1].id()
                        continue
                    info = rows[key]
                    chosen = self._style_at(cut, key, appends)
                    cell = info['candidates'].get(chosen)
                    if cell is None:
                        cell = next(iter(info['candidates'].values()))
                    id_map[key] = cell.id()
                per_cut[cut] = id_map

            row_id_stable = {}
            for key in keys:
                values = set(id_map.get(key) for id_map in per_cut.values())
                values.discard(None)
                row_id_stable[key] = len(values) <= 1

            notes = []
            for key in keys:
                if row_id_stable.get(key, True):
                    continue
                values = sorted(set(id_map[key]
                                    for id_map in per_cut.values()))
                notes.append(
                    '{0}: id depends on the switch point -> {1}'.format(
                        key, ['{0:016x}'.format(value) for value in values]))
            if not feasible:
                notes.append(
                    'no single old->new switch on an append boundary '
                    'satisfies the pinned rows and the caller version '
                    'facts; the single-switch assumption is violated')

            reports.append(SwitchReport(
                source=source,
                appends=tuple(facts.get('appends', ())),
                cut_range=(cut_lo, cut_hi),
                feasible_cuts=tuple(feasible),
                cut_location=self._cut_locations(
                    source, feasible, keys, appends),
                per_cut_ids=per_cut,
                row_id_stable=row_id_stable,
                stable=all(row_id_stable.values()),
                notes=tuple(notes)))
        return reports

    def _expected_styles(self, cut, appends, keys, version_of,
                         known_styles):
        """Map each unit (append or row) to the style imposed by ``cut``."""
        expected = {}
        if appends:
            for index, append in enumerate(appends):
                style = LEGACY if index < cut else STANDARD
                version = version_of.get(append)
                if version is not None and version in known_styles:
                    if known_styles[version] != style:
                        return None
                expected[append] = style
        else:
            for index, key in enumerate(keys):
                expected[key] = LEGACY if index < cut else STANDARD
        return expected

    def _cut_feasible(self, cut, appends, keys, version_of, known_styles,
                      pins):
        expected = self._expected_styles(
            cut, appends, keys, version_of, known_styles)
        if expected is None:
            return False
        for unit, style in expected.items():
            observed = pins.get(unit)
            if observed is not None and style not in observed:
                return False
        return True

    def _style_at(self, cut, key, appends):
        append = self.records[key]['append']
        if appends and append in appends:
            index = appends.index(append)
            return LEGACY if index < cut else STANDARD
        return STANDARD

    def _cut_locations(self, source, feasible, keys, appends):
        if not appends:
            return dict(
                (cut, ('before the first row' if cut == 0
                       else 'after row seq {0}'.format(keys[cut - 1][1])))
                for cut in feasible)
        locations = {}
        for cut in feasible:
            if cut == 0:
                locations[cut] = 'before {0} (already standard)'.format(
                    appends[0])
            elif cut == len(appends):
                locations[cut] = 'after {0} (never switched)'.format(
                    appends[-1])
            else:
                locations[cut] = 'boundary {0} | {1}'.format(
                    appends[cut - 1], appends[cut])
        return locations

    # ------------------------------------------------------------------
    # output assembly
    # ------------------------------------------------------------------

    def _build_outputs(self, resolved, ambiguous, invalids, conflicts):
        outcome_by_key = {}
        for record in resolved:
            outcome_by_key[(record.source, record.seq)] = record
        for record in ambiguous:
            outcome_by_key[(record.source, record.seq)] = record
        for record in invalids:
            outcome_by_key[(record.source, record.seq)] = record

        conflict_members = {}
        for conflict in conflicts:
            for key in conflict.rows:
                conflict_members[key] = conflict
        for key, conflict in conflict_members.items():
            record = self.records[key]
            outcome_by_key[key] = ConflictOutcome(
                key[0], key[1], record['append'], conflict)

        ordered = []
        for source in reversed(self.source_order):
            for seq in sorted(key[1] for key in outcome_by_key
                              if key[0] == source):
                ordered.append(outcome_by_key[(source, seq)])

        return ordered, outcome_by_key


CleanReport = namedtuple(
    'CleanReport',
    'ordered outcomes resolved ambiguous conflicts invalid switch_reports '
    'old_key_map alias_pairs')
