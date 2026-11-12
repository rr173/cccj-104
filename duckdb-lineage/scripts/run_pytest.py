"""pytest 启动器：在 python -I 下显式补回 user site 后再调用 pytest。"""

import site
import sys

for _p in (site.getusersitepackages(), *site.getsitepackages()):
    if _p and _p not in sys.path:
        sys.path.append(_p)

import pytest  # noqa: E402

raise SystemExit(pytest.main(sys.argv[1:]))
