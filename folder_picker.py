"""네이티브 '폴더/파일 선택' 창을 띄운다.

tkinter 는 메인 스레드에서만 안정적으로 도는데 서버는 요청마다 별도 스레드에서 돈다.
그래서 대화상자를 자식 프로세스로 분리해 띄우고 결과만 받아온다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 실행 파일로 묶였을 때 '대화상자만 띄우고 끝내라' 는 신호. run.py 가 가장 먼저 확인한다.
PICK_FLAG = "--pick-path"


def _command(kind: str, title: str, initial: str) -> list[str]:
    """자식 프로세스로 띄울 명령을 만든다.

    PyInstaller 로 묶으면 sys.executable 이 python.exe 가 아니라 **실행 파일 자신**이다.
    거기에 이 파일 경로를 넘기면 대화상자가 아니라 앱이 통째로 다시 켜져서, 서버가 하나 더
    뜨고 브라우저 탭이 새로 열린다. 그래서 묶인 경우에는 전용 플래그로 자신을 다시 부른다.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, PICK_FLAG, kind, title, initial]
    return [sys.executable, str(Path(__file__).resolve()), kind, title, initial]


def _emit(path: str) -> None:
    """고른 경로를 부모 프로세스로 넘긴다.

    반드시 바이트로 쓴다. 텍스트 모드로 쓰면 파이썬이 콘솔 코드페이지(한국어 윈도우는
    cp949)로 인코딩하는데, 부모는 UTF-8 로 읽으므로 한글 경로가 깨져서 도착한다.
    깨진 경로는 존재하지 않는 폴더가 되어 '폴더를 골라도 아무 일도 안 일어나는' 것처럼 보인다.
    """
    data = path.encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:  # 창 모드로 묶여 stdout 이 없는 경우
        sys.stdout.write(path)
        return
    stream.write(data)
    stream.flush()


def _ask(kind: str, initial: str | None, title: str) -> str | None:
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        # text=True 를 쓰지 않는다. 인코딩을 파이썬 기본값에 맡기면 한글 경로가 깨진다.
        done = subprocess.run(
            _command(kind, title, initial or ""),
            capture_output=True,
            timeout=300,
            creationflags=flags,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    picked = (done.stdout or b"").decode("utf-8", errors="replace").strip()
    # 대화상자는 고른 경로만 뱉는다. 앞에 다른 출력이 섞이면 마지막 줄만 쓴다.
    return picked.splitlines()[-1].strip() if picked else None


def ask_folder(initial: str | None = None, title: str = "폴더 선택") -> str | None:
    """폴더 선택 창을 띄우고 고른 경로를 돌려준다. 취소하면 None."""
    return _ask("dir", initial, title)


def ask_file(initial: str | None = None, title: str = "파일 선택") -> str | None:
    """대본 txt 를 고르는 창. 취소하면 None."""
    return _ask("file", initial, title)


def show_dialog(argv: list[str]) -> None:
    """자식 프로세스에서 실제로 대화상자를 띄운다. 고른 경로를 stdout 으로 넘긴다."""
    import tkinter as tk
    from tkinter import filedialog

    kind = argv[0] if argv else "dir"
    title = argv[1] if len(argv) > 1 else "선택"
    initial = argv[2] if len(argv) > 2 else ""
    start_dir = initial if initial and Path(initial).is_dir() else None

    root = tk.Tk()
    root.withdraw()
    # 브라우저 뒤에 숨어버리지 않게 항상 위로 올린다.
    root.attributes("-topmost", True)
    if kind == "file":
        path = filedialog.askopenfilename(
            title=title,
            initialdir=start_dir or (str(Path(initial).parent) if initial else None),
            filetypes=[("대본 파일", "*.txt *.md"), ("모든 파일", "*.*")],
        )
    else:
        path = filedialog.askdirectory(title=title, initialdir=start_dir, mustexist=True)
    root.destroy()
    if path:
        _emit(str(Path(path)))


if __name__ == "__main__":
    show_dialog(sys.argv[1:])
