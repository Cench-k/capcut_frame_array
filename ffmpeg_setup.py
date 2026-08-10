"""ffmpeg 를 자동으로 내려받아 설치한다.

이 도구는 ffmpeg 없이는 아무것도 못 한다. 그런데 받는 사람은 영상 편집자이지 개발자가
아니라서, "PATH 에 넣으세요" 라고 안내하면 대부분 거기서 막힌다. 그래서 앱이 직접 받는다.

받는 곳은 gyan.dev 다. ffmpeg.org 가 윈도우 공식 빌드로 링크하는 곳이고, 배포본마다
SHA-256 을 같이 올려두기 때문에 받은 파일이 온전한지 확인할 수 있다.

essentials 빌드를 쓴다. full 빌드는 같은 도구가 두 배 넘게 크고, 여기서 쓰는 것은
`scdet` 필터와 mjpeg 인코더뿐이라 essentials 로 충분하다.
"""

from __future__ import annotations

import hashlib
import shutil
import ssl
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from errors import UserError
from media import bin_dir, exe_name, run, tool_path

# 항상 최신 배포본을 가리키는 주소. 뒤에서 실제 파일로 넘겨준다.
ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
SHA_URL = ZIP_URL + ".sha256"

# 우리가 꺼내 쓸 것. ffplay 는 필요 없어서 두고 온다.
WANTED = ("ffmpeg", "ffprobe")

# 받다가 멈춘 것을 하염없이 기다리지 않게.
TIMEOUT_S = 60

ProgressFn = Callable[[float, str], None]


class SetupError(UserError):
    """사용자에게 그대로 보여줄 오류."""


def status() -> dict:
    """지금 ffmpeg 를 쓸 수 있는지, 어디 것을 쓰는지."""
    found = {name: tool_path(name) for name in WANTED}
    ready = all(found.values())
    return {
        "ready": ready,
        "paths": {name: str(path) if path else "" for name, path in found.items()},
        "managed": bool(found["ffmpeg"] and bin_dir() in found["ffmpeg"].parents),
        "bin_dir": str(bin_dir()),
        "version": _version() if ready else "",
    }


def _version() -> str:
    """`ffmpeg -version` 첫 줄에서 판 번호만."""
    path = tool_path("ffmpeg")
    if not path:
        return ""
    try:
        done = run([str(path), "-version"], timeout=15)
    except Exception:
        return ""
    first = done.stdout.decode("utf-8", "replace").splitlines()
    if not first:
        return ""
    parts = first[0].split()
    return parts[2] if len(parts) > 2 else first[0][:40]


def _fetch(url: str, dest: Path | None, on_progress: ProgressFn | None = None) -> bytes:
    """URL 을 받는다. dest 가 있으면 파일로 흘려보내고, 없으면 바이트로 돌려준다.

    100MB 를 통째로 메모리에 올리지 않으려고 조각씩 읽는다.
    """
    context = ssl.create_default_context()
    request = urllib.request.Request(url, headers={"User-Agent": "SceneCut"})
    chunks: list[bytes] = []
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=context) as response:
            total = int(response.headers.get("Content-Length") or 0)
            got = 0
            sink = dest.open("wb") if dest else None
            try:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if sink:
                        sink.write(chunk)
                    else:
                        chunks.append(chunk)
                    if on_progress and total:
                        on_progress(got / total, f"{got / 1e6:.0f} / {total / 1e6:.0f} MB")
            finally:
                if sink:
                    sink.close()
    except OSError as exc:
        raise SetupError(
            f"내려받지 못했습니다: {exc}\n"
            "인터넷 연결을 확인하거나, 직접 받아서 아래 폴더에 넣어주세요.\n"
            f"{bin_dir()}"
        ) from exc
    return b"".join(chunks)


def _expected_hash() -> str:
    """배포처가 올려둔 SHA-256."""
    raw = _fetch(SHA_URL, None).decode("utf-8", "replace").strip()
    # '<해시>  <파일이름>' 형태이거나 해시만 있는 경우가 있다.
    return raw.split()[0].lower() if raw else ""


def install(on_progress: ProgressFn | None = None) -> dict:
    """ffmpeg 를 내려받아 bin/ 에 넣는다."""
    def report(fraction: float, message: str) -> None:
        if on_progress:
            on_progress(max(0.0, min(fraction, 1.0)), message)

    target = bin_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError(
            f"{target} 폴더를 만들지 못했습니다: {exc}\n"
            "프로그램을 쓰기 가능한 폴더(예: 바탕화면)로 옮기고 다시 실행해주세요."
        ) from exc

    report(0.0, "배포처 확인 중")
    expected = _expected_hash()

    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "ffmpeg.zip"

        # 전체 진행률에서 내려받기가 대부분을 차지한다. 압축 풀기는 순식간이다.
        _fetch(ZIP_URL, archive, lambda f, m: report(f * 0.88, f"내려받는 중 {m}"))

        report(0.90, "파일 확인 중")
        if expected:
            actual = _sha256(archive)
            if actual != expected:
                # 받다 끊겼거나 중간에 바뀐 것이다. 검증 못 한 실행 파일을 두지 않는다.
                raise SetupError(
                    "내려받은 파일이 배포처가 알려준 것과 다릅니다. 다시 시도해주세요.\n"
                    f"기대: {expected[:16]}…\n받음: {actual[:16]}…"
                )

        report(0.93, "압축 푸는 중")
        extracted = _extract(archive, target)

    missing = [name for name in WANTED if name not in extracted]
    if missing:
        raise SetupError(f"압축 안에서 {', '.join(missing)} 을(를) 찾지 못했습니다.")

    report(0.98, "동작 확인 중")
    _verify()
    report(1.0, "완료")
    return status()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def _extract(archive: Path, target: Path) -> set[str]:
    """압축 안에서 필요한 실행 파일만 꺼낸다.

    zip 안 경로를 그대로 쓰지 않고 **파일 이름만** 보고 target 바로 아래에 쓴다. 압축 파일이
    `../` 같은 경로를 담고 있어도 바깥으로 새어나가지 못하게 하려는 것이다.
    """
    wanted = {exe_name(name): name for name in WANTED}
    found: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if entry.is_dir():
                    continue
                leaf = Path(entry.filename).name
                key = wanted.get(leaf)
                if not key:
                    continue
                destination = target / leaf
                with bundle.open(entry) as source, destination.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                found.add(key)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SetupError(f"압축을 풀지 못했습니다: {exc}") from exc
    return found


def _verify() -> None:
    """정말 도는지, 그리고 `scdet` 필터가 있는지 확인한다.

    필터 확인이 중요하다. scdet 없이 빌드된 ffmpeg 도 세상에 있는데, 그런 것을 받으면
    설치는 성공했다고 나오고 정작 분석할 때 알 수 없는 오류가 난다.
    """
    ffmpeg = tool_path("ffmpeg")
    if not ffmpeg:
        raise SetupError("설치했지만 ffmpeg 를 찾지 못했습니다.")
    try:
        done = run([str(ffmpeg), "-hide_banner", "-filters"], timeout=30)
    except Exception as exc:
        raise SetupError(f"ffmpeg 를 실행하지 못했습니다: {exc}") from exc
    if done.returncode != 0:
        raise SetupError("내려받은 ffmpeg 가 실행되지 않습니다.")
    if b" scdet " not in done.stdout:
        raise SetupError(
            "내려받은 ffmpeg 에 scdet 필터가 없습니다. 장면 분석을 할 수 없습니다."
        )
