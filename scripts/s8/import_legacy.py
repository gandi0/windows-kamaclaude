from __future__ import annotations

import argparse
import json
from pathlib import Path

from kama_claude.core.session.execution import ExecutionStore


# 将指定旧会话目录无损导入显式数据库，不加载 daemon、模型或用户配置
def main() -> None:
    parser = argparse.ArgumentParser(description="Import a legacy session without changing its files")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    store = ExecutionStore(args.database.expanduser().resolve())
    try:
        print(json.dumps(store.import_legacy(args.directory), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
