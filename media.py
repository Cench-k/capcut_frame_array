"""ffmpeg / ffprobe 로 영상 정보를 읽는다.

외부 파이썬 패키지를 쓰지 않는다. 장면 감지에 흔히 쓰는 PySceneDetect 는 opencv 와 numpy 를
끌고 오는데, 그러면 단일 실행 파일이 수십 MB 로 불어난다. ffmpeg 는 어차피 있어야 하므로
감지도 ffmpeg 필터로 한다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from errors import UserError

# CapCut 타임라인과 같은 단위. 모든 시간 계산의 기준이다.
US = 1_000_000

# 자식 프로세스가 콘솔 창을 새로 띄우지 않게 한다. 창 모드로 묶었을 때 검은 창이 깜빡인다.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MediaError(UserError):
    """사용자에게 그대로 보여줄 오류."""


def app_dir() -> Path:
    """프로그램이 놓인 폴더. 여기 하위 `bin/` 에 ffmpeg 를 둔다.

    PyInstaller 로 묶으면 `sys._MEIPASS` 는 실행할 때마다 새로 만들어졌다가 끝나면 지워지는
    임시 폴더다. 내려받은 ffmpeg 를 거기 두면 다음 실행에 사라진다. 그래서 **실행 파일 자신이
    있는 폴더**를 기준으로 삼는다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bin_dir() -> Path:
    """내려받은 ffmpeg 를 두는 곳."""
    return app_dir() / "bin"


def tool_path(name: str) -> Path | None:
    """ffmpeg / ffprobe 를 찾는다. 없으면 None.

    찾는 순서는 곁들인 것 → 묶인 것 → PATH 다. 곁들인 것을 먼저 보는 이유는, PATH 의
    ffmpeg 가 `scdet` 필터 없는 오래된 빌드일 수 있기 때문이다.
    """
    exe = f"{name}.exe" if sys.platform == "win32" else name

    candidates = [bin_dir() / exe]
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        # 빌드할 때 아예 exe 안에 넣어 배포하는 경우.
        candidates.append(Path(bundled) / "bin" / exe)

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    found = shutil.which(name)
    return Path(found) if found else None


def _find_tool(name: str) -> str:
    found = tool_path(name)
    if found:
        return str(found)
    raise MediaError(
        f"{name} 을(를) 찾을 수 없습니다.\n"
        "화면 위쪽의 'ffmpeg 자동 설치' 를 누르거나, 직접 받아서 "
        f"{bin_dir()} 안에 {exe_name(name)} 로 넣어주세요."
    )


def exe_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def tools_ready() -> bool:
    """ffmpeg 와 ffprobe 가 둘 다 있는가."""
    return tool_path("ffmpeg") is not None and tool_path("ffprobe") is not None


def ffmpeg_path() -> str:
    return _find_tool("ffmpeg")


def ffprobe_path() -> str:
    return _find_tool("ffprobe")


def run(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    """자식 프로세스를 돌리고 결과를 바이트로 받는다.

    text=True 를 쓰지 않는다. 인코딩을 파이썬 기본값에 맡기면 한국어 윈도우에서 cp949 로
    디코딩해 경로와 메시지가 깨진다. 필요한 쪽에서 UTF-8 로 직접 푼다.
    """
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW
    )


@dataclass
class VideoInfo:
    """감지와 분할에 필요한 최소한의 영상 정보."""

    path: Path
    width: int
    height: int
    duration_us: int
    fps: float
    frame_count: int
    variable_fps: bool

    @property
    def frame_us(self) -> float:
        """프레임 하나의 길이(마이크로초). 고정 프레임레이트일 때만 의미가 있다."""
        return US / self.fps if self.fps > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "width": self.width,
            "height": self.height,
            "duration_us": self.duration_us,
            "fps": round(self.fps, 3),
            "frame_count": self.frame_count,
            "variable_fps": self.variable_fps,
        }


def _fraction(text: str | None) -> float:
    """'30/1' 같은 유리수 문자열을 실수로. 0/0 이면 0 을 준다."""
    if not text:
        return 0.0
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return float(value) if value.denominator else 0.0


def probe(path: str | os.PathLike) -> VideoInfo:
    """영상의 크기 / 길이 / 프레임레이트를 읽는다."""
    target = Path(path)
    if not target.is_file():
        raise MediaError(f"영상 파일이 없습니다: {target}")

    done = run(
        [
            ffprobe_path(), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            str(target),
        ]
    )
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise MediaError(f"영상 정보를 읽지 못했습니다: {detail[:300]}")

    data = json.loads(done.stdout.decode("utf-8", "replace") or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise MediaError(f"영상 트랙이 없는 파일입니다: {target.name}")
    stream = streams[0]

    seconds = _to_float(stream.get("duration")) or _to_float(
        (data.get("format") or {}).get("duration")
    )
    nominal = _fraction(stream.get("r_frame_rate"))
    average = _fraction(stream.get("avg_frame_rate"))

    # 명목 프레임레이트와 실제 평균이 어긋나면 가변 프레임레이트다. 폰 촬영본과 화면 녹화가
    # 대표적인데, 이때 '프레임 번호 / fps' 로 시각을 계산하면 뒤로 갈수록 밀린다. 그래서
    # 감지 결과의 시각은 항상 ffmpeg 이 알려준 pts 를 그대로 쓰고, fps 는 표시용으로만 둔다.
    variable = bool(nominal and average and abs(nominal - average) / nominal > 0.01)
    fps = average or nominal

    return VideoInfo(
        path=target,
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        duration_us=int(round(seconds * US)),
        fps=fps,
        frame_count=int(stream.get("nb_frames") or 0) or int(round(seconds * fps)),
        variable_fps=variable,
    )


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
