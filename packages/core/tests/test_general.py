from openai import OpenAI

client = OpenAI(
    api_key="sk-MKYsdcbXtyphBSmztNxu448MEfiKnNRDkRwrJLgiq9ogRjup",
    base_url="https://dsjai.xin/v1",
)

response = client.chat.completions.create(
    model="claude-opus-4-7",
    messages=[
        {"role": "system", "content": "你是一个可靠的企业助手。"},
        {"role": "user", "content": "用三句话总结 D-API 的接入方式。"},
    ],
)

print(response.choices[0].message.content)