"""
飞书机器人 - 群聊接收 Excel 文件 + 参数，自动处理并回复文案和文件。
支持两种交互方式：
  1) 交互式卡片弹窗（主流）：集齐3个文件 → 发送卡片 → 点击按钮填弹窗表单 → 提交
  2) 纯文本解析（兼容旧流程）：发送参数文本（总成本:300000 后台消耗:32.8 ...）
WebSocket 长连接模式，无需 ngrok。使用原生 websockets，启动秒级响应。
"""
import asyncio
import concurrent.futures
import inspect
import json
import os
import re
import sys
import time
import shutil
import tempfile
import threading
import traceback
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
import requests
import websockets

load_dotenv()

APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

_FEISHU_DOMAIN = "https://open.feishu.cn"
_WS_ENDPOINT_URI = "/callback/ws/endpoint"

# ---- 文件暂存 ----
_pending_files = {}
_PENDING_TIMEOUT = 600

# ---- 卡片/Dialog JSON ----
def get_dialog_card_json() -> str:
    """返回交互式卡片 JSON（含 Dialog 弹窗表单）。
    按钮在卡片上，点击弹出弹窗含4个输入框；提交后推送 card.action.trigger 事件。

    注意：飞书 IM 消息 API 不支持 form 内嵌标签，dialogue 是唯一可行的交互式表单方案。
    """
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📊 参数填写"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "已收到 **3 个 Excel 文件**，请点击下方按钮填写清洗参数 👇",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📝 填写参数并清洗"},
                        "type": "primary",
                        "value": {},
                        "dialog": {
                            "title": {
                                "tag": "plain_text",
                                "content": "填写清洗参数",
                            },
                            "elements": [
                                {
                                    "tag": "input",
                                    "name": "total_cost",
                                    "label": {"tag": "plain_text", "content": "总成本"},
                                    "placeholder": {
                                        "tag": "plain_text",
                                        "content": "请输入总成本（如 300000）",
                                    },
                                    "required": True,
                                },
                                {
                                    "tag": "input",
                                    "name": "backend_consume",
                                    "label": {"tag": "plain_text", "content": "后台消耗"},
                                    "placeholder": {
                                        "tag": "plain_text",
                                        "content": "请输入后台消耗（如 32.8）",
                                    },
                                    "required": True,
                                },
                                {
                                    "tag": "input",
                                    "name": "prev_actual",
                                    "label": {"tag": "plain_text", "content": "上一时段"},
                                    "placeholder": {
                                        "tag": "plain_text",
                                        "content": "请输入上一时段（如 83.4）",
                                    },
                                    "required": True,
                                },
                                {
                                    "tag": "input",
                                    "name": "d8_pct",
                                    "label": {"tag": "plain_text", "content": "D8百分比"},
                                    "placeholder": {
                                        "tag": "plain_text",
                                        "content": "请输入D8百分比（如 4.4）",
                                    },
                                    "required": True,
                                },
                            ],
                            "submit_actions": [
                                {
                                    "tag": "button",
                                    "text": {
                                        "tag": "plain_text",
                                        "content": "✅ 确认提交",
                                    },
                                    "type": "primary",
                                    "value": {},
                                }
                            ],
                        },
                    }
                ],
            },
        ],
    }
    return json.dumps(card, ensure_ascii=False)


# ---- token 缓存 ----
_token_cache = {"token": "", "expire": 0}


def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire"]:
        return _token_cache["token"]
    resp = requests.post(
        f"{_FEISHU_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"获取token失败: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire"] = now + data.get("expire", 7200) - 300
    return _token_cache["token"]


def _feishu_post(path, json_body=None):
    headers = {"Authorization": f"Bearer {_get_token()}"}
    resp = requests.post(f"{_FEISHU_DOMAIN}{path}", headers=headers, json=json_body, timeout=30)
    # 先尝试读取响应体（飞书即使 400 也会返回 JSON 含 code/msg）
    try:
        data = resp.json()
    except Exception:
        data = {"code": -1, "msg": resp.text[:500]}
    code = data.get("code", -1)
    if code != 0:
        # 构建完整的错误信息
        err_detail = (
            f"\n{'='*60}\n"
            f"[飞书API错误]\n"
            f"  接口: {path}\n"
            f"  HTTP状态: {resp.status_code}\n"
            f"  飞书code: {code}\n"
            f"  飞书msg:  {data.get('msg', '')}\n"
            f"  请求体:   {json.dumps(json_body, ensure_ascii=False) if json_body else 'None'}\n"
            f"{'='*60}"
        )
        print(err_detail)
        raise Exception(f"飞书API错误 [{path}]: code={code} msg={data.get('msg', '')}")
    return data


def send_text(chat_id: str, text: str):
    content = json.dumps({"text": text}, ensure_ascii=False)
    return _feishu_post(
        f"/open-apis/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": chat_id, "msg_type": "text", "content": content},
    )


def reply_text(message_id: str, text: str):
    content = json.dumps({"text": text}, ensure_ascii=False)
    return _feishu_post(
        f"/open-apis/im/v1/messages/{message_id}/reply",
        {"msg_type": "text", "content": content},
    )


def send_interactive_card(chat_id: str, card_json: str):
    """发送交互式卡片消息。card_json 为飞书卡片 JSON 字符串。"""
    return _feishu_post(
        f"/open-apis/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": chat_id, "msg_type": "interactive", "content": card_json},
    )


def upload_file_to_feishu(file_path: str, file_name: str) -> str:
    token = _get_token()
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{_FEISHU_DOMAIN}/open-apis/im/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (file_name, f)},
            data={"file_type": "stream", "file_name": file_name},
            timeout=30,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"上传文件失败: {data}")
    return data["data"]["file_key"]


def send_file_to_chat(chat_id: str, file_key: str):
    content = json.dumps({"file_key": file_key}, ensure_ascii=False)
    return _feishu_post(
        f"/open-apis/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": chat_id, "msg_type": "file", "content": content},
    )


def download_file_from_feishu(message_id: str, file_key: str) -> bytes:
    token = _get_token()
    resp = requests.get(
        f"{_FEISHU_DOMAIN}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
        headers={"Authorization": f"Bearer {token}"},
        params={"type": "file"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


# ---- 简化 protobuf frame 解析 ----
def _pb_varint_size(n: int) -> int:
    size = 1
    while n > 127:
        size += 1
        n >>= 7
    return size


def _pb_write_varint(buf: bytearray, n: int):
    while n > 127:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n & 0x7F)


def _pb_write_tag(buf: bytearray, field: int, wire: int):
    _pb_write_varint(buf, (field << 3) | wire)


def _pb_write_bytes(buf: bytearray, data: bytes):
    _pb_write_varint(buf, len(data))
    buf.extend(data)


def _pb_write_string(buf: bytearray, field: int, value: str):
    _pb_write_tag(buf, field, 2)
    encoded = value.encode("utf-8")
    _pb_write_bytes(buf, encoded)


def _pb_write_uint64(buf: bytearray, field: int, value: int):
    _pb_write_tag(buf, field, 0)
    _pb_write_varint(buf, value)


def _pb_write_int32(buf: bytearray, field: int, value: int):
    _pb_write_tag(buf, field, 0)
    _pb_write_varint(buf, value)


def encode_ping_frame(service_id: int) -> bytes:
    buf = bytearray()
    header_buf = bytearray()
    _pb_write_string(header_buf, 1, "type")
    _pb_write_string(header_buf, 2, "ping")
    _pb_write_int32(buf, 4, 0)
    _pb_write_uint64(buf, 2, 0)
    _pb_write_uint64(buf, 1, 0)
    _pb_write_tag(buf, 3, 0)
    _pb_write_varint(buf, service_id)
    _pb_write_tag(buf, 5, 2)
    _pb_write_varint(buf, len(header_buf))
    buf.extend(header_buf)
    return bytes(buf)


def decode_frame(data: bytes) -> dict:
    result = {}
    pos = 0
    while pos < len(data):
        if pos >= len(data):
            break
        tag = data[pos]
        pos += 1
        field = tag >> 3
        wire = tag & 0x07
        if wire == 0:
            value = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                pos += 1
                value |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            result[field] = value
        elif wire == 2:
            length = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            value = data[pos:pos + length]
            pos += length
            if field == 5:
                headers = []
                hpos = 0
                while hpos < len(value):
                    htag = value[hpos]
                    hpos += 1
                    hfield = htag >> 3
                    hwire = htag & 0x07
                    if hwire == 2:
                        hlen = 0
                        hshift = 0
                        while hpos < len(value):
                            hb = value[hpos]
                            hpos += 1
                            hlen |= (hb & 0x7F) << hshift
                            hshift += 7
                            if not (hb & 0x80):
                                break
                        headers.append((hfield, value[hpos:hpos + hlen].decode("utf-8")))
                        hpos += hlen
                result[field] = headers
            else:
                result[field] = value
    return result


def frame_type(frame: dict) -> int:
    return frame.get(4, -1)


def frame_data_payload(frame: dict) -> bytes:
    return frame.get(8, b"")


# ---- 消息处理 ----
def cleanup_pending():
    now = time.time()
    for chat_id in list(_pending_files):
        if now - _pending_files[chat_id]["last_time"] > _PENDING_TIMEOUT:
            del _pending_files[chat_id]


def parse_params(text: str) -> dict | None:
    text = re.sub(r'@\S+\s*', '', text).strip()
    params = {}
    patterns = [
        (r'总成本[：:=]\s*([\d.]+)', 'total_cost'),
        (r'后台消耗[：:=]\s*([\d.]+)', 'backend_consume'),
        (r'上一时段[：:=]\s*([\d.]+)', 'prev_actual'),
        (r'D\d*百分比[：:=]\s*([\d.]+)', 'd8_pct'),
    ]
    for pattern, key in patterns:
        m = re.search(pattern, text)
        if m:
            params[key] = m.group(1)
    required = ['total_cost', 'backend_consume', 'prev_actual', 'd8_pct']
    if all(k in params for k in required):
        for k in required:
            params[k] = float(params[k])
        return params
    return None


def _execute_process(chat_id: str, files: list, params: dict):
    """核心处理流程：写临时文件 → 调用 process_data → 回复文案和结果文件。
    on_text_message 和 handle_card_action 共用此函数。"""
    from auto_clean import process_data

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
        try:
            send_text(chat_id, err)
        except Exception:
            pass

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)


def on_file_message(msg_id: str, chat_id: str, file_key: str, file_name: str):
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
            card_json = get_dialog_card_json()
            send_interactive_card(chat_id, card_json)
        except Exception as e:
            print(f"    [发送卡片失败] {e}")


def on_text_message(msg_id: str, chat_id: str, text: str):
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
    _execute_process(chat_id, files, params)


def handle_card_action(event_data: dict):
    """处理 Dialog 弹窗表单提交（card.action.trigger 事件）。

    关键时序：
      1. ACK 已在 _read_loop 中 <3ms 内发送（避免飞书超时）
      2. 此处提取 event.action.form_value 中的4个参数
      3. 使用 threading.Thread 后台执行 _execute_process，绝不阻塞事件循环
    """
    event = event_data.get("event", {})
    action = event.get("action", {})
    form_value = action.get("form_value", {}) or {}
    context = event.get("context", {})
    chat_id = context.get("open_chat_id", "") or context.get("chat_id", "")

    if not chat_id:
        print("[卡片动作] 无法获取 chat_id，忽略")
        return

    print(f"[卡片动作] chat_id={chat_id} form_value={form_value}")

    # 提取并校验4个参数（form_value 中都是字符串）
    try:
        params = {
            "total_cost": float(form_value.get("total_cost", 0)),
            "backend_consume": float(form_value.get("backend_consume", 0)),
            "prev_actual": float(form_value.get("prev_actual", 0)),
            "d8_pct": float(form_value.get("d8_pct", 0)),
        }
    except (ValueError, TypeError) as e:
        print(f"[卡片动作] 参数解析失败: {e}")
        send_text(chat_id, "参数格式有误，请填写有效数字后重新提交。")
        return

    print(f"[卡片动作] 解析成功: {params}")

    # 匹配暂存文件（必须在 spawn 线程前取出，避免竞态）
    pending = _pending_files.get(chat_id)
    if not pending or len(pending["files"]) < 3:
        send_text(chat_id,
            f"未找到暂存文件（找到 {len(pending['files']) if pending else 0} 个，需要 3 个）。\n"
            f"请重新发送 3 个 Excel 文件。")
        return

    files = pending["files"][-3:]
    del _pending_files[chat_id]

    send_text(chat_id, "收到参数，正在后台处理...")

    # 关键：使用后台线程执行清洗，绝不阻塞 WebSocket 事件循环
    t = threading.Thread(
        target=_execute_process,
        args=(chat_id, files, params),
        daemon=True,
    )
    t.start()
    print(f"[卡片动作] 已启动后台线程 (thread={t.name}) chat_id={chat_id}")


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


def dispatch_event(event_data: dict):
    event = event_data.get("event", {})

    # ── card.action.trigger 事件（弹窗表单提交）──
    if event.get("type") == "card.action.trigger":
        handle_card_action(event_data)
        return

    # ── 消息事件（file / text）──
    msg = event.get("message", {})
    msg_id = msg.get("message_id", "")
    msg_type = msg.get("message_type", "")
    chat_id = msg.get("chat_id", "")

    sender = event.get("sender", {})
    if sender.get("sender_type") == "app":
        return

    print(f"[事件] type={msg_type} chat_id={chat_id}")

    if _is_duplicate(msg_id):
        return

    cleanup_pending()

    content_str = msg.get("content", "{}")
    try:
        content_json = json.loads(content_str)
    except json.JSONDecodeError:
        print(f"[事件] JSON解析失败: {content_str[:100]}")
        return

    if msg_type == "file":
        on_file_message(msg_id, chat_id,
            content_json.get("file_key", ""),
            content_json.get("file_name", ""))
    elif msg_type == "text":
        on_text_message(msg_id, chat_id, content_json.get("text", ""))


# ---- 自定义 WebSocket 客户端 ----
class FeishuWsClient:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.service_id = ""
        self._reconnect_interval = 120
        self._ping_interval = 120
        self._ws = None
        self._ws_url = ""
        self._ping_task = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self._reconnecting = False

    def _get_ws_url(self):
        resp = requests.post(
            f"{_FEISHU_DOMAIN}{_WS_ENDPOINT_URI}",
            headers={"locale": "zh"},
            json={"AppID": self.app_id, "AppSecret": self.app_secret},
            timeout=30,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"获取WS地址失败: {data}")
        dd = data.get("data", {})
        if dd.get("ClientConfig"):
            cc = dd["ClientConfig"]
            self._reconnect_interval = cc.get("ReconnectInterval", 120)
            self._ping_interval = cc.get("PingInterval", 120)
        return dd["URL"]

    async def _ping_loop(self):
        while True:
            try:
                if self._ws is not None:
                    sid = int(self.service_id) if self.service_id else 0
                    ping = encode_ping_frame(sid)
                    await self._ws.send(ping)
            except Exception as e:
                print(f"[ping失败] {e}")
            await asyncio.sleep(self._ping_interval)

    def _dispatch_sync(self, event_data: dict):
        try:
            dispatch_event(event_data)
        except Exception as e:
            print(f"[分发异常] {e}")
            traceback.print_exc()

    async def _process_event(self, event_data: dict):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._dispatch_sync, event_data)

    async def _read_loop(self):
        while True:
            try:
                raw = await self._ws.recv()
                if isinstance(raw, str):
                    continue
                frame = decode_frame(raw)
                ft = frame_type(frame)
                if ft == 0:
                    continue
                elif ft == 1:
                    payload = frame_data_payload(frame)
                    if not payload:
                        continue
                    event_data = json.loads(payload.decode("utf-8"))

                    # card.action.trigger：毫秒级发送 ACK，避免飞书 3 秒超时
                    event = event_data.get("event", {})
                    if event.get("type") == "card.action.trigger":
                        header = event_data.get("header", {})
                        message_id = header.get("event_id", "")
                        if message_id:
                            ack = json.dumps({
                                "message_id": message_id,
                                "code": 200,
                                "data": "{}",
                            })
                            await self._ws.send(ack)
                            print(f"[ACK] card.action.trigger message_id={message_id}")

                    asyncio.create_task(self._process_event(event_data))
            except websockets.exceptions.ConnectionClosed:
                print("[连接断开]")
                break
            except Exception as e:
                print(f"[读取异常] {e}")
                traceback.print_exc()

    async def _try_connect(self):
        url = self._get_ws_url()
        u = urlparse(url)
        q = parse_qs(u.query)
        self.service_id = q.get("service_id", [""])[0]
        print(f"[WS地址] {url[:80]}...")
        print(f"[服务ID] {self.service_id}")

        params = inspect.signature(websockets.connect).parameters
        kwargs = {"proxy": None} if "proxy" in params else {}
        self._ws = await websockets.connect(url, **kwargs)
        self._ws_url = url
        print("[WS已连接]")
        self._reconnecting = False
        self._ping_task = asyncio.create_task(self._ping_loop())
        await self._read_loop()

    async def connect(self):
        while True:
            try:
                await self._try_connect()
            except Exception as e:
                print(f"[连接失败] {e}")
            if self._ws is not None:
                await self._ws.close()
                self._ws = None
            if self._ping_task is not None:
                self._ping_task.cancel()
                self._ping_task = None
            self._reconnecting = True
            print(f"[重连] {self._reconnect_interval}s 后重试...")
            await asyncio.sleep(self._reconnect_interval)

    def start(self):
        asyncio.run(self.connect())


# ---- 主入口 ----
def main():
    if not APP_ID or not APP_SECRET:
        print("请先在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        sys.exit(1)

    print(f"启动自动清洗机器人 (App ID: {APP_ID[:10]}...)")
    print("使用说明：")
    print("  1) 在群聊发送 3 个 Excel 文件")
    print("  2) 点击 Bot 发送的卡片按钮，在弹窗中填写4个参数并提交")
    print("     （兼容旧流程：也可直接发送文本参数）")
    print("  3) 机器人自动回复文案和处理后的文件")
    print()

    client = FeishuWsClient(APP_ID, APP_SECRET)
    client.start()


if __name__ == "__main__":
    main()
