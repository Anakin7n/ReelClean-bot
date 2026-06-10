# ReelClean-bot — 飞书影城数据自动清洗机器人

## 概述

飞书群聊机器人，接收 3 个 Excel 文件 + 4 个参数（内联表单卡片 / 兼容纯文本），自动处理影院数据，回复三份文案和两个处理后的文件。WebSocket 长连接模式，无需公网 IP。

核心使用原生 `websockets` + `requests` + 自写 protobuf 编解码器；卡片响应部分借用 `lark-oapi` SDK 的 `Frame` 类（连接建立后预加载，不影响启动速度）。

## 文件结构

```
D:\ReelClean-bot\
├── auto_bot.py          # 机器人主程序（WebSocket 客户端 + 事件处理）
├── auto_clean.py        # 核心数据处理逻辑（process_data 入口）
├── requirements.txt     # Python 依赖
├── install.bat          # 一键安装脚本（双击自动创建虚拟环境 + 安装依赖 + 生成 .env 模板）
├── start.vbs            # 双击启动（推荐，零闪屏）
├── start.ps1            # PowerShell 启动（备用）
├── .venv/               # Python 虚拟环境（不提交 git）
├── .env                 # 飞书应用凭证（不提交 git）
├── .gitignore           # Git 忽略规则
└── .seen_msg_ids        # 消息去重缓存（自动生成）
```

所有代码自包含在一个目录中，无需外部文件。

## 环境要求

- Windows 10+
- Python 3.12+
- 推荐使用项目虚拟环境（`install.bat` 一键创建），也可直接使用系统 Python：
  ```
  pip install -r requirements.txt
  ```
- 飞书应用（需开启机器人能力，订阅 `im.message.receive_v1` 事件）

## 飞书应用配置

1. 飞书开放平台 → 创建应用 → 开启**机器人**能力
2. **权限管理**添加：
   - `im:message.group_msg` — 获取群组中所有消息（敏感权限）
   - `im:message:send_as_bot` — 以应用的身份发消息
   - `im:resource` — 获取与上传图片或文件资源
3. **事件订阅**添加 `im.message.receive_v1`（WebSocket 模式无需回调地址）
4. 发布应用并通过审核
5. 获取 App ID / App Secret 填入 `.env`

## .env 格式

```
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxx
```

## 入口

| 方式 | 文件 | 说明 |
|------|------|------|
| 推荐启动 | `start.vbs` | 双击弹出 PowerShell 蓝窗口，零闪屏 |
| 备用启动 | `start.ps1` | 右键"使用 PowerShell 运行"，自动识别 .venv 或系统 Python |
| 安装脚本 | `install.bat` | 首次使用双击运行，一键创建虚拟环境 + 安装依赖 |

启动脚本 `start.ps1` 会自动判断：有 `.venv` 则用虚拟环境 Python，没有则用系统 Python。

## 移植到新设备

1. 复制整个 `ReelClean-bot\` 文件夹到新设备
2. 安装 Python 3.12+（勾选"Add Python to PATH"）
3. 双击 `install.bat`，自动完成虚拟环境创建、pip 升级、依赖安装、.env 模板生成
4. 编辑 `.env` 填入飞书凭证
5. 双击 `start.vbs` 启动

> 如果不想用虚拟环境，可跳过第 3 步，直接 `pip install -r requirements.txt` 安装依赖到系统 Python。启动脚本会自动识别。

## 代码说明

### 架构

不依赖飞书官方 SDK，自建轻量客户端：

- **`FeishuWsClient`** — 自定义 WebSocket 客户端，调用 `/callback/ws/endpoint` 获取连接地址
- **`encode_ping_frame()` / `decode_frame()`** — 自写 protobuf 编解码器（飞书 WS 帧格式仅需几个字段，无需完整 protobuf 库）
- **HTTP API** — 直接用 `requests` 调用飞书 REST API（发消息、上传/下载文件）
- **`auto_clean`** — 懒加载，首次处理 Excel 时才导入（导入 pandas/numpy ~1.7s）

### WebSocket 长连接架构

```
FeishuWsClient.connect()
  └─ _try_connect()
       ├─ _get_ws_url()   → POST /callback/ws/endpoint 获取 WS 地址
       ├─ websockets.connect(url)   ← 直连飞书 WebSocket 网关
       ├─ _ping_loop()    → 定时发送 protobuf 编码的 ping 帧
       └─ _read_loop()
            ├─ recv() → decode_frame() → 帧类型判断
            └─ type=1: asyncio.create_task(_process_event())
                         └─ loop.run_in_executor(线程池) → handle_xxx()
```

关键细节：
- WS 端点 URL 为 `https://open.feishu.cn/callback/ws/endpoint`
- 从 URL 参数 `service_id` 提取后用于 ping 帧编码
- 事件处理用 `loop.run_in_executor` 丢到 `ThreadPoolExecutor(max_workers=2)` 避免阻塞事件循环
- 连接断开自动重连，重连间隔 120s

### Protobuf 编解码

自写简化版 protobuf 编解码器处理 ping 帧和基本事件解码，卡片响应则借用 SDK 的 `Frame` 类正确序列化：
- `encode_ping_frame(service_id)` — 编码 ping 帧（Header: type=ping）
- `decode_frame(data)` — 解码 WebSocket 接收帧，支持 varint、length-delimited、嵌套 header 解析
- `frame_type(frame)` / `frame_data_payload(frame)` — 提取帧类型和载荷
- 卡片 ACK：回传入站 `Frame` + 替换 `payload` 为 `{code: 200, data: base64(...)}`（飞书卡片协议要求）

### 消息流程

```
文件消息 → on_file_message() → 下载并暂存到 _pending_files
                                  ↓ (凑齐3个)
                             发送内联表单卡片（form 标签 + 4个输入框）
卡片提交 → card.action.trigger → WS ACK（toast + 卡片更新为"处理中"）
                               → handle_card_action() → 后台线程 _execute_process()
文本消息 → parse_params() → 解析参数（兼容旧流程）
         → on_text_message() → 取暂存文件 → _execute_process()
                               → 回复文案 + 上传处理后的文件
```

### 关键函数

**Bot 调度层（`auto_bot.py`）：**

| 函数 | 作用 |
|------|------|
| `get_form_card_json()` | 生成内联表单卡片（4个输入框 + 提交按钮） |
| `parse_params()` | 从文本解析 4 个参数，支持中英文冒号和等号 |
| `on_file_message()` | 下载文件并暂存到内存 `_pending_files`，集齐3个后发送表单卡片 |
| `on_text_message()` | 匹配参数与文件，触发处理（兼容纯文本流程） |
| `handle_card_action()` | 表单提交入口：校验参数 → 后台线程执行清洗 |
| `_is_duplicate()` | 消息去重，内存 set + 文件持久化 |

**数据清洗层（`auto_clean.py`）：**

| 函数 | 作用 |
|------|------|
| `process_data()` | 主入口，串联全部处理流程，返回三份文案 + 两个输出文件路径 |
| `identify_files()` | 按命名规则识别三个输入文件（`<电影>-落`、`影城明细-<电影>`、第三个） |
| `process_file3()` | 处理文件3：自动提取目标排片、剔除已撤回/已驳回、计算实际消耗 |
| `process_file1_sheet1()` | 处理文件1 Sheet1：从 File2/File3 合并影城数据，重算 Excel 公式列 |
| `compute_luowei_percentages()` | 计算落位 Sheet 的分日新增比例（F9）和实际开场数量（F16） |
| `save_file1_and_write_d8()` | 将处理后的 Sheet1 回写到 File1，并写入落位 D8 公式 |
| `generate_wenan1()` | 文案1：后台消耗 vs 实际消耗对比 |
| `generate_wenan2()` | 文案2：合作影城开场情况统计 |
| `generate_wenan3()` | 文案3：单体落位预估（低值-高值区间） |

### 参数格式

```
总成本:300000 后台消耗:32.8 上一时段:83.4 今日新增占比:4.4
```

支持 `:` `：` `=` 作为分隔符，参数顺序不限，缺一不触发。

### 去重机制

启动时从 `.seen_msg_ids` 加载已处理 ID 到内存 set，每次新消息追加写入文件。超过 500 条自动裁剪到 300 条。

### 注意事项

- `_pending_files` 在内存中，重启 bot 后暂存的文件会丢失
- 超过 10 分钟未收到参数文本的暂存文件会自动清理
- bot 自己的文件消息会被忽略（sender_type == 'app'）
- 处理是同步的，同时段只处理一个请求
