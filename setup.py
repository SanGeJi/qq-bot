"""
首次运行配置向导
没有 config.json 时自动启动，一步步引导填写配置。
"""
import json, os, re, sys, requests


def get_input(prompt: str, default: str = "") -> str:
    """获取用户输入，支持默认值。"""
    if default:
        val = input(f"{prompt}：").strip()
    else:
        val = input(f"{prompt}: ").strip()
    return val if val else default


def setup():
    print("=" * 50)
    print("  QQ Bot - 首次配置向导")
    print("=" * 50)
    print()
    print("接下来要填 5 个配置项，一步步来~")
    print("（直接回车跳过可选项）")
    print()

    config = {}

    # ---- 1. 平台名称 ----
    config["name"] = get_input("① 平台名称（随便填）", "deepseek")

    # ---- 2. API 地址 ----
    config["api_base"] = get_input("② API 地址", "https://api.deepseek.com")

    # ---- 3. API 密钥 ----
    while True:
        config["api_key"] = get_input("③ API 密钥")
        if config["api_key"]:
            break
        print("  ⚠️  API 密钥不能为空，请重新输入")

    # ---- 4. 管理员 QQ ----
    admin_input = get_input("④ 管理员 QQ 号（多个用逗号分隔）")
    if admin_input:
        try:
            config["admin_qqs"] = [int(x.strip()) for x in admin_input.split(",") if x.strip()]
        except ValueError:
            print("  ⚠️  格式不对，已跳过。启动后可在 config.json 中手动添加")
            config["admin_qqs"] = []

    # ---- 5. NapCatQQ 路径 ----
    napcat_dir = get_input("⑤ NapCatQQ 安装路径", "D:\\napcat")
    print()

    # ---- 6. 自动拉模型 ----
    print("正在从 API 获取可用模型列表 ...")
    model = ""
    models = []
    try:
        resp = requests.get(
            f"{config['api_base'].rstrip('/')}/models",
            headers={"Authorization": f"Bearer {config['api_key']}"},
            timeout=10,
        )
        if resp.status_code == 200:
            models = [item["id"] for item in resp.json().get("data", [])]
            # 过滤掉非聊天模型
            models = [m for m in models
                      if "embed" not in m.lower()
                      and "moderate" not in m.lower()
                      and "tts" not in m.lower()
                      and "whisper" not in m.lower()
                      and "dall" not in m.lower()]
            if models:
                print(f"\n找到 {len(models)} 个可用模型:")
                for i, m in enumerate(models[:20], 1):
                    tag = " ← 推荐" if "chat" in m.lower() else ""
                    print(f"  [{i}] {m}{tag}")
                if len(models) > 20:
                    print(f"  ... 还有 {len(models)-20} 个，用 /model 查看完整列表")
                print(f"  [0] 自动选择")
                print()

                choice = get_input(f"选择模型 (1-{min(len(models), 20)}, 回车=自动): ")
                if choice.isdigit():
                    idx = int(choice)
                    if 1 <= idx <= min(len(models), 20):
                        model = models[idx - 1]
                    else:
                        print("  序号超出范围，自动选择")
                print()
            else:
                print("  API 返回了空列表")
        else:
            print(f"  获取失败 (HTTP {resp.status_code})")
    except requests.exceptions.Timeout:
        print("  请求超时，请检查 API 地址是否正确")
    except requests.exceptions.ConnectionError:
        print(f"  无法连接 {config['api_base']}，请检查 API 地址")
    except Exception as e:
        print(f"  获取失败: {e}")

    # 自动选择的话
    if not model and models:
        chat = [m for m in models if "chat" in m.lower()]
        model = chat[0] if chat else models[0]
        print(f"已自动选择模型: {model}")
        print()

    config["model"] = model

    # ---- 保存 ----
    project_dir = os.path.dirname(os.path.abspath(__file__))

    # config.json
    config_path = os.path.join(project_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"✅ 配置已保存到 config.json")

    # .env（写 NapCat 路径 + API 密钥）
    env_path = os.path.join(project_dir, ".env")
    env_lines = []
    # 保留已有的 .env 内容（如果有）
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            existing = f.read()
        # 去掉已有的 NAPCTAT_DIR / QQBOT_API_KEY 行
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                key = stripped.split("=")[0].strip()
                if key in ("NAPCTAT_DIR", "QQBOT_API_KEY", "QQBOT_API_BASE", "QQBOT_NAME"):
                    continue
            env_lines.append(line)
        env_lines.append("")

    env_lines.append(f"NAPCTAT_DIR={napcat_dir}")
    env_lines.append(f"QQBOT_NAME={config['name']}")
    env_lines.append(f"QQBOT_API_BASE={config['api_base']}")
    env_lines.append(f"QQBOT_API_KEY={config['api_key']}")
    if config.get("admin_qqs"):
        env_lines.append(f"QQBOT_ADMIN_QQS={','.join(str(q) for q in config['admin_qqs'])}")

    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines) + "\n")
    print(f"✅ .env 已写入（包含 NapCat 路径）")

    print()
    print("=" * 50)
    print("  配置完成！")
    print("=" * 50)
    print()

    # ---- 启动机器人 ----
    print("正在启动机器人 ...")
    print()

    # 判断用 launcher 还是直接 main
    launcher = os.path.join(project_dir, "launcher.py")
    if os.path.exists(launcher):
        import subprocess
        subprocess.run([sys.executable, launcher])
    else:
        import subprocess
        subprocess.run([sys.executable, os.path.join(project_dir, "main.py")])


if __name__ == "__main__":
    setup()
