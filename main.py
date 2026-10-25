import os, io, time, uuid, inspect, pkgutil, importlib, threading, tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bcsfe_wrapper import BCSFEWrapper

app = FastAPI(title="냥코 세이브 에디터")

WORK = Path(tempfile.gettempdir()) / "bcsfe_sessions"
WORK.mkdir(exist_ok=True)

MAX_UPLOAD = 5 * 1024 * 1024   # 세이브 파일 최대 5MB
SESSION_TTL = 30 * 60          # 30분

sessions: dict[str, BCSFEWrapper] = {}
last_used: dict[str, float] = {}
lock = threading.RLock()       # 래퍼 호출은 한 번에 하나씩


# ---------------------------------------------------------------- 공통
def get(sid: str) -> BCSFEWrapper:
    if sid not in sessions:
        raise HTTPException(404, "세션이 없어요. 파일을 다시 업로드해 주세요. (30분이 지나면 삭제돼요)")
    last_used[sid] = time.time()
    return sessions[sid]


def call(sid: str, name: str, *args):
    w = get(sid)
    fn = getattr(w, name, None)
    if not callable(fn):
        raise HTTPException(501, f"이 래퍼 버전에는 '{name}' 기능이 없어요")
    try:
        with lock:
            return fn(*args)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"{name} 실패: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 업로드
@app.post("/api/upload")
async def upload(file: UploadFile, cc: str = Form("kr"), gv: str = Form(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "빈 파일이에요")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, "파일이 너무 커요 (5MB 이하)")
    gv = gv.strip()
    if not gv:
        raise HTTPException(400, "게임 버전을 입력해 주세요")

    sid = uuid.uuid4().hex
    path = WORK / sid
    path.write_bytes(data)
    try:
        with lock:
            sessions[sid] = BCSFEWrapper(save_path=str(path), cc=cc, gv=gv)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"세이브를 읽지 못했어요 (국가/버전이 맞는지 확인): {type(e).__name__}: {e}")
    last_used[sid] = time.time()
    return {"session_id": sid}


# ---------------------------------------------------------------- 편집
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
    if body.value < 0 or body.value > 999_999_999:
        raise HTTPException(400, "값 범위가 올바르지 않아요")
    call(sid, SETTERS[field], body.value)
    return {"ok": True}


ACTIONS = {
    "unlock_all_cats",
    "upgrade_all_cats",
    "true_form_all_cats",
    "clear_tutorial",
    "clear_story_chapters",
}


@app.post("/api/{sid}/action/{name}")
def action(sid: str, name: str):
    if name not in ACTIONS:
        raise HTTPException(400, "알 수 없는 동작")
    call(sid, name)
    return {"ok": True}


@app.get("/api/{sid}/inquiry")
def inquiry(sid: str):
    code = call(sid, "get_inquiry_code")
    return {"code": str(code)}


@app.get("/api/{sid}/download")
def download(sid: str):
    call(sid, "save")
    path = WORK / sid
    if not path.exists():
        raise HTTPException(500, "저장된 파일을 찾지 못했어요")
    return FileResponse(path, filename="SAVE_DATA", media_type="application/octet-stream")


# ---------------------------------------------------------------- 점검용
@app.get("/api/health")
def health():
    return {"ok": True, "sessions": len(sessions)}


@app.get("/api/debug", response_class=PlainTextResponse)
def debug():
    """래퍼/bcsfe 안의 실제 함수 이름을 보여준다. 확인 후 ENABLE_DEBUG=0 으로 끌 것."""
    if os.environ.get("ENABLE_DEBUG", "1") != "1":
        raise HTTPException(404, "꺼져 있어요")

    out = io.StringIO()
    w = out.write

    def sig_of(obj):
        try:
            return str(inspect.signature(obj))
        except Exception:
            return "(?)"

    w("== BCSFEWrapper 공개 메서드 ==\n")
    for n, m in inspect.getmembers(BCSFEWrapper, callable):
        if not n.startswith("_"):
            w(f"{n}{sig_of(m)}\n")

    w("\n== cat / unit 관련 모듈 ==\n")
    import bcsfe_wrapper
    errors = []
    for mod in pkgutil.walk_packages(
        bcsfe_wrapper.__path__, bcsfe_wrapper.__name__ + ".",
        onerror=lambda n: errors.append(n),
    ):
        name = mod.name
        low = name.lower()
        if not ("cat" in low or "unit" in low):
            continue
        w(f"\n[module] {name}\n")
        try:
            m = importlib.import_module(name)
        except Exception as e:
            w(f"  import 실패: {type(e).__name__}: {e}\n")
            continue
        for n, o in inspect.getmembers(m):
            if n.startswith("_") or getattr(o, "__module__", None) != name:
                continue
            if inspect.isclass(o):
                w(f"  class {n}\n")
                for mn, mo in inspect.getmembers(o, inspect.isfunction):
                    if not mn.startswith("_"):
                        w(f"    {mn}{sig_of(mo)}\n")
            elif inspect.isfunction(o):
                w(f"  def {n}{sig_of(o)}\n")
        if out.tell() > 120_000:
            w("\n...(너무 길어서 여기서 자름)\n")
            break
    if errors:
        w("\n== 건너뛴 모듈 ==\n" + "\n".join(errors) + "\n")
    return out.getvalue()


# ---------------------------------------------------------------- 정리
def cleanup():
    while True:
        now = time.time()
        for sid, t in list(last_used.items()):
            if now - t > SESSION_TTL:
                sessions.pop(sid, None)
                last_used.pop(sid, None)
                (WORK / sid).unlink(missing_ok=True)
        time.sleep(300)


threading.Thread(target=cleanup, daemon=True).start()

app.mount("/", StaticFiles(directory="static", html=True), name="static")
