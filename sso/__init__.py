"""`sso/` —— 把上游 [shu-sso-poc] 当 **git 子模块**用起来的薄适配层。

目录分工（**为什么这么切**）
--------------------------------------------------------------------------
    vendor/shu-sso-poc/     git 子模块，上游原版，**一行都不改**
    sso/                    本地适配层，只有这几个文件：

| 文件 | 说明 |
| :--- | :--- |
| `__init__.py` | 本文件：把上游 `src.*` 挂成 `sso.*`，并纠正两个落盘路径 |
| `client.py` | 上游 `ShuSSO` 的**子类**，只加 `cookies()` / `cookie_header()` |
| `config.py` | `AIA_*` / shareId / `CAPTURE_DIR` + 裁剪过的 `SYSTEMS` |
| `runner.py` | 只跑本 POC 要的系统（上游那份会跑全部系统） |
| `ui.py` | 终端文案随本 POC 调整 |
| `utils.py` | 更严的脱敏 + `write_private_json` / `mask_cookie_header` |
| `aiagent.py` | 本地新增：把认证会话扩到 `aiagent.shu.edu.cn` |

`qr` / `registry` / `rsa_key` / `system_api` 我们**没改**，所以不复制一份，
而是把上游模块别名成同名模块（`sys.modules["sso.qr"] = src.qr`）——
既能继续 `from sso.qr import ...`，又只有一份实现，上游修 bug 直接受益。

上游更新
--------------------------------------------------------------------------
    git submodule update --remote vendor/shu-sso-poc     # 拉到上游最新
    git add vendor/shu-sso-poc && git commit             # 记录新的子模块指针

因为 `sso/` 与子模块是两个独立目录，上游更新**改不到**本地适配层，不会出现
「上游一 pull，我的改动被冲掉 / 产生冲突」的情况；代价是上游若改了
`ShuSSO` 的公开方法签名，本地层会在运行时暴露出来（`tests/test_offline.py` 兜着）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["config", "registry", "rsa_key", "utils", "client", "system_api",
           "runner", "ui", "qr", "aiagent"]

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

# 让上游包名 `src` 可导入（上游 `src/` 内部用相对导入，因此只依赖这一条路径）
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from src import qr as _qr                      # noqa: E402
from src import registry as _registry          # noqa: E402
from src import rsa_key as _rsa_key            # noqa: E402
from src import system_api as _system_api      # noqa: E402

# 上游把「RSA 公钥缓存」和「证据目录」写在它自己仓库根下（= 子模块目录），会弄脏
# 子模块；把这两个**路径常量**改指到本项目根即可（函数按模块全局读常量，不涉及
# 改逻辑）。这是本文件仅有的两处「上游适配」。
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
