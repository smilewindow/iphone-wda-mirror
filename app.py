# mirror_wda_full_unlock_lowlat.py
# 功能：通过 WDA 镜像 iPhone，并在镜像窗口里用鼠标点击/拖动控制手机（低延迟+稳）
# 操作：左键点击=点按；按住拖动=滑动；ESC/q 退出
# 连接：USB 请先运行 `iproxy 8100 8100`；或将 WDA_URL 改为 http://<设备IP>:8100

import time, math, json, base64, threading
import requests, cv2, numpy as np, wda
from concurrent.futures import ThreadPoolExecutor

# ===================== 配置 =====================
WDA_URL = "http://127.0.0.1:8100"
TARGET_BUNDLE = None     # None=附着当前前台 App；若想固定某 App，请填它的 bundleId
WINDOW_TITLE = "iPhone"
VIEW_MAX_W = 900
VIEW_MAX_H = 1600

# 帧获取策略：优先用 MJPEG（低延迟），失败自动回退到 /screenshot 轮询
USE_MJPEG_FIRST = True
POLL_FPS_FALLBACK = 12

# 注入策略：优先用原生端点（HTTP 轻薄），失败再走 python-wda 的 s.tap/s.swipe
PREFER_RAW_ENDPOINT = True

# 点按/滑动判定（可按手感微调）
TAP_TIME_MAX    = 0.22   # s：短按优先视作 tap
TAP_MOVE_MAX_PX = 10     # 画布像素死区
TAP_MOVE_MAX_PT = 12     # 设备坐标死区（pt）
SWIPE_MIN_PT    = 18     # 小于此位移一律当 tap，避免页面轻微滚动

# HTTP 超时
TIMEOUT_IMG = 6
TIMEOUT_CMD = 4

# ===================== 全局对象 =====================
# 独立 HTTP 会话：图像/命令分离，启用 keep-alive 降握手
S_IMG = requests.Session()
S_IMG.headers.update({"Connection": "keep-alive"})
S_CMD = requests.Session()
S_CMD.headers.update({"Connection": "keep-alive"})

c = wda.Client(WDA_URL)

def wda_status():
    r = S_CMD.get(f"{WDA_URL}/status", timeout=TIMEOUT_CMD)
    r.raise_for_status()
    d = r.json()
    return d.get("value", d)

print("WDA /status:", json.dumps(wda_status(), ensure_ascii=False))

# ---------- 解锁检测 ----------
def is_locked():
    """GET /wda/locked -> {"value": true/false}；拿不到就当未锁，防止卡死"""
    try:
        r = S_CMD.get(f"{WDA_URL}/wda/locked", timeout=2)
        r.raise_for_status()
        return bool(r.json().get("value"))
    except Exception:
        return False

def wait_until_unlocked(timeout=120):
    """等待用户手动解锁（Face/Touch/密码）。避免直接 /wda/unlock 导致 iOS 18 超时。"""
    t0 = time.time()
    hinted = False
    while time.time() - t0 < timeout:
        if not is_locked():
            return True
        if not hinted:
            print("⚠️  设备处于锁屏或未完全解锁状态，请在 iPhone 上解锁后继续 ...")
            hinted = True
        time.sleep(0.5)
    return not is_locked()

SESS_LOCK = threading.Lock()
def ensure_session():
    """
    更稳会话策略：
    1) 先等解锁
    2) 优先附着 TARGET_BUNDLE；否则跟随前台 App
    3) 若前台是 SpringBoard，不带 bundleId，走默认 session（避免 RequestDenied）
    """
    with SESS_LOCK:
        try:
            if not wait_until_unlocked(timeout=120):
                print("⏳ 等待解锁超时，仍尝试创建默认会话 ...")

            bid = TARGET_BUNDLE
            if not bid:
                try:
                    cur = c.app_current() or {}
                    bid = cur.get("bundleId")
                except Exception:
                    bid = None

            if bid and bid != "com.apple.springboard":
                print("Attach session to:", bid)
                return c.session(bid)
            else:
                print("Attach session to: (frontmost)")
                return c.session()
        except Exception as e:
            print("ensure_session error:", repr(e))
            # 刚解锁后短时不可用，稍微等待后再试一次默认会话
            time.sleep(1.0)
            return c.session()

s = ensure_session()

# 设备“点(pt)”尺寸
sz = s.window_size()
if isinstance(sz, tuple):
    device_w, device_h = float(sz[0]), float(sz[1])
else:
    device_w, device_h = float(sz["width"]), float(sz["height"])
print(f"Device window size (pt): {device_w} x {device_h}")

# ===================== 坐标映射：截图(px) -> 画布(px) -> 设备(pt) =====================
shot_w = shot_h = 0
canvas_w = canvas_h = 0
scale = 1.0
offset_x = offset_y = 0

def fit_letterbox(src_w, src_h, dst_w, dst_h):
    sc = min(dst_w / src_w, dst_h / src_h)
    draw_w, draw_h = int(round(src_w * sc)), int(round(src_h * sc))
    dx = (dst_w - draw_w) // 2
    dy = (dst_h - draw_h) // 2
    return sc, dx, dy, draw_w, draw_h

def view_to_device(x_view, y_view):
    """画布(px) -> 设备(pt)"""
    if shot_w == 0 or shot_h == 0 or canvas_w == 0 or canvas_h == 0:
        return None
    x_img = (x_view - offset_x) / (scale if scale else 1.0)
    y_img = (y_view - offset_y) / (scale if scale else 1.0)
    if x_img < 0 or y_img < 0 or x_img > shot_w or y_img > shot_h:
        return None
    x_dev = x_img / shot_w * device_w
    y_dev = y_img / shot_h * device_h
    return float(x_dev), float(y_dev)

# ===================== 原生端点兜底 =====================
def _session_id(sess):
    for k in ("id", "session_id", "_session_id"):
        if hasattr(sess, k):
            return getattr(sess, k)
    return sess.__dict__.get("id") or sess.__dict__.get("session_id") or sess.__dict__.get("_session_id")

def tap_raw(sess, x, y):
    sid = _session_id(sess)
    urls = [
        f"{WDA_URL}/session/{sid}/wda/tap",
        f"{WDA_URL}/session/{sid}/wda/tap/0",  # 兼容旧路由
    ]
    last_err = None
    for u in urls:
        try:
            r = S_CMD.post(u, json={"x": x, "y": y}, timeout=TIMEOUT_CMD)
            r.raise_for_status()
            return
        except Exception as e:
            last_err = e
    raise last_err

def drag_raw(sess, x0, y0, x1, y1, duration):
    sid = _session_id(sess)
    u = f"{WDA_URL}/session/{sid}/wda/dragfromtoforduration"
    p = {"fromX": x0, "fromY": y0, "toX": x1, "toY": y1, "duration": float(duration)}
    r = S_CMD.post(u, json=p, timeout=max(TIMEOUT_CMD, 6))
    r.raise_for_status()

# ===================== 低延迟取帧（MJPEG 优先，轮询回退） =====================
LATEST_FRAME = None
FRAME_LOCK = threading.Lock()
STOP_EVENT = threading.Event()

def capture_mjpeg():
    url = f"{WDA_URL}/mjpegstream"
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        return False
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    while not STOP_EVENT.is_set():
        ok, frame = cap.read()
        if not ok:
            break
        ih, iw = frame.shape[:2]
        with FRAME_LOCK:
            global LATEST_FRAME, shot_w, shot_h
            LATEST_FRAME = frame
            shot_w, shot_h = iw, ih
    cap.release()
    return True

def capture_polling():
    interval = 1.0 / max(1, POLL_FPS_FALLBACK)
    url = f"{WDA_URL}/screenshot"
    headers = {"Accept": "image/png"}
    while not STOP_EVENT.is_set():
        t0 = time.time()
        try:
            r = S_IMG.get(url, timeout=TIMEOUT_IMG, headers=headers)
            ct = r.headers.get("Content-Type", "")
            if ct.startswith("image/"):
                data = r.content
            else:
                j = r.json()
                b64 = j.get("value") or j.get("screenshot")
                if not b64:
                    continue
                data = base64.b64decode(b64)
            arr = np.frombuffer(data, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            ih, iw = img.shape[:2]
            with FRAME_LOCK:
                global LATEST_FRAME, shot_w, shot_h
                LATEST_FRAME = img
                shot_w, shot_h = iw, ih
        except Exception:
            pass
        dt = time.time() - t0
        if dt < interval:
            time.sleep(interval - dt)

def start_capture_thread():
    t = threading.Thread(target=(capture_mjpeg if USE_MJPEG_FIRST else capture_polling), daemon=True)
    t.start()
    if USE_MJPEG_FIRST:
        time.sleep(0.6)
        if LATEST_FRAME is None:
            t2 = threading.Thread(target=capture_polling, daemon=True)
            t2.start()

# ===================== 手势下发（异步） =====================
EXEC = ThreadPoolExecutor(max_workers=2)

def send_tap(tx, ty):
    global s
    try:
        if PREFER_RAW_ENDPOINT:
            try:
                tap_raw(s, tx, ty)
            except Exception:
                s.tap(tx, ty)
        else:
            try:
                s.tap(tx, ty)
            except Exception:
                tap_raw(s, tx, ty)
        print(f"TAP @ ({tx:.1f}, {ty:.1f}) [pt]")
    except Exception as e:
        print("tap error:", repr(e))
        s = ensure_session()
        try:
            if PREFER_RAW_ENDPOINT:
                try:
                    tap_raw(s, tx, ty)
                except Exception:
                    s.tap(tx, ty)
            else:
                try:
                    s.tap(tx, ty)
                except Exception:
                    tap_raw(s, tx, ty)
            print("TAP retried after session refresh.")
        except Exception as e2:
            print("tap retry failed:", repr(e2))

def send_swipe(x0d, y0d, x1d, y1d, dur):
    global s
    dur = max(0.12, min(0.8, dur))
    try:
        if PREFER_RAW_ENDPOINT:
            try:
                drag_raw(s, x0d, y0d, x1d, y1d, dur)
            except Exception:
                if hasattr(s, "drag"):
                    s.drag(x0d, y0d, x1d, y1d, dur)
                else:
                    s.swipe(x0d, y0d, x1d, y1d, dur)
        else:
            try:
                if hasattr(s, "drag"):
                    s.drag(x0d, y0d, x1d, y1d, dur)
                else:
                    s.swipe(x0d, y0d, x1d, y1d, dur)
            except Exception:
                drag_raw(s, x0d, y0d, x1d, y1d, dur)
        print(f"SWIPE {dur:.2f}s: ({x0d:.1f},{y0d:.1f})->({x1d:.1f},{y1d:.1f}) [pt]")
    except Exception as e:
        print("swipe error:", repr(e))
        s = ensure_session()
        try:
            if PREFER_RAW_ENDPOINT:
                try:
                    drag_raw(s, x0d, y0d, x1d, y1d, dur)
                except Exception:
                    if hasattr(s, "drag"):
                        s.drag(x0d, y0d, x1d, y1d, dur)
                    else:
                        s.swipe(x0d, y0d, x1d, y1d, dur)
            else:
                try:
                    if hasattr(s, "drag"):
                        s.drag(x0d, y0d, x1d, y1d, dur)
                    else:
                        s.swipe(x0d, y0d, x1d, y1d, dur)
                except Exception:
                    drag_raw(s, x0d, y0d, x1d, y1d, dur)
            print("SWIPE retried after session refresh.")
        except Exception as e2:
            print("swipe retry failed:", repr(e2))

# ===================== 鼠标事件：时间+位移判定 =====================
drag_start = None
press_t = 0.0

def on_mouse(event, x, y, flags, param):
    global drag_start, press_t
    if event == cv2.EVENT_LBUTTONDOWN:
        drag_start = (x, y)
        press_t = time.time()
        return

    if event != cv2.EVENT_LBUTTONUP or drag_start is None:
        return

    x0, y0 = drag_start
    drag_start = None

    dur = time.time() - press_t
    move_px = math.hypot(x - x0, y - y0)

    p0 = view_to_device(x0, y0)
    p1 = view_to_device(x, y)
    if p0 is None or p1 is None:
        return
    x0d, y0d = p0
    x1d, y1d = p1
    move_pt = math.hypot(x1d - x0d, y1d - y0d)

    is_tap_style = (dur <= TAP_TIME_MAX and (move_px <= TAP_MOVE_MAX_PX or move_pt <= TAP_MOVE_MAX_PT))
    if move_pt < SWIPE_MIN_PT:
        is_tap_style = True

    if is_tap_style:
        tx, ty = x0d, y0d   # 用按下点坐标，抗抖
        EXEC.submit(send_tap, tx, ty)
        print(f"[plan] TAP dur={dur:.3f}s dpx={move_px:.1f} dpt={move_pt:.1f}")
    else:
        EXEC.submit(send_swipe, x0d, y0d, x1d, y1d, dur)
        print(f"[plan] SWIPE dur={dur:.3f}s dpt={move_pt:.1f}")

# ===================== UI 展示 =====================
cv2.namedWindow(WINDOW_TITLE, cv2.WINDOW_AUTOSIZE)
cv2.setMouseCallback(WINDOW_TITLE, on_mouse)

def draw(img):
    global shot_w, shot_h, canvas_w, canvas_h, scale, offset_x, offset_y
    ih, iw = img.shape[:2]
    reset_canvas = (iw != shot_w or ih != shot_h) or (canvas_w == 0 or canvas_h == 0)

    shot_w, shot_h = iw, ih  # 冗余设一次，防 race

    if reset_canvas:
        aspect = ih / iw
        if iw > ih:
            canvas_w = min(VIEW_MAX_W, iw)
            canvas_h = int(round(canvas_w * aspect))
        else:
            canvas_h = min(VIEW_MAX_H, ih)
            canvas_w = int(round(canvas_h / aspect))

    scale, offset_x, offset_y, draw_w, draw_h = fit_letterbox(iw, ih, canvas_w, canvas_h)

    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    if draw_w > 0 and draw_h > 0:
        resized = cv2.resize(img, (draw_w, draw_h), interpolation=cv2.INTER_LINEAR)
        canvas[offset_y:offset_y+draw_h, offset_x:offset_x+draw_w] = resized
    cv2.imshow(WINDOW_TITLE, canvas)

# ===================== 主循环 =====================
try:
    start_capture_thread()
    while True:
        frame = None
        with FRAME_LOCK:
            if LATEST_FRAME is not None:
                frame = LATEST_FRAME.copy()
        if frame is not None:
            draw(frame)
        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q')):
            break
finally:
    STOP_EVENT.set()
    EXEC.shutdown(wait=False)
    cv2.destroyAllWindows()

