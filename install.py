#!/usr/bin/env python3
"""Retired installer entry: the remy-cc binary owns the install family.

The v3 Python installer was retired in R4.4 segment 2/3 (v2.0.0). This
shell only prints migration guidance and exits non-zero. It deliberately
imports nothing from the repository so it cannot drift out of sync with
the authoritative `remy-cc` command surface.
"""
import os
import sys

RELEASES_URL = "https://github.com/MyPeacefulValentine/Remy-CC/releases/latest"

_GUIDANCE = {
    "en": (
        "install.py is retired; the remy-cc binary is the installer.\n"
        "\n"
        "  remy-cc install [--lang en|zh-CN] [--non-interactive]\n"
        "  remy-cc update | verify | uninstall [--yes] [--purge-state]\n"
        "\n"
        "Download the binary for your platform from\n"
        "  {url}\n"
        "or run the bootstrap script: install.sh (POSIX) / install.ps1 (Windows)."
    ),
    "zh-CN": (
        "install.py 已退役；remy-cc 二进制即安装器。\n"
        "\n"
        "  remy-cc install [--lang en|zh-CN] [--non-interactive]\n"
        "  remy-cc update | verify | uninstall [--yes] [--purge-state]\n"
        "\n"
        "请从以下地址下载对应平台的二进制：\n"
        "  {url}\n"
        "或运行引导脚本：install.sh（POSIX）/ install.ps1（Windows）。"
    ),
}


def main() -> int:
    lang = os.environ.get("REMY_LANG", "en")
    template = _GUIDANCE.get(lang, _GUIDANCE["en"])
    print(template.format(url=RELEASES_URL), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
