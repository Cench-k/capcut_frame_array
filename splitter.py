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

    def as_dict(self) -> dict:
        return {
            "pieces": self.pieces,
            "applied_cuts": self.applied_cuts,
            "skipped_clips": self.skipped_clips,
        }


def _deep(value):
    """CapCut 구조는 중첩이 깊다. 얕은 복사는 원본과 값을 공유해 버린다."""
    return json.loads(json.dumps(value))


def source_to_target_us(clip: VideoClip, source_us: int) -> int:
    """원본 파일의 시각을 타임라인 시각으로 옮긴다.

    배속이 걸린 조각은 원본 1 초가 타임라인에서 1 초가 아니다. `speed` 로 나눠준다.
    """
    speed = clip.speed if clip.speed > 0 else 1.0
    return clip.target_start_us + int(round((source_us - clip.source_start_us) / speed))


def usable_cuts(clip: VideoClip, cuts_source_us: list[int]) -> list[int]:
    """이 조각 안에 실제로 넣을 수 있는 컷만 남긴다.

    조각이 원본의 일부만 쓰고 있을 수 있으므로 범위 밖은 버린다. 조각의 시작과 끝에
    딱 붙은 컷도 버린다. 길이 0 짜리 조각이 생기기 때문이다.
    """
    lo = clip.source_start_us
    hi = clip.source_start_us + clip.source_duration_us
    speed = clip.speed if clip.speed > 0 else 1.0
    margin = int(MIN_PIECE_US * speed)

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
        if _has_keyframes(draft, clip):
            # 키프레임은 세그먼트 안에서의 상대 시각으로 저장된다. 쪼개면 각 조각에 맞춰
            # 다시 계산해야 하는데, 잘못 옮기면 확대/이동 애니메이션이 조용히 어긋난다.
            # 손대지 않고 건너뛰고 사용자에게 알린다.
            result.skipped_clips.append(clip.name)
            continue
        made = split_clip(draft, clip, cuts)
        if made:
            result.pieces += made
            result.applied_cuts += made - 1

    return result


def _has_keyframes(draft: Draft, clip: VideoClip) -> bool:
    segment = draft.data["tracks"][clip.track_index]["segments"][clip.segment_index]
    return bool(segment.get("common_keyframes")) or bool(segment.get("keyframe_refs"))
