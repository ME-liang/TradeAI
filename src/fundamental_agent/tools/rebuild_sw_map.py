"""重建申万一级行业映射缓存（独立工具，修复同业对比为空）。

背景：`data/_cache/sw_l1_map.json` 若在某次构建时部分行业失败，会留下残缺
映射（<4000 条），且 7 天 TTL 内一直被复用——导致大量公司 sw_map miss、
「04 行业坐标」同业对比整模块为空（实测 600519 贵州茅台 miss）。

用法（在申万指数源可用时运行，约 1-3 分钟）：
    python -m fundamental_agent.tools.rebuild_sw_map

原理与 `_load_sw_l1_map` 的自检一致：重建结果与旧缓存取更大者写回；
重建完成后 7 天内报告生成不再触发上游请求。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fundamental_agent.data.akshare import AkShareClient, _SW_L1_CACHE, _CACHE_DIR  # noqa: E402


def main() -> int:
    old: dict = {}
    if _SW_L1_CACHE.exists():
        try:
            old = json.loads(_SW_L1_CACHE.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    print(f"[rebuild] 旧缓存条数: {len(old)}")

    client = AkShareClient()
    mapping = client._build_sw_l1_map()
    print(f"[rebuild] 重建条数: {len(mapping)}")

    best = mapping if len(mapping) > len(old) else old
    if len(best) < 4000:
        print(
            f"[rebuild] ⚠️ 重建后仍只有 {len(best)} 条（<4000，申万源可能不稳定），"
            "建议稍后（如交易时段外）重跑本命令。已保留较大的一份。"
        )
    if best:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _SW_L1_CACHE.write_text(
            json.dumps(best, ensure_ascii=False), encoding="utf-8"
        )
    print(f"[rebuild] 写回 {len(best)} 条 → {_SW_L1_CACHE}")
    probe = ["600519", "000062", "300184", "688825"]
    for c in probe:
        print(f"  {c}: {best.get(c)}")
    return 0 if len(best) >= 4000 else 1


if __name__ == "__main__":
    raise SystemExit(main())
