"""OpenAI 兼容的数据模型。

参考: https://platform.openai.com/docs/api-reference/chat
Pi Coding Agent (pi.dev) 通过 models.json 里的 "api": "openai-completions"
接入任何 OpenAI 兼容端点，因此这里需要完整支持 messages / tools / stream。
"""

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FunctionCall(BaseModel):
    name: str
    # OpenAI 的 arguments 是 JSON 字符串；这里以字符串对外暴露
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:16]}")
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    # Pi 会发送 tool / tool_call_id / name 等字段，宽松接收避免校验失败
    model_config = ConfigDict(extra="allow")

    role: str
    # content 可能是字符串，也可能是内容分片数组
    # （如 [{"type": "text", "text": "hi"}]，Pi / 新版 OpenAI 客户端会这样发），
    # 因此用 Any 宽松接收，构建 prompt 时再归一化为纯文本。
    content: Any | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    # Pi 会附带大量标准字段（temperature、top_p、reasoning_effort、...）
    # 用 extra="allow" 全量吞掉，绝不因未知字段报 422
    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-chat"
    messages: list[ChatMessage]
    stream: bool | None = False
    stream_options: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    # 网页版无法控制生成参数，以下字段仅作兼容占位（接收但不生效）
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: Any | None = None


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "deepseek-chat"
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "deepseek-web-bridge"
    # 多模态声明（OpenAI 兼容：
    #   * input_modalities / output_modalities 是 OpenAI 现行字段；
    #   * architecture.modality 是 OpenRouter 约定，Pi / OpenRouter 系客户端会读它做
    #     "这个模型能不能收图" 的判定。两者都给，最大化兼容。
    input_modalities: list[str] = Field(default_factory=lambda: ["text", "image"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    architecture: dict[str, Any] = Field(
        default_factory=lambda: {"modality": "text+image->text"}
    )


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelCard]


# Pi 的 models.json 里引用的 id 需要与这里一致
#: 本桥支持的多模态输入（图片经合成 paste 事件真实粘进网页输入框）。
INPUT_MODALITIES = ["text", "image"]
OUTPUT_MODALITIES = ["text"]
#: OpenRouter 风格的模态串，供 architecture.modality 使用。
MODALITY = "text+image->text"


SUPPORTED_MODELS = [
    {"id": "deepseek-chat", "context_window": 65536},
    {"id": "deepseek-reasoner", "context_window": 65536},
]


def advertised_models() -> list[dict[str, Any]]:
    """本桥对外公布的模型列表。

    单一数据源：``GET /v1/models`` 与桥内命令 ``/bridge models`` 都读它，
    避免帮助文本与真实列表漂移（见 doc/bridge_commands.md §models）。
    """
    candidates = {m["id"]: m for m in SUPPORTED_MODELS}
    candidates.setdefault("deepseek-chat", {"id": "deepseek-chat"})
    return list(candidates.values())
