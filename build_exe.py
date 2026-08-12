"""단일 실행 파일을 만든다.

    pip install pyinstaller
    python build_exe.py

dist/CapCut장면컷나누기.exe 하나만 나온다. 받는 사람은 Python 을 설치하지 않아도 된다.

ffmpeg 는 넣지 않는다. 넣으면 실행 파일이 100MB 를 넘는데, 정작 이 도구를 쓸 사람은 영상
편집을 하는 사람이라 대개 이미 갖고 있다. 없으면 앱이 설치 방법을 알려준다. 같이 배포하고
싶으면 dist 옆에 `bin/ffmpeg.exe`, `bin/ffprobe.exe` 를 두면 media.py 가 그쪽을 먼저 쓴다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "CapCut장면컷나누기"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[!] PyInstaller 가 없습니다.  pip install pyinstaller")
        return 1

    # 앱이 돌고 있으면 윈도우가 실행 파일을 잠가서 덮어쓸 수 없다. PyInstaller 는
    # PermissionError 로 끝나는데, 그 순간 dist 에는 **예전 실행 파일이 그대로 남는다.**
    # 파일이 있는지만 보고 성공으로 넘기면 옛 판을 새 판인 줄 알고 배포하게 된다.
    # 실제로 그럴 뻔했다. 그래서 먼저 확인하고, 끝나고 또 확인한다.
    target = ROOT / "dist" / f"{NAME}.exe"
    stale = target.stat().st_mtime if target.exists() else None

    for folder in ("build", "dist"):
        shutil.rmtree(ROOT / folder, ignore_errors=True)

    if target.exists():
        print(
            f"[!] 예전 실행 파일을 지우지 못했습니다: {target}\n"
            "    앱이 돌고 있으면 윈도우가 파일을 잠급니다. 창을 모두 닫고 다시 시도하세요."
        )
        return 1

    separator = ";" if sys.platform == "win32" else ":"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", NAME,
        # 화면 파일을 실행 파일 안에 넣는다. server.py 가 sys._MEIPASS 에서 찾는다.
        "--add-data", f"{ROOT / 'web'}{separator}web",
        # tkinter 는 폴더 선택 창에 쓴다. 빼면 '폴더에서 찾기' 가 동작하지 않는다.
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.filedialog",
        # 쓰지 않는 무거운 모듈을 빼서 용량을 줄인다. 이 앱은 표준 라이브러리만 쓰므로
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        "--exclude-module", "cv2",
        "--exclude-module", "scenedetect",
        "--exclude-module", "matplotlib",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--noconfirm",
        str(ROOT / "run.py"),
    ]
    print("  " + " ".join(args) + "\n")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    if not target.exists():
        print(f"\n[!] 실행 파일이 만들어지지 않았습니다: {target}")
        return 1
    if stale is not None and target.stat().st_mtime <= stale:
        print(f"\n[!] 실행 파일이 갱신되지 않았습니다. 예전 판이 그대로 남아 있습니다: {target}")
        return 1

    print(f"\n[*] 완성: {target}  ({target.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
