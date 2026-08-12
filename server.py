"""로컬 HTTP 서버. 화면(web/)을 띄우고 JSON API 를 제공한다.

표준 라이브러리만 쓴다. 외부 의존성이 없어야 단일 실행 파일로 묶을 수 있고, 받는 사람이
Python 을 설치하지 않아도 된다. 장면 분석도 ffmpeg 에 맡기므로 파이썬 패키지가 필요 없다.

127.0.0.1 에만 바인딩하고 시작할 때 만든 토큰을 요구한다. 브라우저에 열려 있는 다른 사이트가
이 API 를 찔러 로컬 파일을 읽는 것을 막기 위해서다.

웹으로 호스팅할 수는 없다. Chromium 이 File System Access API 에서 LOCAL_APP_DATA 아래를
하위까지 통째로 막는데, CapCut 드래프트가 정확히 그 안에 있다. 그래서 로컬 서버여야 한다.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import shutil
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import check_draft
import detect
import ffmpeg_setup
import media
import splitter
import thumbs
from capcut_draft import DRAFT_ROOT, Draft, capcut_running, find_draft_file, list_drafts
from errors import UserError
from folder_picker import ask_folder
from jobs import Job, store

WEB_DIR = Path(__file__).resolve().parent / "web"


class ApiError(UserError):
    """클라이언트에게 그대로 보여줄 오류."""


def _resource_dir() -> Path:
    """PyInstaller 로 묶으면 web/ 이 임시 압축해제 폴더에 들어간다."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) / "web" if bundled else WEB_DIR


# ---------------------------------------------------------------- 도메인 로직


def open_draft(folder: str | None) -> Draft:
    if not folder:
        raise ApiError("프로젝트를 고르세요.")
    try:
        return Draft(folder)
    except (FileNotFoundError, ValueError) as exc:
        raise ApiError(str(exc)) from exc


def clip_by_id(draft: Draft, segment_id: str):
    for clip in draft.video_clips():
        if clip.segment_id == segment_id:
            return clip
    raise ApiError("그 영상 조각을 찾지 못했습니다. 프로젝트를 다시 읽어주세요.")


def cut_settings(payload: dict) -> dict:
    """화면에서 온 판정 조건을 정리한다."""
    return {
        "threshold": float(payload.get("threshold", 12.0)),
        "adaptive": bool(payload.get("adaptive", True)),
        "ratio_threshold": float(payload.get("ratio_threshold", 3.0)),
        "min_scene_us": int(payload.get("min_scene_us", 400_000)),
    }


def scan_job(job: Job, draft_folder: str, segment_id: str, whole_file: bool) -> dict:
    """영상을 훑어 점수 곡선을 만들고, 첫 판정 결과까지 돌려준다."""
    draft = open_draft(draft_folder)
    clip = clip_by_id(draft, segment_id)
    if not clip.exists:
        raise ApiError(
            f"원본 영상 파일이 없습니다:\n{clip.path}\n"
            "파일을 옮겼다면 CapCut 에서 소재 경로를 다시 연결해주세요."
        )

    job.message = "영상 정보를 읽는 중"
    info = media.probe(clip.path)

    # 기본은 조각이 실제로 쓰는 구간만 본다. 조각이 원본의 앞 10 초만 쓰는데 한 시간짜리
    # 원본을 통째로 훑으면 대부분이 버려진다.
    if whole_file:
        start_us, span_us = 0, None
    else:
        start_us, span_us = clip.source_start_us, clip.source_duration_us

    job.message = "장면 분석 중"

    def progress(done: float) -> None:
        job.progress = done * 0.95  # 나머지는 판정 몫으로 남긴다

    frames = detect.scan(info, start_us=start_us, duration_us=span_us, on_progress=progress)

    job.message = "컷 판정 중"
    suggested = detect.suggest_threshold(frames)
    settings = {"threshold": suggested, "adaptive": True, "ratio_threshold": 3.0, "min_scene_us": 400_000}
    cuts = detect.find_cuts(frames, **settings)

    # 곡선과 프레임은 화면에 통째로 보내지 않는다. 임계값을 바꿀 때 서버가 다시 판정한다.
    job.payload = {"frames": frames, "info": info, "clip": clip, "draft_folder": draft_folder}

    return {
        "video": info.as_dict(),
        "clip": clip.as_dict(),
        "scanned": {"start_us": start_us, "duration_us": span_us or info.duration_us},
        "curve": detect.score_curve(frames),
        "suggested_threshold": suggested,
        "settings": settings,
        "cuts": [c.as_dict() for c in cuts],
    }


def thumbs_job(job: Job, scan_id: str, times_us: list[int]) -> dict:
    """컷마다 전환 직전 / 직후 그림을 뽑는다."""
    source = store.get(scan_id)
    if not source or not source.payload:
        raise ApiError("분석 결과가 만료됐습니다. 다시 분석해주세요.")
    info = source.payload["info"]

    made: list[dict] = []
    # 한꺼번에 넘기지 않고 나눠 부른다. 진행률을 보여주기 위해서다.
    chunk = 8
    for start in range(0, len(times_us), chunk):
        part = times_us[start : start + chunk]
        made.extend(p.as_dict() for p in thumbs.pairs(info, part))
        job.progress = len(made) / max(len(times_us), 1)
        job.message = f"{len(made)} / {len(times_us)} 장"
    return {"thumbs": made}


# -------------------------------------------------------------------- 핸들러


class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = "SceneCut"

    def log_message(self, fmt: str, *args) -> None:
        pass  # 요청마다 콘솔에 찍으면 안내 문구가 밀려 올라간다

    # ------------------------------------------------------------ 응답 도우미

    def _send(self, status: int, body: bytes, kind: str, *, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=600" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # 사용자가 새로고침하면 흔히 일어난다

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _check_token(self, query: dict) -> None:
        given = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        if not secrets.compare_digest(given, Handler.token):
            raise ApiError("토큰이 맞지 않습니다. 앱을 다시 실행하세요.")

    # ------------------------------------------------------------ 라우팅

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path
        try:
            if route in ("/", "/index.html"):
                return self._serve_index()
            if route.startswith("/static/"):
                return self._serve_static(route[len("/static/") :])

            self._check_token(query)

            if route == "/api/drafts":
                return self._json({"root": str(DRAFT_ROOT), "drafts": self._drafts()})
            if route == "/api/draft":
                return self._json(self._draft_detail(query["folder"][0]))
            if route == "/api/job":
                return self._json(self._job(query["id"][0]))
            if route == "/api/pick":
                return self._json(self._pick())
            if route == "/api/capcut":
                return self._json({"running": capcut_running()})
            if route == "/api/ffmpeg":
                return self._json(ffmpeg_setup.status())
        except UserError as exc:
            # ffmpeg 없음 / 잘못된 프로젝트처럼 읽고 조치할 수 있는 것. 트레이스백 없이 문구만.
            return self._json({"error": str(exc)}, 400)
        except KeyError as exc:
            return self._json({"error": f"빠진 인자: {exc}"}, 400)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        self._json({"error": "없는 주소입니다."}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            self._check_token(query)
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")

            if parsed.path == "/api/scan":
                return self._json(self._scan(payload))
            if parsed.path == "/api/cuts":
                return self._json(self._recut(payload))
            if parsed.path == "/api/thumbs":
                return self._json(self._thumbs(payload))
            if parsed.path == "/api/apply":
                return self._json(self._apply(payload))
            if parsed.path == "/api/restore":
                return self._json(self._restore(payload))
            if parsed.path == "/api/ffmpeg/install":
                return self._json(self._install_ffmpeg())
        except UserError as exc:
            # ffmpeg 없음 / 잘못된 프로젝트처럼 읽고 조치할 수 있는 것. 트레이스백 없이 문구만.
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        self._json({"error": "없는 주소입니다."}, 404)

    # ------------------------------------------------------------ 정적 파일

    def _serve_index(self) -> None:
        path = _resource_dir() / "index.html"
        if not path.exists():
            return self._send(500, b"web/index.html not found", "text/plain")
        html = path.read_text(encoding="utf-8").replace("__TOKEN__", Handler.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_static(self, name: str) -> None:
        # 상위 폴더로 빠져나가지 못하게 이름만 쓴다.
        path = _resource_dir() / Path(name).name
        if not path.is_file():
            return self._send(404, b"not found", "text/plain")
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), kind, cache=True)

    # ------------------------------------------------------------ API 구현

    def _drafts(self) -> list[dict]:
        out = []
        for folder in list_drafts():
            try:
                mtime = folder.stat().st_mtime
            except OSError:
                continue
            out.append({"name": folder.name, "path": str(folder), "mtime": mtime})
        return out

    def _draft_detail(self, folder: str) -> dict:
        draft = open_draft(folder)
        clips = draft.video_clips()
        detail = draft.summary()
        detail["clips"] = [c.as_dict() for c in clips]
        detail["backups"] = [p.name for p in draft.backups()]
        detail["missing"] = [c.name for c in clips if not c.exists]
        detail["capcut_running"] = capcut_running()
        return detail

    def _job(self, job_id: str) -> dict:
        job = store.get(job_id)
        if not job:
            raise ApiError("그 작업을 찾지 못했습니다. 다시 시도해주세요.")
        return job.as_dict()

    def _pick(self) -> dict:
        chosen = ask_folder(initial=str(DRAFT_ROOT), title="CapCut 프로젝트 폴더를 고르세요")
        if not chosen:
            return {"folder": ""}
        folder = Path(chosen)
        if not find_draft_file(folder):
            raise ApiError(
                f"{folder.name} 안에 draft_content.json 이 없습니다.\n"
                "프로젝트 폴더 자체를 골라야 합니다."
            )
        return {"folder": str(folder)}

    def _scan(self, payload: dict) -> dict:
        folder = payload.get("folder")
        segment_id = payload.get("segment_id") or ""
        whole = bool(payload.get("whole_file"))
        job = store.start("scan", lambda j: scan_job(j, folder, segment_id, whole))
        return {"job": job.id}

    def _recut(self, payload: dict) -> dict:
        """임계값만 바꿔 다시 판정한다. 영상을 다시 읽지 않으므로 즉시 끝난다."""
        job = store.get(payload.get("scan_id", ""))
        if not job or not job.payload:
            raise ApiError("분석 결과가 만료됐습니다. 다시 분석해주세요.")
        settings = cut_settings(payload)
        cuts = detect.find_cuts(job.payload["frames"], **settings)
        return {"cuts": [c.as_dict() for c in cuts], "settings": settings}

    def _thumbs(self, payload: dict) -> dict:
        scan_id = payload.get("scan_id", "")
        times = [int(t) for t in payload.get("times_us", [])]
        if not times:
            return {"job": ""}
        job = store.start("thumbs", lambda j: thumbs_job(j, scan_id, times))
        return {"job": job.id}

    def _apply(self, payload: dict) -> dict:
        """고른 컷을 드래프트에 반영하고 저장한다."""
        draft = open_draft(payload.get("folder"))
        segment_id = payload.get("segment_id") or ""
        cuts = [int(c) for c in payload.get("cuts_us", [])]
        if not cuts:
            raise ApiError("적용할 컷이 없습니다.")

        # CapCut 이 켜져 있으면 아예 시작하지 않는다. 여기서 막지 않으면 저장은 성공하는데
        # CapCut 이 곧바로 제 기억으로 되돌려서, 사용자 눈에는 아무 일도 안 일어난 것처럼
        # 보인다. 오류도 안 나기 때문에 원인을 짐작할 방법이 없다.
        if capcut_running():
            raise ApiError(
                "CapCut 이 켜져 있습니다. 완전히 닫고 다시 눌러주세요.\n\n"
                "켜진 채로 적용하면 저장은 되지만 CapCut 이 곧바로 예전 내용으로 "
                "되돌려서 작업이 사라집니다.\n"
                "창을 닫아도 트레이나 백그라운드에 남아 있을 수 있으니, "
                "작업 관리자에서 CapCut 이 없는지 확인해주세요."
            )

        clip = clip_by_id(draft, segment_id)
        before_duration = draft.duration_us
        result = splitter.apply_cuts(draft, {clip.segment_id: cuts})
        if not result.pieces:
            raise ApiError(
                "쪼갤 수 있는 컷이 없습니다. 조각의 시작·끝에 너무 붙어 있는 컷은 건너뜁니다."
            )

        # 저장 직전에 한 번 더 본다. CapCut 은 어긋난 드래프트를 열어도 오류를 내지 않고
        # 조용히 이상하게 그리기 때문에, 여기서 막지 못하면 사용자가 원인을 알 수 없다.
        problems = check_draft.verify(draft, expect_duration_us=before_duration)
        if problems:
            raise ApiError("결과가 어긋나 저장하지 않았습니다:\n- " + "\n- ".join(problems[:5]))

        backup = draft.save(make_backup=bool(payload.get("backup", True)))
        return {
            "pieces": result.pieces,
            "applied_cuts": result.applied_cuts,
            "skipped": result.skipped_clips,
            "backup": backup.name if backup else "",
        }

    def _install_ffmpeg(self) -> dict:
        """ffmpeg 를 내려받는다. 100MB 라 백그라운드로 돌리고 진행률을 보여준다."""
        def work(job: Job) -> dict:
            def progress(fraction: float, message: str) -> None:
                job.progress = fraction
                job.message = message

            try:
                return ffmpeg_setup.install(progress)
            except ffmpeg_setup.SetupError as exc:
                raise ApiError(str(exc)) from exc

        return {"job": store.start("ffmpeg", work).id}

    def _restore(self, payload: dict) -> dict:
        """백업본을 되돌린다."""
        draft = open_draft(payload.get("folder"))
        name = Path(payload.get("backup") or "").name
        # 되돌리기도 파일을 고쳐 쓰는 일이라 같은 조건이다. CapCut 이 켜져 있으면 되돌려
        # 놔도 곧바로 도로 덮인다. 파일을 찾기 전에 먼저 본다.
        if capcut_running():
            raise ApiError("CapCut 이 켜져 있습니다. 완전히 닫고 다시 눌러주세요.")
        source = draft.folder / name
        if not name.endswith(".scenecut-backup.json") or not source.is_file():
            raise ApiError("그 백업 파일을 찾지 못했습니다.")
        shutil.copy2(source, draft.path)
        return {"restored": name, "clips": len(Draft(draft.folder).video_clips())}


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, str, int]:
    """서버를 만들어 돌려준다. port=0 이면 빈 포트를 알아서 고른다."""
    Handler.token = secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd, Handler.token, httpd.server_address[1]
