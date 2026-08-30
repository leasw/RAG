"""data.json을 주기적으로 다시 만든다 (뷰어 실시간 갱신용).

    python viewer/auto_export.py [간격초, 기본 3]

export_data.py를 무한 반복 호출한다. sqlite/jsonl을 매번 다시 읽는 것뿐이라
가볍다 — 무거운 모델 로딩 같은 건 없다.
"""
import subprocess, sys, time
from pathlib import Path

INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
SCRIPT = Path(__file__).resolve().parent / "export_data.py"

print(f"[auto_export] {INTERVAL}초마다 갱신")
while True:
    try:
        subprocess.run([sys.executable, str(SCRIPT)], check=True,
                        capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"[auto_export] 실패: {e.stderr[-300:]}")
    time.sleep(INTERVAL)
