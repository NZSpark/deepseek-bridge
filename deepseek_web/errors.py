"""Driver 层异常与常量（从 driver.py 拆出，避免循环依赖）。

这些异常与常量原先定义在 ``deepseek_web.driver`` 里，外部（server / tests）
一直从该处导入；``driver.py`` 会原样 re-export，保持导入路径不变。
"""


class DeepSeekTimeoutError(RuntimeError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


class DeepSeekContextLimitError(RuntimeError):
    """网页会话已达上下文长度上限（网页版会停止响应，必须换新会话）。"""


class DeepSeekPageLostError(RuntimeError):
    """会话页面的浏览器标签已不可用（被关闭 / 渲染进程崩溃 / 被浏览器回收）。

    与「网页改版导致选择器失效」和「未登录」区分开：页面对象**本身已经不存在**，
    改选择器、等登录、换会话都没有意义，唯一的出路是**重建这一条页面的标签**
    （会话状态保留，按保存的链接重开，必要时重放历史）。

    为什么必须单独成类（真机根因）：标签失效时 ``wait_for_selector`` 会**立刻**抛错，
    以前这被当成「选择器都没命中」，于是每个请求都瞬间 502 并提示「请检查是否登录」，
    而页面池里那条死页面永远不会被替换——整个会话桶从此永久不可用。
    """


class DeepSeekBusyError(RuntimeError):
    """某个会话桶正忙（同一会话已有请求在跑且等待超时）。

    与「上游出错」区分开：这是本地的排队保护，客户端稍后重试即可，
    因此会被映射成 HTTP 503 / SSE ``upstream_busy``，而**不会**触发重试阶梯。
    """


# 未指定任务标识时使用的会话桶（保持与历史行为一致：全局共用一条会话）
DEFAULT_SESSION_KEY = "default"
# DeepSeek 首页（新会话的入口）
HOME_URL = "https://chat.deepseek.com/"
