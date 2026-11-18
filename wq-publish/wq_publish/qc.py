"""平行样品质控：配对 CSV 校验、相对偏差计算与最少待核查键集合。

业务规则
--------
* 配对 CSV 四列（表头兼容中英文）：样本甲、样本乙、检测项目、允许相对偏差；
  最多 60 对，涉及最多 24 个不同的（样本号、检测项目）键；
* 两样本不能相同；同项目的无序配对不可重复；偏差限值须为非负有限数；
* 相对偏差：两浓度均为 0 时偏差为 0，否则
  ``2 × |甲 − 乙| / (甲 + 乙)``（结果为 0..1 的比率，页面按百分比展示）；
* 偏差严格大于限值的配对至少核查一端（构建“超限边”上的最小顶点覆盖）；
* 任一端在所选版本缺失时，该（样本号、检测项目）键必须核查，不按 0 计算。

若最少核查集合存在多个等优解，按键排序后的序列取字典序最小者。
"""
from __future__ import annotations

import csv
import io
import math
from functools import lru_cache

MAX_PAIRS = 60
MAX_DISTINCT_KEYS = 24
MAX_ERRORS = 50

CANONICAL_PAIR_HEADER = ["sample_a", "sample_b", "analyte", "limit"]

# 兼容中英文表头（统一转小写后匹配）
_PAIR_HEADER_ALIASES = {
    "样本甲": "sample_a",
    "甲样本": "sample_a",
    "sample_a": "sample_a",
    "samplea": "sample_a",
    "样本a": "sample_a",
    "样本乙": "sample_b",
    "乙样本": "sample_b",
    "sample_b": "sample_b",
    "sampleb": "sample_b",
    "样本b": "sample_b",
    "检测项目": "analyte",
    "项目": "analyte",
    "analyte": "analyte",
    "item": "analyte",
    "允许相对偏差": "limit",
    "允许偏差": "limit",
    "偏差限值": "limit",
    "相对偏差限值": "limit",
    "limit": "limit",
    "tolerance": "limit",
    "allowed_relative_deviation": "limit",
}


class QCValidationError(Exception):
    """配对 CSV 校验失败，messages 为带行号的中文错误。"""

    def __init__(self, messages: list[str]):
        super().__init__("；".join(messages))
        self.messages = messages


def validate_pair_csv(raw_bytes: bytes) -> list[dict]:
    """校验配对 CSV，成功时返回配对记录列表（保留上传顺序）。

    每条记录为 ``{"sample_a","sample_b","analyte","limit","line_no"}``，
    其中 limit 为非负有限 float。失败抛出 :class:`QCValidationError`。
    """
    try:
        text = raw_bytes.decode("utf-8-sig")  # 兼容 BOM
    except UnicodeDecodeError:
        raise QCValidationError(["CSV 文件必须为 UTF-8 编码"])
    if not text.strip():
        raise QCValidationError(["CSV 文件内容为空"])

    try:
        table = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise QCValidationError([f"CSV 解析失败：{exc}"])
    if not table:
        raise QCValidationError(["CSV 文件内容为空"])

    header = [cell.strip().lower() for cell in table[0]]
    col_index: dict[str, int] = {}
    for idx, name in enumerate(header):
        canon = _PAIR_HEADER_ALIASES.get(name)
        if canon and canon not in col_index:
            col_index[canon] = idx
    missing = [c for c in CANONICAL_PAIR_HEADER if c not in col_index]
    if missing:
        raise QCValidationError([
            "表头缺少必要列：样本甲 / 样本乙 / 检测项目 / 允许相对偏差"
            f"（当前表头：{', '.join(table[0])}）"
        ])

    errors: list[str] = []
    pairs: list[dict] = []
    seen_unordered: set[tuple[str, str, str]] = set()
    distinct_keys: set[tuple[str, str]] = set()

    for line_no, record in enumerate(table[1:], start=2):
        if not any(cell.strip() for cell in record):
            continue  # 跳过整行空白
        if len(record) != len(header):
            errors.append(f"第 {line_no} 行：列数为 {len(record)}，应为 {len(header)}")
            if len(errors) >= MAX_ERRORS:
                break
            continue

        sample_a = record[col_index["sample_a"]].strip()
        sample_b = record[col_index["sample_b"]].strip()
        analyte = record[col_index["analyte"]].strip()
        limit_text = record[col_index["limit"]].strip()
        row_ok = True

        if not sample_a:
            errors.append(f"第 {line_no} 行：样本甲为空")
            row_ok = False
        if not sample_b:
            errors.append(f"第 {line_no} 行：样本乙为空")
            row_ok = False
        if not analyte:
            errors.append(f"第 {line_no} 行：检测项目为空")
            row_ok = False
        if not limit_text:
            errors.append(f"第 {line_no} 行：允许相对偏差为空")
            row_ok = False

        limit = None
        if limit_text:
            try:
                limit = float(limit_text)
            except ValueError:
                errors.append(f"第 {line_no} 行：允许相对偏差“{limit_text}”不是合法数字")
                row_ok = False
            else:
                if math.isnan(limit) or math.isinf(limit):
                    errors.append(f"第 {line_no} 行：允许相对偏差不能为 NaN 或无穷大")
                    row_ok = False
                elif limit < 0:
                    errors.append(f"第 {line_no} 行：允许相对偏差 {limit} 为负数，必须 ≥ 0")
                    row_ok = False

        if sample_a and sample_b and analyte:
            if sample_a == sample_b:
                errors.append(f"第 {line_no} 行：样本甲与样本乙不能相同（{sample_a}）")
                row_ok = False
            else:
                unordered = (min(sample_a, sample_b), max(sample_a, sample_b), analyte)
                if unordered in seen_unordered:
                    errors.append(
                        f"第 {line_no} 行：{analyte} 的配对 {unordered[0]} / "
                        f"{unordered[1]} 与前面的行重复（同项目无序配对不可重复）"
                    )
                    row_ok = False
                else:
                    seen_unordered.add(unordered)
            distinct_keys.add((sample_a, analyte))
            distinct_keys.add((sample_b, analyte))

        if row_ok:
            pairs.append({
                "sample_a": sample_a,
                "sample_b": sample_b,
                "analyte": analyte,
                "limit": limit,
                "line_no": line_no,
            })
        if len(errors) >= MAX_ERRORS:
            errors.append(f"错误过多，仅展示前 {MAX_ERRORS} 条，请修正后重新上传")
            break

    if errors:
        raise QCValidationError(errors)
    if not pairs:
        raise QCValidationError(["CSV 中没有任何有效数据行"])
    if len(pairs) > MAX_PAIRS:
        raise QCValidationError([
            f"配对数量为 {len(pairs)}，超过上限 {MAX_PAIRS} 对"
        ])
    if len(distinct_keys) > MAX_DISTINCT_KEYS:
        raise QCValidationError([
            f"涉及 {len(distinct_keys)} 个不同的（样本号、检测项目）键，"
            f"超过上限 {MAX_DISTINCT_KEYS} 个"
        ])
    return pairs


def relative_deviation(conc_a: float, conc_b: float) -> float:
    """相对偏差：两浓度均为 0 时为 0，否则 2×|甲−乙|/(甲＋乙)。"""
    total = conc_a + conc_b
    if total == 0:
        return 0.0
    return 2.0 * abs(conc_a - conc_b) / total


def minimum_vertex_cover(
    vertices: list,
    edges: list[tuple],
    mandatory: set | None = None,
) -> list:
    """最小顶点覆盖；mandatory 中的顶点必须入选。

    若存在多个等优解，返回按顶点自然顺序排序后字典序最小的那个
    （先比首位元素、再依次向后比较，短序列前缀相同时短者更小）。
    顶点须可排序、可哈希；顶点规模按本业务上限不超过 24。

    实现：先用 FPT 分支定界求出最小覆盖大小 k，再在“最多 k 个”的预算下
    按自然顺序做“优先入选、其次排除”的 DFS。include 先于 exclude，保证
    一旦某个小索引顶点能出现在某个最小覆盖里，就不会被无谓排除，从而
    得到字典序最小的最小覆盖。
    """
    mandatory = set(mandatory or ())
    ordered = sorted(set(vertices))
    index = {v: i for i, v in enumerate(ordered)}
    n = len(ordered)
    if n == 0:
        return []

    full_mask = (1 << n) - 1
    forced = {index[v] for v in mandatory if v in index}
    chosen_mask = 0
    for i in forced:
        chosen_mask |= 1 << i

    adj_mask = [0] * n
    for u, v in edges:
        iu, iv = index[u], index[v]
        if iu == iv:
            continue  # 自环不应出现（两样本不能相同），防御性跳过
        adj_mask[iu] |= 1 << iv
        adj_mask[iv] |= 1 << iu
    # 必选顶点关联的边视为已覆盖，从剩余图中移除
    remaining_adj = [adj & (full_mask ^ chosen_mask) for adj in adj_mask]

    def _strip_isolated(free_mask: int) -> int:
        """移除剩余图中的孤立顶点（边两端都仍未决定才算数）。"""
        result = free_mask
        bits = free_mask
        while bits:
            b = bits & -bits
            i = b.bit_length() - 1
            if not (remaining_adj[i] & result):
                result ^= b
            bits ^= b
        return result

    @lru_cache(maxsize=None)
    def _feasible(free_mask: int, size: int) -> bool:
        """剩余未决定顶点 free_mask 是否存在不超过 size 的顶点覆盖。"""
        if free_mask == 0:
            return True
        if size == 0:
            return False
        # 选度数最大的顶点分支（减小递归深度）
        best_i = -1
        best_deg = -1
        bits = free_mask
        while bits:
            b = bits & -bits
            i = b.bit_length() - 1
            deg = (remaining_adj[i] & free_mask).bit_count()
            if deg > best_deg:
                best_deg, best_i = deg, i
            bits ^= b
        if best_deg <= 0:
            return True  # 全是孤立点
        i = best_i
        if _feasible(_strip_isolated(free_mask ^ (1 << i)), size - 1):
            return True
        neighbors = remaining_adj[i] & free_mask
        neighbor_count = neighbors.bit_count()
        if neighbor_count > size:
            return False
        rest = free_mask ^ (1 << i) ^ neighbors
        return _feasible(_strip_isolated(rest), size - neighbor_count)

    free_full = _strip_isolated(full_mask ^ chosen_mask)

    # 第一阶段：求最小覆盖大小 k
    k = None
    for candidate in range(free_full.bit_count() + 1):
        if _feasible(free_full, candidate):
            k = candidate
            break
    assert k is not None

    # 第二阶段：include 优先的 DFS，预算固定为 k，找字典序最小解。
    @lru_cache(maxsize=None)
    def search(free_mask: int, budget: int):
        """返回剩余图上不超过 budget 的最小覆盖（frozenset 索引），无解返回 None。"""
        if free_mask == 0:
            return frozenset()
        if budget == 0:
            return None
        # 最小未决定顶点：保证按自然顺序做 include/exclude 决策
        low_bit = free_mask & -free_mask
        i = low_bit.bit_length() - 1
        neighbor_mask = remaining_adj[i] & free_mask

        # 分支 1：i 入选（优先），字典序更小
        rest_in = _strip_isolated(free_mask ^ low_bit)
        sub_in = search(rest_in, budget - 1)
        if sub_in is not None:
            return sub_in | {i}

        # 分支 2：i 不选 → 其所有邻居必须入选
        neighbor_count = neighbor_mask.bit_count()
        if neighbor_count <= budget:
            rest_ex = _strip_isolated(free_mask ^ low_bit ^ neighbor_mask)
            sub_ex = search(rest_ex, budget - neighbor_count)
            if sub_ex is not None:
                forced_nb = set()
                nb = neighbor_mask
                while nb:
                    b = nb & -nb
                    forced_nb.add(b.bit_length() - 1)
                    nb ^= b
                return sub_ex | forced_nb
        return None

    tail = search(free_full, k)
    assert tail is not None
    final_indices = forced | set(tail)
    return [ordered[i] for i in sorted(final_indices)]


def analyze_pairs(pairs: list[dict], version_rows: list[dict]) -> dict:
    """基于指定版本的检测结果分析全部配对。

    返回结构：
    ``{"results": [...逐对结果...], "violation_edges": [...],
       "mandatory_keys": set, "check_keys": 排序后的最少核查键列表}``。

    缺失键强制核查且不参与偏差计算；超限配对的两端构成顶点覆盖问题的边。
    """
    values: dict[tuple[str, str], float] = {
        (r["sample_no"], r["analyte"]): r["concentration"] for r in version_rows
    }

    results: list[dict] = []
    mandatory_keys: set[tuple[str, str]] = set()
    edges: list[tuple[tuple[str, str], tuple[str, str]]] = []

    for pair in pairs:
        key_a = (pair["sample_a"], pair["analyte"])
        key_b = (pair["sample_b"], pair["analyte"])
        missing_a = key_a not in values
        missing_b = key_b not in values

        item: dict = {
            **pair,
            "key_a": key_a,
            "key_b": key_b,
            "missing_a": missing_a,
            "missing_b": missing_b,
            "conc_a": None if missing_a else values[key_a],
            "conc_b": None if missing_b else values[key_b],
            "deviation": None,
            "exceeded": False,
            "status": "",
            "reason": "",
        }

        if missing_a or missing_b:
            miss_labels = []
            if missing_a:
                miss_labels.append(f"样本甲 {key_a[0]} 的 {key_a[1]} 在所选版本缺失")
                mandatory_keys.add(key_a)
            if missing_b:
                miss_labels.append(f"样本乙 {key_b[0]} 的 {key_b[1]} 在所选版本缺失")
                mandatory_keys.add(key_b)
            item["status"] = "missing"
            item["reason"] = "；".join(miss_labels) + "（该键必须核查，不按 0 计算）"
        else:
            deviation = relative_deviation(item["conc_a"], item["conc_b"])
            item["deviation"] = deviation
            item["exceeded"] = deviation > pair["limit"]
            if item["exceeded"]:
                item["status"] = "exceeded"
                item["reason"] = (
                    f"相对偏差 {deviation:.6g} 超过限值 {pair['limit']:.6g}，"
                    "至少核查一端"
                )
                edges.append((key_a, key_b))
            else:
                item["status"] = "ok"
                item["reason"] = (
                    f"相对偏差 {deviation:.6g} 未超过限值 {pair['limit']:.6g}"
                )
        results.append(item)

    # 顶点只取超限边涉及的键；必选的缺失键可能不在任何超限边上
    edge_vertices = {k for edge in edges for k in edge}
    cover = minimum_vertex_cover(
        sorted(edge_vertices), edges, mandatory=mandatory_keys & edge_vertices
    )
    check_set = set(cover) | mandatory_keys
    check_keys = sorted(check_set)

    return {
        "results": results,
        "violation_edges": edges,
        "mandatory_keys": mandatory_keys,
        "check_keys": check_keys,
    }
