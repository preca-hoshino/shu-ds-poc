"""本 POC 的配置：上游常量透传 + 自己那几个值。

- `SSO_BASE` / `DEFAULT_TENANT` / `WECOM` 等协议常量直接来自子模块（`src.config`），
  不复制一份 —— 上游改参数时这里立刻跟着变；
- `AIA_*` / shareId / `CAPTURE_DIR` 是本 POC 的东西，上游没有；
- `SYSTEMS` 由上游 `registry.load_systems()` 从子模块的 `systems/` 装出来，
  再按需裁剪（默认只留 `ds`）：本 POC 只需要「换到 ds 会话」来保证身份可用。

导入本模块不会联网（RSA 公钥抓取在 `rsa_key`，首次使用时才发生）。
"""

from __future__ import annotations

import os
from pathlib import Path

# 上游的协议常量（由子模块维护，不在这里改）
from src.config import DEFAULT_TENANT, SSO_BASE, WECOM      # noqa: F401
from src.registry import load_systems

# ---- 本 POC 的目标站（DeepSeek 代理的上游）----
AIA_BASE = "https://aiagent.shu.edu.cn"
AIA_CHAT_PATH = "/api/v2/chat/completions"

# R1 走独立的分享链
R1_SHARE_ID = "rjla9q8p3w7rrmqj9b24cth6"
# 默认分享 id（可用环境变量 SHU_SHARE_ID 覆盖）
DEFAULT_SHARE_ID = "49imnzpquhvt2gnj8xqxmcch"

# 运行产物（脱敏证据）保存目录
CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"

#: 本次要登录的业务系统；默认只 `ds`（千学百科：由后端完成码换 token，能拿到学号/姓名）
WANTED_SYSTEMS: list[str] = [s.strip() for s in
                             (os.getenv("SHU_SSO_SYSTEMS") or "ds").split(",")
                             if s.strip()]

_loaded: dict[str, dict] = load_systems()
_missing = [k for k in WANTED_SYSTEMS if k not in _loaded]
if _missing:
    raise RuntimeError(
        f"子模块 systems/ 里没有这些系统：{_missing}；可选：{sorted(_loaded)}。"
        "用 SHU_SSO_SYSTEMS 指定要跑的系统，例如 SHU_SSO_SYSTEMS=ds"
    )

#: 业务系统注册表（来自子模块 `systems/<域名>/config.py`，键 = 域名首段）
SYSTEMS: dict[str, dict] = {k: _loaded[k] for k in WANTED_SYSTEMS}
