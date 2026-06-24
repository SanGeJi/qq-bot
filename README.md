# 🤖 QQ Chat Bot

基于 [OneBot v11](https://onebot.dev/) 协议的 QQ 机器人，需要配合 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 使用。NapCatQQ 负责连接 QQ，本程序负责 AI 对话。

## ✨ 功能特点

- **首次配置向导**：双击 bat 自动弹出，一步步填完就能跑
- **配置极简**：只需填 3 个字段（平台名、API 地址、密钥）
- **自动获取模型**：启动时自动从 API 拉模型列表，智能选择
- **指令切换模型**：发 /model 查看和切换可用模型
- **自定义预设**：presets/ 文件夹丢 txt 文件，发 /预设 文件名 切人设
- **私聊 + 群聊**：私聊自动回复，群聊 @ 触发
- **独立会话上下文**：每个私聊和群聊维护独立消息历史
- **会话重置**：发送 /重置会话 清空当前聊天上下文
- **自动重连**：WebSocket 断开后自动重连
- **长消息分割**：超长回复自动按句子分割发送

## 🚀 快速开始

### 0. 前置要求

- Python 3.9+
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)（必须，本程序不包含 QQ 登录功能）

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 双击启动（首次自动进配置向导示例）

**Windows 用户直接双击 `启动机器人.bat`**，首次运行会自动弹出配置向导：

```
==================================================
  QQ Bot - 首次配置向导
==================================================

① 平台名称（随便填）：deepseek
② API 地址：https://api.deepseek.com
③ API 密钥：sk-xxxx
④ 管理员 QQ 号（逗号分隔）：123456789
⑤ NapCatQQ 安装路径：D:\napcat

正在从 API 获取可用模型列表 ...
  [1] deepseek-v4-flash
  [2] deepseek-v4-pro
选择模型 (1-2, 回车=自动):
```

填完自动保存 → 直接启动。配置保存在 config.json 和 .env，之后每次双击直接跑。

> 💡 想重新配置？双击 `初始化.bat` 即可重新进入向导。手动方式：`python setup.py`

> ⚠️ 本程序是 NapCatQQ 的**搭档**，不是独立 QQ 客户端。运行前先确保 NapCatQQ 已启动并连上了 QQ。

## 📖 配置说明

### 配置文件

向导自动生成 `config.json` 和 `.env`。也可以手动复制模板：

```bash
cp config.example.json config.json   # 编辑 3 个字段
cp .env.example .env                 # 或直接填环境变量
```

### config.json（只需 3 个字段）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| name | 平台名称（随便取） | "" |
| api_key | API 密钥 | "" |
| api_base | API 地址 | "" |
| model | 模型名（留空自动获取） | "" |
| admin_qqs | 管理员 QQ 号 | [] |
| ws_address | WebSocket 地址 | ws://127.0.0.1:6700 |

### 环境变量

| 变量 | 说明 |
|------|------|
| QQBOT_API_KEY | API 密钥 |
| QQBOT_API_BASE | API 地址 |
| QQBOT_NAME | 平台名称 |
| QQBOT_MODEL | 模型名 |
| QQBOT_ADMIN_QQS | 管理员 QQ（逗号分隔） |
| NAPCTAT_DIR | NapCatQQ 安装路径 |

### 自定义预设

在 `presets/` 文件夹放 `.txt` 文件，每个文件是一个提示词预设。用 `/预设` 指令切换：

```
/预设              → 列出可用预设
/预设 文件名        → 加载该预设（无需加 .txt 后缀）
```

切预设会自动清空所有会话上下文。选中的预设名保存在 bot_state.json，重启后自动恢复。

## 🎮 命令列表

#### 通用命令（所有人可用）

| 命令 | 说明 |
|------|------|
| /菜单 或 /help | 显示帮助 |
| /重置会话 | 清空当前会话上下文 |

#### 管理员命令

| 命令 | 说明 |
|------|------|
| /model | 查看可用模型列表 |
| /model 模型名 | 切换到指定模型 |
| /预设 | 查看可用预设列表 |
| /预设 文件名 | 加载预设文件夹中的提示词 |
| /switch | 查看可切换的配置档 |
| /switch 配置档 | 切换配置档 |
| /本群关闭 | 关闭当前群聊的机器人 |
| /本群开启 | 开启当前群聊的机器人 |
| /本群自由聊天开启 | 开启自由聊天（无需 @） |
| /本群自由聊天关闭 | 关闭自由聊天（需 @ 才回复） |
| /全局私信关闭 | 关闭所有人的私聊（管理员除外） |
| /私信全局开启 | 开启全局私聊 |
| /状态 | 查看机器人运行状态 |
| /用户列表 | 查看所有交互过的用户 |
| /用户信息 QQ号 | 查看指定用户的详细信息 |

## 📁 项目结构

```
qq-bot/
├── setup.py             # 首次配置向导
├── main.py              # 主入口
├── config.py            # 配置管理（JSON + .env + 旧格式兼容）
├── session.py           # 会话管理
├── state.py             # 机器人状态持久化
├── llm_client.py        # LLM API 客户端
├── qqbot.py             # QQ 机器人核心
├── launcher.py          # 智能启动器（自动启动 NapCat）
├── user_profiles.py     # 用户档案
├── test_model.py        # 模型连通性测试工具
├── presets/             # 自定义预设文件夹
│   └── 默认.txt          # 默认预设
├── requirements.txt     # 依赖清单
├── config.example.json  # 配置模板（3 字段）
├── .env.example         # 环境变量模板
├── prompt.txt           # 额外提示词参考
├── start_bot.bat        # 启动脚本（英文）
├── 启动机器人.bat        # 启动脚本（中文）
├── 初始化.bat            # 重新进入配置向导
└── LICENSE
```

## 📄 License

MIT License - 详见 [LICENSE](./LICENSE) 文件。
