from langchain_ollama import ChatOllama
from langchain_core.tools import tool

@tool
def query_logs(question: str) -> str:
    """Query the log database."""
    return "ok"

llm = ChatOllama(model="qwen2.5-coder:7b", temperature=0).bind_tools([query_logs])
msg = llm.invoke("What ERROR events are in the logs? Use query_logs.")
print("CONTENT:", repr(msg.content))
print("TOOL_CALLS:", msg.tool_calls)