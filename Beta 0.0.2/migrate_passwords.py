"""
migrate_passwords.py — 將 jchat_data.json 中的明文密碼升級為 Werkzeug 雜湊

執行方式：
    python migrate_passwords.py

執行前請先備份 jchat_data.json！
"""
import json, os, shutil
from werkzeug.security import generate_password_hash

DATA_FILE = "jchat_data.json"

if not os.path.exists(DATA_FILE):
    print(f"找不到 {DATA_FILE}，請確認路徑正確。")
    exit(1)

# 備份
backup = DATA_FILE + ".bak"
shutil.copy2(DATA_FILE, backup)
print(f"已備份原始檔案至 {backup}")

with open(DATA_FILE, 'r', encoding='utf-8') as f:
    data = json.load(f)

accounts = data.get("accounts", {})
migrated = 0
skipped  = 0

for username, pwd in accounts.items():
    # Werkzeug 雜湊以 "pbkdf2:" 或 "scrypt:" 開頭，若已是雜湊則跳過
    if pwd.startswith("pbkdf2:") or pwd.startswith("scrypt:"):
        skipped += 1
        continue
    accounts[username] = generate_password_hash(pwd)
    migrated += 1

data["accounts"] = accounts

with open(DATA_FILE, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"遷移完成：{migrated} 個帳號已雜湊，{skipped} 個已是雜湊格式（跳過）。")
