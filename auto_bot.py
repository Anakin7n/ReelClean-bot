"""
飞书机器人 - 群聊接收 Excel 文件 + 参数，自动处理并回复文案和文件。
WebSocket 长连接模式，无需 ngrok。
"""
import json
import os
import re
import sys
import time
import shutil
import tempfile
import traceback
from pathlib import Path

from dotenv import load_dotenv
from lark_oapi import Client as ApiClient, LogLevel
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    ReplyMessageRequest, ReplyMessageRequestBody,
)
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client as WsClient
import requests

load_dotenv()

APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_clean import process_data

# ---- 文件暂存 ----
_pending_files = {}
_PENDING_TIMEOUT = 600


def cleanup_pending():
    now = time.time()
    for chat_id in list(_pending_files):
        if now - _pending_files[chat_id]["last_time"] > _PENDING_TIMEOUT:
            del _pending_files[chat_id]


# ---- 参数解析 ----
def parse_params(text: str) -> dict | None:
    text = re.sub(r'@\S+\s*', '', text).strip()
    params = {}
    patterns = [
        (r'目标排片[：:=]\s*([\d.]+)', 'target_paipian'),
        (r'总成本[：:=]\s*([\d.]+)', 'total_cost'),
        (r'后台消耗[：:=]\s*([\d.]+)', 'backend_consume'),
        (r'上一时段[：:=]\s*([\d.]+)', 'prev_actual'),
        (r'D\d*百分比[：:=]\s*([\d.]+)', 'd8_pct'),
    ]
    for pattern, key in patterns:
        m = re.search(pattern, text)
        if m:
            params[key] = m.group(1)
    required = ['target_paipian', 'total_cost', 'backend_consume', 'prev_actual', 'd8_pct']
    if all(k in params for k in required):
        for k in required:
            params[k] = float(params[k])
        return params
    return None


# ---- 飞书 API ----
def _get_token() -> str:
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"获取token失败: {data}")
    return data["tenant_access_token"]


def download_file_from_feishu(message_id: str, file_key: str) -> bytes:
    token = _get_token()
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
        headers={"Authorization": f"Bearer {token}"},
        params={"type": "file"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def upload_file_to_feishu(file_path: str, file_name: str) -> str:
    token = _get_token()
    with open(file_path, "rb") as f:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (file_name, f)},
            data={"file_type": "stream", "file_name": file_name},
            timeout=30,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"上传文件失败: {data}")
    return data["data"]["file_key"]


def _api_client():
    return ApiClient.builder() \
        .app_id(APP_ID).app_secret(APP_SECRET) \
        .log_level(LogLevel.ERROR).build()


def send_text(chat_id: str, text: str):
    c = json.dumps({"text": text}, ensure_ascii=False)
    req = CreateMessageRequest.builder().receive_id_type("chat_id") \
        .request_body(CreateMessageRequestBody.builder()
            .receive_id(chat_id).msg_type("text").content(c).build()).build()
    _api_client().im.v1.message.create(req)


def reply_text(message_id: str, text: str):
    c = json.dumps({"text": text}, ensure_ascii=False)
    req = ReplyMessageRequest.builder().message_id(message_id) \
        .request_body(ReplyMessageRequestBody.builder()
            .msg_type("text").content(c).build()).build()
    _api_client().im.v1.message.reply(req)


def send_file_to_chat(chat_id: str, file_key: str):
    c = json.dumps({"file_key": file_key}, ensure_ascii=False)
    req = CreateMessageRequest.builder().receive_id_type("chat_id") \
        .request_body(CreateMessageRequestBody.builder()
            .receive_id(chat_id).msg_type("file").content(c).build()).build()
    _api_client().im.v1.message.create(req)


# ---- 消息处理 ----
def handle_file_message(msg_id: str, chat_id: str, file_key: str, file_name: str):
    print(f"    [文件处理] file_name={file_name}")
    if not file_name.lower().endswith(('.xlsx', '.xls')):
        print(f"    [跳过] 非Excel文件: {file_name}")
        return

    try:
        content = download_file_from_feishu(msg_id, file_key)
        print(f"    [下载成功] {len(content)} bytes")
    except Exception as e:
        print(f"    [下载失败] {e}")
        return

    if chat_id not in _pending_files:
        _pending_files[chat_id] = {"files": [], "last_time": time.time()}
    _pending_files[chat_id]["files"].append((file_name, content))
    _pending_files[chat_id]["files"] = _pending_files[chat_id]["files"][-3:]
    _pending_files[chat_id]["last_time"] = time.time()
    n = len(_pending_files[chat_id]["files"])
    print(f"    [暂存] {n}/3 个文件")

    if n == 3:
        try:
            send_text(chat_id,
                f"已收到 3 个文件，请发送参数文本，格式如下：\n"
                f"目标排片:0.2 总成本:300000 后台消耗:32.8 上一时段:83.4 D8百分比:4.4")
        except Exception as e:
            print(f"    [发送失败] {e}")


def handle_text_message(msg_id: str, chat_id: str, text: str):
    params = parse_params(text)
    if not params:
        return

    print(f"[参数] chat={chat_id} {params}")

    pending = _pending_files.get(chat_id)
    if not pending or len(pending["files"]) < 3:
        reply_text(msg_id,
            f"参数已收到，但仅找到 {len(pending['files']) if pending else 0} 个 Excel 文件（需要 3 个）。\n"
            f"请先发送 3 个 Excel 文件，再发送参数文本。")
        return

    files = pending["files"][-3:]
    del _pending_files[chat_id]

    reply_text(msg_id, "收到，正在处理...")

    work_dir = tempfile.mkdtemp(prefix="auto_")
    output_dir = tempfile.mkdtemp(prefix="auto_out_")

    try:
        for fname, fcontent in files:
            fpath = os.path.join(work_dir, fname)
            with open(fpath, "wb") as f:
                f.write(fcontent)

        result = process_data(
            work_dir=work_dir,
            output_dir=output_dir,
            target_paipian=params["target_paipian"],
            total_cost=params["total_cost"],
            backend_consume=params["backend_consume"],
            prev_actual=params["prev_actual"],
            d8_pct=params["d8_pct"],
        )

        print(f"  [处理完成] chat={chat_id} movie={result['movie_name']}")

        full_text = (
            f"=== 文案1 ===\n{result['wenan1']}\n\n"
            f"=== 文案2 ===\n{result['wenan2']}\n\n"
            f"=== 文案3 ===\n{result['wenan3']}"
        )
        send_text(chat_id, full_text)
        time.sleep(0.5)

        for fpath in [result["file1_output"], result["file3_output"]]:
            if os.path.exists(fpath):
                fk = upload_file_to_feishu(fpath, os.path.basename(fpath))
                send_file_to_chat(chat_id, fk)
                time.sleep(0.3)

    except Exception as e:
        err = f"处理失败: {e}"
        print(f"  [失败] {err}\n{traceback.format_exc()[-300:]}")
        send_text(chat_id, err)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)


# ---- 事件回调 ----
_SEEN_FILE = Path(__file__).parent / ".seen_msg_ids"
_SEEN_MAX = 500
_SEEN_KEEP = 300
_seen_cache: set | None = None


def _get_seen() -> set:
    global _seen_cache
    if _seen_cache is None:
        if _SEEN_FILE.exists():
            with open(_SEEN_FILE, "r") as f:
                _seen_cache = set(line.strip() for line in f if line.strip())
        else:
            _seen_cache = set()
    return _seen_cache


def _trim_seen():
    global _seen_cache
    if _seen_cache is None or len(_seen_cache) <= _SEEN_MAX:
        return
    with open(_SEEN_FILE, "r") as f:
        lines = [line.strip() for line in f if line.strip()]
    _seen_cache = set(lines[-_SEEN_KEEP:])
    with open(_SEEN_FILE, "w") as f:
        for line in lines[-_SEEN_KEEP:]:
            f.write(line + "\n")


def _is_duplicate(msg_id: str) -> bool:
    seen = _get_seen()
    if msg_id in seen:
        return True
    seen.add(msg_id)
    with open(_SEEN_FILE, "a") as f:
        f.write(msg_id + "\n")
    if len(seen) > _SEEN_MAX:
        _trim_seen()
    return False


def on_message(event):
    msg = event.event.message
    msg_id = msg.message_id
    msg_type = msg.message_type

    # 忽略机器人自己发的消息
    sender = event.event.sender
    if getattr(sender, 'sender_type', '') == 'app':
        return

    print(f"[事件] type={msg_type} chat_id={msg.chat_id}")

    if _is_duplicate(msg_id):
        return

    cleanup_pending()

    chat_id = msg.chat_id
    try:
        content_json = json.loads(msg.content)
    except json.JSONDecodeError:
        print(f"[事件] JSON解析失败: {msg.content[:100]}")
        return

    if msg_type == "file":
        handle_file_message(msg_id, chat_id,
            content_json.get("file_key", ""),
            content_json.get("file_name", ""))
    elif msg_type == "text":
        handle_text_message(msg_id, chat_id, content_json.get("text", ""))


# ---- 主入口 ----
def main():
    if not APP_ID or not APP_SECRET:
        print("请先在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        sys.exit(1)

    print(f"启动自动清洗机器人 (App ID: {APP_ID[:10]}...)")
    print("使用说明：")
    print("  1) 在群聊发送 3 个 Excel 文件")
    print("  2) 发送参数: 目标排片:0.2 总成本:300000 后台消耗:32.8 上一时段:83.4 D8百分比:4.4")
    print("  3) 机器人自动回复文案和处理后的文件")
    print()

    handler = EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    client = WsClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=handler,
        log_level=LogLevel.ERROR,
    )
    client.start()


if __name__ == "__main__":
    main()
