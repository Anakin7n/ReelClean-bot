# ReelClean-bot — 飞书影城数据自动清洗机器人

## 概述

飞书群聊机器人，接收 3 个 Excel 文件 + 6 个参数文本，自动处理影院数据，回复三份文案和两个处理后的文件。WebSocket 长连接模式，无需公网 IP。

## 文件结构

```
D:\ReelClean-bot\
├── auto_bot.py          # 机器人主程序（飞书事件处理 + 文件收发）
├── auto_clean.py        # 核心数据处理逻辑（process_data 入口）
├── requirements.txt     # Python 依赖
├── start.vbs            # 双击启动（推荐，零闪屏）
├── start.ps1            # PowerShell 启动（备用）
├── .venv/               # Python 虚拟环境（不提交 git）
├── .env                 # 飞书应用凭证（不提交 git）
├── .gitignore           # Git 忽略规则
├── .seen_msg_ids        # 消息去重缓存（自动生成）
└── CLAUDE.md            # 本文件
```

所有代码自包含在一个目录中，无需外部文件。

## 环境要求

- Windows 10+
- Python 3.12+
- 使用项目自带的虚拟环境（`.venv/`），不依赖系统 Python：
  ```
  python -m venv .venv
  .\.venv\Scripts\pip install -r requirements.txt
  ```
- 飞书应用（需开启机器人能力，订阅 `im.message.receive_v1` 事件）

## 飞书应用配置

1. 飞书开放平台 → 创建应用 → 开启**机器人**能力
2. **权限管理**添加：
   - `im:message` — 读取消息
   - `im:message:send_as_bot` — 发送消息
   - `im:resource` — 上传/下载文件
3. **事件订阅**添加 `im.message.receive_v1`（WebSocket 模式无需回调地址）
4. 发布应用并通过审核
5. 获取 App ID / App Secret 填入 `.env`

## .env 格式

```
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxx
```

## 移植到新设备

1. 复制整个 `ReelClean-bot\` 文件夹到新设备
2. 安装 Python 3.12+
3. 创建虚拟环境并安装依赖：
   ```
   python -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   ```
4. 填入飞书凭证到 `.env`
5. 双击 `start.vbs` 启动

## 代码说明

### 消息流程

```
文件消息 → handle_file_message() → 下载并暂存
                                      ↓ (凑齐3个)
                                   发送提示：请发参数
文本消息 → parse_params() → 解析参数
         → handle_text_message() → 取暂存文件 → process_data()
         → 回复文案 + 上传处理后的文件
```

### 关键函数

| 函数 | 作用 |
|------|------|
| `process_data()` | 核心处理（来自 auto_clean.py），输入目录+参数，返回结果字典 |
| `parse_params()` | 从文本解析 6 个参数，支持中英文冒号和等号 |
| `handle_file_message()` | 下载文件并暂存到内存 `_pending_files` |
| `handle_text_message()` | 匹配参数与文件，触发处理 |
| `_is_duplicate()` | 消息去重，内存 set + 文件持久化 |

### 参数格式

```
目标排片:0.2 总成本:300000 后台消耗:32.8 上一时段:83.4 D8百分比:4.4
```

支持 `:` `：` `=` 作为分隔符，参数顺序不限，缺一不触发。

### 去重机制

启动时从 `.seen_msg_ids` 加载已处理 ID 到内存 set，每次新消息追加写入文件。超过 500 条自动裁剪到 300 条。

### 注意事项

- `_pending_files` 在内存中，重启 bot 后暂存的文件会丢失
- 超过 10 分钟未收到参数文本的暂存文件会自动清理
- bot 自己的文件消息会被忽略（sender_type == 'app'）
- 处理是同步的，同时段只处理一个请求
