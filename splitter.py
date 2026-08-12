"""컷 지점 목록을 받아 CapCut 세그먼트를 실제로 쪼갠다.

CapCut 이 손으로 분할할 때 하는 일을 그대로 흉내낸다. 실제 드래프트(한 영상을 88 조각으로
나눈 프로젝트)를 뜯어보고 확인한 규칙은 이렇다.

- 조각마다 영상 material 을 **통째로 복제**한다. 88 개 복제본을 비교하면 서로 다른 필드는
  `id` 하나뿐이다. `path` 도 `duration` 도 심지어 `material_id` 도 전부 같다. 즉 복제본의
  `duration` 은 여전히 **원본 파일 전체 길이**이고, 실제로 쓰는 구간은 세그먼트의
  `source_timerange` 가 정한다.
- 세그먼트마다 부속 material 6 종(`speeds`, `placeholder_infos`, `canvases`,
  `sound_channel_mappings`, `material_colors`, `vocal_separations`)을 각자 하나씩 갖는다.
  공유하지 않는다. 빠지면 CapCut 이 소재를 인식하지 못한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from capcut_draft import EXTRA_POOLS, US, Draft, VideoClip, new_id
from errors import UserError

# 조각 하나가 최소한 이만큼은 되어야 한다. 프레임 하나보다 짧은 조각은 CapCut 에서 잡히지도
# 않고 타임라인만 어지럽힌다. 60fps 기준 한 프레임(약 16,667us)보다 약간 넉넉하게 잡았다.
MIN_PIECE_US = 20_000


class SplitError(UserError):
    """사용자에게 그대로 보여줄 오류."""


@dataclass
class SplitResult:
    """분할 결과 요약."""

    pieces: int = 0
    applied_cuts: int = 0
    skipped_clips: list[str] = field(default_factory=list)
    # 건너뛴 이유만 따로. 안내 문구를 이유에 맞게 쓰기 위해서다.
    skip_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pieces": self.pieces,
            "applied_cuts": self.applied_cuts,
            "skipped_clips": self.skipped_clips,
        }


def _deep(value):
    """CapCut 구조는 중첩이 깊다. 얕은 복사는 원본과 값을 공유해 버린다."""
    return json.loads(json.dumps(value))


def effective_speed(clip: VideoClip) -> float:
    """이 조각에서 원본 시간이 타임라인 시간으로 얼마나 줄어드는지.

    `speed` 필드를 그대로 믿지 않고 **실제 길이의 비율**로 구한다. 둘은 대개 같지만 항상
    같지는 않다. 실제 프로젝트를 훑어보니 `speed` 가 1.2 라고 적혀 있는데 길이 비율은 1.2061
    인 조각이 있었다. 0.5% 차이인데, 그 값으로 옮기면 60 초 지점의 컷이 약 250ms 밀린다.
    장면 전환을 프레임 단위로 맞추려는 도구에서 눈에 띄는 어긋남이다.

    길이 비율은 정의상 이 조각의 양 끝을 정확히 맞추므로, 반올림 말고는 틀릴 여지가 없다.
    """
    if clip.source_duration_us > 0 and clip.target_duration_us > 0:
        return clip.source_duration_us / clip.target_duration_us
    return clip.speed if clip.speed > 0 else 1.0


def source_to_target_us(clip: VideoClip, source_us: int) -> int:
    """원본 파일의 시각을 타임라인 시각으로 옮긴다.

    배속이 걸린 조각은 원본 1 초가 타임라인에서 1 초가 아니다.
    """
    return clip.target_start_us + int(
        round((source_us - clip.source_start_us) / effective_speed(clip))
    )


def usable_cuts(clip: VideoClip, cuts_source_us: list[int]) -> list[int]:
    """이 조각 안에 실제로 넣을 수 있는 컷만 남긴다.

    조각이 원본의 일부만 쓰고 있을 수 있으므로 범위 밖은 버린다. 조각의 시작과 끝에
    딱 붙은 컷도 버린다. 길이 0 짜리 조각이 생기기 때문이다.
    """
    lo = clip.source_start_us
    hi = clip.source_start_us + clip.source_duration_us
    # 최소 길이는 타임라인 기준이므로 원본 기준으로 바꿔서 잰다.
    margin = int(MIN_PIECE_US * effective_speed(clip))

    kept: list[int] = []
    for cut in sorted(set(int(c) for c in cuts_source_us)):
        if cut <= lo + margin or cut >= hi - margin:
            continue
        if kept and cut - kept[-1] < margin:
            continue
        kept.append(cut)
    return kept


def split_clip(draft: Draft, clip: VideoClip, cuts_source_us: list[int]) -> int:
    """조각 하나를 컷 지점에서 쪼갠다. 만들어진 조각 수를 돌려준다.

    타임라인 전체 길이는 변하지 않는다. 나누기만 할 뿐 늘이거나 줄이지 않으므로 오디오와
    자막 트랙을 건드릴 필요가 없다.
    """
    cuts = usable_cuts(clip, cuts_source_us)
    if not cuts:
        return 0

    track = draft.data["tracks"][clip.track_index]
    segments = track["segments"]
    original = segments[clip.segment_index]
    if original.get("id") != clip.segment_id:
        raise SplitError(
            "드래프트가 그사이 바뀌었습니다. 프로젝트를 다시 읽어주세요."
        )

    material = draft.video_material(clip.material_id)
    if material is None:
        raise SplitError(f"영상 소재를 찾지 못했습니다: {clip.name}")

    # 원본 시각 경계. 양 끝을 포함하므로 조각 수는 경계 수보다 하나 적다.
    src_edges = [clip.source_start_us, *cuts, clip.source_start_us + clip.source_duration_us]

    # 타임라인 경계. 마지막은 반올림 오차가 끼지 않게 원래 끝값을 그대로 쓴다. 이렇게 해야
    # 조각들의 길이 합이 원래 길이와 정확히 같아지고, 뒤 트랙과 어긋나지 않는다.
    tgt_edges = [source_to_target_us(clip, edge) for edge in src_edges]
    tgt_edges[0] = clip.target_start_us
    tgt_edges[-1] = clip.target_start_us + clip.target_duration_us

    new_segments = []
    for i in range(len(src_edges) - 1):
        piece = _deep(original)
        piece["id"] = new_id()
        piece["material_id"] = _clone_material(draft, material)
        piece["extra_material_refs"] = _clone_extras(draft, original.get("extra_material_refs", []))
        piece["source_timerange"] = {
            "start": src_edges[i],
            "duration": src_edges[i + 1] - src_edges[i],
        }
        piece["target_timerange"] = {
            "start": tgt_edges[i],
            "duration": tgt_edges[i + 1] - tgt_edges[i],
        }
        # 첫 조각이 아니면 원본 세그먼트를 가리키던 흔적을 지운다. 남겨두면 CapCut 이
        # 서로 다른 조각을 같은 것으로 묶어 버린다.
        piece.pop("raw_segment_id", None)
        new_segments.append(piece)

    segments[clip.segment_index : clip.segment_index + 1] = new_segments

    # 원본 세그먼트가 쓰던 material 과 부속들은 이제 아무도 가리키지 않는다. 남겨두면
    # CapCut 이 '사용하지 않는 소재' 로 안고 가므로 정리한다.
    _drop_material(draft, material["id"])
    _drop_extras(draft, original.get("extra_material_refs", []))

    return len(new_segments)


def _clone_material(draft: Draft, material: dict) -> str:
    """영상 material 을 복제하고 새 id 를 돌려준다. 바뀌는 것은 id 뿐이다."""
    clone = _deep(material)
    clone["id"] = new_id()
    draft.materials.setdefault("videos", []).append(clone)
    return clone["id"]


def _clone_extras(draft: Draft, ref_ids: list[str]) -> list[str]:
    """세그먼트에 딸린 부속 material 6 종을 복제한다.

    풀 이름을 미리 정해두지 않고 id 로 찾는다. 버전에 따라 딸리는 부속이 늘거나 줄 수 있는데,
    원본이 가리키던 것을 그대로 하나씩 복제하면 그런 변화를 따라갈 필요가 없다.
    """
    index = _pool_index(draft)
    cloned: list[str] = []
    for ref in ref_ids:
        found = index.get(ref)
        if not found:
            # 어느 풀에도 없는 참조. 원본이 이미 깨져 있던 것이므로 그대로 넘긴다.
            cloned.append(ref)
            continue
        pool_name, item = found
        clone = _deep(item)
        clone["id"] = new_id()
        draft.materials[pool_name].append(clone)
        cloned.append(clone["id"])
    return cloned


def _pool_index(draft: Draft) -> dict[str, tuple[str, dict]]:
    """materials 아래 모든 목록을 훑어 id -> (풀 이름, 항목) 색인을 만든다."""
    index: dict[str, tuple[str, dict]] = {}
    for pool_name, items in draft.materials.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                index[item["id"]] = (pool_name, item)
    return index


def _drop_material(draft: Draft, material_id: str) -> None:
    videos = draft.materials.get("videos", [])
    draft.materials["videos"] = [m for m in videos if m.get("id") != material_id]


def _drop_extras(draft: Draft, ref_ids: list[str]) -> None:
    targets = set(ref_ids)
    if not targets:
        return
    for pool_name in EXTRA_POOLS:
        items = draft.materials.get(pool_name)
        if isinstance(items, list):
            draft.materials[pool_name] = [m for m in items if m.get("id") not in targets]


# ------------------------------------------------------------------ 여러 조각 한 번에


def apply_cuts(draft: Draft, plan: dict[str, list[int]]) -> SplitResult:
    """`{세그먼트 id: [원본 시각 컷들]}` 을 드래프트에 반영한다.

    뒤쪽 조각부터 처리한다. 앞쪽을 먼저 쪼개면 세그먼트가 늘어나면서 뒤쪽 조각의
    `segment_index` 가 밀려 엉뚱한 것을 건드리게 된다.
    """
    result = SplitResult()
    clips = {c.segment_id: c for c in draft.video_clips()}

    ordered = sorted(
        (clips[sid] for sid in plan if sid in clips),
        key=lambda c: (c.track_index, c.segment_index),
        reverse=True,
    )

    for clip in ordered:
        cuts = plan.get(clip.segment_id) or []
        if not cuts:
            continue
        blocker = split_blocker(draft, clip)
        if blocker:
            # 손대지 않고 건너뛴다. 왜 건너뛰었는지 같이 들고 가야 사용자가 조치할 수 있다.
            result.skipped_clips.append(f"{clip.name} ({blocker})")
            result.skip_reasons.append(blocker)
            continue
        made = split_clip(draft, clip, cuts)
        if made:
            result.pieces += made
            result.applied_cuts += made - 1

    return result


def split_blocker(draft: Draft, clip: VideoClip) -> str | None:
    """이 조각을 쪼개면 안 되는 이유. 쪼개도 되면 None.

    여기 걸리는 것들은 모두 **원본 시각과 타임라인 시각의 관계가 단순 비례가 아니거나**,
    쪼개는 순간 딸린 정보를 같이 옮겨야 하는 경우다. 억지로 쪼개면 파일은 멀쩡해 보이고
    CapCut 도 오류를 내지 않는데 결과만 조용히 어긋난다. 이 도구에서 가장 나쁜 실패 방식이라,
    어설프게 처리하느니 손대지 않고 이유를 알린다.

    계산 자체가 불가능한 것은 아니다. 다만 맞는지 확인할 실제 자료가 없다. 사용자의 프로젝트
    356 개에 이 셋을 쓰는 것이 하나도 없었다. 검증 못 한 변환을 넣는 것보다 건너뛰는 편이 낫다.
    """
    segment = draft.data["tracks"][clip.track_index]["segments"][clip.segment_index]

    # 키프레임은 세그먼트 안에서의 상대 시각으로 저장된다. 쪼개면 각 조각에 맞춰 다시
    # 계산해야 하는데, 잘못 옮기면 확대/이동 애니메이션이 어긋난다.
    if segment.get("common_keyframes") or segment.get("keyframe_refs"):
        return "키프레임(확대·이동 애니메이션)"

    # 거꾸로 재생하는 조각은 원본의 뒤가 타임라인의 앞에 온다. 같은 식으로 옮기면 컷이
    # 좌우로 뒤집힌 자리에 박힌다.
    if segment.get("reverse"):
        return "역재생"

    # 변속 커브는 구간마다 배속이 다르다. 평균 배속으로 옮기면 곡선이 휜 만큼 밀린다.
    speeds = {s["id"]: s for s in draft.materials.get("speeds", []) if s.get("id")}
    for ref in segment.get("extra_material_refs", []):
        item = speeds.get(ref)
        if item and item.get("curve_speed"):
            return "변속 커브"

    return None
