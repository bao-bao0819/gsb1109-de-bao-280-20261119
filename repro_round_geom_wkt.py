# -*- coding: utf-8 -*-
"""
复现：geomet round_geom + WKT 两位小数输出导致线段退化（零长度边/顶点合并）。

三条数据：
  CASE-MAIN  合法多边形，内环有相邻顶点 x=1.0000 与 x=1.0051（y 相同），
             外环与内环之间有间隔。
  CASE-DUP   对照：外环原本就重复一个内部顶点。
  CASE-UNCL  对照：外环缺最后的闭合点，但首尾很接近（0.004 / 0.0049），
             按两位小数写出恰好变成相同坐标。

两条导出路径：
  PATH-A  直接 wkt.dumps(geom, decimals=2)
  PATH-B  先 util.round_geom(geom, 3)，再 wkt.dumps(..., decimals=2)

每条路径分三阶段观察：
  阶段1 进 WKT 前的几何对象（含 round_geom 的中间对象）
  阶段2 写出的 WKT 文本
  阶段3 wkt.loads 读回的坐标
"""
import copy
import json

from geomet import util, wkt

# ---------------------------------------------------------------
# 小数据
# ---------------------------------------------------------------
CASE_MAIN = {
    'type': 'Polygon',
    'coordinates': [
        # 外环（ring 0）：与内环间隔 >= 1.0
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],
        # 内环（ring 1）：顶点3 (1.0051, 8.0) 与顶点4 (1.0000, 8.0) 相邻、纵坐标相同
        [(1.0, 2.0), (8.0, 2.0), (8.0, 8.0), (1.0051, 8.0), (1.0, 8.0),
         (1.0, 2.0)],
    ],
}

CASE_DUP = {
    'type': 'Polygon',
    'coordinates': [
        # 外环：顶点2 与顶点3 原本就是重复的 (5.0, 5.0)
        [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (5.0, 5.0), (0.0, 5.0),
         (0.0, 0.0)],
    ],
}

CASE_UNCL = {
    'type': 'Polygon',
    'coordinates': [
        # 外环：缺闭合点；首 (0.004, 0.0) 尾 (0.0049, 0.0) 很接近，
        # 两位小数下都写成 0.00
        [(0.004, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0049, 0.0)],
    ],
}

CASES = [('CASE-MAIN', CASE_MAIN), ('CASE-DUP', CASE_DUP),
         ('CASE-UNCL', CASE_UNCL)]


# ---------------------------------------------------------------
# 独立观察点（全部作用在原坐标/读回坐标上，可定位到环与顶点）
# ---------------------------------------------------------------
def find_repeat_vertices(coords):
    """相邻重复顶点（零长度边）。返回 [(ring_idx, vertex_idx_of_2nd, pt)]。"""
    hits = []
    for r, ring in enumerate(coords):
        for i in range(1, len(ring)):
            if tuple(ring[i]) == tuple(ring[i - 1]):
                hits.append((r, i, tuple(ring[i])))
    return hits


def find_unclosed_rings(coords):
    """首尾不相等的环。返回 [(ring_idx, first, last)]。"""
    hits = []
    for r, ring in enumerate(coords):
        if tuple(ring[0]) != tuple(ring[-1]):
            hits.append((r, tuple(ring[0]), tuple(ring[-1])))
    return hits


def find_collapsing_edges(coords, decimals=2):
    """在原坐标上预测：两端点 round 到 decimals 位后落到同一网格点的边，
    导出后必然退化为零长度边。返回 [(ring_idx, vertex_idx_of_2nd, p1, p2)]。"""
    hits = []
    for r, ring in enumerate(coords):
        for i in range(1, len(ring)):
            a = tuple(round(v, decimals) for v in ring[i - 1])
            b = tuple(round(v, decimals) for v in ring[i])
            if a == b:
                hits.append((r, i, tuple(ring[i - 1]), tuple(ring[i])))
    return hits


def report_geom(label, geom):
    coords = geom['coordinates']
    print('    %s' % label)
    for r, ring in enumerate(coords):
        print('      ring %d: %s' % (r, json.dumps(ring)))
    reps = find_repeat_vertices(coords)
    uncl = find_unclosed_rings(coords)
    coll = find_collapsing_edges(coords, 2)
    for r, i, pt in reps:
        print('      [异常] 相邻重复顶点: ring %d, vertex %d, %s' % (r, i, pt))
    for r, f, l in uncl:
        print('      [异常] 环未闭合: ring %d, first=%s, last=%s' % (r, f, l))
    for r, i, a, b in coll:
        print('      [预警] 两位网格下将合并的边: ring %d, vertex %d, '
              '%s -> %s' % (r, i, a, b))
    if not (reps or uncl or coll):
        print('      [正常] 无重复顶点、环闭合、两位网格下无边合并')
    return reps, uncl, coll


def run_path(case_name, geom, path):
    print('  -- %s --' % path)
    if path == 'PATH-A 直接写两位小数':
        stage1 = geom
    else:
        stage1 = util.round_geom(geom, 3)
        report_geom('阶段1b round_geom(geom, 3) 后的中间对象:', stage1)
    text = wkt.dumps(stage1, decimals=2)
    print('    阶段2 写出的 WKT 文本:')
    print('      %s' % text)
    back = wkt.loads(text)
    report_geom('阶段3 wkt.loads 读回的对象:', back)
    return text, back


def main():
    for name, geom in CASES:
        print('=' * 78)
        print('%s' % name)
        print('=' * 78)
        snapshot = copy.deepcopy(geom)
        report_geom('阶段1 原始对象（进 WKT 前）:', geom)

        text_a, _ = run_path(name, geom, 'PATH-A 直接写两位小数')
        text_b, _ = run_path(name, geom, 'PATH-B 先 round_geom 到三位再写两位')

        # 普通只写文本，原对象是否改变
        assert geom == snapshot, '原对象被修改!'
        print('  [检查] dumps 写出文本后，原对象与写前逐值相等: True '
              '(wkt.dumps 不修改原对象)')
        rounded = util.round_geom(geom, 3)
        print('  [检查] round_geom 返回新对象，原对象仍未变: %s'
              % (geom == snapshot and rounded is not geom))
        print()


if __name__ == '__main__':
    main()
