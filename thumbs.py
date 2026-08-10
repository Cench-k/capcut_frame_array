"""컷 경계의 프레임 그림을 뽑는다.

컷마다 '전환 직전' 과 '전환 직후' 두 장을 보여준다. 한 장만 보면 그 자리가 정말 장면이
바뀌는 곳인지 알 수 없다. 두 장이 나란히 있어야 사용자가 한눈에 판단한다.
"""

from __future__ import annotations

import base64
import concurrent.futures
import subprocess
from dataclasses import dataclass
from pathlib import Path

from media import NO_WINDOW, US, VideoInfo, ffmpeg_path

# 검수용이므로 작아도 된다. 가로 160 이면 컷 100 개를 띄워도 화면이 버벅이지 않는다.
THUMB_WIDTH = 160

# 한 장 뽑는 데 이보다 오래 걸리면 포기한다. 손상된 구간에서 ffmpeg 이 붙잡고 있는 것을 막는다.
TIMEOUT_S = 20

# 동시에 몇 개까지 뽑을지. 디스크를 여러 갈래로 읽으므로 너무 늘리면 오히려 느려진다.
WORKERS = 4


@dataclass
class ThumbPair:
    """컷 하나에 딸린 두 장."""

    time_us: int
    before: str  # data URI, 실패하면 빈 문자열
    after: str

    def as_dict(self) -> dict:
        return {"time_us": self.time_us, "before": self.before, "after": self.after}


def grab(info: VideoInfo, time_us: int) -> str:
    """그 시각의 프레임 한 장을 data URI 로 뽑는다. 실패하면 빈 문자열.

    `-ss` 를 `-i` **앞에** 둔다. 뒤에 두면 ffmpeg 이 처음부터 전부 디코딩하며 그 시각까지
    가므로, 영상 뒤쪽 컷 한 장에 수십 초가 걸린다. 앞에 두면 곧장 건너뛴다.

    앞에 두면 키프레임 단위로만 정확한 것이 보통이지만, 요즘 ffmpeg 은 이 경우에도
    목표 시각까지 디코딩해 맞춰준다. 어차피 검수용 그림이라 한두 프레임 차이는 상관없다.
    """
    time_us = max(time_us, 0)
    cmd = [
        ffmpeg_path(), "-v", "error", "-nostdin",
        "-ss", f"{time_us / US:.6f}",
        "-i", str(info.path),
        "-frames:v", "1",
        "-vf", f"scale={THUMB_WIDTH}:-2",
        "-f", "image2", "-c:v", "mjpeg", "-q:v", "6",
        "-",
    ]
    try:
        done = subprocess.run(
            cmd, capture_output=True, timeout=TIMEOUT_S, creationflags=NO_WINDOW
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if done.returncode != 0 or not done.stdout:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(done.stdout).decode("ascii")


def pairs(info: VideoInfo, times_us: list[int]) -> list[ThumbPair]:
    """컷 목록마다 직전/직후 두 장을 뽑는다.

    '직전' 은 한 프레임 앞이다. 컷 시각 그대로 뽑으면 이미 바뀐 뒤라 두 장이 똑같이 나온다.
    가변 프레임레이트면 프레임 간격이 일정하지 않으므로 넉넉하게 한 프레임 반을 뺀다.
    """
    if not times_us:
        return []

    step = info.frame_us or (US / 30)
    back = int(step * (1.5 if info.variable_fps else 1.0))

    jobs: list[tuple[int, int]] = []
    for time_us in times_us:
        jobs.append((max(time_us - back, 0), time_us))

    # 앞/뒤 두 장을 한 목록으로 펴서 한꺼번에 병렬로 뽑는다.
    flat = [t for pair in jobs for t in pair]
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        images = list(pool.map(lambda t: grab(info, t), flat))

    return [
        ThumbPair(time_us=times_us[i], before=images[i * 2], after=images[i * 2 + 1])
        for i in range(len(times_us))
    ]
