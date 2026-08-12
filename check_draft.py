"""분할 결과가 성립하는지 검사한다.

CapCut 은 어긋난 드래프트를 열어도 대개 오류를 내지 않고 조용히 이상하게 그린다. 그래서
파일을 쓰기 전에 여기서 조건을 확인하고, 하나라도 걸리면 저장하지 않는다.

**무엇이 '정상' 인지 미리 정해두지 않는다.** 처음에는 "영상 트랙의 조각은 빈틈 없이 이어져야
한다" 를 규칙으로 삼았는데, 실제 프로젝트 356 개에 대보니 33 개(9%)가 걸렸다. 오버레이(PIP)
트랙은 화면에 잠깐씩만 얹히므로 사이가 비는 것이 정상인데, 그것을 고장으로 본 것이다.
멀쩡한 프로젝트를 손도 못 대게 막는 셈이었다.

그래서 절대 기준 대신 **고치기 전과 후를 견준다.** 분할은 나누기만 할 뿐 늘이거나 줄이지
않으므로, 트랙이 덮는 시간대가 그대로여야 한다. 원래 빈틈이 있었으면 그대로 있으면 된다.
"""

from __future__ import annotations

from capcut_draft import Draft

# 트랙 종류별 (시작, 길이) 목록.
Snapshot = dict[int, list[tuple[int, int]]]


def snapshot(draft: Draft) -> Snapshot:
    """지금 트랙들이 어느 시간대를 덮고 있는지 적어둔다. 고치기 **전에** 불러야 한다."""
    return {
        index: [
            (
                int((s.get("target_timerange") or {}).get("start", 0)),
                int((s.get("target_timerange") or {}).get("duration", 0)),
            )
            for s in track.get("segments", [])
        ]
        for index, track in enumerate(draft.data.get("tracks", []))
    }


def _covered(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """맞닿거나 겹치는 구간을 하나로 합친다.

    조각 하나를 여럿으로 나누면 조각 목록은 달라지지만 합쳐놓은 구간은 그대로다. 분할이
    제대로 됐는지 보려면 이 모양을 견주면 된다.
    """
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [list(ordered[0])]
    for start, length in ordered[1:]:
        last = merged[-1]
        if start <= last[0] + last[1]:  # 맞닿거나 겹친다
            last[1] = max(last[0] + last[1], start + length) - last[0]
        else:
            merged.append([start, length])
    return [(a, b) for a, b in merged]


def verify(
    draft: Draft,
    *,
    before: Snapshot | None = None,
    expect_duration_us: int | None = None,
) -> list[str]:
    """이상한 점을 문자열 목록으로 돌려준다. 비어 있으면 정상이다."""
    problems: list[str] = []
    ids = {
        item["id"]
        for items in draft.materials.values()
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict) and item.get("id")
    }

    for index, track in enumerate(draft.data.get("tracks", [])):
        segments = track.get("segments", [])
        ordered = sorted(segments, key=lambda s: (s.get("target_timerange") or {}).get("start", 0))

        previous_end = None
        for segment in ordered:
            target = segment.get("target_timerange") or {}
            start, length = int(target.get("start", 0)), int(target.get("duration", 0))

            if length <= 0:
                problems.append(f"트랙 {index}: 길이가 0 이하인 조각이 있습니다 ({start}us)")
            if segment.get("material_id") and segment["material_id"] not in ids:
                problems.append(f"트랙 {index}: 소재를 잃은 조각이 있습니다 ({start}us)")
            for ref in segment.get("extra_material_refs", []):
                if ref not in ids:
                    problems.append(f"트랙 {index}: 부속 소재를 잃은 조각이 있습니다 ({start}us)")
                    break

            # 한 트랙 안에서 조각이 겹치면 어느 쪽이 보일지 알 수 없다. 실제 프로젝트
            # 356 개를 훑어보니 겹치는 것이 하나도 없었다. 정상이 아닌 상태로 본다.
            if previous_end is not None and start < previous_end:
                problems.append(f"트랙 {index}: 조각이 {previous_end - start}us 겹칩니다 ({start}us 지점)")
            previous_end = start + length

        # 빈틈은 그 자체로는 잘못이 아니다. 오버레이 트랙은 원래 띄엄띄엄 놓인다.
        # 우리가 새로 만들어낸 빈틈만 잡는다.
        if before is not None:
            was = _covered(before.get(index, []))
            now = _covered([
                (
                    int((s.get("target_timerange") or {}).get("start", 0)),
                    int((s.get("target_timerange") or {}).get("duration", 0)),
                )
                for s in segments
            ])
            if was != now:
                problems.append(
                    f"트랙 {index}: 덮는 구간이 달라졌습니다. 분할은 나누기만 해야 합니다."
                )

    # 세그먼트끼리 id 나 소재를 나눠 쓰면 CapCut 이 서로 묶어서 다룬다.
    seen_segments: set[str] = set()
    seen_materials: set[str] = set()
    for track in draft.data.get("tracks", []):
        for segment in track.get("segments", []):
            sid = segment.get("id")
            if sid in seen_segments:
                problems.append(f"세그먼트 id 가 중복입니다: {sid}")
            seen_segments.add(sid)
            mid = segment.get("material_id")
            if mid and mid in seen_materials:
                problems.append(f"소재를 두 조각이 나눠 쓰고 있습니다: {mid}")
            seen_materials.add(mid)

    if expect_duration_us is not None and draft.duration_us != expect_duration_us:
        problems.append(f"전체 길이가 바뀌었습니다: {expect_duration_us} -> {draft.duration_us}")

    return problems
