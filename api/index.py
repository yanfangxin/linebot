import os
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from supabase import create_client, Client

app = Flask(__name__)

# 從環境變數讀取金鑰
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

# 設定台灣時區 (UTC+8)
tw_tz = timezone(timedelta(hours=8))

@app.route("/api/webhook", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    
    # 預設回覆
    reply_text = "請直接輸入「120/80」紀錄血壓，或點選下方選單按鈕來紀錄用藥與查詢。"

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
            reply_text = "⚠️ 血壓記錄失敗，請檢查 Supabase 資料庫設定。"

    # 2. 如果使用者按了圖文選單的「紀錄血壓」
    elif user_message in ["血壓", "紀錄血壓", "記錄血壓"]:
        reply_text = "請直接輸入您的血壓數值即可紀錄喔！\n📝 例如：120/80"

    # 3. 處理用藥紀錄 (寫入資料庫)
    elif user_message in ["吃藥", "吃藥打卡", "已用藥", "用藥"]:
        try:
            supabase.table('medication_logs').insert({
                "user_id": user_id,
                "action": "took_pills"
            }).execute()
            
            now_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M")
            reply_text = f"💊 已成功記錄用藥！\n🕒 時間：{now_str}"
        except Exception as e:
            reply_text = "⚠️ 用藥記錄失敗，請稍後再試。"

    # 4. 查詢上一次用藥時間
    elif user_message in ["上次用藥", "上次用藥查詢", "查詢用藥", "查詢用藥紀錄", "我吃藥了嗎"]:
        try:
            response = supabase.table('medication_logs') \
                .select('created_at') \
                .eq('user_id', user_id) \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
            
            if response.data and len(response.data) > 0:
                last_time_str = response.data[0]['created_at']
                formatted_time = last_time_str[:16].replace('T', ' ')
                reply_text = f"你上一次的用藥紀錄是：\n🕒 {formatted_time}"
            else:
                reply_text = "資料庫裡目前沒有你的用藥紀錄喔！"
                
        except Exception as e:
            reply_text = "⚠️ 查詢失敗，請稍後再試。"

    # 5. 查詢血壓
    elif user_message in ["查詢血壓", "上次血壓", "我的血壓"]:
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
                formatted_time = last_time_str[:16].replace('T', ' ')
                
                reply_text = f"📊 你上次紀錄的血壓是：\n收縮壓 {systolic} / 舒張壓 {diastolic}\n🕒 紀錄時間：{formatted_time}"
            else:
                reply_text = "資料庫裡目前沒有你的血壓紀錄喔！"
                
        except Exception as e:
            reply_text = "⚠️ 查詢失敗，請稍後再試。"

    # 回傳文字訊息給 LINE
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

# 讓 Vercel 知道這是一個可執行的 app
if __name__ == "__main__":
    app.run()