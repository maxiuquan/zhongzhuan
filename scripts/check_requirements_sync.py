"""校验 requirements*.txt 与 pyproject.toml 依赖声明保持同步。

pyproject.toml 是依赖的唯一事实源（R-P0-08）。requirements.txt 与
requirements-vps.txt 都是它的派生物。本脚本在 CI 中运行，一旦两边漂移就以
非零退出码失败，从根上消除双事实源。

用法::

    python scripts/check_requirements_sync.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 回退
    import tomli as tomllib  # type: ignore[no-redef]


ROOT: Path = Path(__file__).resolve().parent.parent


def _normalize(spec: str) -> str:
    """把一条依赖声明归一化为可比较形式（去空白、统一小写）。"""
    return "".join(spec.split()).lower()


def _read_requirements(path: Path) -> list[str]:
    """读取 requirements 文件中的有效依赖行（跳过注释、空行与 -r 引用）。"""
    if not path.exists():
        return []
    out: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        out.append(_normalize(line))
    return out


def _read_pyproject() -> tuple[list[str], dict[str, list[str]]]:
    """返回 (核心依赖, extras 映射)，均已归一化。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    core = [_normalize(d) for d in project.get("dependencies", [])]
    extras_raw = project.get("optional-dependencies", {}) or {}
    extras = {name: [_normalize(d) for d in deps] for name, deps in extras_raw.items()}
    return core, extras


def main() -> int:
    """执行同步校验，返回进程退出码。"""
    core, extras = _read_pyproject()
    errors: list[str] = []

    # requirements.txt == [project].dependencies（顺序也必须一致，便于 diff review）
    req = _read_requirements(ROOT / "requirements.txt")
    if req != core:
        errors.append(
            "requirements.txt 与 pyproject.toml [project].dependencies 不同步\n"
            f"  pyproject: {core}\n"
            f"  requirements.txt: {req}"
        )

    # requirements-vps.txt == extras['tidb'] + extras['admin']
    expected_vps = list(extras.get("tidb", [])) + list(extras.get("admin", []))
    vps = _read_requirements(ROOT / "requirements-vps.txt")
    if vps != expected_vps:
        errors.append(
            "requirements-vps.txt 与 pyproject.toml extras[tidb]+extras[admin] 不同步\n"
            f"  pyproject: {expected_vps}\n"
            f"  requirements-vps.txt: {vps}"
        )

    if errors:
        for err in errors:
            print(f"[FAIL] {err}", file=sys.stderr)
        print(
            "\n修复方式：以 pyproject.toml 为准，重写 requirements*.txt。",
            file=sys.stderr,
        )
        return 1

    print("[OK] requirements*.txt 与 pyproject.toml 同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
