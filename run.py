"""진입점. 로컬 서버를 띄우고 브라우저를 연다.

더블클릭으로 실행하는 것을 전제로 한다. 콘솔 창이 열려 있어야 서버가 살아 있으므로,
창을 닫으면 앱도 끝난다는 것을 안내한다.
"""

from __future__ import annotations

import sys
import threading
import webbrowser

from folder_picker import PICK_FLAG, show_dialog


def main() -> int:
    # 실행 파일로 묶이면 폴더 선택 창을 띄울 때 자기 자신을 다시 부른다. 이 검사가 서버를
    # 띄우기 **전에** 와야 앱이 통째로 다시 켜지지 않는다. sys.executable 이 python.exe 가
    # 아니라 exe 자신이 되기 때문인데, 소스로 돌릴 땐 멀쩡해서 놓치기 쉽다.
    if len(sys.argv) > 1 and sys.argv[1] == PICK_FLAG:
        show_dialog(sys.argv[2:])
        return 0

    from server import serve  # 대화상자 경로에서는 서버 모듈을 불러올 필요가 없다

    try:
        httpd, token, port = serve()
    except OSError as exc:
        print(f"[!] 서버를 시작하지 못했습니다: {exc}")
        input("엔터를 누르면 닫힙니다...")
        return 1

    url = f"http://127.0.0.1:{port}/?t={token}"
    # flush 를 지정한다. 콘솔이 아니라 파일이나 파이프로 내보내면 파이썬이 출력을 모아두는데,
    # 그러면 정작 필요한 주소가 한참 뒤에야 보인다.
    print(
        f"\n  CapCut 장면 컷 나누기가 실행 중입니다."
        f"\n  브라우저가 안 열리면 아래 주소를 붙여넣으세요:\n\n    {url}\n"
        f"\n  끝내려면 이 창을 닫거나 Ctrl+C 를 누르세요.\n",
        flush=True,
    )

    # 서버가 요청을 받을 준비가 된 뒤에 열어야 첫 화면이 실패하지 않는다.
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
