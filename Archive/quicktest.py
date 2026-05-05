import ollama
resp = ollama.chat(
    model="qwen2.5-coder:7b",
    messages=[{"role": "user", "content": "What ERROR events are in the logs? Use the query_logs tool."}],
    tools=[{
        "type": "function",
        "function": {
            "name": "query_logs",
            "description": "Query the log database",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    }],
)
print("CONTENT:", repr(resp["message"].get("content")))
print("TOOL_CALLS:", resp["message"].get("tool_calls"))