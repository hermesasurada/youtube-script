"""큐 팝업(2026-11-21 사용자 지시): 맨 위/맨 아래 이동, 캡처·증류 실제 여부 표시."""
import db


def _pending_ids():
    return [v["id"] for v in db.queue_overview()["waiting"] if v["status"] == "pending"]


def test_move_to_top_and_bottom():
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM watch_queue")
    for i in range(4):
        db.enqueue_video(f"qv{i}", f"https://youtu.be/qv{i}", f"T{i}", "manual")
    ids = _pending_ids()
    assert db.queue_move(ids[-1], "top")
    assert _pending_ids()[0] == ids[-1]
    assert not db.queue_move(ids[-1], "top")            # 이미 맨 위
    assert db.queue_move(ids[-1], "bottom")
    assert _pending_ids() == ids                         # 제자리
    assert not db.queue_move(ids[-1], "bottom")
    assert not db.queue_move(ids[0], "sideways")


def test_flags_follow_video_then_channel_then_default():
    db.init()
    conn = db._conn()
    conn.execute("DELETE FROM watch_queue")
    db.enqueue_video("qf1", "https://youtu.be/qf1", "A", "manual")
    db.enqueue_video("qf2", "https://youtu.be/qf2", "B", "manual", capture=False, distill=False)
    rows = {v["yt_id"]: v for v in db.queue_overview()["waiting"]}
    assert (rows["qf1"]["capture_on"], rows["qf1"]["capture_src"]) == (True, "default")
    assert (rows["qf2"]["capture_on"], rows["qf2"]["capture_src"]) == (False, "video")
    assert (rows["qf2"]["distill_on"], rows["qf2"]["distill_src"]) == (False, "video")
