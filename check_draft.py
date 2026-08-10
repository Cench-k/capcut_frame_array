"""분할 결과가 성립하는지 검사한다.

CapCut 은 어긋난 드래프트를 열어도 대개 오류를 내지 않고 조용히 이상하게 그린다.
그래서 파일을 쓰기 전에 여기서 조건을 확인한다. 개발 중 검증용이자, 서버가 저장 직전에
한 번 더 부르는 안전장치다.
"""

from __future__ import annotations

from capcut_draft import Draft


class DraftBroken(Exception):
    """드래프트 구조가 어긋났다."""


def verify(draft: Draft, *, expect_duration_us: int | None = None) -> list[str]:
    """이상한 점을 문자열 목록으로 돌려준다. 비어 있으면 정상이다."""
    problems: list[str] = []
    ids = {
        item["id"]
        for items in draft.materials.values()
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict) and item.get("id")
    }

    for track_index, track in enumerate(draft.data.get("tracks", [])):
        segments = track.get("segments", [])
        ordered = sorted(segments, key=lambda s: s.get("target_timerange", {}).get("start", 0))
        previous_end = None
        for segment in ordered:
            target = segment.get("target_timerange") or {}
            start, length = int(target.get("start", 0)), int(target.get("duration", 0))

            if length <= 0:
                problems.append(f"트랙 {track_index}: 길이가 0 인 조각이 있습니다 ({start}us)")
            if segment.get("material_id") and segment["material_id"] not in ids:
                problems.append(f"트랙 {track_index}: 소재를 잃은 조각이 있습니다 ({start}us)")
            for ref in segment.get("extra_material_refs", []):
                if ref not in ids:
                    problems.append(f"트랙 {track_index}: 부속 소재를 잃은 조각이 있습니다 ({start}us)")
                    break

            # 영상 트랙은 조각이 이어 붙어 있어야 한다. 겹치면 어느 쪽이 보일지 알 수 없고,
            # 벌어지면 검은 화면이 낀다. 분할은 나누기만 하므로 둘 다 생기면 안 된다.
            if track.get("type") == "video" and previous_end is not None and start != previous_end:
                gap = start - previous_end
                word = "겹침" if gap < 0 else "빈틈"
                problems.append(f"트랙 {track_index}: 조각 사이 {word} {abs(gap)}us ({start}us 지점)")
            previous_end = start + length

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
        problems.append(
            f"전체 길이가 바뀌었습니다: {expect_duration_us} -> {draft.duration_us}"
        )

    return problems
