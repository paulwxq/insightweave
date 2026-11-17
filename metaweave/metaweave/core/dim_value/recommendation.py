"""维度值字段推荐的共享校验（doc 7.1 §5.2）。

JSON 合并、Dim 生成与配置读取接口共用同一口径：推荐列表仅允许 0 或 1 个
元素，字段必须真实存在于画像 `column_profiles` 键中；非空推荐必须携带
0～1 的有限数值置信度。任何非法形态整组清空为 `[]` 与 `None`，不部分
接纳、不截取首项，也不为缺失置信度补默认分数。
"""

import math
from typing import Any, Collection, List, Optional, Tuple


def validate_dimension_value_recommendation(
    raw_columns: Any,
    raw_confidence: Any,
    column_names: Collection[str],
) -> Tuple[List[str], Optional[float], Optional[str]]:
    """校验维度值字段推荐；返回 (columns, confidence, invalid_reason)。

    合法无推荐与合法单字段推荐的 reason 为 None；整组清空时
    columns=[]、confidence=None、reason 说明非法原因（供调用方记日志）。
    """

    if raw_columns is None:
        # 缺失即无推荐（doc 7.1 §4.2），不视为非法
        return [], None, None
    if not isinstance(raw_columns, list):
        return [], None, "dimension_value_columns 不是数组"
    if not raw_columns:
        # 空列表统一置信度为 null，忽略同时给出的任何分数
        return [], None, None
    if len(raw_columns) > 1:
        return [], None, "推荐字段多于一个"
    column = raw_columns[0]
    if not isinstance(column, str):
        return [], None, "推荐字段不是字符串"
    if column not in column_names:
        return [], None, f"推荐字段不在画像字段中: {column}"
    confidence = _valid_confidence(raw_confidence)
    if confidence is None:
        return [], None, "非空推荐缺少合法置信度"
    return [column], confidence, None


def _valid_confidence(raw: Any) -> Optional[float]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    # 先比较再转换：10**400 等超大整数 int 可比较，但 float() 会抛
    # OverflowError，须按非法置信度整组清空而不是中断校验
    if not 0 <= raw <= 1:
        return None
    value = float(raw)
    if not math.isfinite(value):
        return None
    return value
