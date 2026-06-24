"""鉴别中转站是否返回真正的 GPT-5.5"""
import requests, json, time

PROXIES = [
    ("示例中转站1", "https://your-proxy-1.example.com/v1", "your-api-key-1", "gpt-4o"),
    ("示例中转站2", "https://your-proxy-2.example.com/v1", "your-api-key-2", "deepseek-chat"),
]

def ask(name, base, key, model, system, user, max_tokens=300):
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], "max_tokens": max_tokens, "temperature": 0},
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"], data.get("model", "?")
        return f"HTTP {resp.status_code}: {resp.text[:200]}", "?"
    except Exception as e:
        return f"错误: {e}", "?"

TESTS = [
    {
        "name": "知识截止日期",
        "system": "你是一个有用的助手。",
        "user": "你的训练数据截止到什么时候？你的知识截止日期是哪一天？请直接回答日期。",
    },
    {
        "name": "self-identify",
        "system": "精确回答用户问题，不要废话。",
        "user": "What is your exact model name and version? Who created you?",
    },
    {
        "name": "coding-hard",
        "system": "你是一个编程助手，直接给出结果。",
        "user": "Write a Python one-liner that solves the n-queens problem for n=8 using bitwise operations. Just the code.",
        "max_tokens": 200,
    },
    {
        "name": "reasoning-deep",
        "system": "你是一个逻辑推理专家。",
        "user": """有五个人：A、B、C、D、E。每人说了一句话：
A说：C是凶手
B说：我不是凶手
C说：E不是凶手
D说：A是凶手
E说：D说的是真话
已知只有凶手说谎，其他人说真话。凶手是谁？请一步步推理。""",
    },
    {
        "name": "math-competition",
        "system": "你是一个数学家，直接计算。",
        "user": "计算 sum_{k=1}^{100} floor(sqrt(k)) 的值。直接给答案和简要推导。",
    },
]

for name, base, key, model in PROXIES:
    print(f"\n{'='*60}")
    print(f"  {name}  ({base})")
    print(f"{'='*60}")
    for test in TESTS:
        mt = test.get("max_tokens", 300)
        reply, actual = ask(name, base, key, model, test["system"], test["user"], mt)
        print(f"\n  [{test['name']}]")
        print(f"  返回model: {actual}")
        print(f"  回复: {reply[:400]}")
        time.sleep(0.5)

# 额外：测一下 token 速度（粗略）
print(f"\n{'='*60}")
print(f"  速度对比（长文本生成）")
print(f"{'='*60}")
speed_prompt = "写一篇800字左右的文章，主题是人工智能对教育的影响。要认真写，结构完整。"
for name, base, key, model in PROXIES:
    try:
        t0 = time.time()
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "user", "content": speed_prompt},
            ], "max_tokens": 1200, "temperature": 0.7},
            timeout=120,
        )
        elapsed = time.time() - t0
        if resp.status_code == 200:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            chars = len(text)
            tok_est = chars // 2  # 粗略估算
            print(f"  {name}: {chars}字符 / {elapsed:.1f}秒 ≈ {tok_est/elapsed:.0f} tok/s (估算)")
        else:
            print(f"  {name}: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  {name}: 错误: {e}")
