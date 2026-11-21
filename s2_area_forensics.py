#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史 S2 面积报告取证脚本（只读分析，不改动任何面积实现）。

背景
----
旧导出程序对一份报告统一选用一套"计算设置"（运行期间不变）：

- 聚合方式（3 种）
    raw     : 按原始行求面积。小计 = 该段原始行面积和；总计 = 全部原始行面积和。
    segnorm : 先对每个分段的本地覆盖做 CellUnion.normalize 再相加。
              小计 = 该段规整后面积和；总计 = 各段规整结果之和。
    union   : 小计算法与 segnorm 完全相同（只规整该段本地覆盖）；
              总计 = 把全报告所有行做报告级并集规整后再求和。
              => 两种规整模式的差别只体现在总计。
- 面积函数（2 种）: exact_area 或 approx_area，整份报告统一。

分段小计与整份总计各自独立舍入到 6 位小数打印，不能当成无误差的同一个和。
历史报告只留存了打印值（部分缺失，部分为区间舍入值）和分段格清单，设置已遗失。

本脚本对每条记录枚举全部 6 种设置，重算并核对打印值，给出：
  * 每种判断使用的固定源码计算与原始格组合（CellId token 一并打印）；
  * 被排除解释的证据（逐项数值差）；
  * 对每种可能解释，用同一个"精确并集基准"把 覆盖重复偏差 + 面积近似偏差 加回；
  * 仍有歧义时，指出补查哪一项旧数据可区分；找不到就保留"未定"。

第二部分处理进程重启：相邻报告时间有序、共享段末累计格清单；重启位置未记录，
重启前后可各用一套设置（同一次运行内设置不变）。只允许"至多一次重启"的历史，
不把每段猜中的设置拼成旧程序不可能运行的一条历史。

运行: python3 s2_area_forensics.py
"""

from s2sphere import CellId, Cell, CellUnion

DIGITS = 6  # 打印值舍入到小数点后 6 位


def rnd(x):
    return round(x, DIGITS)


# ---------------------------------------------------------------- 固定源码计算

def exact_area(cid):
    return Cell(cid).exact_area()


def approx_area(cid):
    return Cell(cid).approx_area()


AREA_FUN = {'exact': exact_area, 'approx': approx_area}
AGG_MODES = ('raw', 'segnorm', 'union')
AREA_MODES = ('exact', 'approx')
SETTINGS = [(a, m) for a in AGG_MODES for m in AREA_MODES]


def normalize(ids):
    u = CellUnion(list(ids))
    u.normalize()
    return u.cell_ids()


def setting_cells(agg, segments):
    """返回 (各分段用于求小计的格清单, 用于求总计的格清单)。"""
    if agg == 'raw':
        segs = [list(s) for s in segments]
        total = [c for s in segments for c in s]
    elif agg == 'segnorm':
        segs = [normalize(s) for s in segments]
        total = [c for s in segs for c in s]
    else:  # union
        segs = [normalize(s) for s in segments]
        total = normalize([c for s in segments for c in s])
    return segs, total


def compute(setting, segments):
    """按设置重算一份报告：返回 (各段小计列表, 总计)，均已按打印规则舍入。"""
    agg, area = setting
    segs, total = setting_cells(agg, segments)
    fun = AREA_FUN[area]
    subs = [rnd(sum(fun(c) for c in s)) for s in segs]
    tot = rnd(sum(fun(c) for c in total))
    return subs, tot


def baseline_exact_union(segments):
    """唯一精确并集基准：全报告所有行的并集，按 exact_area 求和（不舍入）。"""
    return sum(exact_area(c) for c in normalize([c for s in segments for c in s]))


def decompose(setting, segments):
    """相对精确并集基准拆出两项偏差（不舍入）：
    dup_dev    = 覆盖重复偏差（设置的总计格清单精确面积和 - 基准）
    approx_dev = 面积近似偏差（设置的近似/精确算法结果 - 同一清单精确面积和）
    """
    agg, area = setting
    _, total = setting_cells(agg, segments)
    exact_sum = sum(exact_area(c) for c in total)
    dup_dev = exact_sum - baseline_exact_union(segments)
    approx_dev = sum(AREA_FUN[area](c) for c in total) - exact_sum
    return dup_dev, approx_dev


# ---------------------------------------------------------------- 记录与匹配

def value_ok(computed, printed):
    """printed 为 None（未留存）/ 标量（舍入值）/ (lo, hi)（区间舍入值）。"""
    if printed is None:
        return True
    if isinstance(printed, tuple):
        return printed[0] <= computed <= printed[1]
    return computed == printed


def setting_matches(setting, record):
    subs, tot = compute(setting, record['segments'])
    if not value_ok(tot, record.get('total')):
        return False
    for i, v in record.get('subtotals', {}).items():
        if not value_ok(subs[i], v):
            return False
    return True


def matchset(record):
    return [s for s in SETTINGS if setting_matches(s, record)]


def mismatches(setting, record):
    """逐项列出该设置与留存打印值的矛盾（被排除解释的证据）。"""
    subs, tot = compute(setting, record['segments'])
    out = []
    if not value_ok(tot, record.get('total')):
        out.append('总计 重算=%.6f 留存=%s' % (tot, fmt_val(record.get('total'))))
    for i, v in record.get('subtotals', {}).items():
        if not value_ok(subs[i], v):
            out.append('段%d小计 重算=%.6f 留存=%s' % (i + 1, subs[i], fmt_val(v)))
    return out


def fmt_val(v):
    if v is None:
        return '缺失'
    if isinstance(v, tuple):
        return '[%.6f, %.6f]' % v
    return '%.6f' % v


def fmt_setting(s):
    return '(%s, %s_area)' % (s[0], s[1])


# ---------------------------------------------------------------- 固定原始格组合

MAXL = CellId.MAX_LEVEL


def cell_at(face, pos, level):
    """面上第 pos 个 level 级格（pos 为该层 Hilbert 位置）。"""
    return CellId.from_face_pos_level(face, pos << (2 * (MAXL - level)), level)


# 两组位于不同面位置的父格 + 各自后代/子格（搜索确定，判断可重算）
P1 = cell_at(0, 237, 4)                 # 父格组一：face 0
D1 = cell_at(0, 237 * 1024 + 936, 9)    # P1 的 9 级后代（跨分段重复的那一枚）
Q1 = cell_at(1, 158, 4)                 # 父格组二：face 1
CH_Q1 = list(Q1.children())             # Q1 被完整子格代替

P2 = cell_at(3, 43, 4)                  # 第二套：face 3
D2 = cell_at(3, 43 * 1024 + 790, 9)     # P2 的 9 级后代
Q2 = cell_at(2, 0, 4)                   # face 2
GGC_Q2 = [c3 for c1 in Q2.children() for c2 in c1.children()
          for c3 in c2.children()]      # Q2 的 64 枚曾孙格（完整拆分）

E1 = cell_at(2, 100, 4)                 # 无重叠的独立格（链式报告增量段）
E2 = cell_at(3, 50, 4)
E3 = cell_at(4, 60, 4)
E4 = cell_at(5, 70, 4)
E5 = cell_at(5, 200, 4)                 # R2 中小计未留存的独立段

NOLAP = [cell_at(5, 10, 4), cell_at(5, 20, 4), cell_at(5, 30, 4)]  # 无重叠清单


def show_cells():
    print('固定原始格组合（CellId token）:')
    for name, c in [('P1', P1), ('D1', D1), ('Q1', Q1), ('P2', P2), ('D2', D2),
                    ('Q2', Q2), ('E1', E1), ('E2', E2), ('E3', E3), ('E4', E4),
                    ('E5', E5)]:
        print('  %-2s = face %d level %2d token %s' % (name, c.face(), c.level(),
                                                       c.to_token()))
    print('  CH_Q1 = Q1 的 4 枚完整子格; GGC_Q2 = Q2 的 64 枚曾孙格')
    print('  NOLAP = %s' % [c.to_token() for c in NOLAP])


# ---------------------------------------------------------------- 报告分析输出

def fmt_cells(seg):
    toks = [c.to_token() for c in seg]
    if len(toks) > 5:
        return '%s ... (共%d格)' % (toks[:4], len(toks))
    return str(toks)


def analyze_record(name, record, note):
    print('\n' + '=' * 74)
    print('记录 %s —— %s' % (name, note))
    print('-' * 74)
    for i, seg in enumerate(record['segments']):
        print('  段%d 原始行: %s' % (i + 1, fmt_cells(seg)))
    print('  留存总计: %s' % fmt_val(record.get('total')))
    for i, v in sorted(record.get('subtotals', {}).items()):
        print('  留存段%d小计: %s' % (i + 1, fmt_val(v)))

    feas = matchset(record)
    print('\n  全部 6 种设置重算对照（总计 / 各段小计）:')
    for s in SETTINGS:
        subs, tot = compute(s, record['segments'])
        tag = '  <== 吻合' if s in feas else ''
        print('    %-18s 总计 %.6f 小计 %s%s'
              % (fmt_setting(s), tot, ['%.6f' % x for x in subs], tag))

    print('\n  被排除解释的证据:')
    for s in SETTINGS:
        if s not in feas:
            print('    %-18s 矛盾: %s' % (fmt_setting(s), '; '.join(mismatches(s, record))))

    base = baseline_exact_union(record['segments'])
    print('\n  精确并集基准 B0 = %.9f（全行并集的 exact_area 和）' % base)
    if record.get('total') is not None and record.get('subtotals'):
        total_only = matchset({'segments': record['segments'],
                               'total': record.get('total'), 'subtotals': {}})
        wider = [s for s in total_only if s not in feas]
        if wider:
            print('  只看总计时的可能解释（小计留存前）偏差分解:')
            for s in wider:
                dup, apx = decompose(s, record['segments'])
                print('    %-18s 覆盖重复 %+ .6e  面积近似 %+ .6e  合计 %+.6e'
                      % (fmt_setting(s), dup, apx, dup + apx))
    print('  当前留存值下的可能解释偏差分解（打印总计 ~= B0 + 覆盖重复 + 面积近似）:')
    for s in feas:
        dup, apx = decompose(s, record['segments'])
        print('    %-18s 覆盖重复 %+ .6e  面积近似 %+ .6e  合计 %+.6e'
              % (fmt_setting(s), dup, apx, dup + apx))
    return feas


def disambiguation_advice(record, feas):
    """仍有歧义时：找出能区分解释的旧数据（未留存的小计/总计）；找不到就保留未定。"""
    if len(feas) <= 1:
        return
    print('  歧义未消（%d 种解释）。逐项检查"若补查哪项旧数据可区分":' % len(feas))
    n_seg = len(record['segments'])
    candidates = [('总计', None)] if record.get('total') is None else []
    candidates += [('段%d小计' % (i + 1), i) for i in range(n_seg)
                   if i not in record.get('subtotals', {})]
    best = None
    for label, idx in candidates:
        values = {}
        for s in feas:
            subs, tot = compute(s, record['segments'])
            values[s] = tot if idx is None else subs[idx]
        groups = {}
        for s, v in values.items():
            groups.setdefault(v, []).append(s)
        if len(groups) > 1:
            print('    补查%s 可区分: %s'
                  % (label, '; '.join('%s -> %s' % (fmt_val(v), [fmt_setting(s) for s in ss])
                                      for v, ss in sorted(groups.items()))))
            if best is None or len(groups) > len(best[1]):
                best = (label, groups)
        else:
            print('    补查%s 无用: 所有解释给出相同值 %s'
                  % (label, fmt_val(next(iter(values.values())))))
    residual = feas
    if best is not None:
        # 取补查后仍可能残留的最大解释组
        residual = max(best[1].values(), key=len)
    if len(residual) > 1:
        print('    残留未定: %s —— 这些解释对任何分段小计都给出相同值，'
              '没有可补查的旧数据能再区分，保留未定。'
              % [fmt_setting(s) for s in residual])


# ---------------------------------------------------------------- 第一部分记录

def part1():
    print('\n' + '#' * 74)
    print('# 第一部分：单份报告的设置归因')
    print('#' * 74)

    # 对照组：无重叠清单 —— 三种聚合完全一致；同覆盖不同拆法只影响 approx
    print('\n对照（无重叠清单 NOLAP，单段）:')
    for s in SETTINGS:
        subs, tot = compute(s, [NOLAP])
        print('    %-18s 总计 %.6f' % (fmt_setting(s), tot))
    ex_w = exact_area(Q1)
    ex_s = sum(exact_area(c) for c in CH_Q1)
    ap_w = approx_area(Q1)
    ap_s = sum(approx_area(c) for c in CH_Q1)
    print('对照（同一覆盖的两种拆法: [Q1] vs Q1 的 4 枚完整子格）:')
    print('    exact: %.9f vs %.9f  差 %.2e（拆法不影响精确面积）'
          % (ex_w, ex_s, ex_s - ex_w))
    print('    approx: %.9f vs %.9f  差 %.2e（同样覆盖换个拆法，近似结果就变了）'
          % (ap_w, ap_s, ap_s - ap_w))

    # R1: 只看总计有两种设置吻合；补一个实存小计后唯一
    r1 = {
        'segments': [[P1, D1], [D1] + CH_Q1],
        'total': rnd(exact_area(P1) + exact_area(Q1)),       # (union, exact) 的打印值
        'subtotals': {0: rnd(exact_area(P1))},               # 实存段1小计
    }
    feas1 = analyze_record('R1', r1, '父格+跨分段重复后代 / 父格换完整子格；'
                                     '总计+段1小计留存')
    assert len(matchset({'segments': r1['segments'], 'total': r1['total'],
                         'subtotals': {}})) >= 2, 'R1 只看总计应至少两种吻合'
    assert feas1 == [('union', 'exact')], feas1
    print('  判断: 只看总计有 >=2 种设置吻合；补段1小计后唯一 -> (union, exact_area)')

    # R2: 结合现存小计仍有多种解释，且重复/近似分摊不同
    r2 = {
        'segments': [[P2, D2], GGC_Q2, [E5]],
        'total': None,                                        # 总计未留存
        'subtotals': {0: rnd(exact_area(P2)),                 # (union, exact) 的打印值
                      1: rnd(exact_area(Q2))},
    }
    feas2 = analyze_record('R2', r2, '段内重复后代 / 父格拆成曾孙格；'
                                     '段1、段2小计留存，总计与段3小计缺失')
    assert set(feas2) == {('union', 'exact'), ('segnorm', 'exact'), ('raw', 'approx')}, feas2
    disambiguation_advice(r2, feas2)

    # R3: 看似重复问题，但组合可查数字与所有设置冲突
    aP1, aD1, aQ1 = exact_area(P1), exact_area(D1), exact_area(Q1)
    r3 = {
        'segments': [[P1, D1], [D1] + CH_Q1],
        'total': rnd(aP1 + aQ1),                # 像"并集去重后"的总计
        'subtotals': {0: rnd(aP1 + aD1),        # 段1却像 raw 模式（未去重）
                      1: rnd(aD1 + aQ1)},       # 段2像规整模式（已去重）
    }
    feas3 = analyze_record('R3', r3, '看似重复被去重，实为互相矛盾的记录')
    assert feas3 == [], feas3
    print('  判断: 原始行面积和 = %.6f，打印总计 = %.6f，差值恰像 2 份重复被去掉；'
          % (rnd(aP1 + 2 * aD1 + aQ1), r3['total']))
    print('        但段1小计 %.6f 说明段1未去重（raw 风格），段2小计 %.6f 又是规整风格；'
          % (r3['subtotals'][0], r3['subtotals'][1]))
    print('        逐项单独看都能解释，组合起来与全部 6 种设置冲突 -> 记录本身不一致'
          '（可能拼接自不同运行/手工改动），无法归因。')
    return r1


# ---------------------------------------------------------------- 第二部分：重启链

def fmt_restart(r, k):
    if r is None:
        return '无需重启'
    return '第%d/%d份报告之间' % (r, r + 1)


def chain_disambiguation(reports, histories):
    """找出"只需补查哪一次小计"可消除的剩余歧义，并说明消除不了的轴。"""
    if len(histories) <= 1:
        return
    print('  补查分析（候选: 各报告未留存的分段小计）:')
    best = None
    for ri, rep in enumerate(reports):
        for si in range(len(rep['segments'])):
            if si in rep.get('subtotals', {}):
                continue
            values = {}
            for h in histories:
                setting = h[1] if (h[0] is None or ri < h[0]) else h[2]
                subs, _ = compute(setting, rep['segments'])
                values.setdefault(subs[si], set()).add(h)
            if len(values) > 1:
                print('    报告%d段%d小计 可区分: %s'
                      % (ri + 1, si + 1, '; '.join(
                          '%s -> %d条历史' % (fmt_val(v), len(hs))
                          for v, hs in sorted(values.items()))))
                if best is None or len(values) > len(best[1]):
                    best = ((ri, si), values)
    if best is None:
        print('    没有任何小计能再区分。')
        return
    (ri, si), values = best
    print('    依据: 报告%d段%d小计在不同可行历史下取值不同（见上），'
          '补查它即可按取值归并历史。' % (ri + 1, si + 1))
    residual = max(values.values(), key=len)
    if len(residual) > 1:
        print('    补查后仍残留 %d 条历史: %s'
              % (len(residual), [(fmt_restart(h[0], len(reports)), fmt_setting(h[1]),
                                  fmt_setting(h[2])) for h in residual]))
        # 检查是否所有小计对残留历史都同值
        all_same = True
        for rj, rep in enumerate(reports):
            for sj in range(len(rep['segments'])):
                vals = set()
                for h in residual:
                    setting = h[1] if (h[0] is None or rj < h[0]) else h[2]
                    subs, _ = compute(setting, rep['segments'])
                    vals.add(subs[sj])
                if len(vals) > 1:
                    all_same = False
        if all_same:
            print('    残留历史对任何分段小计都给出相同值（segnorm 与 union 的小计'
                  '算法本就一致，差别只在报告级总计），没有可补查的小计 -> 该轴保留未定。')


def part2(r1_segments):
    print('\n' + '#' * 74)
    print('# 第二部分：进程重启与相邻报告链')
    print('#' * 74)

    # 链一：每份单看都有多种解释；合起来只剩一个重启区间
    g1, g2 = r1_segments                      # A 的两段（含跨分段重复 D1）
    g3, g4, g5, g6 = [E1], [E2], [E3], [E4]
    aP1, aQ1 = exact_area(P1), exact_area(Q1)
    AP1, AQ1 = approx_area(P1), approx_area(Q1)
    AD1 = approx_area(D1)

    repA = {'segments': [g1, g2], 'total': rnd(aP1 + aQ1), 'subtotals': {}}

    def interval(lo_setting_value, hi_setting_value):
        return (rnd(lo_setting_value) - 5e-7, rnd(hi_setting_value) + 5e-7)

    carryB_lo = AP1 + AQ1                     # (union, approx) 的累计值
    carryB_hi = AP1 + AD1 + AQ1               # (segnorm, approx) 的累计值
    totB_lo = carryB_lo + approx_area(E1) + approx_area(E2)
    totB_hi = carryB_hi + approx_area(E1) + approx_area(E2)
    repB = {'segments': [g1, g2, g3, g4],
            'total': interval(totB_lo, totB_hi),
            'subtotals': {2: rnd(approx_area(E1))},
            'carry': interval(carryB_lo, carryB_hi)}

    carryC_lo = totB_lo
    carryC_hi = totB_hi
    totC_lo = carryC_lo + approx_area(E3) + approx_area(E4)
    totC_hi = carryC_hi + approx_area(E3) + approx_area(E4)
    repC = {'segments': [g1, g2, g3, g4, g5, g6],
            'total': interval(totC_lo, totC_hi),
            'subtotals': {4: rnd(approx_area(E3))},
            'carry': interval(carryC_lo, carryC_hi)}

    # 为匹配共享累计值，把 carry 转成对前缀段的"总计"约束
    def expand(reps):
        out = []
        prefix = []
        for rep in reps:
            rep = dict(rep)
            carry = rep.pop('carry', None)
            if carry is not None:
                # 累计覆盖 = 上一份报告的累计格清单（报告是累积的，前缀即上一段末清单）
                rep.setdefault('_extra', []).append((list(prefix), carry))
            prefix = list(rep['segments'])
            out.append(rep)
        return out

    reps1 = expand([repA, repB, repC])

    def matchset_with_extra(rep):
        res = []
        for s in SETTINGS:
            if not setting_matches(s, rep):
                continue
            ok = True
            for segs, printed in rep.get('_extra', []):
                _, tot = compute(s, segs)
                if not value_ok(tot, printed):
                    ok = False
                    break
            if ok:
                res.append(s)
        return res

    # 用带 carry 约束的 matchset 重新算可行历史
    def histories_with_extra(reps):
        """枚举全链可行历史 (重启区间 r, 重启前设置, 重启后设置)。
        r=None 表示无需重启；r=k 表示重启在第 k 与 k+1 份报告之间。
        设置在一次运行内不变 —— 只允许运行级历史，不做逐段拼接。"""
        sets = [matchset_with_extra(r) for r in reps]
        histories = []
        common = set.intersection(*[set(s) for s in sets])
        for s in common:
            histories.append((None, s, s))
        for r in range(1, len(reps)):
            pre = set.intersection(*[set(s) for s in sets[:r]])
            post = set.intersection(*[set(s) for s in sets[r:]])
            for a in pre:
                for b in post:
                    histories.append((r, a, b))
        return histories, sets

    print('\n链一说明: 报告B/C 的总计与上份末累计值只留区间舍入值，段小计部分留存。')
    h1, sets1 = histories_with_extra(reps1)
    print('\n' + '=' * 74)
    print('链 一 —— 每份单看多种解释，全链只剩一个重启区间')
    print('-' * 74)
    for i, rep in enumerate(reps1):
        print('  报告%d 留存: 总计=%s 小计=%s 上份末累计=%s'
              % (i + 1, fmt_val(rep.get('total')),
                 {j + 1: fmt_val(v) for j, v in sorted(rep.get('subtotals', {}).items())},
                 fmt_val(rep['_extra'][0][1]) if rep.get('_extra') else None))
    print('  各报告单独可用的设置:')
    for i, ss in enumerate(sets1):
        print('    报告%d: %s' % (i + 1, [fmt_setting(s) for s in ss]))
    print('  全链可行历史（共 %d 条）:' % len(h1))
    for r, a, b in h1:
        print('    重启[%s]  前=%s  后=%s' % (fmt_restart(r, 3), fmt_setting(a),
                                              fmt_setting(b)))
    assert h1 and all(h[0] == 1 for h in h1), h1

    def attr(histories, label, key):
        vals = {key(h) for h in histories}
        tag = '共有（确定）' if len(vals) == 1 else '仍不确定'
        show = next(iter(vals)) if len(vals) == 1 else sorted(str(v) for v in vals)
        print('    %-12s %-10s %s' % (label, tag, show))

    print('  逐项归因:')
    attr(h1, '重启区间', lambda h: fmt_restart(h[0], 3))
    attr(h1, '重启前设置', lambda h: fmt_setting(h[1]))
    attr(h1, '重启后设置', lambda h: fmt_setting(h[2]))
    chain_disambiguation(reps1, h1)

    # 链二：每份的总计/小计各自可解释，但任何重启位置都解释不了共享累计值
    aD1 = exact_area(D1)
    aE1, aE2, aE3, aE4 = map(exact_area, (E1, E2, E3, E4))
    repA2 = {'segments': [g1, g2], 'total': rnd(aP1 + aQ1), 'subtotals': {}}
    repB2 = {'segments': [g1, g2, g3, g4],
             'total': rnd(aP1 + aD1 + aQ1 + aE1 + aE2),      # (segnorm, exact)
             'subtotals': {2: rnd(aE1)},
             'carry': rnd(aP1 + aD1 + aQ1)}                 # 共享累计值: segnorm 口径
    repC2 = {'segments': [g1, g2, g3, g4, g5, g6],
             'total': rnd(aP1 + aQ1 + aE1 + aE2 + aE3 + aE4),  # (union, exact)
             'subtotals': {4: rnd(aE3)},
             'carry': rnd(aP1 + aQ1 + aE1 + aE2)}           # 共享累计值: union 口径
    reps2 = expand([repA2, repB2, repC2])
    h2, sets2 = histories_with_extra(reps2)
    print('\n' + '=' * 74)
    print('链 二 —— 总计/小计各自可解释，但共享累计值无法被任何重启位置解释')
    print('-' * 74)
    for i, rep in enumerate(reps2):
        print('  报告%d 留存: 总计=%s 小计=%s 上份末累计=%s'
              % (i + 1, fmt_val(rep.get('total')),
                 {j + 1: fmt_val(v) for j, v in sorted(rep.get('subtotals', {}).items())},
                 fmt_val(rep['_extra'][0][1]) if rep.get('_extra') else None))
    print('  各报告单独可用的设置:')
    for i, ss in enumerate(sets2):
        print('    报告%d: %s' % (i + 1, [fmt_setting(s) for s in ss]))
    print('\n  共享累计值核对:')
    carry_diff = (aP1 + aD1 + aQ1) - (aP1 + aQ1)
    print('    报告1总计(=段1-2累计) = %.6f；报告2留存的同一份累计 = %.6f'
          % (repA2['total'], repB2['carry']))
    print('    未舍入差值 = %.6e = 恰为 D1 的 exact_area %.6e（重复后代的面积）：'
          '两份报告对同一份累计覆盖用了不同的去重口径。' % (carry_diff, aD1))
    print('    报告2末累计(segnorm 口径) 与报告3留存累计(union 口径)又差同一份 D1：'
          '口径变回去需要第二次重启。')
    print('  结论: 可行历史数 = %d —— 不重启、重启于 1/2 之间、重启于 2/3 之间'
          '全部矛盾；记录链不一致（疑似拼接自不同运行），无法归因。' % len(h2))
    assert h2 == [], h2


def main():
    show_cells()
    r1 = part1()
    part2(r1['segments'])
    print('\n全部自检断言通过。')


if __name__ == '__main__':
    main()
