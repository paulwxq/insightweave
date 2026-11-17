"""pandas DataFrame 构造行为探针（frontend/app.py 的 pd.DataFrame(rows, columns=cols) 模式）。"""
import pandas as pd

cases = {
    "basic": ([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], ["a", "b"]),
    "none_values": ([{"a": 1, "b": None}, {"a": None, "b": "y"}], ["a", "b"]),
    "mixed_rows_lists": ([["2026-10-01", 100, "paid"], ["2026-10-02", 200, "cancelled"]], ["dt", "amount", "status"]),
    "empty_rows": ([], ["dt", "amount"]),
    "date_like_strings": ([{"dt": "2026-10-01"}, {"dt": "2026-10-02"}], ["dt"]),
}
print("pandas", pd.__version__)
for name, (rows, cols) in cases.items():
    df = pd.DataFrame(rows, columns=cols)
    print(f"{name}: shape={df.shape} dtypes={dict(df.dtypes.astype(str))} cols={list(df.columns)}")
    if name == "none_values":
        print("  isna:", df.isna().sum().to_dict())
