"""泵 UO 脉冲测试：开泵 → 等待 → 停泵（经 scara_do.exe）。

前提：关掉官方 SCARA / snrobot serve（控制器通常只认一条连接）。

用法:
  python tools\\test_scara_pump_pulse.py              # UO1 开 2s 再关
  python tools\\test_scara_pump_pulse.py 1 3          # UO1 开 3s 再关

开/关对应 UO 电平 0 或 1，见 do_client.py 顶部 UO_LEVEL_ON / UO_LEVEL_OFF。
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

_CLIENT = Path(__file__).resolve().parents[1] / "src" / "scara" / "controller" / "do_client.py"


def _load_client():
    spec = importlib.util.spec_from_file_location("do_client", _CLIENT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    hold_s = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    if hold_s < 0:
        print("ERR 保持时间不能为负")
        return 1

    client = _load_client()
    on_lv, off_lv = client.UO_LEVEL_ON, client.UO_LEVEL_OFF

    print(f"--- 开泵 UO[{ch}]={on_lv} ---")
    ok1, msg1 = client.set_uo(ch, True)
    print(msg1)
    if not ok1:
        print("FAIL")
        return 2

    print(f"--- 保持 {hold_s:g}s ---")
    time.sleep(hold_s)

    print(f"--- 停泵 UO[{ch}]={off_lv} ---")
    ok2, msg2 = client.set_uo(ch, False)
    print(msg2)
    if not ok2:
        print("FAIL")
        return 2

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
