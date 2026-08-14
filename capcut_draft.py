"""CapCut draft_content.json 읽기 / 쓰기.

CapCut International(`platform.app_source == "cc"`) 6.x~9.x 의 평문 드래프트만 다룬다.
剪映(JianYing) 6.0+ 는 저장할 때마다 암호화하므로 이 방식이 통하지 않는다.

capcut_matcher 의 같은 이름 모듈에서 갈라져 나왔다. 그쪽은 자막과 이미지를 다루고
이쪽은 영상 세그먼트를 다룬다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# CapCut 타임라인의 모든 시간 값은 마이크로초 단위다.
US = 1_000_000

# 드래프트 본문 파일 이름. 윈도우는 draft_content.json, 맥은 draft_info.json 을 쓴다.
DRAFT_FILENAMES = ("draft_content.json", "draft_info.json")

# 세그먼트 하나에 반드시 딸려야 하는 부속 material 6 종. `extra_material_refs` 가 이들을
# 가리키며, 하나라도 빠지면 CapCut 이 그 세그먼트를 소재로 인식하지 못한다.
EXTRA_POOLS = (
    "speeds",
    "placeholder_infos",
    "canvases",
    "sound_channel_mappings",
    "material_colors",
    "vocal_separations",
)


def capcut_user_data() -> Path:
    """CapCut 이 사용자 데이터를 두는 곳."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "CapCut" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Movies" / "CapCut" / "User Data"
    return Path.home() / ".capcut" / "User Data"


DRAFT_ROOT = capcut_user_data() / "Projects" / "com.lveditor.draft"


def capcut_running() -> bool:
    """CapCut 이 지금 켜져 있는가.

    켜진 채로 드래프트를 고쳐 쓰면 **작업이 조용히 사라진다.** CapCut 은 프로젝트를 메모리에
    들고 있다가 제 시점에 통째로 다시 쓰는데, 그때 우리가 쓴 내용이 통째로 덮인다. 파일 쓰기는
    성공하고 오류도 안 나므로, 사용자 눈에는 '프로그램이 안 먹는다' 로만 보인다.

    실제로 이 일이 있었다. 적용 네 번이 전부 저장에 성공했는데 CapCut 이 매번 되돌려서,
    조각이 하나도 안 늘었다.
    """
    if sys.platform != "win32":
        return False
    try:
        done = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq CapCut.exe", "/NH"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        # 확인할 수 없으면 막지 않는다. 막았다가 쓰지도 못하게 되는 쪽이 더 나쁘다.
        return False
    # 찾는 것이 없으면 tasklist 는 안내 문구만 내보낸다. 이름이 보일 때만 켜진 것으로 본다.
    return b"CapCut.exe" in done.stdout


def count_video_segments(folder: str | Path) -> int:
    """지금 파일에 영상 조각이 몇 개인지. 드래프트를 통째로 해석하지 않고 세기만 한다.

    적용한 결과가 살아남았는지 확인하는 데 쓴다. CapCut 이 되가져갔는지는 **결과를 다시
    읽어보는 것** 말고 확실한 방법이 없다. 프로세스가 떠 있는지 보는 것은 어디까지나 짐작이라,
    오늘처럼 적용 직후에 CapCut 이 켜지는 경우를 놓친다.
    """
    root = Path(folder)
    found = find_draft_file(root)
    if not found:
        return -1
    # 본문이 여러 곳에 있으면 가장 최근 것을 본다. Draft 가 읽는 것과 같은 기준이어야
    # '저장한 것이 살아남았는가' 를 제대로 판단할 수 있다.
    newest = max([found, *timeline_copies(root)], key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return -1
    return sum(
        len(t.get("segments", []))
        for t in data.get("tracks", [])
        if t.get("type") == "video"
    )


def find_draft_file(folder: Path) -> Path | None:
    for name in DRAFT_FILENAMES:
        candidate = folder / name
        if candidate.exists():
            return candidate
    return None


def timeline_copies(folder: Path) -> list[Path]:
    """`Timelines/<타임라인 id>/draft_content.json` 사본들.

    최신 CapCut 은 한 프로젝트에 여러 타임라인을 둘 수 있고, 그 구조에서는 본문이 **두 곳에
    똑같이** 있다. 폴더 맨 위의 것과 `Timelines/<id>/` 안의 것이다. 실제 프로젝트 359 개 중
    127 개가 이 구조였고, 그중 126 개는 두 파일이 바이트까지 같았다. CapCut 이 저장할 때
    양쪽을 함께 쓰기 때문이다.

    **맨 위의 것만 고치면 안 된다.** CapCut 이 프로젝트를 열 때 `Timelines/` 쪽을 읽어서
    예전 내용이 올라오고, 그것이 다시 맨 위로 써지면서 우리 작업이 사라진다. "적용됐다고
    했는데 CapCut 을 열면 원래대로" 가 정확히 이 증상이다.

    `.bak` 과 `template-2.tmp` 도 같은 내용을 담고 있지만 건드리지 않는다. `.bak` 은 CapCut
    자신의 복구용이라 남겨두는 편이 사용자에게 안전하고, `.tmp` 는 저장할 때 다시 만들어진다.
    """
    root = folder / "Timelines"
    if not root.is_dir():
        return []

    # project.json 이 주 타임라인을 알려준다. 없으면 있는 것을 전부 맞춰둔다.
    wanted: set[str] = set()
    config = root / "project.json"
    if config.is_file():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        main = data.get("main_timeline_id")
        if main:
            wanted.add(str(main))

    found: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir() or (wanted and child.name not in wanted):
            continue
        for name in DRAFT_FILENAMES:
            candidate = child / name
            if candidate.is_file():
                found.append(candidate)
    return found


def new_id() -> str:
    """CapCut 이 쓰는 대문자 UUID 포맷."""
    return str(uuid.uuid4()).upper()


def list_drafts(root: Path | None = None) -> list[Path]:
    """드래프트 폴더를 최근 수정순으로."""
    root = root or DRAFT_ROOT
    if not root.exists():
        return []
    folders = [p for p in root.iterdir() if p.is_dir() and find_draft_file(p)]
    return sorted(folders, key=lambda p: p.stat().st_mtime, reverse=True)


@dataclass
class VideoClip:
    """영상 트랙에 놓인 조각 하나. 여기에 컷을 넣는다."""

    track_index: int
    segment_index: int
    segment_id: str
    material_id: str
    path: str
    name: str
    # 원본 파일에서 어느 구간을 쓰는지.
    source_start_us: int
    source_duration_us: int
    # 타임라인에서 어디에 놓였는지.
    target_start_us: int
    target_duration_us: int
    speed: float
    exists: bool

    def as_dict(self) -> dict:
        return {
            "track_index": self.track_index,
            "segment_index": self.segment_index,
            "segment_id": self.segment_id,
            "path": self.path,
            "name": self.name,
            "source_start_us": self.source_start_us,
            "source_duration_us": self.source_duration_us,
            "target_start_us": self.target_start_us,
            "target_duration_us": self.target_duration_us,
            "speed": self.speed,
            "exists": self.exists,
        }


class Draft:
    """draft_content.json 한 개를 감싼다."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        found = find_draft_file(self.folder)
        if not found:
            raise FileNotFoundError(
                f"드래프트 파일({' 또는 '.join(DRAFT_FILENAMES)})이 없습니다: {self.folder}"
            )
        # 본문이 여러 곳에 똑같이 있는 구조라면 **가장 최근에 쓰인 것**을 읽는다. 맨 위의
        # 것이 낡아 있는 경우가 실제로 있었다(359 개 중 1 개). 낡은 쪽을 읽어 고치면 새 내용을
        # 옛것으로 덮어쓰게 된다.
        self.mirrors = timeline_copies(self.folder)
        self.path = max([found, *self.mirrors], key=lambda p: p.stat().st_mtime)
        raw = self.path.read_text(encoding="utf-8")
        if not raw.lstrip().startswith("{"):
            raise ValueError(
                f"{self.folder.name}: {self.path.name} 이 평문 JSON 이 아닙니다. "
                "剪映(JianYing) 6.0+ 는 저장할 때마다 드래프트를 암호화해서 읽을 수 없습니다."
            )
        self.data: dict = json.loads(raw)
        self._verify_supported()

    # ------------------------------------------------------------------ 검증

    def _verify_supported(self) -> None:
        platform = self.data.get("platform") or {}
        source = platform.get("app_source")
        if source and source != "cc":
            raise ValueError(
                f"{self.folder.name}: app_source='{source}' 는 CapCut International 이 아닙니다. "
                "이 도구는 app_source='cc' 만 지원합니다."
            )

    # ------------------------------------------------------------------ 조회

    @property
    def name(self) -> str:
        return self.folder.name

    @property
    def duration_us(self) -> int:
        return int(self.data.get("duration", 0))

    @property
    def canvas(self) -> tuple[int, int]:
        cfg = self.data.get("canvas_config") or {}
        return int(cfg.get("width", 1920)), int(cfg.get("height", 1080))

    @property
    def materials(self) -> dict:
        return self.data.setdefault("materials", {})

    def tracks_of(self, kind: str) -> list[dict]:
        return [t for t in self.data.get("tracks", []) if t.get("type") == kind]

    def video_material(self, material_id: str) -> dict | None:
        for mat in self.materials.get("videos", []):
            if mat.get("id") == material_id:
                return mat
        return None

    def video_clips(self) -> list[VideoClip]:
        """영상 트랙의 조각들을 트랙 순서 / 타임라인 순서대로 펼친다.

        `type == "photo"` 인 소재는 뺀다. CapCut 은 이미지도 `materials.videos` 에 담는데,
        정지 이미지에는 감지할 장면 전환이 없다.
        """
        clips: list[VideoClip] = []
        for track_index, track in enumerate(self.data.get("tracks", [])):
            if track.get("type") != "video":
                continue
            segments = track.get("segments", [])
            order = sorted(
                range(len(segments)),
                key=lambda i: segments[i].get("target_timerange", {}).get("start", 0),
            )
            for segment_index in order:
                segment = segments[segment_index]
                material = self.video_material(segment.get("material_id", ""))
                if not material or material.get("type") != "video":
                    continue
                raw_path = material.get("path") or ""
                source = segment.get("source_timerange") or {}
                target = segment.get("target_timerange") or {}
                clips.append(
                    VideoClip(
                        track_index=track_index,
                        segment_index=segment_index,
                        segment_id=segment.get("id", ""),
                        material_id=material.get("id", ""),
                        path=raw_path,
                        name=material.get("material_name") or Path(raw_path).name or "(이름없음)",
                        source_start_us=int(source.get("start", 0)),
                        source_duration_us=int(source.get("duration", 0)),
                        target_start_us=int(target.get("start", 0)),
                        target_duration_us=int(target.get("duration", 0)),
                        speed=float(segment.get("speed", 1.0) or 1.0),
                        exists=bool(raw_path) and Path(raw_path).is_file(),
                    )
                )
        return clips

    def summary(self) -> dict:
        width, height = self.canvas
        platform = self.data.get("platform") or {}
        return {
            "folder": str(self.folder),
            "name": self.name,
            "app_version": platform.get("app_version", "unknown"),
            "width": width,
            "height": height,
            "duration_us": self.duration_us,
            "tracks": [
                {"type": t.get("type", "?"), "segments": len(t.get("segments", []))}
                for t in self.data.get("tracks", [])
            ],
        }

    # ------------------------------------------------------------------ 저장

    def backup(self) -> Path:
        """타임스탬프가 붙은 백업본을 만들고 경로를 돌려준다."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = self.folder / f"{self.path.stem}.{stamp}.scenecut-backup.json"
        shutil.copy2(self.path, dest)
        return dest

    def backups(self) -> list[Path]:
        """이 도구가 만든 백업본을 최신순으로."""
        found = list(self.folder.glob("*.scenecut-backup.json"))
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

    def save(self, *, make_backup: bool = True) -> Path | None:
        """드래프트 파일을 덮어쓴다. CapCut 이 닫혀 있어야 한다.

        임시 파일에 먼저 쓰고 바꿔치기한다. 쓰는 도중에 문제가 생겨도 원본이 반쯤 덮인
        상태로 남지 않는다.
        """
        backup_path = self.backup() if make_backup else None
        # CapCut 은 공백 없는 compact JSON 으로 저장한다. 포맷을 맞춰준다.
        self.write_all(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")))
        self._touch_meta()
        return backup_path

    def write_all(self, text: str) -> list[Path]:
        """본문이 있는 곳을 **전부** 같은 내용으로 맞춘다.

        한 곳만 고치면 안 된다. CapCut 이 다른 쪽을 읽어 예전 내용을 되살리고, 그것이 도로
        덮어써서 작업이 사라진다. 게다가 한 곳만 새것이 되면 '가장 최근 것' 을 고르는 규칙이
        낡은 쪽을 가리키게 되어, 다음에 읽을 때도 엉뚱한 내용을 보게 된다.
        """
        root = find_draft_file(self.folder)
        targets = {p.resolve() for p in ([root] if root else []) + self.mirrors}
        targets.add(self.path.resolve())

        written: list[Path] = []
        for target in sorted(targets):
            # 임시 파일에 먼저 쓰고 바꿔치기한다. 쓰는 도중 문제가 생겨도 반쯤 덮이지 않는다.
            temp = target.with_suffix(target.suffix + ".writing")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, target)
            written.append(target)
        return written

    def restore_from(self, backup: Path) -> list[Path]:
        """백업 내용을 모든 사본에 되돌려 놓는다."""
        return self.write_all(backup.read_text(encoding="utf-8"))

    def _touch_meta(self) -> None:
        """draft_meta_info.json 의 길이와 수정 시각을 본문과 맞춘다.

        CapCut 은 프로젝트 목록을 이 파일로 그리므로, 갱신하지 않으면 목록의 길이 표시가
        옛날 값으로 남는다. 본문과 무관한 파일이라 없거나 깨져 있어도 그냥 넘어간다.
        """
        meta_path = self.folder / "draft_meta_info.json"
        if not meta_path.is_file():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        meta["tm_duration"] = self.duration_us
        meta["tm_draft_modified"] = int(datetime.now().timestamp() * US)
        try:
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
        except OSError:
            return
