"""`sso/` —— 把上游 [shu-sso-poc] 当 **git 子模块**用的薄适配层。

    vendor/shu-sso-poc/   子模块，上游原版，一行不改
    sso/                  本地适配层

| 文件 | 说明 |
| :--- | :--- |
| `__init__.py` | 本文件：把上游 `src.*` 挂成 `sso.*`，并纠正两个落盘路径 |
| `client.py` | 上游 `ShuSSO` 的子类，只加 `cookies()` / `cookie_header()` |
| `config.py` | `AIA_*` / shareId / `CAPTURE_DIR` + 裁剪过的 `SYSTEMS` |
| `runner.py` | 只跑本 POC 要的系统（上游那份会跑全部系统） |
| `ui.py` | 终端文案随本 POC 调整 |
| `utils.py` | 更严的脱敏 + `write_private_json` / `mask_cookie_header` |

`qr` / `registry` / `rsa_key` / `system_api` 没改，别名成同名模块即可
（`sys.modules["sso.qr"] = src.qr`）；上游更新：`git submodule update --remote`。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["config", "registry", "rsa_key", "utils", "client", "system_api",
           "runner", "ui", "qr"]

#: 子模块位置；想同时改上游代码时，用 `SHU_SSO_POC_DIR` 指到别处
VENDOR_DIR = Path(os.getenv("SHU_SSO_POC_DIR") or
                  Path(__file__).resolve().parent.parent / "vendor" / "shu-sso-poc")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if not (VENDOR_DIR / "src" / "client.py").is_file():
    raise RuntimeError(
        f"找不到上游子模块：{VENDOR_DIR}\n"
        "  首次克隆后请执行： git submodule update --init --recursive\n"
        "  上游放在别处时：   SHU_SSO_POC_DIR=/path/to/shu-sso-poc"
    )

# 让上游包名 `src` 可导入（上游 `src/` 内部用相对导入）
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from src import qr as _qr                      # noqa: E402
from src import registry as _registry          # noqa: E402
from src import rsa_key as _rsa_key            # noqa: E402
from src import system_api as _system_api      # noqa: E402

# 上游把公钥缓存与证据目录写在子模块根下，会弄脏子模块；改指项目根即可
# （函数按模块全局读这两个常量，不涉及改逻辑）。
_rsa_key.CACHE_PATH = PROJECT_ROOT / ".rsa_public_key.pem"
sys.modules["src.config"].CAPTURE_DIR = PROJECT_ROOT / "captures"

# 没被本地覆盖的上游模块 → 挂成本包同名模块，保证只有一份实现
sys.modules[__name__ + ".qr"] = _qr
sys.modules[__name__ + ".registry"] = _registry
sys.modules[__name__ + ".rsa_key"] = _rsa_key
sys.modules[__name__ + ".system_api"] = _system_api

qr = _qr
registry = _registry
rsa_key = _rsa_key
system_api = _system_api
