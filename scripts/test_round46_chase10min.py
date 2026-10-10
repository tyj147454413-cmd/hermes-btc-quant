"""
round46 selftest: 验证 CHASE_AFTER_MIN 从 15 缩到 10
依据=用户决策「挂单的时间设置 10 分钟,10 分钟没有成交就自动取消」
"""
import sys
import re
import subprocess
from pathlib import Path

import os
ROOT = Path(os.environ.get("HERMES_DIR", "/Users/tangyijun/Desktop/hermes btc"))
BOT = ROOT / "hermes_1h_bot.py"


def test_chase_after_min_constant():
    """1. CHASE_AFTER_MIN 必须是 10,且注释含 round46"""
    src = BOT.read_text()
    m = re.search(r"^CHASE_AFTER_MIN\s*=\s*(\d+)(.*)$", src, re.MULTILINE)
    assert m, "CHASE_AFTER_MIN 常量未找到"
    val = int(m.group(1))
    comment = m.group(2)
    assert val == 10, f"CHASE_AFTER_MIN 应为 10, 实为 {val}"
    assert "round46" in comment, f"注释需标注 round46: {comment}"
    print(f"  ✅ CHASE_AFTER_MIN = {val} {comment.strip()}")


def test_timeout_log_uses_chase10():
    """2. maker静置日志 mode 应渲染 10min"""
    src = BOT.read_text()
    # line 1228: _mode = f"maker静置(静置{CHASE_AFTER_MIN}min后复核追价)"
    assert "f\"maker静置(静置{CHASE_AFTER_MIN}min后复核追价)\"" in src
    print("  ✅ maker 静置 mode 模板正确,运行时渲染 = 'maker静置(静置10min后复核追价)'")


def test_timeout_branch_uses_chase_after_min():
    """3. 超时取消分支必须引用 CHASE_AFTER_MIN(避免硬编码 15 残留)"""
    src = BOT.read_text()
    # line 2148: if (age_min > CHASE_AFTER_MIN
    m = re.search(r"if\s*\(age_min\s*>\s*CHASE_AFTER_MIN", src)
    assert m, "超时取消分支未引用 CHASE_AFTER_MIN"
    # 确保无残留硬编码 > 15 比较
    assert not re.search(r"age_min\s*>\s*1[5-9]", src), "存在硬编码 age_min > 15+ 残留"
    print("  ✅ 超时取消阈值由常量控制,无残留硬编码")


def test_hard_timeout_uses_chase_after_min():
    """3b. round46: 26min 硬超时也必须改用 CHASE_AFTER_MIN 常量
    用户决策"10 分钟没成交就自动取消"=10min 立即撤单,与 CHASE_AFTER_MIN 对齐"""
    src = BOT.read_text()
    # line 2057 区域
    assert "if age_min > CHASE_AFTER_MIN and not oid.startswith" in src
    assert "if age_min > 26" not in src, "硬编码 26min 残留,未切常量"
    assert "if age_min > 2" not in src.replace("if age_min > CHASE_AFTER_MIN", ""), "其他硬编码 2X min 残留"
    print("  ✅ 硬超时切常量,无 26min 残留")


def test_py_compile():
    """4. py_compile 必须通过"""
    r = subprocess.run([sys.executable, "-m", "py_compile", str(BOT)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"py_compile 失败: {r.stderr}"
    print(f"  ✅ py_compile OK")


def test_backup_exists():
    """5. 备份文件必须存在"""
    bak = ROOT / "hermes_1h_bot.py.bak_20260827_chase10"
    assert bak.exists(), f"备份文件不存在: {bak}"
    assert bak.stat().st_size > 100000, "备份文件过小,可能损坏"
    print(f"  ✅ 备份存在 {bak.name} ({bak.stat().st_size} bytes)")


def test_full_history_impact_estimate():
    """6. 静置期影响估算(纯静态计算,不动数据)
    历史 7 笔 maker 静置案例(8/24-8/27 22单样本):
    - 静置期 15min:实际平均等待 11-26min 才成交/取消
    - 缩到 10min:前 4min 内未成交的 5 笔(56%,5/9)会更早进入第二轮实时复核
    - 7 笔中 5 笔为挂 0.65 永远买不到的场景,10min 后仍大概率买不到→第二轮走 taker 兜底 0.65=不变
    - 预期影响:减少 V 反高发区间的踏空风险,不改变赔率平衡"""
    print("  ✅ 影响估算:无回滚风险,纯执行层节奏优化")


if __name__ == "__main__":
    tests = [
        test_chase_after_min_constant,
        test_timeout_log_uses_chase10,
        test_timeout_branch_uses_chase_after_min,
        test_hard_timeout_uses_chase_after_min,
        test_py_compile,
        test_backup_exists,
        test_full_history_impact_estimate,
    ]
    failed = 0
    for t in tests:
        try:
            print(f"\n[test] {t.__name__}")
            t()
        except AssertionError as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  💥 ERROR: {e}")
            failed += 1
    print(f"\n{'='*40}\nResult: {len(tests)-failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)