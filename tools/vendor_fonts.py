"""
把网页界面用到的字体从 Google Fonts 下载到本地，生成 web/static/fonts.css。

会议现场常常没有外网，或者 Google Fonts 被挡。字体请求失败时浏览器**不会报错**，
只会静默回落到系统字体，界面当场变样。所以字体必须随程序走。

用法（需要能联网，一次性）：
    python tools/vendor_fonts.py

产物：
    web/static/fonts.css          @font-face 定义，src 指向本地文件
    web/static/fonts/*.woff2      各语言子集，浏览器按 unicode-range 按需读取

改了 index.html 里的字体后，把下面的 FAMILIES 同步改掉再跑一遍。
"""

from __future__ import annotations

import os
import re
import sys
import urllib.request

# 伪装成新版 Chrome，否则 Google 会返回老旧的 ttf 而不是 woff2
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

FAMILIES = {
    # 西文正文 / 界面
    "source-sans-3": "https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@300;400;600&display=swap",
    # 中文译文
    "noto-sans-sc": "https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400&display=swap",
    # 设置里的接口地址 / 密钥用等宽
    "ibm-plex-mono": "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400&display=swap",
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "web", "static")
FONT_DIR = os.path.join(STATIC, "fonts")

FACE_RE = re.compile(r"@font-face\s*\{(.*?)\}", re.S)
PROP_RE = re.compile(r"([a-z-]+)\s*:\s*([^;]+);")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=60).read()


def main() -> int:
    os.makedirs(FONT_DIR, exist_ok=True)
    out: list[str] = [
        "/* 由 tools/vendor_fonts.py 生成，勿手改。",
        "   字体随程序走，断网也不会变样。 */",
        "",
    ]
    total_files = total_bytes = 0

    for slug, css_url in FAMILIES.items():
        print(f"{slug} …", flush=True)
        css = fetch(css_url).decode("utf-8")

        for i, match in enumerate(FACE_RE.finditer(css)):
            props = dict(PROP_RE.findall(match.group(1)))
            remote = re.search(r"url\((https://[^)]+\.woff2)\)", props.get("src", ""))
            if not remote:
                continue

            weight = props.get("font-weight", "400").strip()
            name = f"{slug}-{weight}-{i}.woff2"
            path = os.path.join(FONT_DIR, name)

            if not os.path.exists(path):
                data = fetch(remote.group(1))
                with open(path, "wb") as f:
                    f.write(data)
            total_files += 1
            total_bytes += os.path.getsize(path)

            face = [
                "@font-face {",
                f"  font-family: {props.get('font-family', '').strip()};",
                f"  font-style: {props.get('font-style', 'normal').strip()};",
                f"  font-weight: {weight};",
                "  font-display: swap;",
                f"  src: url(fonts/{name}) format('woff2');",
            ]
            if "unicode-range" in props:
                face.append(f"  unicode-range: {props['unicode-range'].strip()};")
            face.append("}")
            out.append("\n".join(face))

    with open(os.path.join(STATIC, "fonts.css"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")

    print(f"\n{total_files} 个字体文件，共 {total_bytes / 1024 / 1024:.2f} MB")
    print(f"已写出 {os.path.join(STATIC, 'fonts.css')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
