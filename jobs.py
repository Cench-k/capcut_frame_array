"""오래 걸리는 작업을 백그라운드로 돌리고 진행률을 알려준다.

장면 분석은 4 분짜리 영상에 5 초쯤 걸리고, 한 시간짜리면 1 분을 넘는다. 그동안 HTTP 응답을
붙잡고 있으면 브라우저가 먼저 끊어 버리고 진행률도 보여줄 수 없다. 그래서 작업을 시작만
하고 번호를 돌려준 뒤, 화면이 그 번호로 상태를 물어보게 한다.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from errors import UserError

# 끝난 작업을 몇 개까지 들고 있을지. 분석 결과(프레임별 점수)가 통째로 메모리에 남으므로
# 무한정 쌓으면 긴 영상 몇 개만으로도 수백 MB 가 된다.
MAX_KEPT = 8


@dataclass
class Job:
    """작업 하나의 상태."""

    id: str
    kind: str
    state: str = "running"  # running | done | error
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str = ""
    # 화면에는 보내지 않고 서버가 들고 있는 것. 프레임별 점수 곡선이 여기 있다.
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "progress": round(self.progress, 3),
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }


class JobStore:
    """작업들을 담아두는 곳. 여러 요청 스레드가 같이 만지므로 자물쇠를 건다."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def start(self, kind: str, work: Callable[[Job], Any]) -> Job:
        """작업을 만들어 별도 스레드에서 돌린다."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict()

        def run() -> None:
            try:
                job.result = work(job)
                job.progress = 1.0
                job.state = "done"
            except UserError as exc:
                # 읽고 조치할 수 있는 오류. 클래스 이름을 앞에 붙이면 안내 문구가 지저분해진다.
                job.error = str(exc)
                job.state = "error"
            except Exception as exc:  # 진짜 버그. 콘솔에 자취를 남긴다.
                traceback.print_exc()
                job.error = f"{type(exc).__name__}: {exc}"
                job.state = "error"

        threading.Thread(target=run, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _evict(self) -> None:
        """오래된 것부터 버린다. 아직 도는 작업은 건드리지 않는다."""
        while len(self._order) > MAX_KEPT:
            for i, job_id in enumerate(self._order):
                if self._jobs[job_id].state != "running":
                    self._order.pop(i)
                    self._jobs.pop(job_id, None)
                    break
            else:
                return  # 전부 도는 중이면 지울 것이 없다


store = JobStore()
