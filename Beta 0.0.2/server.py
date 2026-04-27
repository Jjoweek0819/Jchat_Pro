from flask import Flask, request, send_file
from flask_socketio import SocketIO, emit
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import json, os, base64, io, threading

app = Flask(__name__)
socketio_server = SocketIO(app, cors_allowed_origins="*", async_mode='threading',
                           max_http_buffer_size=20 * 1024 * 1024)  # 20MB for files/media

# ── 存檔執行緒鎖，防止高併發下 JSON 毀損 ───────────────────
_save_lock = threading.Lock()

DATA_FILE   = "jchat_data.json"
AVATAR_DIR  = "avatars"
EMOJI_DIR   = "server_emojis"
MEDIA_DIR   = "media_files"
os.makedirs(AVATAR_DIR, exist_ok=True)
os.makedirs(EMOJI_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

# ── 資料讀寫 ──────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"accounts": {}, "rooms": {}, "custom_emojis": {}, "profiles": {}}

def save_data():
    with _save_lock:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({"accounts": accounts, "rooms": rooms,
                       "custom_emojis": custom_emojis, "profiles": profiles}, f, ensure_ascii=False, indent=2)

def default_room(name="一般"):
    return {"name": name, "history": [], "pinned": ""}

_data    = load_data()
accounts      = _data.get("accounts", {})   # {username: password}
rooms         = _data.get("rooms", {})
custom_emojis = _data.get("custom_emojis", {})  # {code: {b64, ext}}
profiles      = _data.get("profiles", {})   # {username: {bio, status}}

if "general" not in rooms:
    rooms["general"] = default_room("一般")
    save_data()

users = {}   # {sid: {name, voice, room}}

# ── 頭像工具 ──────────────────────────────────────────────
def avatar_path(username):
    # secure_filename 防止路徑穿越攻擊（如 ../../server.py）
    safe_name = secure_filename(f"{username}.png")
    return os.path.join(AVATAR_DIR, safe_name)

def get_avatar_b64(username):
    path = avatar_path(username)
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    return ""

def all_avatars():
    result = {}
    for fname in os.listdir(AVATAR_DIR):
        if fname.endswith('.png'):
            uname = fname[:-4]
            result[uname] = get_avatar_b64(uname)
    return result

# ── 工具函式 ──────────────────────────────────────────────
def room_list_payload():
    return [{"id": rid, "name": r["name"]} for rid, r in rooms.items()]

def broadcast_room_list():
    socketio_server.emit('update_room_list', room_list_payload())

def users_in_room(room_id):
    return [{"name": u["name"], "voice": u["voice"],
             "avatar": get_avatar_b64(u["name"])}
            for u in users.values() if u["room"] == room_id]

def broadcast_user_list(room_id):
    socketio_server.emit('update_user_list',
                         {"room_id": room_id, "users": users_in_room(room_id)})

def get_profile(username):
    p = profiles.get(username, {})
    # 使用 'user_status' 避免與回應狀態欄位 'status': 'success' 衝突
    return {"bio": p.get("bio", ""), "user_status": p.get("status", "")}

# ── 連線 ──────────────────────────────────────────────────
@socketio_server.on('connect')
def handle_connect():
    print(f"新連線: {request.sid}")

@socketio_server.on('disconnect')
def handle_disconnect():
    if request.sid in users:
        room_id = users[request.sid]["room"]
        del users[request.sid]
        broadcast_user_list(room_id)
    print(f"斷線: {request.sid}")

# ── 帳號 ──────────────────────────────────────────────────
@socketio_server.on('register')
def handle_register(data):
    u, p = data.get('username','').strip(), data.get('password','').strip()
    if not u or not p:
        return {'status':'fail','message':'帳號或密碼不得為空'}
    if u in accounts:
        return {'status':'fail','message':'帳號已存在'}
    accounts[u] = generate_password_hash(p)  # 密碼雜湊後儲存
    save_data()
    return {'status':'success','message':'註冊成功，請登入'}

@socketio_server.on('login')
def handle_login(data):
    u, p = data.get('username','').strip(), data.get('password','').strip()
    if not u or not p:
        return {'status':'fail','message':'帳號或密碼不得為空'}
    if u not in accounts:
        return {'status':'fail','message':'帳號不存在，請先註冊'}
    if not check_password_hash(accounts[u], p):  # 比對雜湊密碼
        return {'status':'fail','message':'密碼錯誤'}
    users[request.sid] = {"name": u, "voice": False, "room": "general"}
    prof = get_profile(u)
    sid  = request.sid  # 先存 sid，callback 裡用

    # 先立即回傳 acknowledgment，避免 sio.call() timeout
    # 大量資料（歷史紀錄、頭像、emoji）用背景 thread 延遲發送
    def _push_data():
        import time
        time.sleep(0.1)  # 讓 ack 先送出
        socketio_server.emit('update_room_list', room_list_payload(), to=sid)
        socketio_server.emit('load_history',
            {"room_id":"general", "history": rooms["general"]["history"]}, to=sid)
        socketio_server.emit('update_pinned',
            {"room_id":"general", "text": rooms["general"]["pinned"]}, to=sid)
        socketio_server.emit('load_avatars', all_avatars(), to=sid)
        socketio_server.emit('load_custom_emojis', custom_emojis, to=sid)
        broadcast_user_list("general")

    import threading
    threading.Thread(target=_push_data, daemon=True).start()

    return {
        'status':   'success',
        'nickname': u,
        'avatar':   get_avatar_b64(u),
        'profile':  {'bio': prof['bio'], 'status': prof['user_status']},
    }

# ── 帳號改名（需密碼驗證）──────────────────────────────────
@socketio_server.on('rename_account')
def handle_rename_account(data):
    if request.sid not in users:
        return {'status': 'fail', 'message': '未登入'}
    old_name = users[request.sid]['name']
    password = data.get('password', '').strip()
    new_name = data.get('new_username', '').strip()

    if not password or not new_name:
        return {'status': 'fail', 'message': '請填寫所有欄位'}
    if not check_password_hash(accounts.get(old_name, ''), password):  # 比對雜湊密碼
        return {'status': 'fail', 'message': '密碼錯誤，無法改名'}
    if new_name in accounts:
        return {'status': 'fail', 'message': '此帳號名稱已被使用'}
    if len(new_name) < 2 or len(new_name) > 20:
        return {'status': 'fail', 'message': '帳號名稱需為 2~20 個字元'}

    # 遷移帳號資料
    accounts[new_name] = accounts.pop(old_name)

    # 遷移頭像
    old_avatar = avatar_path(old_name)
    new_avatar = avatar_path(new_name)
    if os.path.exists(old_avatar):
        os.rename(old_avatar, new_avatar)

    # 遷移個人資料
    if old_name in profiles:
        profiles[new_name] = profiles.pop(old_name)

    # 更新所有訊息的 sender
    for room in rooms.values():
        for msg in room.get('history', []):
            if msg.get('sender') == old_name:
                msg['sender'] = new_name

    save_data()

    # 更新目前 session
    users[request.sid]['name'] = new_name

    # 廣播頭像更新
    new_b64 = get_avatar_b64(new_name)
    if new_b64:
        socketio_server.emit('avatar_updated', {'username': new_name, 'avatar': new_b64})

    broadcast_user_list(users[request.sid]['room'])
    return {'status': 'success', 'new_username': new_name}

# ── 個人資料 ───────────────────────────────────────────────
@socketio_server.on('update_profile')
def handle_update_profile(data):
    if request.sid not in users:
        return {'status': 'fail', 'message': '未登入'}
    username = users[request.sid]['name']
    bio    = data.get('bio', '').strip()
    status = data.get('status', '').strip()

    if len(bio) > 200:
        return {'status': 'fail', 'message': '自介上限 200 字'}
    if len(status) > 100:
        return {'status': 'fail', 'message': '動態上限 100 字'}

    profiles[username] = {'bio': bio, 'status': status}
    save_data()

    # 廣播給所有人（讓點頭像的人可以即時看到更新）
    # 同樣使用 'user_status' 避免客戶端 key 衝突
    socketio_server.emit('profile_updated', {
        'username':    username,
        'bio':         bio,
        'user_status': status,
    })
    return {'status': 'success'}

@socketio_server.on('get_profile')
def handle_get_profile(data):
    username = data.get('username', '').strip()
    if not username:
        return {'status': 'fail', 'message': '未指定使用者'}
    p = get_profile(username)
    # 注意：回傳欄位使用 'user_status' 而非 'status'，
    # 避免與回應狀態 'status': 'success' 在客戶端發生 key 衝突
    return {
        'status':      'success',
        'username':    username,
        'bio':         p['bio'],
        'user_status': p['user_status'],
        'avatar':      get_avatar_b64(username),
    }

# ── 頭像上傳 ───────────────────────────────────────────────
@socketio_server.on('upload_avatar')
def handle_upload_avatar(data):
    if request.sid not in users:
        return {'status':'fail','message':'未登入'}
    username = users[request.sid]['name']
    b64 = data.get('image_b64','')
    if not b64:
        return {'status':'fail','message':'無圖片資料'}
    try:
        img_bytes = base64.b64decode(b64)
        if len(img_bytes) > 2 * 1024 * 1024:
            return {'status':'fail','message':'圖片太大（上限 2MB）'}
        # 驗證是否為合法圖片格式（PNG/JPEG/GIF/WEBP magic bytes）
        VALID_MAGIC = [
            b'\x89PNG',      # PNG
            b'\xff\xd8\xff', # JPEG
            b'GIF8',         # GIF
            b'RIFF',         # WEBP (RIFF....WEBP)
        ]
        if not any(img_bytes.startswith(m) for m in VALID_MAGIC):
            return {'status':'fail','message':'不支援的圖片格式'}
        with open(avatar_path(username), 'wb') as f:
            f.write(img_bytes)
        socketio_server.emit('avatar_updated', {'username': username, 'avatar': b64})
        broadcast_user_list(users[request.sid]['room'])
        return {'status':'success'}
    except Exception as e:
        return {'status':'fail','message':str(e)}

# ── 自訂 Emoji 同步 ────────────────────────────────────────
@socketio_server.on('upload_emoji')
def handle_upload_emoji(data):
    if request.sid not in users:
        return {'status': 'fail', 'message': '未登入'}
    code = data.get('code', '').strip()
    b64  = data.get('b64', '')
    ext  = data.get('ext', '.png').lower()
    if not code or not b64:
        return {'status': 'fail', 'message': '參數不完整'}
    if not (code.startswith(':') and code.endswith(':') and len(code) >= 3):
        return {'status': 'fail', 'message': '代號格式錯誤'}
    # 限制 code 只允許字母、數字、底線、連字號，防止 XSS
    import re
    inner = code[1:-1]  # 去掉首尾冒號
    if not re.match(r'^[\w\-]+$', inner):
        return {'status': 'fail', 'message': 'Emoji 代號只能包含字母、數字、底線、連字號'}
    # 限制 ext 白名單
    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        return {'status': 'fail', 'message': '不支援的圖片格式'}
    try:
        img_bytes = base64.b64decode(b64)
        if len(img_bytes) > 2 * 1024 * 1024:
            return {'status': 'fail', 'message': '圖片太大（上限 2MB）'}
        # 驗證 magic bytes
        VALID_MAGIC = [b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'RIFF']
        if not any(img_bytes.startswith(m) for m in VALID_MAGIC):
            return {'status': 'fail', 'message': '不支援的圖片格式'}
        custom_emojis[code] = {'b64': b64, 'ext': ext}
        save_data()
        # 廣播給所有人（包含發送者，確保同步）
        socketio_server.emit('emoji_updated', {'code': code, 'b64': b64, 'ext': ext})
        return {'status': 'success'}
    except Exception as e:
        return {'status': 'fail', 'message': str(e)}

@socketio_server.on('delete_emoji')
def handle_delete_emoji(data):
    if request.sid not in users:
        return {'status': 'fail', 'message': '未登入'}
    code = data.get('code', '').strip()
    if code in custom_emojis:
        del custom_emojis[code]
        save_data()
        socketio_server.emit('emoji_deleted', {'code': code})
        return {'status': 'success'}
    return {'status': 'fail', 'message': '找不到此 emoji'}

# ── 請求完整 emoji 清單（修正同步問題）──────────────────────
@socketio_server.on('request_emojis')
def handle_request_emojis(data):
    """客戶端主動請求完整 emoji 清單"""
    emit('load_custom_emojis', custom_emojis)
    return {'status': 'success', 'count': len(custom_emojis)}

# ── 房間管理 ───────────────────────────────────────────────
@socketio_server.on('create_room')
def handle_create_room(data):
    name = data.get('name','').strip()
    if not name:
        return {'status':'fail','message':'房間名稱不得為空'}
    room_id = f"room_{int(datetime.now().timestamp()*1000)}"
    rooms[room_id] = default_room(name)
    save_data()
    broadcast_room_list()
    return {'status':'success','room_id': room_id}

@socketio_server.on('rename_room')
def handle_rename_room(data):
    room_id = data.get('room_id','')
    name    = data.get('name','').strip()
    if room_id not in rooms or not name:
        return {'status':'fail','message':'參數錯誤'}
    rooms[room_id]['name'] = name
    save_data()
    broadcast_room_list()
    return {'status':'success'}

@socketio_server.on('delete_room')
def handle_delete_room(data):
    room_id = data.get('room_id','')
    if room_id == 'general':
        return {'status':'fail','message':'預設房間不可刪除'}
    if room_id not in rooms:
        return {'status':'fail','message':'房間不存在'}
    del rooms[room_id]
    save_data()
    for sid, u in users.items():
        if u['room'] == room_id:
            u['room'] = 'general'
            socketio_server.emit('force_join_room',
                                 {"room_id":"general","name":rooms["general"]["name"]}, to=sid)
    broadcast_room_list()
    broadcast_user_list('general')
    return {'status':'success'}

@socketio_server.on('join_room')
def handle_join_room(data):
    room_id = data.get('room_id','')
    if room_id not in rooms or request.sid not in users:
        return {'status':'fail','message':'房間不存在'}
    old_room = users[request.sid]['room']
    users[request.sid]['room'] = room_id
    broadcast_user_list(old_room)
    broadcast_user_list(room_id)
    room = rooms[room_id]
    emit('load_history',  {"room_id": room_id, "history": room["history"]})
    emit('update_pinned', {"room_id": room_id, "text":    room["pinned"]})
    return {'status':'success'}

# ── 聊天（含多媒體）─────────────────────────────────────────
@socketio_server.on('chat_message')
def handle_message(data):
    user    = users.get(request.sid, {"name":"未知","room":"general"})
    room_id = user['room']
    if room_id not in rooms:
        return

    msg_type = data.get('type', 'text')  # text / image / voice / video / file

    msg = {
        'sender':  user['name'],
        'text':    data.get('text', ''),
        'time':    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'room_id': room_id,
        'type':    msg_type,
    }

    # 媒體訊息：存 base64 到訊息中（小檔）或存檔（大檔）
    if msg_type in ('image', 'voice', 'video', 'file'):
        b64_data  = data.get('data', '')
        # secure_filename 過濾檔名，防止路徑穿越
        file_name = secure_filename(data.get('file_name', '') or 'file')
        file_size = data.get('file_size', 0)

        if b64_data:
            raw = base64.b64decode(b64_data)
            # 超過 8MB 拒絕
            if len(raw) > 8 * 1024 * 1024:
                return
            msg['data']      = b64_data
            msg['file_name'] = file_name
            msg['file_size'] = file_size

    rooms[room_id]['history'].append(msg)
    save_data()
    for sid, u in users.items():
        if u['room'] == room_id:
            socketio_server.emit('receive_message', msg, to=sid)

# ── 釘選 ──────────────────────────────────────────────────
@socketio_server.on('pin_request')
def handle_pin(data):
    room_id = data.get('room_id','')
    text    = data.get('text','')
    if room_id not in rooms:
        return
    rooms[room_id]['pinned'] = text
    save_data()
    for sid, u in users.items():
        if u['room'] == room_id:
            socketio_server.emit('update_pinned', {"room_id": room_id, "text": text}, to=sid)

# ── 語音 ──────────────────────────────────────────────────
@socketio_server.on('toggle_voice')
def handle_voice(status):
    if request.sid in users:
        users[request.sid]['voice'] = status
        broadcast_user_list(users[request.sid]['room'])

# ── WebRTC 語音信令轉發 ─────────────────────────────────────
def find_sid_by_name(username):
    for sid, u in users.items():
        if u['name'] == username:
            return sid
    return None

@socketio_server.on('voice_join')
def handle_voice_join(data):
    if request.sid not in users:
        return
    user = users[request.sid]
    room_id = user['room']
    # 通知同房間所有其他人，讓他們向新加入者發 offer
    for sid, u in users.items():
        if sid != request.sid and u['room'] == room_id and u.get('voice'):
            socketio_server.emit('voice_user_joined', {'from': user['name']}, to=sid)

@socketio_server.on('voice_leave')
def handle_voice_leave(data):
    if request.sid not in users:
        return
    user = users[request.sid]
    room_id = user['room']
    for sid, u in users.items():
        if sid != request.sid and u['room'] == room_id:
            socketio_server.emit('voice_user_left', {'from': user['name']}, to=sid)

@socketio_server.on('voice_offer')
def handle_voice_offer(data):
    if request.sid not in users:
        return
    sender = users[request.sid]['name']
    target_sid = find_sid_by_name(data.get('to', ''))
    if target_sid:
        socketio_server.emit('voice_offer', {'from': sender, 'sdp': data['sdp']}, to=target_sid)

@socketio_server.on('voice_answer')
def handle_voice_answer(data):
    if request.sid not in users:
        return
    sender = users[request.sid]['name']
    target_sid = find_sid_by_name(data.get('to', ''))
    if target_sid:
        socketio_server.emit('voice_answer', {'from': sender, 'sdp': data['sdp']}, to=target_sid)

@socketio_server.on('voice_ice')
def handle_voice_ice(data):
    if request.sid not in users:
        return
    sender = users[request.sid]['name']
    target_sid = find_sid_by_name(data.get('to', ''))
    if target_sid:
        socketio_server.emit('voice_ice', {'from': sender, 'candidate': data['candidate']}, to=target_sid)

# ── 打字提示 ──────────────────────────────────────────────
@socketio_server.on('typing_start')
def handle_typing_start():
    if request.sid not in users:
        return
    user = users[request.sid]
    room_id = user['room']
    # 廣播給同房間其他人
    for sid, u in users.items():
        if sid != request.sid and u['room'] == room_id:
            socketio_server.emit('user_typing', {'username': user['name']}, to=sid)

@socketio_server.on('typing_stop')
def handle_typing_stop():
    if request.sid not in users:
        return
    user = users[request.sid]
    room_id = user['room']
    for sid, u in users.items():
        if sid != request.sid and u['room'] == room_id:
            socketio_server.emit('user_stop_typing', {'username': user['name']}, to=sid)

@app.route('/')
def index():
    return send_file('index.html')

if __name__ == '__main__':
    socketio_server.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=False)
