import os
import re
import sys
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client

app = Flask(__name__)

# 從環境變數讀取金鑰 (若為 None 則給予空字串避免 SDK 報錯)
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN') or ''
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET') or ''
SUPABASE_URL = os.environ.get("SUPABASE_URL") or ''
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or ''

# 驗證環境變數是否齊全，並印出警告於 Vercel 紀錄中
missing_envs = []
if not LINE_ACCESS_TOKEN: missing_envs.append('LINE_CHANNEL_ACCESS_TOKEN')
if not LINE_SECRET: missing_envs.append('LINE_CHANNEL_SECRET')
if not SUPABASE_URL: missing_envs.append('SUPABASE_URL')
if not SUPABASE_KEY: missing_envs.append('SUPABASE_KEY')

if missing_envs:
    print(f"⚠️ 警告: 缺少以下環境變數，LINE Bot 可能無法正常運作: {', '.join(missing_envs)}", file=sys.stderr)

# 初始化 LINE SDK 與 Supabase Client
line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# Supabase create_client 會驗證 URL 格式，若為空字串會報錯，因此需做安全判斷
if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None
    print("⚠️ 警告: Supabase 連線資訊不完整，資料庫功能已被停用。", file=sys.stderr)

# 設定台灣時區 (UTC+8)
tw_tz = timezone(timedelta(hours=8))

# 定義關鍵字集合，便於後續維護與擴充
BP_RECORD_KEYWORDS = {"血壓", "紀錄血壓", "記錄血壓"}
MED_RECORD_KEYWORDS = {"吃藥", "吃藥打卡", "已用藥", "用藥"}
MED_QUERY_KEYWORDS = {"上次用藥", "上次用藥查詢", "查詢用藥", "查詢用藥紀錄", "我吃藥了嗎"}
BP_QUERY_KEYWORDS = {"查詢血壓", "上次血壓", "我的血壓"}

def normalize_text(text: str) -> str:
    """
    將全形字元（數字、斜線、連字號、空白）轉換成半形字元，以提升使用者輸入的容錯率。
    """
    if not text:
        return ""
    result = []
    for char in text:
        code = ord(char)
        if code == 0x3000:  # 全形空白
            result.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:  # 全形 ASCII 字元範圍
            result.append(chr(code - 0xfee0))
        else:
            result.append(char)
    return "".join(result).strip()

def format_utc_to_tw(utc_str: str) -> str:
    """
    將 Supabase 回傳的 UTC 時間字串轉換為台灣時間（UTC+8）格式。
    """
    if not utc_str:
        return "未知時間"
    try:
        # 將 'Z' 取代為 '+00:00' 以支援較舊的 python datetime 解析
        clean_str = utc_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean_str)
        dt_tw = dt.astimezone(tw_tz)
        return dt_tw.strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        print(f"解析時間字串出錯: {e}, 原始字串: {utc_str}", file=sys.stderr)
        # 降級處理：直接切片字串並替換 T
        return utc_str[:16].replace('T', ' ')

@app.route("/api/webhook", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        print("Error: Missing X-Line-Signature header.", file=sys.stderr)
        abort(400)
        
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Error: Invalid signature.", file=sys.stderr)
        abort(400)
    except Exception as e:
        print(f"Error handling webhook: {e}", file=sys.stderr)
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    raw_message = event.message.text
    user_message = normalize_text(raw_message)
    
    # 預設回覆
    reply_text = "請直接輸入「120/80」紀錄血壓，或點選下方選單按鈕來紀錄用藥與查詢。"

    # 防禦性檢查：若 Supabase 初始化失敗，無法進行資料庫操作
    if not supabase:
        reply_text = "⚠️ 系統目前無法連接資料庫，請聯絡管理員檢查環境變數設定。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 1. 判斷是否為直接輸入血壓數值 (例如: 120/80, 120-80, 或是保留原本的 血壓 120/80)
    bp_match = re.match(r'^(?:血壓\s*)?(\d+)\s*[/-]\s*(\d+)$', user_message)
    
    if bp_match:
        systolic = int(bp_match.group(1))   
        diastolic = int(bp_match.group(2))  
        
        try:
            supabase.table('blood_pressure_logs').insert({
                "user_id": user_id,
                "systolic": systolic,
                "diastolic": diastolic
            }).execute()
            
            now_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M")
            reply_text = f"✅ 已記錄血壓：{systolic}/{diastolic}\n🕒 時間：{now_str}"
        except Exception as e:
            print(f"Error recording blood pressure for user {user_id}: {e}", file=sys.stderr)
            reply_text = "⚠️ 血壓記錄失敗，請檢查 Supabase 資料庫設定。"

    # 2. 如果使用者按了圖文選單的「紀錄血壓」
    elif user_message in BP_RECORD_KEYWORDS:
        reply_text = "請直接輸入您的血壓數值即可紀錄喔！\n📝 例如：120/80"

    # 3. 處理用藥紀錄 (寫入資料庫)
    elif user_message in MED_RECORD_KEYWORDS:
        try:
            supabase.table('medication_logs').insert({
                "user_id": user_id,
                "action": "took_pills"
            }).execute()
            
            now_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M")
            reply_text = f"💊 已成功記錄用藥！\n🕒 時間：{now_str}"
        except Exception as e:
            print(f"Error recording medication for user {user_id}: {e}", file=sys.stderr)
            reply_text = "⚠️ 用藥記錄失敗，請稍後再試。"

    # 4. 查詢上一次用藥時間
    elif user_message in MED_QUERY_KEYWORDS:
        try:
            response = supabase.table('medication_logs') \
                .select('created_at') \
                .eq('user_id', user_id) \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
            
            if response.data and len(response.data) > 0:
                last_time_str = response.data[0]['created_at']
                formatted_time = format_utc_to_tw(last_time_str)
                reply_text = f"你上一次的用藥紀錄是：\n🕒 {formatted_time}"
            else:
                reply_text = "資料庫裡目前沒有你的用藥紀錄喔！"
                
        except Exception as e:
            print(f"Error querying medication log for user {user_id}: {e}", file=sys.stderr)
            reply_text = "⚠️ 查詢失敗，請稍後再試。"

    # 5. 查詢血壓
    elif user_message in BP_QUERY_KEYWORDS:
        try:
            response = supabase.table('blood_pressure_logs') \
                .select('*') \
                .eq('user_id', user_id) \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
            
            if response.data and len(response.data) > 0:
                latest_bp = response.data[0]
                systolic = latest_bp.get('systolic')
                diastolic = latest_bp.get('diastolic')
                
                last_time_str = latest_bp.get('created_at')
                formatted_time = format_utc_to_tw(last_time_str)
                
                reply_text = f"📊 你上次紀錄的血壓是：\n收縮壓 {systolic} / 舒張壓 {diastolic}\n🕒 紀錄時間：{formatted_time}"
            else:
                reply_text = "資料庫裡目前沒有你的血壓紀錄喔！"
                
        except Exception as e:
            print(f"Error querying blood pressure log for user {user_id}: {e}", file=sys.stderr)
            reply_text = "⚠️ 查詢失敗，請稍後再試。"

    # 回傳文字訊息給 LINE
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        print(f"Error replying message to LINE: {e}", file=sys.stderr)

# 讓 Vercel 知道這是一個可執行的 app
if __name__ == "__main__":
    app.run()