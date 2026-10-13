from openai import OpenAI

# 将 base_url 指向你本地服务的 API 端口
client = OpenAI(
    api_key="none",  # 本地代理无需真正的 API Key
    base_url="http://127.0.0.1:8000/v1",
    timeout=240.0,   # 避免服务端卡住时客户端无限等待
    max_retries=0,
)

# 第一轮对话：请求写 Python 脚本
response1 = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "user", "content": "用Python写一个简单的FastAPI HelloWorld程序。"}
    ]
)

print("AI 回复内容：")
print(response1.choices[0].message.content)
