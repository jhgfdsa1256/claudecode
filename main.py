import uuid, time, threading, tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from bcsfe_wrapper import BCSFEWrapper

app = FastAPI()
WORK = Path(tempfile.gettempdir()) / "bcsfe_sessions"
WORK.mkdir(exist_ok=True)
sessions: dict[str, BCSFEWrapper] = {}
CREATED: dict[str, float] = {}


def get(sid: str) -> BCSFEWrapper:
    if sid not in sessions:
        raise HTTPException(404, "세션 없음 (30분 지나면 삭제돼요)")
    return sessions[sid]


@app.post("/api/upload")
async def upload(file: UploadFile, cc: str = Form("kr"), gv: str = Form(...)):
    sid = uuid.uuid4().hex
    path = WORK / sid
    path.write_bytes(await file.read())
    try:
        sessions[sid] = BCSFEWrapper(save_path=str(path), cc=cc, gv=gv)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"세이브 파싱 실패: {e}")
    CREATED[sid] = time.time()
    return {"session_id": sid}


class Value(BaseModel):
    value: int


SETTERS = {
    "catfood": "set_catfood",
    "xp": "set_xp",
    "rare_tickets": "set_rare_tickets",
    "battle_items": "set_battle_items",
}


@app.post("/api/{sid}/set/{field}")
def set_value(sid: str, field: str, body: Value):
    if field not in SETTERS:
        raise HTTPException(400, "알 수 없는 항목")
    getattr(get(sid), SETTERS[field])(body.value)
    return {"ok": True}


ACTIONS = {"unlock_all_cats", "upgrade_all_cats",
           "true_form_all_cats", "clear_story_chapters"}


@app.post("/api/{sid}/action/{name}")
def action(sid: str, name: str):
    if name not in ACTIONS:
        raise HTTPException(400, "알 수 없는 동작")
    getattr(get(sid), name)()
    return {"ok": True}


@app.get("/api/{sid}/download")
def download(sid: str):
    get(sid).save()
    return FileResponse(WORK / sid, filename="SAVE_DATA")


def cleanup():
    while True:
        now = time.time()
        for sid, t in list(CREATED.items()):
            if now - t > 1800:  # 30분
                sessions.pop(sid, None)
                CREATED.pop(sid, None)
                (WORK / sid).unlink(missing_ok=True)
        time.sleep(300)


threading.Thread(target=cleanup, daemon=True).start()

app.mount("/", StaticFiles(directory="static", html=True), name="static")
