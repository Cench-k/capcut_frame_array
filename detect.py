"""장면 전환 감지.

ffmpeg 의 `scdet` 필터를 임계값 0 으로 돌려 **모든 프레임의 장면 점수**를 받아온 뒤,
컷 판정은 파이썬에서 한다. 두 단계를 나눈 것은 의도적이다.

- ffmpeg 에 임계값을 맡기면 값을 바꿀 때마다 영상을 다시 스캔해야 한다. 점수 곡선을
  들고 있으면 임계값은 즉시 다시 계산된다. 사용자가 슬라이더를 움직이며 컷 수를
  확인하는 화면이 이 구조에서 나온다.
- 고정 임계값 하나로는 밝은 장면과 어두운 장면을 같이 다루기 어렵다. 곡선이 있으면
  주변 프레임 대비 얼마나 튀는지를 같이 볼 수 있다.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from media import NO_WINDOW, US, MediaError, VideoInfo, ffmpeg_path

# 점수 계산은 화면 내용만 보면 되므로 원본 해상도가 필요 없다. 가로 320 으로 줄이면
# 1080x1920 4 분짜리가 5 초 남짓에 끝난다. 줄이지 않으면 십수 배 느리다.
ANALYZE_WIDTH = 320

# metadata=print 가 뱉는 두 줄. 프레임 헤더 다음 줄들에 값이 붙는다.
_FRAME_LINE = re.compile(r"^frame:(\d+)\s+pts:(-?\d+)\s+pts_time:(-?[\d.]+)")
_SCORE_LINE = re.compile(r"^lavfi\.scd\.score=([\d.]+)")

# 주변 몇 프레임과 비교해 '튀는 정도' 를 볼지. 좌우로 이만큼씩 본다.
NEIGHBOR_RADIUS = 3

# 주변 대비 배수의 분모가 0 에 가까우면 배수가 무한대로 튄다. 정지 화면에서 아주 미세한
# 흔들림이 컷으로 잡히는 것을 막는 바닥값이다.
_RATIO_FLOOR = 0.6


@dataclass
class Frame:
    """한 프레임의 장면 점수."""

    index: int
    time_us: int
    score: float


@dataclass
class Cut:
    """장면이 바뀌는 지점. 이 프레임부터 새 장면이 시작한다."""

    frame: int
    time_us: int
    score: float
    ratio: float

    def as_dict(self) -> dict:
        return {
            "frame": self.frame,
            "time_us": self.time_us,
            "score": round(self.score, 2),
            "ratio": round(self.ratio, 2),
        }


# ------------------------------------------------------------------- 1단계: 스캔


def scan(
    info: VideoInfo,
    *,
    start_us: int = 0,
    duration_us: int | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> list[Frame]:
    """영상을 훑어 프레임별 장면 점수를 모은다.

    `-copyts` 를 붙여 잘라 읽어도 pts 가 원본 시각을 유지하게 한다. 이것이 없으면 구간
    스캔의 결과 시각이 0 부터 다시 시작해, 타임라인에 되돌려 놓을 때 통째로 밀린다.
    """
    cmd = [ffmpeg_path(), "-v", "error", "-nostdin"]
    if start_us > 0:
        cmd += ["-ss", f"{start_us / US:.6f}", "-copyts"]
    cmd += ["-i", str(info.path)]
    if duration_us:
        # `-copyts` 를 쓸 때는 반드시 `-to`(끝 시각)여야 하고 `-t`(길이)를 쓰면 안 된다.
        # 타임스탬프가 원본 시각을 유지하는데 `-t` 는 그 값을 길이로 재기 때문에, 시작이
        # 60 초일 때 `-t 20` 은 "20 초에 멈춰라" 가 되어 곧바로 끝나 버린다.
        end_us = start_us + duration_us
        cmd += ["-to" if start_us > 0 else "-t", f"{(end_us if start_us > 0 else duration_us) / US:.6f}"]
    cmd += [
        "-an", "-sn", "-dn",
        "-vf", f"scale={ANALYZE_WIDTH}:-2,scdet=s=0:t=0,metadata=print:file=-",
        "-f", "null", "-",
    ]

    span_us = duration_us or max(info.duration_us - start_us, 1)
    frames: list[Frame] = []
    pending: Frame | None = None
    reported = -1.0

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW
    )
    assert proc.stdout is not None
    try:
        # 한 줄씩 읽는다. 통째로 받으면 긴 영상에서 진행률을 알려줄 수가 없다.
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            head = _FRAME_LINE.match(line)
            if head:
                # 점수는 다음 줄에 온다. 프레임 헤더만 보고는 아직 확정할 수 없다.
                pending = Frame(
                    index=int(head.group(1)),
                    time_us=int(round(float(head.group(3)) * US)),
                    score=0.0,
                )
                continue
            value = _SCORE_LINE.match(line)
            if value and pending is not None:
                pending.score = float(value.group(1))
                frames.append(pending)
                pending = None
                if on_progress:
                    done = min((frames[-1].time_us - start_us) / span_us, 1.0)
                    if done - reported >= 0.02:
                        reported = done
                        on_progress(done)
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        if proc.stderr:
            proc.stderr.close()
        proc.wait()

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()
        raise MediaError(f"장면 분석에 실패했습니다: {detail[:300]}")
    if not frames:
        raise MediaError("프레임을 하나도 읽지 못했습니다. 영상이 손상됐을 수 있습니다.")

    if on_progress:
        on_progress(1.0)
    return frames


# ---------------------------------------------------------------- 2단계: 컷 판정


def find_cuts(
    frames: list[Frame],
    *,
    threshold: float = 12.0,
    adaptive: bool = True,
    ratio_threshold: float = 3.0,
    min_scene_us: int = 400_000,
) -> list[Cut]:
    """점수 곡선에서 컷 지점을 고른다. 영상을 다시 읽지 않으므로 즉시 끝난다.

    두 조건을 모두 넘겨야 컷으로 본다.

    - 절대 점수가 `threshold` 이상. 화면이 실제로 크게 바뀌었는가.
    - 주변 프레임 평균의 `ratio_threshold` 배 이상. 원래 움직임이 많은 구간에서
      평소만큼 흔들린 것을 컷으로 착각하지 않기 위해서다. 카메라가 계속 패닝하는
      장면은 점수가 통째로 높아서 절대값만 보면 온통 컷이 된다.

    `min_scene_us` 는 너무 짧은 장면을 지운다. 플래시나 한두 프레임 튀는 노이즈가
    컷 두 개를 만들어 내는 것을 막는다.
    """
    if not frames:
        return []

    picked: list[Cut] = []
    last_us: int | None = None

    for i, frame in enumerate(frames):
        # 첫 프레임은 항상 점수가 0 이고, 장면의 '시작' 이지 '전환' 이 아니다.
        if i == 0 or frame.score < threshold:
            continue

        ratio = _neighbor_ratio(frames, i)
        if adaptive and ratio < ratio_threshold:
            continue

        # 직전 컷과 너무 붙어 있으면, 더 강한 쪽만 남긴다. 앞엣것을 그냥 두면 약한 컷이
        # 뒤의 진짜 전환을 밀어내 버린다.
        if last_us is not None and frame.time_us - last_us < min_scene_us:
            if frame.score > picked[-1].score:
                picked[-1] = Cut(frame.index, frame.time_us, frame.score, ratio)
                last_us = frame.time_us
            continue

        picked.append(Cut(frame.index, frame.time_us, frame.score, ratio))
        last_us = frame.time_us

    return picked


def _neighbor_ratio(frames: list[Frame], i: int) -> float:
    """이 프레임 점수가 주변 평균의 몇 배인지."""
    lo = max(0, i - NEIGHBOR_RADIUS)
    hi = min(len(frames), i + NEIGHBOR_RADIUS + 1)
    others = [frames[j].score for j in range(lo, hi) if j != i]
    if not others:
        return float("inf")
    baseline = max(sum(others) / len(others), _RATIO_FLOOR)
    return frames[i].score / baseline


# ------------------------------------------------------------------- 곁들이 정보


def score_curve(frames: list[Frame], buckets: int = 600) -> list[float]:
    """화면에 그릴 점수 곡선. 프레임 수가 많으므로 구간별 최대값으로 줄인다.

    평균이 아니라 최대값으로 줄인다. 컷은 한 프레임짜리 뾰족한 봉우리라서 평균을 내면
    사라져 버리고, 그러면 곡선을 봐도 임계값을 어디에 둘지 알 수 없다.
    """
    if not frames:
        return []
    if len(frames) <= buckets:
        return [round(f.score, 2) for f in frames]

    step = len(frames) / buckets
    out: list[float] = []
    for b in range(buckets):
        lo = int(b * step)
        hi = max(int((b + 1) * step), lo + 1)
        out.append(round(max(f.score for f in frames[lo:hi]), 2))
    return out


def suggest_threshold(frames: list[Frame], bins: int = 256) -> float:
    """점수 분포를 보고 임계값을 하나 제안한다.

    컷 프레임은 전체의 몇 퍼센트뿐이라 점수 분포가 두 덩어리로 갈린다. 대부분의 프레임이
    모인 낮은 덩어리와, 실제 전환이 만든 높은 봉우리다. 그 사이 골짜기를 Otsu 이진화로
    찾는다. 두 무리로 갈랐을 때 무리 사이 분산이 가장 커지는 경계다.

    백분위수로 자르는 방식은 컷 비율을 미리 안다고 가정하는 셈이라, 컷이 촘촘한 영상과
    긴 장면 하나짜리 영상에서 서로 다른 방향으로 빗나간다. Otsu 는 분포 모양만 본다.
    """
    scores = [f.score for f in frames[1:]]
    top = max(scores, default=0.0)
    if not scores or top <= 0:
        return 12.0

    width = top / bins
    hist = [0] * bins
    for value in scores:
        hist[min(int(value / width), bins - 1)] += 1

    total = len(scores)
    sum_all = sum(i * hist[i] for i in range(bins))
    seen = 0
    sum_low = 0.0
    best_var = -1.0
    best_bin = 0
    for i in range(bins):
        seen += hist[i]
        if seen == 0 or seen == total:
            continue
        sum_low += i * hist[i]
        mean_low = sum_low / seen
        mean_high = (sum_all - sum_low) / (total - seen)
        variance = seen * (total - seen) * (mean_low - mean_high) ** 2
        if variance > best_var:
            best_var = variance
            best_bin = i

    # 경계 칸의 위쪽 끝을 쓴다. 골짜기 칸 자체는 아직 낮은 덩어리에 속한다.
    return round(min(max((best_bin + 1) * width, 4.0), 60.0), 1)
